"""Shared SKILL.md preprocessing helpers."""

from __future__ import annotations

import json
import logging
import re
import subprocess
from pathlib import Path, PurePosixPath

from hermes_cli._subprocess_compat import IS_WINDOWS, windows_hide_flags

logger = logging.getLogger(__name__)

# Matches ${HERMES_SKILL_DIR} / ${HERMES_SESSION_ID} tokens in SKILL.md.
# Tokens that don't resolve (e.g. ${HERMES_SESSION_ID} with no session) are
# left as-is so the user can debug them.
_SKILL_TEMPLATE_RE = re.compile(r"\$\{(HERMES_SKILL_DIR|HERMES_SESSION_ID)\}")

# Matches inline shell snippets like:  !`date +%Y-%m-%d`
# Non-greedy, single-line only -- no newlines inside the backticks.
_INLINE_SHELL_RE = re.compile(r"!`([^`\n]+)`")

# Cap inline-shell output so a runaway command can't blow out the context.
_INLINE_SHELL_MAX_OUTPUT = 4000

_ARTIFACT_CONTRACT_FILENAME = "ARTIFACTS.md"
_ARTIFACT_REGISTRY_DIRNAME = ".artifact-contracts"
_ARTIFACT_REGISTRY_FILENAME = "registry.json"
_ARTIFACT_CONTRACT_MAX_CHARS = 12_000
_ARTIFACT_REGISTRY_SEARCH_DEPTH = 12


def load_skills_config() -> dict:
    """Load the ``skills`` section of config.yaml (best-effort)."""
    try:
        from hermes_cli.config import load_config_readonly

        cfg = load_config_readonly() or {}
        skills_cfg = cfg.get("skills")
        if isinstance(skills_cfg, dict):
            return skills_cfg
    except Exception:
        logger.debug("Could not read skills config", exc_info=True)
    return {}


def substitute_template_vars(
    content: str,
    skill_dir: Path | None,
    session_id: str | None,
) -> str:
    """Replace ${HERMES_SKILL_DIR} / ${HERMES_SESSION_ID} in skill content.

    Only substitutes tokens for which a concrete value is available --
    unresolved tokens are left in place so the author can spot them.
    """
    if not content:
        return content

    skill_dir_str = str(skill_dir) if skill_dir else None

    def _replace(match: re.Match) -> str:
        token = match.group(1)
        if token == "HERMES_SKILL_DIR" and skill_dir_str:
            return skill_dir_str
        if token == "HERMES_SESSION_ID" and session_id:
            return str(session_id)
        return match.group(0)

    return _SKILL_TEMPLATE_RE.sub(_replace, content)


def run_inline_shell(command: str, cwd: Path | None, timeout: int) -> str:
    """Execute a single inline-shell snippet and return its stdout (trimmed).

    Failures return a short ``[inline-shell error: ...]`` marker instead of
    raising, so one bad snippet can't wreck the whole skill message.
    """
    _popen_kwargs = {"creationflags": windows_hide_flags()} if IS_WINDOWS else {}
    try:
        completed = subprocess.run(
            ["bash", "-c", command],
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True, encoding="utf-8", errors="replace",
            timeout=max(1, int(timeout)),
            check=False,
            stdin=subprocess.DEVNULL,
            **_popen_kwargs,
        )
    except subprocess.TimeoutExpired:
        return f"[inline-shell timeout after {timeout}s: {command}]"
    except FileNotFoundError:
        return "[inline-shell error: bash not found]"
    except RuntimeError as exc:
        if "live-system guard: blocked os.kill" in str(exc):
            return f"[inline-shell timeout after {timeout}s: {command}]"
        return f"[inline-shell error: {exc}]"
    except Exception as exc:
        return f"[inline-shell error: {exc}]"

    output = (completed.stdout or "").rstrip("\n")
    if not output and completed.stderr:
        output = completed.stderr.rstrip("\n")
    if len(output) > _INLINE_SHELL_MAX_OUTPUT:
        output = output[:_INLINE_SHELL_MAX_OUTPUT] + "...[truncated]"
    return output


def expand_inline_shell(
    content: str,
    skill_dir: Path | None,
    timeout: int,
) -> str:
    """Replace every !`cmd` snippet in ``content`` with its stdout.

    Runs each snippet with the skill directory as CWD so relative paths in
    the snippet work the way the author expects.
    """
    if "!`" not in content:
        return content

    def _replace(match: re.Match) -> str:
        cmd = match.group(1).strip()
        if not cmd:
            return ""
        return run_inline_shell(cmd, skill_dir, timeout)

    return _INLINE_SHELL_RE.sub(_replace, content)


def _read_artifact_contract(path: Path) -> str:
    """Read one bounded UTF-8 Artifact Contract, returning empty on failure."""
    try:
        if not path.is_file():
            return ""
        content = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        logger.warning("Could not read Artifact Contract: %s", path, exc_info=True)
        return ""

    content = content.strip()
    if not content:
        return ""
    if len(content) > _ARTIFACT_CONTRACT_MAX_CHARS:
        logger.warning(
            "Artifact Contract exceeds %d characters and was ignored: %s",
            _ARTIFACT_CONTRACT_MAX_CHARS,
            path,
        )
        return ""

    try:
        from tools.threat_patterns import scan_for_threats

        findings = scan_for_threats(content, scope="context")
    except Exception:
        logger.warning("Artifact Contract threat scan failed: %s", path, exc_info=True)
        return ""
    if findings:
        logger.warning(
            "Artifact Contract blocked by context threat scan (%s): %s",
            ", ".join(findings),
            path,
        )
        return ""
    return content


def _safe_registry_target(value: object) -> PurePosixPath | None:
    """Validate one registry target as a bounded relative POSIX path."""
    if not isinstance(value, str) or not value or "\x00" in value:
        return None
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        return None
    if not path.parts or path.parts[0] != _ARTIFACT_REGISTRY_DIRNAME:
        return None
    return path


def _find_artifact_registry(skill_dir: Path) -> Path | None:
    """Find the nearest bounded artifact-contract registry above a skill."""
    try:
        current = skill_dir.resolve()
    except OSError:
        current = skill_dir.absolute()

    candidates = (current, *current.parents)
    for directory in candidates[:_ARTIFACT_REGISTRY_SEARCH_DEPTH]:
        candidate = directory / _ARTIFACT_REGISTRY_DIRNAME / _ARTIFACT_REGISTRY_FILENAME
        if candidate.is_file():
            return candidate
    return None


def _match_registry_contract(registry: dict, relative_skill_path: str) -> object:
    """Return the exact or longest-prefix registry target for one skill."""
    exact = registry.get("contracts")
    if isinstance(exact, dict) and relative_skill_path in exact:
        return exact[relative_skill_path]

    prefixes = registry.get("prefix_contracts")
    if not isinstance(prefixes, dict):
        return None

    matches: list[tuple[int, object]] = []
    for raw_prefix, target in prefixes.items():
        if not isinstance(raw_prefix, str) or not raw_prefix:
            continue
        prefix = raw_prefix.rstrip("/")
        if relative_skill_path == prefix or relative_skill_path.startswith(prefix + "/"):
            matches.append((len(prefix), target))
    if not matches:
        return None
    matches.sort(key=lambda item: item[0], reverse=True)
    return matches[0][1]


def load_artifact_contract(skill_dir: Path | None) -> str:
    """Resolve a local sidecar or repository registry Artifact Contract.

    ``ARTIFACTS.md`` inside the skill package is authoritative. Otherwise the
    nearest ``.artifact-contracts/registry.json`` may map the exact skill path
    or a path prefix to a contract stored under that registry directory.
    Malformed, missing, oversized, unsafe, or blocked data is a no-op so skill
    loading remains available.
    """
    if skill_dir is None:
        return ""
    skill_dir = Path(skill_dir)

    local_contract = _read_artifact_contract(skill_dir / _ARTIFACT_CONTRACT_FILENAME)
    if local_contract:
        return local_contract

    registry_path = _find_artifact_registry(skill_dir)
    if registry_path is None:
        return ""

    try:
        registry = json.loads(registry_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        logger.warning("Could not load Artifact Contract registry: %s", registry_path, exc_info=True)
        return ""
    if not isinstance(registry, dict) or registry.get("schema_version") != 1:
        logger.warning("Unsupported Artifact Contract registry: %s", registry_path)
        return ""

    registry_root = registry_path.parent.parent
    try:
        relative_skill_path = skill_dir.resolve().relative_to(registry_root.resolve()).as_posix()
    except (OSError, ValueError):
        return ""

    target_value = _match_registry_contract(registry, relative_skill_path)
    target = _safe_registry_target(target_value)
    if target is None:
        return ""

    try:
        contract_path = (registry_root / Path(*target.parts)).resolve()
        allowed_root = registry_path.parent.resolve()
        contract_path.relative_to(allowed_root)
    except (OSError, ValueError):
        logger.warning("Unsafe Artifact Contract registry target ignored: %r", target_value)
        return ""
    return _read_artifact_contract(contract_path)


def append_artifact_contract(content: str, skill_dir: Path | None) -> str:
    """Append the resolved Artifact Contract exactly once."""
    contract = load_artifact_contract(skill_dir)
    if not contract or contract in content:
        return content
    return f"{content.rstrip()}\n\n{contract}\n"


def preprocess_skill_content(
    content: str,
    skill_dir: Path | None,
    session_id: str | None = None,
    skills_cfg: dict | None = None,
) -> str:
    """Apply Artifact Contract, template, and inline-shell preprocessing."""
    if not content:
        return content

    content = append_artifact_contract(content, skill_dir)
    cfg = skills_cfg if isinstance(skills_cfg, dict) else load_skills_config()
    if cfg.get("template_vars", True):
        content = substitute_template_vars(content, skill_dir, session_id)
    if cfg.get("inline_shell", False):
        timeout = int(cfg.get("inline_shell_timeout", 10) or 10)
        content = expand_inline_shell(content, skill_dir, timeout)
    return content

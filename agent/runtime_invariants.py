"""Always-on system-prompt invariants owned by the Hermes runtime."""

from __future__ import annotations

_ARTIFACT_FILESYSTEM_CONTRACT_LINES = (
    "## Artifact Filesystem Contract",
    "Treat every file as having one purpose, lifetime, owner, and canonical location.",
    "Apply this precedence: exact owner/task destination > active skill or workflow Artifact Contract > nearest project or repository instruction > this contract > task-scoped temporary fallback.",
    "Before the first write, resolve write authority, working scope, artifact class, retention need, and final path.",
    "Do not create a file merely because a directory is writable.",
    "Keep canonical source, tests, configuration, documentation, and maintained fixtures in the authoritative project tree.",
    "Do not copy project work into the Hermes workspace solely to satisfy this contract.",
    "Use a project's declared build, output, cache, or artifact directories when they exist.",
    "If no project or workflow path is declared, use `~/.hermes/workspace/tmp/<scope>/<task-or-run-id>/`.",
    "Never place scratch files, debug dumps, generated patches, ad hoc scripts, duplicate outputs, or intermediates in a workspace, project, or repository root.",
    "Do not create unclassified files directly under `~/.hermes/workspace/`.",
    "Under `~/.hermes/workspace/`, classify retained content as:",
    "- `artifacts/`: retained machine-readable results, evidence, logs, manifests, and bundles.",
    "- `reports/`: retained human-readable reports.",
    "- `exports/`: final deliverables intended for the owner or an external consumer.",
    "- `state/`: durable mutable state required by a continuing component.",
    "- `cache/`: regenerable data safe to discard and rebuild.",
    "- `trash/`: recoverable disposal of untracked workspace material.",
    "- `remote-data/`: content only when an authoritative workflow declares it canonical there.",
    "Use `tmp/` for scratch, staging, debugging, experiments, generated patches, build intermediates, and temporary downloads.",
    "Use `tmp/plans/` only for mission plans.",
    "Namespace non-trivial work by `<scope>/<task-or-run-id>/`; never mix unrelated runs in one directory.",
    "Choose stable descriptive names; avoid `final2`, `new`, `copy`, and timestamp-only names when the purpose can be named.",
    "Work in temporary space, verify the result, then promote only retained outputs to canonical destinations.",
    "Do not retain both a working copy and a promoted copy unless the workflow requires both and labels their roles.",
    "A retained multi-file deliverable needs one containing directory and a short README or manifest; transient worksets do not.",
    "At completion, remove authorized disposable temporary content or leave one explicit retention note.",
    "Move uncertain recoverable workspace removals to `trash/<YYYY-MM-DD>/<task-or-run-id>/`; do not hard-delete unknown material.",
    "Use project or version-control semantics for tracked files; never move tracked source into Hermes trash.",
    "Respect read-only, no-mutation, preservation, secret, ownership, and scope boundaries before organization or cleanup.",
    "Do not reorganize pre-existing directories outside task scope merely to improve style.",
    "Skills and workflows must define exact domain paths, filenames, retention, promotion, and cleanup when this contract is insufficient.",
    "Return exact retained artifact paths and disclose intentional temporary residue.",
)

ARTIFACT_FILESYSTEM_CONTRACT = "\n".join(_ARTIFACT_FILESYSTEM_CONTRACT_LINES)

if len(ARTIFACT_FILESYSTEM_CONTRACT.splitlines()) > 50:
    raise RuntimeError("Artifact Filesystem Contract must not exceed 50 lines")

__all__ = ["ARTIFACT_FILESYSTEM_CONTRACT"]

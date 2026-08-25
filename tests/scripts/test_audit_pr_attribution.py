import importlib.util
from pathlib import Path

import pytest


_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "audit_pr_attribution.py"
_SPEC = importlib.util.spec_from_file_location("audit_pr_attribution", _SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
audit = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(audit)


def test_resolve_base_ref_prefers_github_base(monkeypatch):
    monkeypatch.setenv("GITHUB_BASE_REF", "release")

    def fake_run(*args, check=True):
        if args[:3] == ("git", "symbolic-ref", "--quiet"):
            return "refs/remotes/origin/master"
        if args[:4] == ("git", "rev-parse", "--verify", "--quiet"):
            return "sha" if args[4] == "origin/release" else ""
        raise AssertionError(args)

    monkeypatch.setattr(audit, "run", fake_run)

    assert audit.resolve_base_ref() == "origin/release"


def test_resolve_base_ref_falls_back_to_master(monkeypatch):
    monkeypatch.delenv("GITHUB_BASE_REF", raising=False)

    def fake_run(*args, check=True):
        if args[:3] == ("git", "symbolic-ref", "--quiet"):
            return ""
        if args[:4] == ("git", "rev-parse", "--verify", "--quiet"):
            return "sha" if args[4] == "origin/master" else ""
        raise AssertionError(args)

    monkeypatch.setattr(audit, "run", fake_run)

    assert audit.resolve_base_ref() == "origin/master"


def test_new_emails_rejects_shallow_history(monkeypatch):
    monkeypatch.setattr(audit, "run", lambda *args, **kwargs: "true")

    with pytest.raises(RuntimeError, match="requires complete history"):
        audit.new_emails()

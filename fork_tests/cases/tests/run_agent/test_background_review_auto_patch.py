from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from agent import background_review as br


def _assistant_skill_patch_call(
    *,
    call_id: str,
    action: str = "patch",
    name: str = "demo",
    file_path: str = "SKILL.md",
) -> dict:
    return {
        "role": "assistant",
        "tool_calls": [
            {
                "id": call_id,
                "type": "function",
                "function": {
                    "name": "skill_manage",
                    "arguments": json.dumps(
                        {
                            "action": action,
                            "name": name,
                            "file_path": file_path,
                        }
                    ),
                },
            }
        ],
    }


def _tool_success(
    call_id: str | None,
    message: str,
) -> dict:
    payload = {"success": True, "message": message}
    if call_id is None:
        return {"role": "tool", "content": json.dumps(payload)}
    return {
        "role": "tool",
        "tool_call_id": call_id,
        "content": json.dumps(payload),
    }


def test_collect_background_skill_patch_events_parses_skill_manage_tool_call():
    review_messages = [
        _assistant_skill_patch_call(call_id="call_skill"),
        _tool_success(
            "call_skill",
            "Patched SKILL.md in skill 'demo' (1 replacement).",
        ),
    ]

    events = br._collect_background_skill_patch_events(review_messages, [])

    assert events == [
        {
            "name": "demo",
            "file_path": "SKILL.md",
            "tool_call_id": "call_skill",
            "message": "Patched SKILL.md in skill 'demo' (1 replacement).",
        }
    ]


def test_collect_background_skill_patch_events_skips_non_skilled_messages():
    snapshot = [
        _tool_success(None, "Patched SKILL.md in skill 'demo' (1 replacement)."),
    ]
    review_messages = [
        {
            "role": "assistant",
            "tool_calls": [],
        },
        _tool_success(
            None,
            "Patched SKILL.md in skill 'demo' (1 replacement).",
        ),
        _assistant_skill_patch_call(
            call_id="call_edit",
            action="edit",
            name="demo",
        ),
        _tool_success(
            "call_edit",
            "Patched SKILL.md in skill 'demo' (1 replacement).",
        ),
    ]

    events = br._collect_background_skill_patch_events(review_messages, snapshot)

    assert events == []


def _fake_completed(stdout: str = "") -> SimpleNamespace:
    return SimpleNamespace(returncode=0, stdout=stdout, stderr="")


def test_auto_patch_skills_from_background_review_commits_and_pushes(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    skill_dir = repo / "skills" / "demo"
    (skill_dir).mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("# Demo\n")

    calls: list[tuple[tuple[str, ...], Path]] = []

    def _fake_run_git(cmd: list[str], repo_root: Path, timeout_seconds: int = 30):
        calls.append((tuple(cmd), repo_root))
        if cmd[0:3] == ["diff", "--cached", "--name-only"]:
            return _fake_completed("skills/demo/SKILL.md\n")
        return _fake_completed()

    monkeypatch.setattr(br, "_run_git", _fake_run_git)
    monkeypatch.setattr(br, "_git_repo_for_path", lambda _path: repo)
    monkeypatch.setattr(
        "tools.skill_manager_tool._find_skill",
        lambda name: {"path": str(skill_dir)} if name == "demo" else None,
    )

    br._auto_patch_skills_from_background_review(
        [
            {
                "name": "demo",
                "file_path": "SKILL.md",
                "tool_call_id": "call_skill",
                "message": "Patched SKILL.md in skill 'demo' (1 replacement).",
            }
        ],
        task_cfg={
            "auto_skill_patch": {
                "enabled": True,
                "commit_message": "auto patch skill",
                "remote": "origin",
                "branch": "master",
                "repo_path": "",
            }
        },
    )

    assert any(cmd[0] == "add" for cmd, _ in calls)
    assert any("commit" in cmd for cmd, _ in calls)
    assert any(
        cmd[0] == "push" and cmd[1:] == ("--ff-only", "origin", "HEAD:master")
        for cmd, _ in calls
    )


def test_auto_patch_skills_from_background_review_disabled_by_default(monkeypatch):
    calls = []

    monkeypatch.setattr(br, "_run_git", lambda *args, **kwargs: calls.append(args))

    br._auto_patch_skills_from_background_review(
        [
            {
                "name": "demo",
                "file_path": "SKILL.md",
            }
        ],
        task_cfg={"auto_skill_patch": {"enabled": False}},
    )

    assert calls == []

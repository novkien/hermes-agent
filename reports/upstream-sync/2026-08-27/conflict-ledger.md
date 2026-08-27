# Upstream merge conflict ledger — 2026-08-27

Immutable inputs:

- owner base: `6f04871ef0b1328bb5a40dcd2407cb3a4107b88d`
- upstream tip: `7a1aafb4e1dac5d2840cd3ab524f6d6bd0658694`
- common base: `1bbb6e5bce56e721ab685af4cd87df21bbff4d35`

## `tools/skill_manager_tool.py`

- Kind: content conflict in the public `skill_manage` function schema.
- Upstream behavior: stop advertising legacy `edit`; use
  `patch(content=...)` for a complete `SKILL.md` rewrite; keep the
  curator-only `absorbed_into` delete argument out of the public schema.
- Owner behavior: derive the visible description window from
  `SKILL_PROMPT_DESC_LIMIT`; explain that pinned skills remain patchable but
  cannot be deleted until the user unpins them.
- Composed resolution: use upstream's compact public action set and full
  rewrite semantics, remove the stale `patch/edit/delete` wording, retain the
  dynamic description limit and pinned-skill deletion guidance. The handler's
  backward-compatible `edit` and curator-only `absorbed_into` paths remain
  internal implementation details.
- Verification: `fork_tests/test_skill_manager_schema.py` plus the maintained
  upstream `tests/tools/test_skill_manager_tool.py` suite.
- Remaining uncertainty: none after the focused and full CI lanes pass.

## Clean-merge semantic reconciliations

These paths did not produce Git conflict markers but lost owner behavior in the
automatic merge result.

### Nous subscription capability prompt

- Paths: `agent/prompt_builder.py`, `agent/system_prompt.py`, `run_agent.py`.
- Upstream result: removed the capability builder, its system-prompt insertion,
  and the compatibility re-export.
- Owner contract: when managed Nous tools are enabled, expose accurate feature
  state without asking subscribers for redundant provider keys or mentioning a
  subscription when it is irrelevant.
- Resolution: restore the owner builder and call it through `run_agent`'s lazy
  patch-compatible namespace at the same stable-prefix boundary.
- Regression selection: promote the three
  `TestBuildNousSubscriptionPrompt` cases and
  `TestBuildSystemPrompt::test_includes_nous_subscription_prompt` through
  `semantic_nodeids`; their presence in a copied case file alone is not treated
  as execution evidence.

### Mission Control gateway module lookup

- Path: `hermes_cli/mission_control.py`.
- Upstream result: local package-attribute imports can retain a stale gateway
  module binding when tests or runtime code replace `hermes_cli.gateway`.
- Owner contract: resolve the current module for each lifecycle operation.
- Resolution: centralize `importlib.import_module("hermes_cli.gateway")` in
  `_gateway_cli()` and use it at all nine call sites.
- Verification: the isolated owner `tests/hermes_cli/test_mission_control.py`
  case and the normal upstream Mission Control tests.

## CI completeness reconciliation

The changed-file detector used the GitHub Compare API's maximum 300-file list
as if it were complete. A large pull request could therefore skip a required
lane when the relevant path appeared after file 300. The detector now compares
the returned count with the pull-request event's immutable `changed_files`
count and fails open to all lanes on truncation or missing count. All normal
Python tests, all normal workspace checks, and isolated fork regressions remain
separate required executions.

The pristine shared suite and fork cases can intentionally encode mutually
exclusive policies. The manifest therefore records both sides of a replacement:
`nodeids`/`semantic_nodeids` run in the fork overlay, while
`replaced_upstream_nodeids` are the exact pristine assertions deselected by the
canonical per-file runner. Unrelated upstream tests in the same file still run;
neither the whole shared file nor the whole upstream suite is suppressed.

`ci.yaml` also passed `sparse-checkout` and `sparse-checkout-cone-mode` to the
local `detect-changes` action even though that action declares neither input.
GitHub rejects undeclared local-action inputs before classification runs; the
two misplaced inputs were removed. `actionlint` validates the caller and the
referenced composite action together.

The Python overlay runner also resolved `.venv/bin/python` through its symlink
to uv's base interpreter. That bypassed the virtual environment and made pytest
unavailable despite a successful dependency install. The runner now makes the
requested path absolute relative to the repository without dereferencing the
venv symlink; a fork tooling test locks this command-path contract.

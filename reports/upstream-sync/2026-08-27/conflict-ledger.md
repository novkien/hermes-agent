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

Test-function ASTs are not the complete semantics of a test file. A fork can
change a fixture, helper, import, constant, or non-test `Test*` class member
without changing the inherited test function itself. The classifier now runs
each such inherited node against the fork case's support context and replaces
only common-base upstream nodes; newly added upstream nodes in the same file
remain active. For this lock that produces 802 selected fork nodes and 750 exact
upstream replacements, including the owner steer-window helper required by 34
inherited `tests/run_agent/test_steer.py` nodes.

The canonical runner also retains its public support for absolute test paths
outside the repository. Fork replacement lookup is applied only to repo-local
paths, so `/tmp` probes used by the runner's own regression suite are neither
rejected by `Path.relative_to()` nor accidentally classified as shared tests.

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

GitHub Checkout records the canonical HTTPS origin without the optional
`.git` suffix. Fork verification now accepts the two equivalent canonical URL
spellings while continuing to reject every other repository. The lock refresh
also updates each case checksum, so lint-only comments and later intentional
case edits cannot leave a stale manifest that fails only in the overlay job.
The Python test job uses full Git history because the verifier must prove the
locked upstream commit is an ancestor and read its exact blobs; a depth-one PR
merge checkout contains neither the parent graph nor those objects.

The aggregate gate previously failed only for a literal `failure` result. A
cancelled detector therefore skipped every downstream lane while the aggregate
reported success. Cancelled required jobs now fail the gate, and `detect` must
specifically succeed before classifier-approved downstream skips are accepted.

## Full-suite regressions repaired

The first retry-disabled full Python run exposed three deterministic cache
boundaries in otherwise pristine upstream tests:

- Honcho's gateway cache used only `(path, mtime_ns)`, so two writes in one
  filesystem timestamp tick could retain a stale `pinPeerName`. The small JSON
  file's content digest now participates in the key.
- `EventBridge` could miss the first post-baseline message when the SQLite file
  retained the baseline mtime. The first poll after baselining now always
  inspects the DB; per-session message timestamps still suppress history.
- the browser snapshot threshold combined a process-wide raw-config cache with
  a profile-agnostic lifecycle cache. It now performs one uncached explicit
  profile read per browser lifecycle and keys the local cache by Hermes home.
- topic recovery was dispatched through the shared thread executor for every
  platform and chat type even though only Telegram DMs can recover a topic.
  The structural eligibility check now runs before `asyncio.to_thread`, so a
  no-op group turn cannot exhaust a short turn-lease timeout while waiting for
  unrelated executor capacity.

The existing unmodified shared tests are the regression proof for all four
fixes; no fork assertion was added to the upstream-owned paths.

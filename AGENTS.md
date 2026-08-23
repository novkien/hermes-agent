# Hermes Agent Repository Contract

## Authority

Le Kien owns this fork and is the final decision authority for owner-specific Hermes work. The newest explicit owner directive overrides older repository prose. Current source, tests, schemas, and verified runtime evidence outrank stale documentation.

## Repository role

`novkien/hermes-agent` is the owner fork of Hermes Agent and includes the integrated AgentOS Mission Control source under `apps/mission-control/`. It is projected into the Hermes superproject at:

```text
live path:       /home/jarvis/.hermes/hermes-agent
canonical repo:  novkien/hermes-agent
parent repo:     novkien/hermes
parent gitlink:  hermes-agent
```

Use the trusted host alias `jarvis` for current production access. Do not encode volatile LAN/Tailnet IP snapshots in durable repository instructions; reverify runtime locators when an operational action depends on them.

## Mandatory repository transaction

Repository mutation includes Git completion even when the owner does not separately ask for commit/push.

1. Before editing, run `pwd -P`, `git rev-parse --show-toplevel`, `git remote get-url origin`, `git branch --show-current`, and `git status --short --untracked-files=all`.
2. Confirm the physical root is this repository and `origin` is the intended `novkien/hermes-agent` remote before staging or pushing.
3. Never edit or commit on detached HEAD. Create or reuse a task branch from the intended current base first.
4. Every intended source-changing task must end with a coherent commit in this repository. Do not leave non-ignored staged, modified, deleted, renamed, conflicted, or untracked source state when changing repository, invoking sync, or reporting completion.
5. Classify every untracked path: canonical source/docs/tests/config must be tracked and committed; generated/runtime/credential/session/log/database/machine-local state must be narrowly ignored. Commit a required `.gitignore` correction. Never broad-ignore canonical source.
6. Preserve pre-existing changes. Never discard, reset, clean, stash, overwrite, or silently mix unrelated changes into the task commit. Resolve safe pre-existing source work in a separate truthful commit; stop with exact path/status evidence when safe classification is impossible.
7. Push every agent-created commit non-force to the exact canonical `origin` and exact intended branch. Verify branch/upstream and ahead/behind state; never guess a destination or rewrite a remote URL.
8. Normal publication is task branch -> commit -> push -> PR -> review -> merge. After merge, the exact merged child SHA must be prepared in the aggregate `novkien/hermes` gitlink PR, then parent merge, then `Sync Hermes`.
9. Never use `git add .`, `git add -A`, `git add --all`, force push, history rewrite, `git clean`, destructive reset, or automatic stash to manufacture a clean state.
10. Read-only/no-op work reports `NO_SOURCE_MUTATION`; do not create an empty commit.

A temporary `M`/`??` is acceptable only while actively composing the current coherent change. Resolve it before leaving this repository or declaring the task done.

## Progressive repository context

Keep root context compact. Before changing a subtree, read the nearest scoped `AGENTS.md`/`CLAUDE.md` and relevant current source. More-specific scoped instructions apply to their subtree unless they conflict with a newer owner directive.

High-value scopes include:

- `apps/mission-control/` for the Mission Control BFF, Repository control plane, frontend, contracts, and deployment surfaces;
- `apps/desktop/` for the Electron/Desktop application;
- current component docs/tests next to the subsystem being changed.

Do not reconstruct subsystem behavior from this root file when current source or scoped context can answer it.

## Source ownership boundaries

- Hermes executable/core/runtime framework work belongs here.
- AgentOS Mission Control source belongs here under `apps/mission-control/`.
- Owner-managed live shared skills/profile skill packs belong to `novkien/hermes-skills`, not this repository merely because Hermes Agent also ships bundled/optional skills.
- Reviewed owner profile `SOUL.md` definitions belong to `novkien/agents`.
- Owner-managed external runtime/gateway plugins belong to `novkien/hermes-plugins`.

When the requested path is outside this repository's ownership, change the owning repository instead of duplicating source here.

## Engineering rules

- Think before coding: inspect definitions, callers, tests, configuration, and nearby conventions before mutation.
- Simplicity first: implement the smallest architecture-compatible change that satisfies the request.
- Surgical changes: no drive-by refactors, broad renames, unrelated formatting, or new framework layers without a concrete need.
- Documentation is part of implementation. If behavior/config/API/commands/setup/architecture/user-visible behavior changes, reconcile the existing canonical docs in the same task.
- Never commit `.env`, tokens, keys, cookies, passwords, local DB/WAL/SHM, sessions, copied environments, logs, or machine-specific state.
- Preserve existing fail-closed security and mutation boundaries; do not weaken auth, CSRF/origin/host/rate-limit/audit controls to make tests pass.
- Use `get_hermes_home()`/`display_hermes_home()` rather than hard-coding `~/.hermes` in profile-aware runtime code.

## Validation

Run the smallest relevant maintained tests after changes. For general Python tests prefer the repository's `scripts/run_tests.sh` rather than bare `pytest` when applicable. For Mission Control, follow `apps/mission-control/AGENTS.md` and its focused Python/Node contract commands. Run `git diff --check` for changed source/doc work when available.

Do not turn an unrelated baseline formatter debt into a broad cleanup. Report independent baseline failures separately when the scoped change is otherwise verified.

## Completion evidence

A repository-changing task is not done until the final report can state separately:

```text
repository root
branch
commit SHA
push/upstream result
PR URL/state
merge SHA when merged
parent gitlink SHA/state when projected
production Sync Hermes state when requested
runtime reload/session refresh/behavior result when requested
final non-ignored git status
```

Do not claim one stage proves another.
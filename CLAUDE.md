# Claude Code — Hermes Agent

Read the root `AGENTS.md` before repository mutation, then read the nearest scoped `AGENTS.md` for the subtree you will change.

## Critical Git contract

- Resolve the physical Git root, origin, branch, and status before editing.
- Never edit or commit on detached HEAD.
- Every source-changing task must end with a coherent commit in `novkien/hermes-agent`, pushed non-force to the exact intended branch on canonical `origin`.
- Do not leave non-ignored modified/staged/deleted/renamed/conflicted/untracked source state when switching repositories, syncing, or reporting completion.
- Track canonical untracked source; narrowly ignore generated/runtime/secret/machine-local state. Never hide source with a broad ignore.
- Preserve unrelated pre-existing changes; never stash, clean, reset, overwrite, or absorb them into the task commit.
- Normal flow: child branch -> commit -> push -> PR/review/merge -> exact merged SHA in `novkien/hermes` gitlink `hermes-agent` -> parent merge -> `Sync Hermes`.
- Never force-push or rewrite history.

## Repository identity

```text
canonical repo: novkien/hermes-agent
live path:      /home/jarvis/.hermes/hermes-agent
parent:         novkien/hermes
parent gitlink: hermes-agent
production host alias: jarvis
```

This repository owns Hermes executable/runtime framework source and integrated Mission Control. Owner live shared skills belong to `novkien/hermes-skills`, profile SOUL definitions to `novkien/agents`, and owner external plugins to `novkien/hermes-plugins`.

Keep changes simple and surgical. Reconcile affected documentation with behavior and run the nearest maintained tests. Current source/scoped context outrank stale prose.
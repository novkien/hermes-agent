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

---

## Upstream Engineering Invariants (grafted from NousResearch/main, merge 2026-08-25)

The sections below are merged verbatim from the upstream `AGENTS.md` additions
introduced after the fork snapshot point (`c896c09c`). They are additive
engineering contracts only and remain subordinate to the Authority rules at the
top of this file: nothing here weakens the mandatory repository transaction,
owner authority, or Mission Control ownership. Future upstream merges should
refresh these blocks against the current upstream text.

### Bot Mode (`apps/desktop/src/plugins/hermes-bots/`)

The desktop "Bots" experience ships bundled in-tree. Each bot is a Hermes
agent **profile** with a persistent identity. Its design rests on one settled
invariant that has been regressed repeatedly, cost users real conversation
history each time, and is not open for re-litigation in a routine PR:

**One bot = ONE canonical forever-chat, identified by NAME.** The chat's one
and only identity is **(profile, session titled exactly "Bot Chat")** — the
state DB's UNIQUE(title) index makes that pair an exact registry of at most
one row. The full lifecycle when a bot row is clicked:

1. **Resolve the registry, every time.** Look up the profile's `Bot Chat`
   session by exact title via `session.list {title, include_hidden: true}`
   (indexed, window-free; hidden rows resolve because canonical chats are
   always hidden; compression lineages resolve to the live tip). Row exists →
   open it. That is the entire happy path.
2. **No row → create it,** titled `Bot Chat`, born hidden, kicked off with
   the bot's intro. Creation adopts-before-minting: it re-runs the registry
   lookup first, so a concurrent or pre-existing row is opened, never forked.
   (`set_session_title` silently drops conflicting titles — returns 0 rows —
   which is how the 2026-08 infinite fork loop started; adopt-before-mint is
   what kills it.)

**There is NO session-id pin.** The previous design stored a pointer in
`ui_meta['hermes-bots'].chat` and verified it per click; five hardening
waves (#88690, #90732, #90751, the #91791 revert, #92042) each guarded a new
way that pointer dangled or got stolen — rows[0] steals, `last_session`
adoptions, transient clears, drifted-title welds (a pin re-anchored onto a
cron session passed every guard). Name-as-identity removes the failure class:
a name cannot dangle, and a corrupted historical pointer simply never gets
read. Legacy `chat` keys in ui_meta are ignored and dropped from merges.

Why recency must never win (the #91791 → #92042 lesson): canonical Bot
Chats are **unconditionally hidden** from the Sessions sidebar, so the bot
row is the ONLY door to the forever-chat. A "newest visible session wins"
preference doesn't re-order two equivalent entry points — it walls the
entire relationship off behind a row that previews one session and opens
another, and any stray draft that catches a prompt captures the row.
Side-chats started via "New chat with this agent" are not plumbing-titled,
stay visible in the Sessions sidebar, and are reachable there; they are
never the bot row's target.

Corollaries for reviewers:

- There is no per-bot session browser, by explicit design (removed in
  #90732). Do not add one back.
- Reject any PR that reintroduces a stored session-id pointer as canonical
  identity — including "as a fallback tier" or "for verification". The
  registry lookup is the whole contract; pointers are how every prior
  incident started.
- Reject any PR that consults recency, visibility, or "where the user left
  off" for the bot row's target — reports that motivate such a change are
  almost always about side-chats, and the fix belongs in the Sessions
  sidebar (hide-sweep false positives), not in the bot row's target.
- The gateway reports the registry row per profile as `canonical_session`
  on `profiles.list` (resolved server-side by title); roster preview,
  activity signals, and the `/new`→`/compact` guard all read it, so preview
  identity and click identity are the same row by construction.

Regression tests encoding this contract:
`tests/canonical-chat-registry.test.mjs` (includes a tripwire asserting the
open path never reads or writes a stored pointer),
`tests/canonical-chat-creation.test.mjs`, `tests/hide-bot-chats.test.mjs`,
and `tests/tui_gateway/test_profiles_list_canonical_session.py`.


---

## Update Pipeline (`hermes update`)

The updater is transactional in shape (fleet-update campaign, #91277 —
Aug 2026). Every stage exists because its absence was a real field
failure; PRs that weaken a stage need to answer for the failure class it
guards:

```
plan → snapshot → apply → restart-per-kind → verify → report
```

- **Plan** (`hermes_cli/update_inventory.py`, `hermes update --plan`):
  read-only inventory — install kind, all profiles, every live gateway
  with supervisor + running code version. Deployment kinds are
  first-class: `git` updates in place; `docker`/`nix`/`apt` are NOT
  in-place-updatable and the updater reports the correct external
  command instead of fighting the deployment model.
- **Snapshot** (`hermes_cli/backup.py`): pre-update quick snapshot for
  EVERY profile (the code swap + fleet restart touch all of them), each
  into its own `state-snapshots/`, identical file set + 1 GiB per-file
  cap + keep=1. **Never add a partial/tiered snapshot set** — mixed
  coverage creates torn-restore states across schema generations. Quick
  snapshots are FILE-LOSS RECOVERY (the per-profile cron-jobs safety
  net restores from them), NOT code-rollback insurance; `--backup` full
  mode owns rollback.
- **Apply**: git pull, or the Windows ZIP fallback — which fires ONLY
  when git itself failed (`_should_zip_fallback_on_update_error`,
  argv-classified; a dependency-install failure must never trigger a
  tree-clobbering re-download), REFUSES a dirty working tree
  (`-uall`, plus a pre-swap TOCTOU re-check), and grafts the live
  `apps/desktop/release/` into the staged swap (the GitHub source ZIP
  has no built desktop app; without the graft the swap deletes it).
- **Restart-per-kind**: systemd and launchd restarts are FLEET-WIDE
  (every `hermes-gateway*` unit / `ai.hermes.gateway*` LaunchAgent),
  drain-first (SIGUSR1) with per-unit/per-label failure isolation.
  Restarting only the invoking profile's service leaves siblings on
  stale `sys.modules` until they crash — the largest dupe-PR cluster in
  the repo's history came from that bug.
- **Verify**: gateways stamp their running `code_sha`/`code_version`
  into `gateway_state.json` on every runtime-status write
  (`gateway/status.py`); after the restart phase the updater compares
  each live gateway against the fresh checkout and prints a fleet
  version matrix. A provably-stale gateway fails the update (exit 1) —
  automation must never treat a mixed-version fleet as healthy.
- **Report**: every run writes a machine-readable receipt to
  `~/.hermes/logs/update_receipts/` (`latest.json` pointer; steps,
  skips WITH reasons, restart outcome, plan, fleet snapshot).
  Finalization is owned by the `cmd_update` command boundary — early
  `sys.exit` paths (preflight refusals, fetch failures) still persist
  a receipt with the real exit code. A begun-but-unwritten receipt is
  a bug: the refused/failed runs are the ones receipts exist for.

Architecture direction: process-scan-based coordination between the
updater, serve/dashboard, and the gateway is being replaced by a
gateway-owned control socket (#92091). Do not add new scan heuristics
without checking that design; scans are the fallback layer.

### Gateway lifecycle vs. the Desktop app

`hermes serve` (control plane, desktop-spawned child) dies with the app
— by design. The messaging gateway (`gateway run`) SURVIVES the app: the
serve backend's `/api/gateway/*` endpoints spawn it detached
(`_spawn_hermes_action` — `start_new_session` / `DETACHED_PROCESS`), so
`before-quit`'s backend SIGTERM never reaches it. Bots keep running
when the user closes the app. The known breach of this contract is the
Windows shim-unlock teardown (`taskkill /T /F` on venv-shim holders,
#85265) — it exists to let updates proceed, and its replacement is
#92091's `pause-for-update`. Do not "fix" gateway-dies-with-app reports
by re-parenting the gateway under the backend, and do not "fix" update
locks by widening the tree-kill.


---

### Multiplex profile-scoped env reads must fail closed

(`agent/secret_scope.py` contract; #72348, #86905). Under `gateway.multiplex_profiles`,
   `os.environ` holds the **default profile's** values; a secondary profile's `.env` lives
   only in its secret scope (installed per-turn by `_profile_runtime_scope`). Any
   profile-level env config — credentials (`app_secret`, tokens) AND authorization
   (`FEISHU_ALLOWED_USERS`, `{PLATFORM}_ALLOW_ALL_USERS`, `GATEWAY_ALLOW_ALL_USERS`,
   `group_policy`, `allow_bots`, ...) — must be read scope-aware:
   - Adapters: `_get_scoped_secret()` (canonical fail-closed copy in
     `plugins/platforms/feishu/adapter.py`, #86905).
   - Gateway authz: `_auth_env()` / `_platform_gate_env()` (`gateway/authz_mixin.py`).
   Rules:
   - Scope installed + multiplex active → a scoped miss returns the **default**.
     NEVER fall through to `os.environ` — that leaks another profile's value and
     silently breaks routing/admission (a leaked default allowlist skips the
     allow-all check and rejects every secondary-profile sender, #86905).
   - Unscoped default-profile path (`UnscopedSecretError`) and single-profile
     deployments keep the `os.environ` read — there it IS the profile's own value.
   - Authorization config is the sharpest edge: allowlist/allow-all leaks cause
     silent rejections (or worse, fail-open) that only show up as missing replies.
   - The `_get_scoped_secret` wrapper is copy-pasted across ~15 platform adapters —
     when touching any of them, make sure the fail-closed semantics are present;
     do not reintroduce the `except _UnscopedSecretError: val = os.getenv(...)`
     fallback-after-miss shape.


---

### DO NOT infer process identity from argv substrings
The bug class behind ~10 fleet-update issues (#90778, #87594, #78089,
#76129, #91964, ...): classifying a process by `"serve" in cmdline` or
similar. `kanban --preserve-cache` contains "serve"; a flag VALUE can
equal a subcommand (`-m dashboard serve`); truncated cmdlines hide the
real subcommand. Rules:
- Use the canonical matchers: `gateway.status.looks_like_gateway_command_line`
  (gateway run), `hermes_cli.update_cmd._hermes_holder_subcommand`
  (top-level subcommand of any Hermes argv). Never hand-roll token scans.
- Flag sets must be DERIVED from the parser
  (`_holder_value_flags()` introspects `build_top_level_parser()`), never
  hand-written lists — they drift.
- Never blanket-exclude ancestors from process scans: when `/update` runs
  as the gateway's child, a gateway ancestor must stay visible to the
  pause machinery (#87594). Exclude interactive ancestry, carve out
  gateway-shaped ancestors.
- Match on FULL cmdlines; truncate only at display time (#78089).
- Before adding any new scan heuristic, read #92091 — the gateway control
  socket replaces scans as the primary coordination mechanism; scans are
  the fallback layer for old/crashed processes.


---

### Streaming delivery contract (stream-is-the-message adapters) — duplicate-final class
Adapters with `draft_stream_is_message = True` (relay Slack native streaming)
keep ONE cumulative native stream per turn; the stream IS the final message.
Four invariants, each learned from a live duplicate-final incident (NS-658
canary ledger, hermes#85796 / gateway-gateway#210). Violating any of them
re-creates a duplicate or a frozen stream:

1. **Draft frames must be prefix-stable.** The connector computes append-only
   deltas: frame N must be a string prefix of frame N+1. NEVER mutate draft
   frames per-tick — no fence-closing (`ensure_closed_code_fences`), no cursor
   suffix, no segment-state resets at tool boundaries, no mrkdwn conversion.
   Any non-prefix frame triggers a whole-snapshot re-append on the platform
   ("stacked copies"). The finalize path may still transform the real final.
2. **The consumer declares the final; the adapter never guesses.**
   `finish(final_text)` carries the completed `final_response` (verifier
   footer, completion explainer included) as the authoritative finalize
   payload. New post-stream response augmentation MUST ride this payload —
   if it mutates `final_response` after the stream sealed, it re-opens the
   #11 bug (`delivered_final_matches` mismatch → corrective duplicate send).
3. **Interim sends must carry `_interim_send` metadata.** Any consumer-side
   `adapter.send()` that is NOT the turn-final (commentary, segment-tail
   flushes) must set `metadata["_interim_send"] = True`, or the relay
   adapter's seal-interception will seal the live stream with interim text.
   Seal-interception exists at BOTH egress doors (`send()` AND
   `send_for_platform()`); a new egress door needs the same two checks.
4. **Reconcile by edit, never by plain send.** Any lane that delivers a final
   beside an already-sealed stream (queued follow-ups, media-accompanied
   finals, future lanes) must first try `edit_message` on the consumer's
   `message_id`; plain `send()` is the fallback only when no editable message
   exists. A sealed native stream is a regular message — `chat.update` on it
   works (live-verified).

Contract tests: `tests/gateway/test_stream_final_contract.py` (all four
invariants, mutation-checked). Slack streaming API ground truth (live-probed,
also encoded in connector comments/tests): `chat.*Stream` speaks STANDARD
markdown, not mrkdwn; `stopStream.markdown_text` APPENDS (never replaces);
`startStream`/`stopStream` are rate-limit Tier 2 (~20/min).

Guard style note: check `draft_stream_is_message` with `is True` — MagicMock
adapters in older tests auto-create truthy attributes.


---

#### Live Windows process-topology E2E: the `wine2e` lane For claims about
real Windows process behavior that mocks cannot reproduce (venv-holder
scans, process-tree parentage, launcher/worker chains, detach semantics),
there is an on-demand workflow `windows-venv-e2e.yml` that runs
`tests/hermes_cli/test_venv_holder_windows_live.py` on a real
`windows-latest` runner — spawning actual processes and driving the real
detection code, no mocked psutil. It fires ONLY on pushes to `wine2e/**`
branches (inert on PRs and main; costs nothing on normal work). The proven
workflow: write probes that pin CORRECT behavior, push to a `wine2e/`
branch to reproduce the bugs live on unfixed code, build the fix, iterate
until the lane is green, then open the PR — the live receipt on the exact
head is the Windows proof reviewers ask for. Extend the live suite when
touching that subsystem; assert against the gateway ANCESTOR found by
argv, not the direct parent (the venv shim makes every spawn a
launcher/worker chain).

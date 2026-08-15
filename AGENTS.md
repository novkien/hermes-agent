# Repository Guidelines

This repository contains AgentOS Mission Control, the browser BFF/control plane used by
Jarvis/Hermes. Current source and tests take precedence over older repository notes.

## Project Structure & Module Organization

- `agent_mission_control/` is the FastAPI backend package.
  - `main.py` creates the module-level application and startup guard.
  - `app.py` is the composition root for clients, store, cache, capabilities, event
    fabric, alert/pulse engines, workers and routes.
  - `routes.py` owns the main HTTP boundary, allowlists, mutation gates and envelopes.
  - `clients.py` contains the Dashboard, Gateway and Adapter upstream clients.
  - `store.py` manages the SQLite WAL control store and migrations.
  - `workers.py` runs bounded source polling loops.
  - `chat_proxy.py` relays Hermes gateway chat/session traffic including SSE streaming.
  - `search.py` implements bounded federated search over adapter-backed sources.
- `agent_mission_control/migrations/` contains SQL representations of runtime schema
  migrations; keep them aligned with the migration definitions in `store.py`.
- `frontend/dist/` is the committed **source-of-truth SPA**, despite the `dist` name.
  It is served directly with native ES modules; there is no separate frontend build
  pipeline in this repository.
- `frontend/dist/tabs/` contains dynamically loaded dashboard tabs.
- `frontend/dist/pure/` contains data-shape, routing, state and transformer modules used
  directly by Node contract tests.
- `tests/` contains the active Python and Node contract suites.
- `deploy/agent-mission-control.service` and related deploy files define the systemd
  deployment surface.
- Root-level `agentos-dashboard.db*` files are runtime state, not source artifacts.

## Runtime Architecture

AgentOS is a FastAPI BFF between the browser SPA and three Hermes upstream families:

```text
Browser / frontend/dist
        ↓
AgentOS FastAPI BFF
        ├── DashboardClient → Hermes dashboard API :9119
        ├── GatewayClient   → Hermes gateway API :8642
        └── AdapterClient   → external adapter :8643
```

Current upstream responsibilities include:

- dashboard health/status and supported dashboard mutations through `DashboardClient`;
- session/chat and chat SSE streaming through `GatewayClient`;
- Kanban, permits, issues, timeline, fingerprints, memory files and related supported
  adapter surfaces through `AdapterClient`.

Reads and writes are intentionally bounded by explicit allowlists. Do not turn any
upstream client into an arbitrary path proxy.

The backend also maintains a bounded SQLite control store, capability registry, cache,
event bus/replay buffer, source workers and alert/pulse engines. Source deltas are
published to the event fabric and exposed through the event stream.

Observed Hermes production host: `jarvis@192.168.1.128`. Reverify the host from current
runtime evidence before operational actions. Credentials must be obtained from the
host's secure secret store and must not be recorded here.

## Security and Mutation Invariants

Preserve the existing fail-closed boundaries:

- Public/network binding requires an explicit `ALLOWED_CIDRS` allowlist.
- Read paths, adapter paths and supported mutation paths have separate allowlists.
- Protected mutations preserve the established chain: session validation → CSRF →
  Origin/Host checks → per-session rate limit → audit `pending` write **before** the
  upstream call → audit completion after the response.
- If the initial audit write fails, the upstream mutation must not run.
- Preserve `X-Request-Id`/correlation handling and the response envelope contract.
- Do not weaken `meta.mutations_supported`; frontend action gating and backend capability
  contracts must remain aligned.
- Keep credentials in environment variables only. Never commit `.env`, tokens, keys,
  cookies, passwords or local database copies.

## Local Run Commands

Use Python 3.11+ with FastAPI, Uvicorn and HTTPX available in a compatible virtual
environment. The repository does not currently provide a package manifest that makes a
copied `.venv` portable across machines.

From the repository root:

```bash
source .venv/bin/activate
FRONTEND_DIR=frontend/dist \
STORE_PATH=/tmp/agent-mission-control-dev.db \
ALLOWED_ORIGIN=http://127.0.0.1:51763 \
ALLOWED_HOST=127.0.0.1:51763 \
python -m uvicorn agent_mission_control.main:app --host 127.0.0.1 --port 51763
```

When binding to the network, keep application startup configuration and Uvicorn host
consistent and provide the CIDR allowlist:

```bash
FRONTEND_DIR=frontend/dist \
ALLOWED_CIDRS=192.168.0.0/24 \
BIND_HOST=0.0.0.0 \
python -m uvicorn agent_mission_control.main:app --host 0.0.0.0 --port 51763
```

The commonly used health request is:

```bash
curl -sf http://127.0.0.1:51763/api/health
```

`/api/health` is an allowlisted proxy to the Hermes dashboard upstream; it is not a
separate standalone BFF-health implementation.

Systemd operations:

```bash
systemctl --user restart hermes-mission-control
systemctl --user status hermes-mission-control --no-pager
journalctl --user -u hermes-mission-control -f
```

Do not claim a restart or healthy deployment without the corresponding runtime result.

## Test and Validation Commands

This repository **does have an automated contract suite**. Run all four maintained
suites for full repository validation:

```bash
python tests/test_runtime_contracts.py
python tests/test_static_repair_surface.py
node tests/frontend_contracts.mjs
node tests/skills_surface.mjs
```

`test_runtime_contracts.py` runs its synchronous and asynchronous contract tests through
its own entrypoint. Bare `pytest -q` is not the canonical full-suite command because the
repository does not configure the async pytest plugin required to reproduce that suite
as-is.

Useful additional checks:

```bash
python -m compileall -q agent_mission_control
```

If `ruff` is available, the current repository convention permits:

```bash
ruff check agent_mission_control tests
ruff format --check agent_mission_control tests
```

No repository-specific formatter/linter configuration is currently checked in, so do
not invent stricter formatting rules without an explicit change.

## Frontend Rules

`frontend/dist/` is committed source, not a generated build output to be recreated from
a missing `frontend/src` tree.

- Edit the ES modules/CSS in `frontend/dist/` directly.
- Preserve the canonical URL shape:
  `/?profile=<id>#/<route>?<entity-or-filter-params>`.
- Profile belongs in the document query, not in the hash.
- Tab instances are profile-scoped; do not reuse state across profiles.
- When adding/removing/renaming routes or tabs, update the corresponding route
  registries/specs/inventory and loader maps together.
- Keep chat SSE streaming incremental rather than buffering the complete response.
- Do not move tab state into Web Storage merely for convenience.

## Coding Style & Naming

- Use Python 3.11+ syntax, four-space indentation and explicit type hints on new or
  materially changed functions.
- Use `snake_case` for functions/modules/variables, `PascalCase` for classes and
  `UPPER_SNAKE_CASE` for constants.
- Keep imports organized and avoid wildcard imports.
- Preserve current architecture boundaries instead of collapsing route, client, store,
  worker and frontend responsibilities into a single module.

## Git and Pull Requests

This repository has valid Git history. Do not rely on older notes claiming otherwise.

- Work from the current default branch/HEAD and keep task changes scoped.
- After completing any task that changes repository files, create a Git commit before
  sending the final response. Do not leave completed changes uncommitted unless the
  user explicitly asks for that, and include the resulting commit hash in the response.
- Use clear imperative commit subjects.
- PRs should summarize behavior/documentation changes, affected routes/configuration,
  validation commands/results, and migration impact when applicable.
- Never commit runtime DB/WAL/SHM files, copied environments, credentials, logs, or
  machine-specific state.

## Source-of-Truth Rule

For implementation claims, current repository source and contract tests outrank this
file. For deployed behavior, current service/process/API evidence is required in
addition to source. A passing source review does not prove the Pi deployment has been
updated or restarted.

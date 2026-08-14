STATUS: DRAFT — NOT AUTHORIZED FOR HANDOFF OR EXECUTION
DRAFT_REVISION_ID: BRAINSTORM-AGENTOS-DASHBOARD-REFACTOR-R2
SUPERSEDES_OWNER_REVIEW_DRAFT: BRAINSTORM-AGENTOS-DASHBOARD-REFACTOR-R1
OWNER_COMMIT_STATUS: PENDING
HANDOFF_AUTHORIZATION_STATUS: BLOCKED_PENDING_OWNER_COMMIT
RECIPIENT_INSTRUCTION: Do not execute, route, delegate, queue, deploy, or treat this draft as approved. Return it to the owner-review path.

---

# Hermes/Jarvis AgentOS Dashboard — Refactor Mission Contract (v2 Research-Backed Draft)

## 1. Task Identity

- **Task-ID:** `HERMES-AGENTOS-DASHBOARD-REFACTOR-2026-08-07`
- **Draft revision:** `BRAINSTORM-AGENTOS-DASHBOARD-REFACTOR-R2`
- **Supersedes for owner review:** `BRAINSTORM-AGENTOS-DASHBOARD-REFACTOR-R1`
- **Recipient after owner commit:** Hermes CEO (mission planning, routing, recovery, integration, closure)
- **Owner and final decision authority:** Le Kien (Ông Chủ)
- **Current status:** `DRAFT — awaiting owner commit`

## 2. Owner Outcome

Replace the current single-view Agent Mission Control dashboard (`agent-mission-control.service` on Pi `192.168.0.140`, port `51763`) with a **full Hermes/Jarvis AgentOS control plane**.

The result is one browser application with:

- a persistent three-pane layout: left navigation, center workspace, right contextual inspector;
- native Hermes views implemented through the same supported API contracts used by Hermes Web/Desktop, without iframing the Hermes Web UI;
- Jarvis operational views for Kanban, Issues, Permits, Task Room Binding, agent hierarchy, task/run inspection, alerts, and audit activity;
- external llama-proxy visibility through a controlled iframe integration;
- profile-aware routing and data isolation;
- real live updates, preserved tab state, global search, and deep links;
- a backend-for-frontend on the Pi that keeps all upstream credentials server-side;
- a narrow read-only data adapter on the Hermes host for plugin-owned SQLite data;
- no SSH shell-outs, no `sshpass`, no hardcoded credentials, no remote SQLite mounts, and no direct database writes.

The dashboard must feel like the **Hermes interface brought into a Jarvis operations workspace**, not a decorative status page.

## 3. Problem and Value

### 3.1 Current state — verified 2026-08-07

- `agent-mission-control.service` is active on the Pi and listens on `0.0.0.0:51763`.
- The current backend is FastAPI; the frontend is a vanilla JavaScript SPA.
- `server.py` reads Hermes data by SSH-ing from the Pi to `jarvis@192.168.0.129` with `sshpass -p '1'` and inline Python. Each request creates a shell/SSH hop.
- `index.html` is a single glassmorphism page with five tabs: Overview, Agents, Tasks, Schedule, and Files. The “Living Office” SVG is decorative rather than evidence-backed.
- `server.py` assumes `tasks.updated_at`; the real `kanban.db` schema uses `created_at`, `started_at`, `completed_at`, and `last_heartbeat_at`. The fallback causes wrong age/order semantics and an apparently empty board.
- The service is exposed to the LAN without authentication. The Files tab can write into `~/.hermes/` on the Hermes host.
- `/events` emits synthetic heartbeat messages while the frontend still polls every 15 seconds.
- `kanban.db` contains `tasks`, `task_links`, `task_comments`, `task_events`, `task_runs`, `kanban_notify_subs`, and `task_attachments`.
- `permits.db` contains the `permits` table with permit status, severity, approval, evidence, recommendation, action-plan, execution-result, and expiry fields.
- The authoritative Rectify issue store remains unknown. Candidate `issues.db` files have not established the live queue schema and must not be guessed.
- Telegram room bindings live under `platforms.telegram.room_slots` in `~/.hermes/config.yaml`.
- Hermes exposes a dashboard REST surface on `9119` and an OpenAI-compatible gateway/API surface on `8642` / profile-multiplexed routes. The observed dashboard API rejects unauthenticated requests.
- The current `state.db` is approximately `4.67 GB`; full scans are unacceptable.
- The separate llama-proxy React dashboard is available at `http://192.168.0.140:8082/dashboard`.

### 3.2 Why this refactor matters

The current service is stale, insecure, schema-fragile, and operationally shallow. It cannot safely become the owner’s main control surface.

The replacement must provide four qualities simultaneously:

1. **Operational truth:** every state is backed by a named source and freshness marker.
2. **Control:** safe Hermes mutations are available through verified API operations, never database writes.
3. **Governance:** permits, issues, action audit, source health, and degraded states are visible.
4. **Usability:** fast tab switching, global search, deep links, contextual detail, and profile awareness.

### 3.3 External research addendum — consulted 2026-08-07

| Research source | Relevant verified pattern | R2 integration decision |
|---|---|---|
| Hermes Web Dashboard documentation | Profile switcher; sessions and FTS5 search; analytics; cron; profiles; logs; memory; system operations; authenticated non-loopback deployment | Reuse live Hermes API contracts, add profile-scoped navigation, Sessions search, Analytics/Spend, Memory, Models/Plugins/Channels/System surfaces, and fail-closed browser auth |
| Hermes API Server documentation | Server-to-server API-key access, SSE streaming, explicit CORS, `Idempotency-Key` deduplication | Keep upstream keys in the Pi BFF; use streaming where available; require idempotency on supported mutations |
| builderz-labs Mission Control | Tasks, agents, activity, alerts, logs, token/cost, memory, skills, approvals, audits, evals, completion receipts | Add Run Inspector, Fleet/Topology, Alert Center, Activity/Audit, Memory, Analytics/Spend, and completion-evidence views |
| OperatorBoard | Approval queue, constraint visibility, earned-trust signals, full action audit | Present permit/action context and per-agent operating signals; do not add a new autonomous trust policy or enforcement engine in v1 |
| OpenTelemetry GenAI semantic conventions | Stable concepts for agent spans/events/metrics and parent-child relationships | Normalize identifiers and timeline records into an OTel-aligned internal envelope without adding an OpenTelemetry ingestion stack |
| SQLite official guidance | Network-separated clients should use an application server; direct network-filesystem access has latency and locking hazards | Add a Hermes-host read-only data adapter; explicitly forbid NFS/SSHFS/CIFS/direct remote SQLite access |
| FastAPI and browser SSE guidance | Standard events have `id`, `event`, `data`, `retry`, and `Last-Event-ID` reconnection | Replace synthetic SSE and per-tab polling with one reconnectable event fabric and bounded server-side polling fallbacks |
| OWASP browser security guidance | Protect state-changing requests with session-bound CSRF tokens; do not store session identifiers or secrets in browser local storage | Use a server-side session, `HttpOnly` cookie, custom CSRF header, and no browser-stored upstream tokens |
| MDN iframe policy guidance | `frame-ancestors` and `X-Frame-Options` may block embedding | Probe llama-proxy headers first; use a fixed-origin same-host reverse-proxy fallback only when required |

### 3.4 R2 design corrections to R1

R2 corrects five material gaps in R1:

1. **Remote DB transport was underspecified.** “API + DB” now means API plus a local-on-`.129` read-only adapter, not a remote filesystem or SSH helper.
2. **Authentication covered writes too narrowly.** Because sessions, logs, files, config, permits, and room bindings are sensitive, every dashboard route requires authentication; mutations receive additional CSRF, allowlist, idempotency, and audit controls.
3. **“Preload all tabs” could overload the Pi and Hermes.** R2 defines staged code prefetch, lightweight summary prefetch, heavy-detail-on-demand, cache persistence, and concurrency limits.
4. **Several native Hermes surfaces were omitted from the visible navigation.** R2 adds Chat, Analytics/Spend, Models, Plugins, Channels/Messaging, Memory, and Fleet/Topology while grouping the sidebar to avoid sprawl.
5. **Community features were names without contracts.** R2 gives every added feature a source, transport, mutation boundary, degradation behavior, acceptance criterion, and evidence requirement.

## 4. Owner Directives and R2 Design Commitments

### 4.1 Locked owner directives

- **D-001:** Dashboard backend and frontend run on the Pi. They connect to Hermes only through supported Hermes APIs and the approved read-only data access contract.
- **D-002:** Layout is three panes: left navigation, center content, right contextual inspector.
- **D-003:** Tabs switch without full-page reload and keep view state.
- **D-004:** Bind remains `0.0.0.0` on the Pi. Security protections remain mandatory.
- **D-005:** Full tab scope is required, not a reduced MVP subset.
- **D-006:** Hermes Web UI `9119` must not be iframed. Hermes functions are implemented natively through the same API contracts.
- **D-007:** Pi-to-Hermes API authentication uses the Hermes API key, stored only on the server side.
- **D-008:** Issues, Permits, and Task Room Binding are sourced from their authoritative plugin/config stores after live discovery; direct DB writes are forbidden.
- **D-009:** The right sidebar is a contextual inspector that changes with the active tab and selected entity.
- **D-010:** Incorporate control-plane functions that fit Jarvis data: activity/audit, run review, permit queue, agent health, alerting, and usage/cost visibility.

### 4.2 R2 commitments proposed for owner commit

- **R2-D-001 — Read-only data adapter:** Create an isolated `agentos-data-adapter` service on the Hermes host. It opens approved SQLite databases locally in read-only/query-only mode and exposes only typed, allowlisted endpoints.
- **R2-D-002 — BFF security:** The browser never receives the Hermes API key or data-adapter token. The Pi backend authenticates the owner and proxies all upstream access.
- **R2-D-003 — Capability/provenance registry:** Every source-backed module declares source, schema/API fingerprint, freshness, read/write capability, and degraded reason.
- **R2-D-004 — Real event fabric:** One authenticated SSE channel distributes normalized live events and cache invalidations; synthetic heartbeats and independent tab polling are removed.
- **R2-D-005 — Run Inspector:** Correlate sessions, task runs, tool calls, permits, issues, artifacts, timing, errors, and token/cost data where identifiers exist. Missing correlations remain explicitly partial.
- **R2-D-006 — Fleet/Topology:** Show CEO → Manager → Worker hierarchy, profiles, active sessions, room slots, task ownership, heartbeat age, and current health evidence.
- **R2-D-007 — Global Search and Command Palette:** Search supported sources through bounded APIs/queries and deep-link to exact records. Navigation commands are always available; mutations retain their normal confirmation/security path.
- **R2-D-008 — Alert Center and Pulse:** Add deterministic alerts, a compact time-window pulse, and local acknowledge/snooze state without implementing autonomous remediation.
- **R2-D-009 — Dashboard-local control store:** Add a Pi-local database only for owner sessions, preferences, saved views, alert acknowledgements/rules, action audit, cache metadata, and schema fingerprints. It must never become a mirror or source of truth for Hermes records.
- **R2-D-010 — iframe fallback:** Probe llama-proxy embedding headers; if direct embedding is blocked, expose only the fixed llama-proxy dashboard through a same-origin constrained reverse proxy.

## 5. Authoritative Source and Evidence Basis

### 5.1 Current Hermes evidence

- `/home/pi/agent-mission-control/{server.py,index.html,requirements.txt}`
- `~/.hermes/scripts/agent-mc-helper.py`
- `~/.hermes/kanban.db`
- `~/.hermes/workspace/state/permits.db`
- current room-slot configuration under `~/.hermes/config.yaml`
- `~/.hermes/cron/jobs.json`
- `~/.hermes/gateway_state.json`
- `~/.hermes/state.db`
- Hermes source under `hermes_cli/web_server.py`, `hermes_cli/web_routers/*`, `gateway/platforms/api_server.py`, `web/src/pages/*`, `apps/desktop/src/**`, and shared gateway clients
- current systemd unit and process/listener facts for the dashboard
- llama-proxy response and headers at port `8082`

### 5.2 External design references

- Hermes Web Dashboard: `https://hermes-agent.nousresearch.com/docs/user-guide/features/web-dashboard`
- Hermes API Server: `https://github.com/NousResearch/hermes-agent/blob/main/website/docs/user-guide/features/api-server.md`
- builderz-labs Mission Control: `https://github.com/builderz-labs/mission-control`
- OperatorBoard: `https://operatorboard.dev/`
- OpenTelemetry GenAI conventions: `https://github.com/open-telemetry/semantic-conventions-genai`
- SQLite appropriate-use guidance: `https://sqlite.org/whentouse.html`
- FastAPI SSE: `https://fastapi.tiangolo.com/tutorial/server-sent-events/`
- OWASP CSRF guidance: `https://cheatsheetseries.owasp.org/cheatsheets/Cross-Site_Request_Forgery_Prevention_Cheat_Sheet.html`
- OWASP HTML5 storage guidance: `https://cheatsheetseries.owasp.org/cheatsheets/HTML5_Security_Cheat_Sheet.html`
- MDN `frame-ancestors`: `https://developer.mozilla.org/en-US/docs/Web/HTTP/Reference/Headers/Content-Security-Policy/frame-ancestors`

## 6. Facts, Observations, Inferences, Assumptions, and Unknowns

### 6.1 Verified facts

- All current-state facts in §3.1 were directly inspected in the R1 evidence run.
- Hermes-host SQLite files are not local to the Pi.
- The Hermes API is authenticated and exposes broad administrative/read surfaces.
- The existing dashboard contains unauthenticated write paths and hardcoded SSH credentials.
- `state.db` requires bounded, indexed, aggregate, or source-provided API access.

### 6.2 Observations

- The current “Agents floor” can show a fabricated worker when Kanban is empty.
- The fake SSE channel provides no useful invalidation or live-state semantics.
- R1 listed some backend API domains—analytics, models, plugins, channels, memory—but omitted their first-class frontend placement.
- A flat list of 25+ top-level tabs would reduce usability; grouped navigation and command search are required.

### 6.3 Reconstructions and inferences

- A separate Pi SPA can reuse Hermes REST/gateway contracts without modifying or iframing Hermes Web UI.
- Plugin DB access is safest when an application service runs beside the database files and returns bounded records over an authenticated API.
- Full distributed tracing cannot be reconstructed unless Hermes records consistent identifiers. A useful partial timeline is still feasible when coverage is clearly labeled.
- A single BFF event stream can fan out source changes more efficiently than each tab polling independently.

### 6.4 Assumptions

- **A-01:** The Hermes API key can be provisioned to the Pi through a `0600` environment file or systemd credential mechanism without committing it to source.
- **A-02:** The Hermes host permits creation of one isolated read-only adapter service as part of this dashboard suite.
- **A-03:** The adapter can bind to the Hermes Tailscale address or another owner-approved private address and accept only the Pi/dashboard caller.
- **A-04:** Room-slot data is readable through a safe config API or through the adapter’s typed room-binding endpoint.
- **A-05:** llama-proxy remains a separately deployed application and is not modified by this task.
- **A-06:** Existing Hermes APIs remain the source of truth for mutations and core entities.

### 6.5 Unknowns requiring live discovery

- **U-01:** Exact Rectify issue storage, schema, and supported issue mutations/tools.
- **U-02:** Exact current Hermes endpoint coverage for every proposed tab and operation.
- **U-03:** Which `state.db` queries are indexed and safe, and which views must use an existing Hermes API instead.
- **U-04:** Exact Pi-to-Hermes authentication header/cookie contract and profile multiplexing behavior.
- **U-05:** llama-proxy `X-Frame-Options`, CSP `frame-ancestors`, relative asset paths, and same-origin requirements.
- **U-06:** Identifier coverage across session, run, task, thread, tool call, permit, issue, artifact, and agent records.
- **U-07:** Which sources provide native events versus requiring bounded server-side polling.
- **U-08:** Exact API path for native browser Chat, streaming responses, session resume, and profile routing without a remote PTY/shell.
- **U-09:** Which cost values are provider-reported, Hermes-calculated, or estimable from verified model-rate metadata.

Unknowns are implementation-time evidence dependencies. They are not permission for implementers to invent schemas, data, endpoints, or success states.

## 7. Decision-Right Map

| Decision | Owner | CEO | Manager | Worker | Evidence gate |
|---|---:|---:|---:|---:|---|
| Outcome, full scope, non-goals, mutation boundary, owner commit | ✅ |  |  |  | Owner directive |
| Mission stages, current routing, recovery, integration, closure |  | ✅ |  |  | Current routing evidence |
| Domain decomposition and bounded Worker assignments |  |  | ✅ |  | Current capabilities |
| Exact framework/components inside the locked architecture |  |  |  | ✅ | Build/test evidence |
| Current Hermes API/auth/schema/event coverage |  |  |  | ✅ | Live inspection |
| Issue-store discovery |  |  |  | ✅ | U-01 |
| Safe query plans for large databases |  |  |  | ✅ | `EXPLAIN QUERY PLAN`, timing, limits |
| Whether a mutation is exposed in the UI | ✅ boundary | ✅ integration | ✅ domain | ✅ implementation | Verified Hermes API operation + security tests |
| Final acceptance and remediation decision | ✅ | ✅ synthesis |  |  | AC/EV package |

## 8. Target Architecture and Data Contracts

### 8.1 Required components

1. **Browser SPA on the Pi origin**
   - Three-pane UI.
   - No upstream credentials.
   - One authenticated same-origin API and event connection.

2. **Pi Backend-for-Frontend (`agent-mission-control.service`)**
   - Serves static frontend assets.
   - Owns owner login/session, CSRF, rate limiting, source clients, cache, normalized API, event fan-out, mutation allowlist, and action audit.
   - Connects server-to-server to Hermes APIs and the read-only adapter.

3. **Hermes API clients**
   - Dashboard REST API on the verified Hermes endpoint.
   - Gateway/OpenAI-compatible API for Chat/streaming/session operations where verified.
   - Profile-aware routing.

4. **Hermes-host read-only data adapter (`agentos-data-adapter.service`)**
   - Runs beside approved plugin SQLite files on `.129`.
   - Opens SQLite with URI read-only mode and `PRAGMA query_only=1`.
   - Exposes typed endpoints only; no generic SQL endpoint.
   - Enforces parameterized queries, pagination, maximum limits, query timeouts, source allowlists, and response-size limits.
   - Returns schema fingerprints and source freshness.
   - Has no mutation endpoints.

5. **Pi-local control store (`agentos-dashboard.db`)**
   - Stores only dashboard-owned state: owner sessions, preferences, saved views, alert definitions/acknowledgements/snoozes, mutation audit, capability snapshots, cache metadata, and schema fingerprints.
   - Must not duplicate full Hermes sessions, messages, tasks, permits, issues, files, or logs.

6. **llama-proxy integration**
   - Direct iframe when allowed by headers.
   - Otherwise, a fixed-origin, fixed-path, read-only reverse-proxy route under the Pi backend.

### 8.2 Required network paths

| Caller | Target | Transport | Credential location | Allowed purpose |
|---|---|---|---|---|
| Browser | Pi BFF | Same-origin HTTP on LAN/Tailscale | Server-side session cookie; CSRF header for mutations | UI data, approved mutations, SSE |
| Pi BFF | Hermes dashboard API | Private network/Tailscale HTTP | Pi server-side Hermes API key | Supported Hermes reads/mutations |
| Pi BFF | Hermes gateway API | Private network/Tailscale HTTP/SSE | Pi server-side Hermes API key | Chat, responses, sessions/runs where verified |
| Pi BFF | Hermes data adapter | Tailscale/private HTTP | Pi server-side adapter token | Bounded read-only plugin/config data |
| Hermes adapter | Local SQLite/config | Local filesystem only | Unix service permissions | Typed read-only queries |
| Browser | llama-proxy iframe route | Direct fixed origin or same-origin proxy | No Hermes key; no generic proxy credential forwarding | Existing llama-proxy dashboard only |

Forbidden paths:

- Browser → Hermes API directly.
- Browser → adapter directly.
- Pi → `.129` through shell commands, `sshpass`, or inline remote Python.
- Pi → SQLite over SSHFS, NFS, CIFS, SMB, or another network mount.
- Generic SQL submitted by the browser or BFF.
- Generic open reverse proxy.

### 8.3 Normalized source envelope

Every BFF read response must include or inherit the following metadata:

```json
{
  "data": {},
  "meta": {
    "source_id": "hermes-api|gateway-api|kanban-db|permits-db|issues-store|room-binding|dashboard-local",
    "source_version": "string-or-null",
    "schema_fingerprint": "string-or-null",
    "profile_id": "string-or-null",
    "fetched_at": "RFC3339",
    "stale_after": "RFC3339-or-null",
    "freshness": "live|fresh|stale|unavailable|unsupported|partial",
    "read_only": true,
    "mutations_supported": [],
    "degraded_reason": null,
    "request_id": "string"
  }
}
```

The UI must expose freshness and degraded state where operationally relevant. An empty dataset must not be visually indistinguishable from an unavailable source.

### 8.4 Normalized identity and correlation model

Use source-native identifiers when available and preserve them without replacement:

- `profile_id`
- `agent_id`
- `session_id`
- `run_id`
- `task_id`
- `thread_id`
- `tool_call_id`
- `permit_id`
- `issue_id`
- `artifact_id`
- `parent_id`
- `source_event_id`

Create a dashboard correlation record only when evidence supports the relation. Never infer a direct relationship from timestamps alone without labeling it `inferred`.

### 8.5 Event envelope

The BFF SSE channel must emit normalized events:

```json
{
  "event_id": "monotonic-or-source-stable-id",
  "event_type": "source.health|task.changed|run.changed|session.changed|permit.changed|issue.changed|cron.changed|log.appended|alert.changed|cache.invalidated",
  "occurred_at": "RFC3339",
  "profile_id": "string-or-null",
  "entity_type": "string",
  "entity_id": "string-or-null",
  "source_id": "string",
  "payload": {},
  "coverage": "native|polled|derived"
}
```

Requirements:

- event `id`, type, data, and retry support;
- `Last-Event-ID` reconnection and a bounded replay buffer;
- duplicate suppression by source/event identity;
- heartbeat comments for connection liveness only, never fake business events;
- a single browser `EventSource` connection;
- server-side polling only for sources without native events, with bounded cadence and no per-tab polling loops.

### 8.6 Mutation contract

Every dashboard mutation must:

1. map to a verified Hermes API/tool operation;
2. be explicitly allowlisted in the BFF;
3. require authenticated owner session and CSRF validation;
4. carry a request ID and `Idempotency-Key` when the upstream supports it;
5. redact secrets from request/response logs;
6. write an action-audit row to the local control store;
7. return upstream truth without rewriting success;
8. never fall back to a direct database write or shell command.

## 9. Navigation and Information Architecture

### 9.1 Persistent application chrome

- **Left header:** Hermes/Jarvis identity, active profile selector, source-health indicator, global search/command trigger.
- **Left navigation:** grouped sections below.
- **Center workspace:** active route, tab-specific toolbar, primary content.
- **Right inspector:** selected entity, filters, actions, provenance, relations, and source status.
- **Global status strip:** current profile, event connection, stale source count, pending permits, critical alerts, running tasks.

### 9.2 Grouped navigation

#### OPERATE

- Overview
- Chat
- Sessions
- Fleet / Topology
- Kanban
- Run Inspector
- Cron
- Activity
- Alerts
- Analytics / Spend

#### GOVERN

- Issues
- Permits
- Room Binding
- Action Audit

#### BUILD & INTEGRATE

- Skills
- Memory
- Profiles
- Models
- Tools / Toolsets
- MCP
- Plugins
- Webhooks
- Channels / Messaging
- Artifacts
- Files

#### SYSTEM

- Logs
- Command Center
- Settings / System
- llama-proxy

The UI may use nested groups and icons, but every locked surface must remain reachable from the left navigation and the command palette.

### 9.3 Profile context

- The active profile is visible at all times.
- Profile selection is represented in the URL, for example `?profile=<id>` or an equivalent route segment.
- Every profile-scoped request is keyed by the selected profile.
- Before any profile-changing mutation, the UI shows the target profile in the confirmation surface.
- Switching profiles invalidates only profile-scoped caches and preserves global UI preferences.
- Unsupported profile-scoped data must show `unsupported`, not silently fall back to the default profile.

## 10. Feature Integration Matrix

| Feature | Primary source(s) | Transport | Mutation scope | Required degraded behavior |
|---|---|---|---|---|
| Overview | Hermes status, sessions, Kanban, permits, issues, cron, analytics, source registry | BFF fan-in | None | Cards show stale/unavailable source badges independently |
| Chat | Hermes gateway/API sessions/responses after U-08 verification | BFF streaming proxy | Send message, create/resume session through verified API only | Disable send and show exact missing capability; never spawn remote shell/PTY |
| Sessions | Hermes sessions API and message-history endpoints | Hermes API | Only verified rename/archive/export/delete actions; destructive actions require explicit confirmation | Search/history remains available independently of Chat capability |
| Fleet / Topology | Profiles API, sessions, Kanban ownership/heartbeat, room slots, agent/config evidence | Hermes API + adapter | Existing safe lifecycle operations only if verified; no invented kill switch | Unknown hierarchy edges visibly labeled; no fabricated agents |
| Kanban | `kanban.db` through adapter | Read-only adapter | Read-only in v1 unless a verified Kanban API/tool is found and owner scope permits it | Correct schema mapping; empty is distinct from unavailable |
| Run Inspector | Sessions/messages/tool calls, task runs/events, permits, issues, artifacts, analytics | Hermes API + adapter | Review/navigation only | Timeline labeled `complete`, `partial`, or `unsupported` by source coverage |
| Cron | Hermes cron API | Hermes API | Create/edit/pause/resume/trigger/delete when verified | Per-action capability flags; no hidden fallback |
| Activity | Task events, session/run events, BFF mutations, logs, source health | API + adapter + local audit | Read-only filters/export | Source and coverage shown per event |
| Alerts | Derived deterministic rules + source health + failures | BFF + local store | Acknowledge/snooze/save rule locally; optional delivery only through verified webhook/channel API | In-app alert remains even when outbound delivery is unsupported |
| Analytics / Spend | Hermes analytics API, token fields, verified rate metadata | Hermes API | None | Always separate actual, provider-reported, estimated, and unavailable cost |
| Issues | Live-discovered Rectify store/tool | Adapter or verified tool/API | Read-only unless authoritative issue tooling supports mutation and owner scope allows it | Show discovery blocker; never read an empty decoy DB as authoritative |
| Permits | `permits.db` for read; permit tooling/API for decisions | Adapter + verified mutation API/tool | Approve/reject only through permit operation, never DB | Read remains available when decision endpoint is unavailable |
| Room Binding | Config API or typed adapter endpoint | API/adapter | Read-only in v1 unless an existing validated binding API is explicitly selected | Exact source path and freshness shown |
| Action Audit | Pi-local control store | BFF local | Append-only by BFF; UI read/export | Audit failure blocks mutation rather than silently losing the record |
| Skills | Hermes skills API | Hermes API | Existing toggle/install/update/uninstall only when owner-authorized and verified | Capability badges per action |
| Memory | Hermes memory API | Hermes API | Read/provider/reset only when explicitly exposed and confirmed; reset requires destructive confirmation | Provider unavailable shown explicitly |
| Profiles | Hermes profiles API | Hermes API | Existing create/edit/select/rename/delete operations; destructive confirmation for delete | Active/default/scoped profile distinction preserved |
| Models | Hermes models API | Hermes API | Existing model selection/config actions only | Provider/config errors preserved |
| Tools / MCP / Plugins / Webhooks / Channels | Corresponding Hermes APIs | Hermes API | Verified native operations only | Unsupported operations hidden or disabled with reason |
| Artifacts | Hermes artifact/file APIs and task attachment metadata | API + adapter | Existing safe file/artifact operations only; path guards mandatory | Source and target path constrained and shown |
| Files | Hermes files API | Hermes API | No arbitrary filesystem access; only API-exposed roots and operations | Traversal attempts rejected; unavailable roots explicit |
| Logs | Hermes logs API + event invalidation | Hermes API | Read/export only | Bounded line counts; no unrestricted filesystem log path |
| Command Center | Hermes doctor/security-audit/backup/import/actions APIs | Hermes API | Existing operations only, with live action output and confirmations | No shell-hook creation unless separately authorized and verified |
| Settings / System | Hermes config/system APIs | Hermes API | Verified settings operations; secrets remain redacted | Read-only fallback when write capability absent |
| llama-proxy | Existing `8082/dashboard` | Direct iframe or constrained same-origin proxy | No dashboard-side llama-proxy mutation beyond what embedded app already provides | Header probe result and fallback mode visible in diagnostics |
| Global Search | Sessions FTS API; bounded adapters for tasks/issues/permits; native APIs for other entities | BFF federated search | Navigation only | Per-source partial results and timeouts shown; no full DB scan |

## 11. New R2 Functional Requirements

### 11.1 Capability and provenance registry

At startup and on demand, the BFF must discover and cache:

- API health and version;
- supported route/capability set;
- active profiles;
- adapter health;
- approved database/config sources;
- schema fingerprints and required-column checks;
- event capability per source;
- mutation capability per module;
- source freshness policy.

The frontend must use this registry to render supported, disabled, partial, stale, and unavailable states. It must not infer capability from a `200` response on an unrelated endpoint.

### 11.2 Global Search and command palette

Provide one keyboard-accessible command/search surface, preferably `Ctrl/Cmd+K`, with:

- navigation to every tab;
- profile switch commands;
- federated entity search;
- bounded results per source;
- source chips and result type;
- exact record deep links that restore tab, selected entity, filters, and inspector;
- cancellation/timeouts for slow sources;
- no mutation commands that bypass normal confirmation or security controls.

Search implementation:

- Sessions: existing Hermes FTS endpoint.
- Tasks/issues/permits: typed indexed adapter queries with query-length, limit, and timeout bounds.
- Skills/profiles/models/tools/MCP/plugins/webhooks/channels/files/logs: native API search/filter where available; otherwise bounded client-side filtering of an already bounded response.
- `state.db`: never global-scan for search.

### 11.3 Run Inspector

Provide two synchronized views:

1. **Tree:** parent-child execution structure when identifiers exist.
2. **Trajectory:** chronological ordered events across all correlated sources.

Each node/event may show:

- source and coverage;
- agent/profile/session/task/run identity;
- start/end/duration;
- status/error;
- model;
- tool name and redacted arguments/result summary;
- token counts and cost classification;
- permit/issue relations;
- artifact/attachment relations;
- completion evidence or missing evidence.

The inspector must not claim replay capability unless a source provides a safe, verified replay operation. In v1, “review” means inspect and navigate evidence.

### 11.4 Fleet and topology

Show the operational hierarchy and current evidence:

- CEO, Manager, Worker role nodes;
- profile association;
- Telegram room/thread binding where known;
- active/recent sessions;
- assigned/running Kanban tasks;
- last heartbeat and age;
- last error/failure signal;
- pending permit/issue count;
- token/cost summary where available;
- link to related sessions, tasks, runs, issues, and room slot.

Hierarchy must come from current configuration/routing evidence. Unknown edges remain unconnected or labeled unknown. No fake office occupants or inferred “online” status from static configuration.

### 11.5 Alert Center and operational Pulse

Add deterministic, explainable alerts for at least:

- upstream source unavailable;
- source/schema fingerprint changed;
- stale source beyond policy;
- failed cron run;
- failed run/task;
- stale running-task heartbeat;
- expiring or long-pending permit;
- unresolved high-severity issue;
- event-stream disconnected;
- token/cost spike relative to a configurable local threshold;
- repeated authenticated mutation failure.

Each alert includes rule ID, source evidence, first/last seen, severity, related entity, current state, and exact reason.

The Overview contains a compact Pulse strip for a selected time window:

- event count;
- failures/errors;
- p50/p95 latency where source timings exist;
- input/output tokens;
- actual/estimated cost classification;
- active sessions/tasks;
- pending permits and issues.

Clicking or dragging a Pulse interval filters linked Activity/Run/Analytics views when feasible.

Alert acknowledgement, snooze, saved thresholds, and view state are local dashboard metadata. They do not mutate Hermes issues or permits.

### 11.6 Activity and action audit

Keep two distinguishable feeds:

- **Operational Activity:** source events from tasks, runs, sessions, cron, permits, issues, logs, and source health.
- **Dashboard Action Audit:** every attempted BFF mutation with actor/session, request ID, action, target, selected profile, timestamp, redacted request summary, upstream response status, and final result.

Action audit is append-only through the application. If the audit row cannot be persisted, the mutation must not be sent upstream.

### 11.7 Real-time event fabric

Replace the current fake `/events` behavior with:

- one authenticated SSE endpoint;
- event IDs and reconnection;
- bounded replay;
- source-specific polling workers only where native events are absent;
- cache invalidation/delta events rather than full payload floods;
- pause/backoff when a source is down;
- visible connection/degraded status;
- no 15-second polling loop per tab.

REST remains the mutation transport. Chat may use a separate streaming response managed through the same authenticated BFF session.

### 11.8 Staged preload and keep-alive strategy

“Preload” means fast route activation without triggering an all-data startup storm.

Required sequence:

1. Load application shell, auth state, profile list, capability registry, source health, and Overview summaries.
2. Prefetch route code chunks during browser idle time.
3. Prefetch lightweight tab summaries with a maximum of four concurrent upstream requests.
4. Fetch heavy lists/details only on first activation or explicit prefetch priority.
5. Cache data by profile, route, filters, and source fingerprint using stale-while-revalidate semantics.
6. Preserve scroll, selection, filters, column state, and right-inspector state.
7. Keep recently used heavy tabs in an LRU keep-alive set rather than retaining every large DOM tree indefinitely.
8. Create the llama-proxy iframe once after first use and hide/show it without reloading.
9. Cancel obsolete in-flight requests when the profile or filter context changes.
10. Prevent repeated tab switches from producing duplicate upstream requests.

### 11.9 Cost semantics

- Show token usage whenever the source provides it.
- Label cost as one of: `provider-reported`, `Hermes-calculated`, `estimated-from-verified-rate`, or `unavailable`.
- Do not present estimated cost as actual cost.
- Do not silently fetch or guess current provider pricing.
- No forecasting or budget-stop enforcement in v1.

### 11.10 llama-proxy embedding hardening

Before frontend implementation:

1. capture response headers and asset paths;
2. test direct iframe embedding from the Pi dashboard origin;
3. if blocked only by origin/frame policy, implement a constrained same-origin proxy with:
   - one fixed upstream origin;
   - one fixed dashboard path family;
   - no arbitrary target parameter;
   - no Hermes credential forwarding;
   - stripped upstream cookies/authorization unless the existing app explicitly requires its own safe session;
   - bounded timeouts and response sizes;
   - WebSocket support only if the current dashboard requires it and the route is fixed;
4. preserve `8082` and the original standalone URL;
5. record whether the deployed mode is `direct-iframe` or `proxied-iframe`.

## 12. In-Scope Work

### 12.1 Discovery and contract capture

- Discover U-01 through U-09.
- Export the current Hermes API route/capability list and representative payloads.
- Determine profile scoping.
- Record adapter source paths, table schemas, required indexes, and schema fingerprints.
- Run bounded query-plan checks before accepting any large-DB query.
- Probe llama-proxy embedding.

### 12.2 Hermes-host read-only data adapter

- Create the isolated service and configuration.
- Restrict file/source allowlists.
- Implement typed endpoints for Kanban, permits, issues after discovery, room binding when needed, task runs/events/attachments, and only approved aggregate data.
- Implement health, capabilities, schema fingerprint, pagination, query limit, timeout, and redaction.
- Bind only to the owner-approved private interface; never `0.0.0.0` unless separately authorized.
- Store the adapter credential outside source with `0600` permissions or a systemd credential.

### 12.3 Pi Backend-for-Frontend

- Replace SSH helper behavior with Hermes API clients and adapter client.
- Implement browser auth, session, CSRF, rate limits, request IDs, mutation allowlist, action audit, cache, capability registry, and SSE event fabric.
- Serve frontend assets.
- Maintain a Pi-local control-store schema and migrations.
- Enforce source timeouts, concurrency limits, and response bounds.

### 12.4 Frontend

- Build the full grouped navigation and all locked tabs.
- Implement three-pane responsive desktop-first layout.
- Implement profile selector, global search/command palette, deep links, source status, stale/degraded states, Run Inspector, Fleet, Alerts/Pulse, Activity, and Action Audit.
- Preserve the glassmorphism visual language while prioritizing operational density and readability.
- Implement staged preload/keep-alive.

### 12.5 Safe native mutations

- Implement only operations present in the verified Hermes API/tool contract.
- Require explicit confirmation for destructive actions.
- Preserve exact upstream errors and asynchronous action state.
- Do not add direct plugin DB mutations.

### 12.6 Deployment and verification

- Back up the existing Pi dashboard before replacement.
- Deploy the Pi dashboard service and the Hermes-host read-only adapter service.
- Restart only those two task-owned services.
- Verify unrelated services before and after.
- Produce the full evidence package and README.

## 13. Out of Scope

- Modifying Hermes core, Hermes Web UI, or Hermes Desktop source.
- Iframing Hermes Web UI `9119`.
- Remote PTY, terminal, shell, SSH, or `sshpass` integration for Chat or data access.
- Direct or network-mounted SQLite access from the Pi.
- Generic SQL endpoints.
- Direct writes to `state.db`, `kanban.db`, `permits.db`, issue stores, config files, or room-binding storage.
- A mirrored warehouse or replicated copy of Hermes operational records in `agentos-dashboard.db`.
- OpenTelemetry collector/SDK ingestion, Langfuse/LangSmith/Phoenix deployment, or another observability platform.
- Autonomous alert remediation, agent trust promotion, policy enforcement, or a new kill switch.
- Enterprise RBAC/SSO/IdP. This remains a single-owner dashboard.
- Public internet exposure or new TLS termination.
- General multi-host infrastructure monitoring. Existing Hermes system stats may be displayed through its API; this task does not add a host-monitoring agent.
- Cost forecasting, dynamic provider-price scraping, or budget enforcement.
- Full deterministic run replay unless a verified Hermes operation already provides it.
- Merging or rebuilding llama-proxy.
- Restarting unrelated services.

## 14. Authority and Mutation Boundary

### 14.1 Read authority

- Authenticated browser session → Pi BFF.
- Pi BFF → supported Hermes APIs.
- Pi BFF → typed read-only adapter endpoints.
- Adapter → allowlisted local files/databases in read-only/query-only mode.

All browser-visible data routes require authentication because they expose operationally sensitive content.

### 14.2 Write authority

Dashboard writes are allowed only through verified Hermes API/tool operations included in this task’s approved surface.

Direct DB/config writes are forbidden.

Examples of potentially eligible API mutations after verification:

- cron create/edit/pause/resume/trigger/delete;
- skill toggle and owner-authorized lifecycle actions;
- profile edits;
- supported session actions;
- supported config/system actions;
- permit decision through authoritative permit tooling;
- webhook/channel operations;
- Chat message/session operations.

Eligibility is not inferred from the UI concept. Each action requires a live endpoint/tool and the security contract in §8.6.

### 14.3 Authentication and browser security

- Fail closed when bound to `0.0.0.0`.
- All pages, APIs, static application routes that reveal operational data, and SSE require an authenticated owner session.
- Use a server-side session with a random session identifier in an `HttpOnly`, `SameSite=Strict` cookie. Set `Secure` whenever HTTPS is present.
- Do not store session identifiers, Hermes keys, adapter tokens, or other secrets in `localStorage`, `sessionStorage`, IndexedDB, URL parameters, HTML, or frontend bundles.
- Use a session-bound CSRF token sent in a custom header for state-changing requests.
- Enforce origin/host checks, bounded request bodies, and rate limits.
- Redact keys, secrets, credentials, message content designated sensitive, and raw tool secrets from logs/audit.
- Keep upstream keys in `0600` files, protected environment files, or systemd credentials.

### 14.4 Deployment authority

After owner commit, this mission may:

- replace/restart `agent-mission-control.service` on the Pi;
- create/restart the task-owned `agentos-data-adapter.service` on the Hermes host;
- create task-owned application/config/data directories and credentials for those services;
- create backups of the existing dashboard and task-owned files.

It may not restart or modify other services without a newer explicit owner instruction.

## 15. Constraints and Preservation Rules

- Hermes remains the source of truth.
- The Pi-local control store remains metadata-only.
- Adapter SQLite connections use read-only URI mode and `PRAGMA query_only=1`.
- No adapter endpoint accepts raw SQL, arbitrary file paths, arbitrary table names, or arbitrary sort expressions.
- Every list endpoint has explicit allowed filters, sort keys, maximum page size, and timeout.
- Heavy queries require index/query-plan evidence.
- `state.db` full-table scans are prohibited.
- Credentials never enter the source tree, frontend, screenshots, or evidence package.
- Frontend assets are local; no new external CDN runtime dependency.
- Only fixed upstreams may be proxied.
- Back up `~/agent-mission-control/` before replacement.
- Preserve the standalone llama-proxy URL and port.
- Preserve unrelated Pi/Hermes services.
- Preserve raw upstream statuses and errors in the evidence package with secrets redacted.
- Unsupported and unavailable are first-class UI states; fake or placeholder operational data is prohibited.

## 16. Success Definition

The owner opens one Pi URL and can:

- authenticate and remain within one browser SPA;
- switch profiles with visible scope;
- navigate all grouped tabs without a document reload;
- retain tab state and receive live updates;
- search across supported Hermes/Jarvis entities and deep-link to a record;
- inspect sessions, tasks, runs, tools, permits, issues, artifacts, and correlations;
- understand fleet hierarchy and current agent/task/session health;
- review permits/issues and use only safe verified mutations;
- see source provenance, freshness, partial coverage, failures, alerts, tokens, and cost classification;
- use native Hermes management surfaces without exposing the Hermes key;
- open llama-proxy inside the application without modifying the external service;
- verify that no SSH bridge, hardcoded password, remote SQLite mount, direct DB write, or fabricated operational state remains.

## 17. Acceptance Criteria

| ID | Criterion | Verification |
|---|---|---|
| AC-01 | Three-pane layout renders with persistent left nav, center workspace, and contextual right inspector | Browser inspection |
| AC-02 | Navigation is grouped as Operate/Govern/Build & Integrate/System and every locked surface is reachable | Route inventory + browser walkthrough |
| AC-03 | Switching tabs causes no full document reload; scroll, selection, filters, and inspector state persist | Browser network/performance capture |
| AC-04 | Staged preload uses idle code prefetch, summary prefetch, heavy-on-demand fetch, LRU keep-alive, and maximum four concurrent upstream preload requests | Network trace + instrumentation |
| AC-05 | Repeated tab switches do not trigger duplicate request storms | Network trace |
| AC-06 | Active profile is persistent, URL-addressable, visible, and applied to every scoped request | Multi-profile test |
| AC-07 | Pi no longer invokes `ssh`, `sshpass`, shell helpers, or inline remote Python for dashboard data | Code grep + process/command audit |
| AC-08 | No SQLite file is mounted/read remotely from the Pi; plugin DB reads pass through the local-on-Hermes adapter | Mount/process/config inspection |
| AC-09 | Adapter opens allowlisted DBs read-only/query-only and exposes no generic SQL or mutation route | Code review + write probes |
| AC-10 | Kanban uses the real schema/timestamps and matches raw local DB samples | Adapter payload vs local SQL sample |
| AC-11 | Permits render real fields; any decision action uses authoritative permit tooling/API and never DB writes | Read comparison + mutation trace |
| AC-12 | Issues use the live-discovered authoritative store; a decoy/empty DB is not accepted as proof | U-01 evidence + sample comparison |
| AC-13 | Room Binding shows all configured slots/threads from the authoritative config source | Source comparison |
| AC-14 | Hermes native tabs render through verified APIs: Sessions, Cron, Skills, Profiles, Models, Tools, MCP, Plugins, Webhooks, Channels, Files, Logs, Memory, Analytics, System/Command Center | API/UI comparison |
| AC-15 | Chat streams through a verified Hermes API via the BFF, supports selected-profile context, and does not spawn a remote PTY/shell | Chat trace + process audit |
| AC-16 | Global Search uses Sessions FTS plus bounded source-specific searches, supports cancellation, and deep-links to exact records | Search test matrix |
| AC-17 | Search and dashboard queries do not full-scan `state.db`; accepted heavy queries have query-plan/timing evidence | `EXPLAIN QUERY PLAN` + timings |
| AC-18 | Run Inspector provides tree and trajectory views with explicit complete/partial/unsupported coverage | Correlated sample walkthrough |
| AC-19 | Fleet/Topology uses real hierarchy/config/session/task/heartbeat evidence and contains no fabricated agent/online state | Source comparison |
| AC-20 | Overview includes Pulse; Alert Center detects the required deterministic conditions and supports local acknowledge/snooze | Rule injection/test fixtures + UI |
| AC-21 | Operational Activity and Dashboard Action Audit are distinct; every attempted mutation has an append-only audit record | Mutation test + local DB query |
| AC-22 | One authenticated SSE channel emits real typed events with IDs, retry, replay/reconnect, deduplication, and source coverage | Disconnect/reconnect test + event capture |
| AC-23 | Synthetic business heartbeats and per-tab 15-second polling are absent | Code/network inspection |
| AC-24 | Every source-backed response exposes provenance/freshness/capability metadata and UI distinguishes empty, stale, unavailable, unsupported, and partial | Contract/UI test matrix |
| AC-25 | All application data routes and SSE require authentication under `0.0.0.0`; unauthenticated requests fail closed | HTTP auth matrix |
| AC-26 | Browser contains no Hermes key, adapter token, or session ID in Web Storage/URL/frontend source; session cookie is HttpOnly/SameSite and mutations require CSRF | Browser storage/cookie/source inspection + CSRF probes |
| AC-27 | Mutations are allowlisted, rate-limited, request-ID traced, audited, and idempotent where upstream supports it | Mutation contract tests |
| AC-28 | Files/log/config operations are path/source bounded and reject traversal or arbitrary target requests | Traversal/fuzz probes |
| AC-29 | Pi-local `agentos-dashboard.db` contains only permitted dashboard metadata and no mirrored Hermes record bodies | Schema + row-content inspection |
| AC-30 | Analytics labels token/cost origin as provider-reported, Hermes-calculated, verified estimate, or unavailable | Sample payload/UI inspection |
| AC-31 | llama-proxy loads in a retained iframe via direct or constrained-proxy mode; no generic proxy is exposed and port `8082` remains unchanged | Header probe + iframe/network test |
| AC-32 | Existing dashboard files are backed up; only the two task-owned services are created/restarted; unrelated services remain unchanged | Before/after service and file evidence |
| AC-33 | No secret, hardcoded password, `sshpass`, arbitrary SQL, fake operational data, or direct DB write exists in source/frontend/logs | Static/security scan |
| AC-34 | README documents architecture, data contracts, auth, credentials, profile semantics, event model, local-store boundary, tab/source map, deployment, rollback, and update workflow | File review |

## 18. Required Evidence

| ID | Evidence |
|---|---|
| EV-01 | Deployed Pi URL and screenshots of every major navigation group plus Login, Overview, Run Inspector, Fleet, Search, Alerts, and contextual inspector |
| EV-02 | Architecture and trust-boundary diagram showing browser, Pi BFF, Hermes APIs, adapter, local SQLite sources, control store, and llama-proxy |
| EV-03 | Full route/capability inventory with profile scope, read/write status, source, and degraded behavior |
| EV-04 | Browser network capture proving SPA navigation, staged preload, concurrency cap, no duplicate switch storms, and no full document reload |
| EV-05 | Auth/security evidence: unauthenticated failures, cookie attributes, CSRF rejection, origin/host checks, rate-limit result, and absence of keys in browser storage/source |
| EV-06 | Adapter evidence: unit configuration, bind/listener, credential permissions, source allowlist, read-only/query-only proof, rejected write/raw-SQL/path probes |
| EV-07 | Schema fingerprints and raw-vs-adapter samples for Kanban, Permits, Issues, Room Binding, task runs/events, and attachments |
| EV-08 | Large-DB query evidence: accepted SQL/API path, limit, `EXPLAIN QUERY PLAN`, timing, row count, timeout, and cache policy |
| EV-09 | SSE event samples and disconnect/reconnect/Last-Event-ID/dedup test; proof fake heartbeat/poll loops were removed |
| EV-10 | Correlation coverage matrix and sample Run Inspector tree/trajectory with source/coverage labels |
| EV-11 | Global Search test matrix across sources with deep links, bounded results, timeout behavior, and no full scans |
| EV-12 | Alert rule test results, Pulse screenshots, acknowledge/snooze rows, and source-health degradation example |
| EV-13 | Mutation audit samples linking browser request ID, local audit row, upstream action, exact result, and idempotency behavior |
| EV-14 | llama-proxy response-header probe, selected embedding mode, route allowlist, and standalone `8082` availability |
| EV-15 | Backup artifact and before/after service/listener checks for dashboard, adapter, and unrelated services |
| EV-16 | Static scan for secrets, `sshpass`, shell-outs, remote mounts, raw SQL endpoints, traversal, and undeclared proxy behavior |
| EV-17 | README, deployment files, local-store schema/migrations, source contract, and recovery/rollback instructions |

## 19. Dependencies and Unlock Conditions

| Dependency | Type | Required return | Unlocks |
|---|---|---|---|
| U-01 issue discovery | Live inspection | Exact authoritative storage/tool, schema, identifiers, representative records | Issues module and correlations |
| U-02 API coverage | Live inspection | Route/method/auth/profile/payload/error samples | Native tabs and mutations |
| U-03 safe DB access | Query inspection | Source paths, indexes, query plans, limits, timings | Adapter endpoints |
| U-04 auth/profile handshake | Live inspection | Exact header/cookie/key source/profile route behavior | Hermes clients |
| U-05 iframe policy | HTTP/browser probe | Headers, assets, cookies, WebSocket needs, direct embed result | llama-proxy mode |
| U-06 correlation coverage | Data inspection | Identifier map and missing relations | Run Inspector/Fleet accuracy |
| U-07 event coverage | Source inspection | Native stream/event/poll capability per source | Event workers and cadence |
| U-08 Chat API | Live API test | Streaming/session/profile/request/response contract | Native Chat |
| U-09 cost semantics | Payload/config inspection | Actual/estimated markers and verified rate source | Analytics/Spend labels |
| Hermes API key | Credential | Server-side key provisioned on Pi without artifact exposure | Authenticated upstream access |
| Adapter credential | Credential | Server-side shared credential on Pi/Hermes with restrictive permissions | Adapter access |
| Owner-login secret/provider | Credential/config | Fail-closed BFF login configured | Browser access |
| Existing dashboard backup | Artifact | Readable backup archive with hash | Pi replacement |

Discovery of one blocked optional feature must not block unrelated modules. The CEO returns the smallest blocker and continues all independently unlocked stages.

## 20. Risks, Stop Conditions, Correction, and Escalation

| Risk | Event | Consequence | Control/detection | Required response |
|---|---|---|---|---|
| R-01 | Hermes API changes during build | Broken/unsafe tab | Capability snapshot, contract tests, version/fingerprint | Update client against current API; do not modify core |
| R-02 | Remote SQLite or network mount introduced | Locking, latency, corruption risk | Mount/code/process scan | Stop that path; use local adapter |
| R-03 | Large query causes I/O/latency | Hermes degradation | Query plans, timeout, LIMIT, cache, concurrency cap | Cancel/optimize or use API; do not accept full scan |
| R-04 | Wrong issue source selected | False operational truth | Require representative authoritative records/tool linkage | Mark unavailable and escalate exact evidence |
| R-05 | API/adapter key exposed | Security incident | Server-only secrets, redaction, browser/static scans | Stop deployment, rotate key, remediate, re-test |
| R-06 | `0.0.0.0` starts without auth | LAN exposure | Fail-closed startup and unauthenticated probe | Service must refuse startup or reject every request |
| R-07 | SSE/poll fan-out creates request storm | Load and stale UI | One stream, dedupe, backoff, concurrency metrics | Throttle/correct workers; no tab-local polling fallback |
| R-08 | Partial timeline shown as complete | Misdiagnosis | Correlation coverage field and UI badge | Downgrade to partial and document missing source |
| R-09 | Local control store mirrors Hermes | Divergent source of truth | Schema/content inspection and storage quotas | Remove mirrored data and rebuild metadata-only schema |
| R-10 | iframe blocked or proxy broadens scope | Broken panel/security exposure | Header probe and fixed-route tests | Use constrained fallback or mark blocked; never generic proxy |
| R-11 | Mutation lacks audit/idempotency/CSRF | Duplicate or untraceable action | Pre-send guards and tests | Block mutation until contract is satisfied |
| R-12 | Cost estimate shown as actual | Misleading reporting | Required cost-origin enum | Correct label or show unavailable |
| R-13 | Unrelated service changed | Outage/scope breach | Before/after service, listener, hash checks | Restore task-owned backup where applicable; escalate facts |
| R-14 | Scope causes unusable sidebar/startup | Poor UX/performance | Grouped nav, command palette, staged preload, performance capture | Correct information architecture/cache; do not delete locked scope |
| R-15 | Implementer invents fake/placeholder state | False confidence | Source/provenance requirement and acceptance walkthrough | Remove fabricated data; mark unsupported/unavailable |

## 21. Required Deliverables

1. **Pi BFF/backend source** with authentication, upstream clients, cache, capability registry, event fabric, search aggregation, mutation controls, action audit, and static serving.
2. **Hermes-host read-only adapter source** with typed endpoints, source allowlists, read-only/query-only SQLite handling, schema fingerprints, limits, and health/capabilities.
3. **Frontend SPA source/build** with grouped navigation, three panes, profile awareness, all locked tabs, global search, deep links, Run Inspector, Fleet, Alerts/Pulse, and real-time updates.
4. **Pi local-control-store schema and migrations** limited to dashboard-owned metadata.
5. **Systemd/config files** for `agent-mission-control.service` and `agentos-data-adapter.service`, with secrets externalized.
6. **Existing dashboard backup archive** with hash and recovery instructions.
7. **README** covering architecture, trust boundaries, auth, profile routing, data sources, source envelope, event envelope, mutation boundary, search, preload, deployment, rollback, update, and troubleshooting.
8. **Evidence package** satisfying §18.
9. **Feature support matrix** stating `SUPPORTED`, `READ_ONLY`, `PARTIAL`, `UNAVAILABLE`, or `UNSUPPORTED` for each tab/action after deployment.

## 22. Reporting and Status Vocabulary

Use only evidence-backed states:

- `DRAFT`
- `IN_PROGRESS`
- `BLOCKED`
- `APPLIED`
- `DEPLOYED`
- `ACCEPTED`
- `ACCEPTED_WITH_LIMITATIONS`
- `FAILED`

Data/module states:

- `LIVE`
- `FRESH`
- `STALE`
- `PARTIAL`
- `UNAVAILABLE`
- `UNSUPPORTED`
- `READ_ONLY`

Failure classes:

- `CONTENT_FAILURE`
- `ENVELOPE_INVALID`
- `CAPABILITY_MISMATCH`
- `PROCEDURE_UNAVAILABLE`
- `CONTRACT_INCOMPLETE`
- `ACCEPTANCE_FAILED`
- `DELIVERY_UNSTABLE`
- `ACTIVATION_REQUIRED`
- `SOURCE_UNAVAILABLE`
- `SCHEMA_DRIFT`
- `SECURITY_CONTRACT_FAILED`
- `QUERY_BUDGET_EXCEEDED`
- `CORRELATION_PARTIAL`

Do not report a write, restart, deployment, source discovery, activation, event behavior, or acceptance result without corresponding evidence.

## 23. CEO Recipient Role Contract

After owner commit, the CEO owns the outcome end to end.

The CEO must:

- load current planning/orchestration procedures before substantive mutation;
- inspect current routing evidence;
- assign one accountable Level-2 Manager per domain stage;
- preserve discovery dependencies and parallelize independent work;
- route specialist implementation to Workers;
- integrate cross-domain contracts;
- initiate correction when acceptance fails;
- return the final evidence-backed result to the owner.

The CEO must not personally absorb backend, frontend, database, security, deployment, or verification work merely because terminal or SSH tools are available.

## 24. Manager and Worker Execution Contract

For substantive implementation, each Manager must:

1. decompose its domain into bounded Worker tasks;
2. dispatch each substantive code/research/security/deployment/verification unit through the current Kanban task mechanism (`kanban_create` or its current verified equivalent);
3. include exact inputs, outputs, constraints, acceptance checks, and evidence paths;
4. remain responsible for supervision, blocker resolution, integration, and domain synthesis;
5. wait for Worker results rather than performing the delegated specialist work in parallel itself;
6. perform only small coordination/integration actions that do not replace the assigned Worker’s bounded task;
7. return literal artifacts and evidence to the CEO.

Workers execute bounded tasks, write complete files, run requested tests, and return artifacts/evidence. They do not silently broaden architecture or change the owner outcome.

## 25. Planning and Stage Order

The CEO should use the following dependency-aware order while allowing safe parallel work:

1. **Stage 1 — Live discovery and contract capture**
   - U-01 through U-09.
   - Current source/service backups and fingerprints.
   - Route/capability, schema, query, event, auth/profile, correlation, Chat, cost, and iframe findings.

2. **Stage 2 — Architecture freeze**
   - Final route/source matrix.
   - Adapter endpoint contract.
   - BFF source/mutation/event envelopes.
   - Local control-store schema.
   - Frontend route/profile/deep-link contract.

3. **Stage 3 — Hermes-host adapter**
   - Build, test, secure, and deploy the read-only adapter.
   - Verify source samples and query budgets.

4. **Stage 4 — Pi BFF foundation**
   - Auth/session/CSRF.
   - Hermes and adapter clients.
   - Capability registry, cache, local store, request IDs, action audit.

5. **Stage 5 — Event/search/correlation services**
   - SSE fan-out and source workers.
   - Federated search.
   - Correlation engine and Run Inspector data model.
   - Alerts/Pulse derivation.

6. **Stage 6 — Frontend shell and primary operations**
   - Three panes, grouped nav, profile switcher, deep links, preload/keep-alive.
   - Overview, Chat, Sessions, Fleet, Kanban, Run Inspector, Cron, Activity, Alerts, Analytics.

7. **Stage 7 — Governance and integration tabs**
   - Issues, Permits, Room Binding, Action Audit.
   - Skills, Memory, Profiles, Models, Tools, MCP, Plugins, Webhooks, Channels, Artifacts, Files.
   - Logs, Command Center, Settings/System, llama-proxy.

8. **Stage 8 — Security and performance review**
   - Auth, CSRF, secrets, traversal, route allowlists, adapter write rejection, query budgets, event load, preload behavior, local-store boundary.

9. **Stage 9 — Deployment and independent verification**
   - Backup and deploy task-owned services.
   - Full AC matrix.
   - Unrelated-service preservation.

10. **Stage 10 — Evidence, README, correction, closure**
    - Assemble EV package.
    - Correct failed criteria.
    - Return support matrix, limitations, URL, and artifacts.

One accountable Level-2 owner is assigned per stage. Managers delegate substantive execution to Workers and integrate their returns.

## 26. Return-to-Owner Contract

Return:

- deployed dashboard URL;
- authentication/access instructions without exposing secrets;
- backup and recovery artifact paths/identifiers;
- architecture and source-contract summary;
- feature support matrix;
- AC/EV result matrix;
- exact limitations and unsupported/partial modules;
- current source/schema/event health;
- files/services created or replaced;
- services restarted;
- unrelated-service verification;
- README and evidence package identifiers;
- smallest unresolved blocker and exact owner input only when truly required.

A blocked optional module does not block the entire mission. Continue all independent work and return `ACCEPTED_WITH_LIMITATIONS` only when the owner outcome is materially usable and every limitation is explicit.

## 27. Completion Envelope

```yaml
TASK_ID: HERMES-AGENTOS-DASHBOARD-REFACTOR-2026-08-07
MODE: SPECIFICATION
DRAFT_REVISION_ID: BRAINSTORM-AGENTOS-DASHBOARD-REFACTOR-R2
SUPERSEDES_OWNER_REVIEW_DRAFT: BRAINSTORM-AGENTOS-DASHBOARD-REFACTOR-R1
OWNER_COMMIT_REQUIRED: YES
OWNER_COMMIT_STATUS: PENDING
OWNER_COMMIT_EVIDENCE: NONE
HANDOFF_AUTHORIZATION_STATUS: BLOCKED_PENDING_OWNER_COMMIT
TRANSPORT_STATUS: NOT_ATTEMPTED
OWNER_OUTCOME_STATUS: LOCKED
R2_ARCHITECTURE_STATUS: DEFINED
REMOTE_SQLITE_STATUS: FORBIDDEN
READ_ONLY_ADAPTER_STATUS: REQUIRED
BFF_SECURITY_STATUS: DEFINED
PROFILE_SCOPE_STATUS: DEFINED
CAPABILITY_PROVENANCE_STATUS: DEFINED
EVENT_FABRIC_STATUS: DEFINED
SEARCH_STATUS: DEFINED
RUN_INSPECTOR_STATUS: DEFINED
FLEET_TOPOLOGY_STATUS: DEFINED
ALERT_PULSE_STATUS: DEFINED
LOCAL_CONTROL_STORE_STATUS: DEFINED
MATERIAL_AMBIGUITY_STATUS: RESOLVED_FOR_OWNER_DECISION; U-01..U-09 REQUIRE LIVE DISCOVERY
OWNER_DECISION_STATUS: ALL LOCKED EXCEPT FINAL COMMIT
EXTERNAL_RESEARCH_STATUS: VERIFIED_AND_INTEGRATED_2026-08-07
LOCAL_CURRENT_FACT_STATUS: VERIFIED_FROM_R1_EVIDENCE_2026-08-07
DECISION_RIGHT_STATUS: ALLOCATED
SCOPE_STATUS: DEFINED
ACCEPTANCE_STATUS: DEFINED_AC-01_THROUGH_AC-34
EVIDENCE_STATUS: DEFINED_EV-01_THROUGH_EV-17
DEPENDENCY_RISK_STATUS: DEFINED_R-01_THROUGH_R-15
RECIPIENT_AFTER_COMMIT: CEO
TASK_ARTIFACT_STATUS: COMPLETE_DRAFT
DRIFT_REVIEW_STATUS: PASSED
STATIC_VALIDATION_STATUS: PASSED
DEPLOYMENT_STATUS: NOT_AUTHORIZED
ACTIVATION_STATUS: NOT_CHECKED
BEHAVIORAL_STATUS: NOT_CHECKED
UNRESOLVED_BLOCKERS: NONE_FOR_DRAFT; LIVE_DISCOVERY_DEPENDENCIES_ONLY
```

---

*Draft v2 — research-backed owner-review revision. No transport, delegation, execution, deployment, restart, or activation is authorized until the owner explicitly commits this draft.*

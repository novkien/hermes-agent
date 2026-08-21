# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Tooling và lệnh thường dùng

Repository không có `pyproject.toml`, `requirements.txt`, `package.json`, lockfile, Makefile hay CI. Runtime yêu cầu Python 3.11+ với FastAPI, Uvicorn và HTTPX; các lệnh Python dưới đây giả định một `.venv` hoạt động đúng. Không suy diễn thêm bước cài đặt hoặc build từ một package manifest không tồn tại.

`.venv` là artifact cục bộ được gitignore, không phải môi trường tái tạo được từ repository. Nếu executable/shebang hoặc native wheels không khớp máy hiện tại (workspace này từng chứa bản copy ARM/Python 3.13 từ Pi), hãy tạo một venv tương thích và cài FastAPI, Uvicorn, HTTPX cùng pytest trước khi chạy lệnh Python. Production systemd dùng venv riêng dưới `/home/pi/agent-mission-control`; đừng xem `.venv` của workspace dev là deploy artifact.

`AGENTS.md` là tài liệu bổ sung, nhưng hai nhận định “không có test suite” và “không có git history” trong đó đã lỗi thời.

### Chạy ứng dụng

```bash
source .venv/bin/activate
FRONTEND_DIR=frontend/dist \
STORE_PATH=/tmp/agent-mission-control-dev.db \
ALLOWED_ORIGIN=http://127.0.0.1:51763 \
ALLOWED_HOST=127.0.0.1:51763 \
python -m uvicorn agent_mission_control.main:app --host 127.0.0.1 --port 51763
```

Các path cấu hình được resolve từ working directory: khi chạy ở repository root, đặt `FRONTEND_DIR=frontend/dist`; dùng một `STORE_PATH` tạm để không migrate/ghi vào DB runtime ở root.

`BIND_HOST` (được ứng dụng dùng cho startup guard) phải khớp với `uvicorn --host` (socket thực sự). Khi bind ra mạng, luôn cấu hình cả hai và allowlist:

```bash
FRONTEND_DIR=frontend/dist \
ALLOWED_CIDRS=192.168.0.0/24 BIND_HOST=0.0.0.0 \
python -m uvicorn agent_mission_control.main:app --host 0.0.0.0 --port 51763
```

`GET /api/health` không phải health endpoint riêng của BFF; nó được allowlist-proxy tới dashboard upstream ở cổng 9119:

```bash
curl -sf http://127.0.0.1:51763/api/health
```

Systemd deployment dùng `deploy/agent-mission-control.service`; `deploy/agent-mission-control-runtime.conf` là drop-in đổi `ExecStart` sang project venv:

```bash
sudo systemctl restart agent-mission-control
sudo systemctl status agent-mission-control --no-pager
sudo journalctl -u agent-mission-control -f
```

### Kiểm thử

Không có test runner tổng hợp. Chạy đủ bốn contract suite:

```bash
python tests/test_runtime_contracts.py
python tests/test_static_repair_surface.py
node tests/frontend_contracts.mjs
node tests/skills_surface.mjs
```

`test_runtime_contracts.py` tự chạy cả test sync và async qua `asyncio.run(main())`; đây là entrypoint chuẩn. Bare `pytest -q` hiện không phải lệnh full-suite hợp lệ vì repository không cấu hình async pytest plugin.

Chạy riêng một runtime contract (đổi tên hàm ở cuối lệnh khi cần):

```bash
python -c "import asyncio, inspect, runpy; n=runpy.run_path('tests/test_runtime_contracts.py'); f=n['test_adapter_allowlist']; asyncio.run(f()) if inspect.iscoroutinefunction(f) else f()"
```

Kiểm tra cú pháp Python không cần import dependency:

```bash
python -m compileall -q agent_mission_control
```

Không có cấu hình lint/format/typecheck bắt buộc. Nếu `ruff` có sẵn, quy ước hiện có khuyến nghị dùng defaults:

```bash
ruff check agent_mission_control tests
ruff format --check agent_mission_control tests
```

### Frontend không có bước build

`frontend/dist/` là source-of-truth được commit và serve trực tiếp, bất chấp tên `dist`. Không có `frontend/src/`, bundler hay npm script. Sửa các ES module/CSS trong `frontend/dist/` trực tiếp rồi chạy các suite frontend ở trên. Asset mới dưới `/tabs/`, `/pure/`, `/assets/` hoặc có suffix tĩnh đã được middleware bypass; nếu thêm một root path không khớp các quy tắc đó, cập nhật static bypass constants trong `agent_mission_control/app.py`.

## Kiến trúc tổng thể

### FastAPI BFF và upstreams

`agent_mission_control/main.py` tạo app ở module import, cài log redaction và áp dụng fail-closed startup guard. `agent_mission_control/app.py:create_app()` là composition root: nó dựng SQLite store, ba upstream client, cache, capability registry, event bus, correlation/run-inspector, alert/pulse engines, source workers và `Router`, rồi lưu bundle trong `app.state.deps` để lifespan và tests dùng chung.

BFF đứng giữa SPA và ba upstream có hợp đồng auth khác nhau:

- `DashboardClient` → Hermes dashboard `:9119`, đăng nhập bằng password rồi giữ/replay cookie session; read và một tập write route được kiểm chứng đi qua đây.
- `GatewayClient` → Hermes gateway `:8642`, dùng bearer token; session/chat và chat SSE streaming đi qua đây. Cron fire dùng JWT mint từ `NAS_JWT_SECRET`.
- `AdapterClient` → adapter `:8643`, dùng bearer token; cung cấp kanban, permits, issues, timeline, fingerprints và memory files.

Các client trong `clients.py` truyền `X-Request-Id`, trả tuple `(status, body, headers)` và chuẩn hóa lỗi thành `UpstreamError`. Giữ chat stream incremental; không thay bằng một request buffer toàn bộ response.

### Ranh giới HTTP và các bất biến bảo mật

`routes.py` là ranh giới BFF chính. Route cụ thể được đăng ký trước, sau đó mới đến dashboard read catch-all `/api/{path:path}`, rồi static/SPA catch-all. Dashboard reads, adapter reads và writes đều có allowlist riêng; không biến các proxy này thành arbitrary path proxy.

Middleware ngoài cùng là IP allowlist + auto-session gate. Các mutation gọi upstream và memory-file writes phải giữ toàn bộ chuỗi kiểm tra hiện có: session → CSRF → Origin/Host → per-session rate limit → ghi audit `pending` **trước** upstream call → hoàn tất audit sau response. Nếu audit ban đầu lỗi, mutation phải dừng mà không gọi upstream. Local alert acknowledgement có handler riêng, nên đừng suy rộng chuỗi này sang mọi `POST` chỉ dựa trên HTTP verb.

Response read/proxy chuẩn là:

```json
{"data": "...", "meta": {"source_id": "...", "profile_id": "...", "freshness": "...", "read_only": true, "mutations_supported": [], "request_id": "..."}}
```

`frontend/dist/api.js` unwrap envelope đúng một lần; `pure/data-shape.js` vẫn chấp nhận legacy nested envelopes để deploy chuyển tiếp an toàn. `meta.mutations_supported` là contract hai chiều: khi thêm write surface, cập nhật đồng bộ `UPSTREAM_MUTATION_SPECS`, `READ_PATH_MUTATIONS`, UI action gating và runtime/frontend contract tests. Không bật UI action chỉ bằng cách đoán từ dữ liệu.

`profile` có hai nghĩa forwarding khác nhau: dashboard reads/writes có thể nhận profile scope, còn adapter xem profile là provenance của BFF và loại nó khỏi query trước khi forward.

### State, migrations và event fabric

`store.py` quản lý SQLite WAL control-store: tám bảng dashboard-control cộng buffer `event_replay`. Nó không được lưu body của Hermes session/message/task/permit/issue. Root `agentos-dashboard.db*` là runtime state, không phải source.

`session_persona_store.py` quản lý một file thứ hai, `store.db`, cho dữ liệu dashboard mà Hermes không có chỗ lưu — hiện chỉ có bảng `session_persona` (`session_id -> profile_name`). Nó tồn tại vì control-store ở trên đã bị đóng băng ở đúng tám bảng, không phải vì BFF được phép tự giữ state tùy ý.

**Quy tắc bắt buộc cho mọi local storage của BFF:** chỉ lưu đúng phần dữ liệu không thể lấy lại được từ upstream, và chỉ ở dạng con trỏ. Mọi thứ Hermes dashboard/gateway/adapter đã phục vụ — chi tiết profile, model/provider, nội dung SOUL.md, metadata session, message body — phải luôn fetch trực tiếp từ upstream mỗi lần cần, không được cache hay nhân bản xuống DB local. Khi thêm một store/bảng/cột mới, kiểm tra lại điều này trước: nếu upstream trả về được dữ liệu đó, nó không thuộc về đây.

Runtime migrations được nhúng trong `store.py` (`_SCHEMA_VERSION`, `_MIGRATIONS`) và có SQL tương ứng trong `agent_mission_control/migrations/`; `session_persona_store.py` theo cùng convention với file SQL riêng (`store_db_001_*.sql`). Khi đổi schema, tăng version và giữ hai biểu diễn đồng bộ; migration phải hội tụ cho cả DB mới và DB đã tồn tại.

`workers.py` chạy các polling loop cho source data. Source delta được publish vào `EventBus` và phát qua `/api/events/stream`; capabilities fingerprint changes còn invalidate backend cache cho source adapter. `event_bus.py` giữ ring buffer trong memory cùng bounded SQLite replay. Lifespan trong `app.py` chịu trách nhiệm start/stop workers, registry probe, alert tick và đóng clients/store.

`chat_proxy.py` giữ contract session-create và relay SSE frame qua gateway; `search.py` thực hiện federated, bounded fan-out qua adapter với timeout theo nguồn. Giữ các handler trong `routes.py` như lớp auth/CSRF/audit/envelope quanh hai module này.

### Zero-build SPA

`frontend/dist/index.html` import `app.js` bằng native ES modules. `app.js` tạo shell, profile-scoped tab cache, routing, preload, inspector và một SSE client; các tab được nạp bằng dynamic `import()` từ `frontend/dist/tabs/`. `frontend/dist/pure/` chứa data-shape, router, state và các transformer thuần được Node contract tests import trực tiếp.

URL canonical là:

```text
/?profile=<id>#/<route>?<entity-or-filter-params>
```

`profile` chỉ nằm trong document query, không nằm trong hash. Dùng `buildHash`, `parseRouteWithProfile` và helpers trong `profile.js`; đừng tự nối `profile` vào hash.

Route/tab registration bị phân tán có chủ ý giữa `pure/route-registry.js`, `pure/s7-route-spec.js`, `pure/route-inventory.js`, `pure/s7-registration.js` và loader maps trong `app.js`. Khi thêm, xóa hoặc đổi tên tab, cập nhật các registry/spec/inventory/loader tương ứng cùng một lượt; `registerS7Routes()` sẽ throw khi registry lệch.

Tab factory trả lifecycle object dạng `{mount, activate, deactivate, refresh?, renderInspector?, data}`. Instance key là `profile::route`, nên không reuse state giữa profile. `pure/state-store.js` giữ state memory-only; không chuyển dữ liệu tab sang Web Storage.

## Vai trò của từng test suite

- `test_runtime_contracts.py`: backend allowlists, envelope/profile semantics, error status, bounded adapter params và mutation/write capability contract bằng fake upstreams.
- `test_static_repair_surface.py`: khóa các marker và deployable asset của những lỗi frontend đã sửa; đổi tên/di chuyển marker cần cập nhật test có chủ ý.
- `frontend_contracts.mjs`: kiểm tra pure data-shape, health summary và hash/profile routing.
- `skills_surface.mjs`: kiểm tra Skills normalization/action gating/frontmatter và round-trip code-highlighting tokenization.

"""agent-mission-control — Pi BFF (S4 foundation + S5 capabilities, unified).

FastAPI backend for the AgentOS dashboard refactor. Stage 4 foundation
(auth/CSRF, clients, registry, cache, control store, audit) + Stage 5
event fabric (SSE), source workers, chat proxy, search, correlation,
run-inspector, alerts/pulse — merged into ONE canonical package by S8.

Pure stdlib + fastapi/uvicorn/httpx only. No itsdangerous, no pyarmor, no
platform-specific dependencies — the target runtime is Raspberry Pi arm64
Python 3.11+.
"""

__version__ = "0.1.0"

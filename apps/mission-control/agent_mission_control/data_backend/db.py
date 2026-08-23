"""Read-only SQLite connection handling with query-time guards.

Every source connection is opened with ``file:<path>?mode=ro&immutable=0`` and
``PRAGMA query_only=ON``. The state.db connection additionally enforces a
per-query deadline via ``set_progress_handler``; the other sources use a soft
deadline of the source budget plus a generous scheduler margin.

Forbidden statements (ANALYZE/VACUUM/checkpoint/journal/delete/insert/update/
drop/alter/pragma write) are rejected before execution.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Sequence

from .config import SourceSpec

_WRITE_PREFIXES = (
    "insert",
    "update",
    "delete",
    "replace",
    "drop",
    "alter",
    "create",
    "vacuum",
    "reindex",
    "attach",
    "detach",
    "pragma wal_checkpoint",
    "pragma journal_mode",
    "pragma locking_mode",
    "pragma synchronous",
    "analyze",
    "begin",
    "commit",
    "rollback",
    "savepoint",
    "release",
    "grant",
    "revoke",
)


def assert_read_only(sql: str) -> None:
    stripped = sql.lstrip().lower()
    for prefix in _WRITE_PREFIXES:
        if stripped.startswith(prefix):
            raise sqlite3.OperationalError(
                f"write statement rejected by adapter guard: {prefix!r}"
            )
    if stripped.startswith("with"):
        import re

        match = re.search(r"\b(insert|update|delete|replace)\b", stripped)
        if match:
            raise sqlite3.OperationalError(
                f"write statement rejected by adapter guard: {match.group(1)!r}"
            )


_ADAPTER_STATE: dict[int, dict] = {}
"""Per-connection adapter state keyed by id(con) (Python 3.13 sqlite3
connections have no __dict__, so attributes cannot be set on them)."""


def open_read_only_connection(path: str, timeout_ms: int) -> sqlite3.Connection:
    """Open a SQLite connection in read-only URI mode.

    immutable=0 keeps the WAL as part of the live database (a WAL database
    must never be opened immutable, or reads can miss WAL content).
    """
    uri = f"file:{path}?mode=ro&immutable=0"
    con = sqlite3.connect(uri, uri=True, check_same_thread=False)
    con.execute("PRAGMA query_only=ON")
    con.row_factory = sqlite3.Row

    # Progress-handler deadline: sqlite3 calls this every ~1000 VM instructions.
    # If the deadline passes, the query is aborted with sqlite3.OperationalError.
    con_id = id(con)
    _ADAPTER_STATE[con_id] = {
        "deadline": time.monotonic() + max(timeout_ms, 1) / 1000.0,
        "armed": True,
    }

    def _progress() -> int:
        st = _ADAPTER_STATE.get(con_id)
        if st is not None and st.get("armed"):
            if time.monotonic() > st.get("deadline", 0):
                return 1
        return 0

    con.set_progress_handler(_progress, 1000)
    return con


def close_read_only_connection(con: sqlite3.Connection) -> None:
    """Close a connection and release its progress-deadline bookkeeping."""
    _ADAPTER_STATE.pop(id(con), None)
    con.close()


def _deadline_setter(con: sqlite3.Connection):
    def set_deadline(ms: int) -> None:
        st = _ADAPTER_STATE.get(id(con))
        if st is not None:
            st["deadline"] = time.monotonic() + ms / 1000.0

    return set_deadline


def execute_bounded(
    con: sqlite3.Connection,
    sql: str,
    params: Sequence = (),
    timeout_ms: int | None = None,
) -> list[sqlite3.Row]:
    """Run a read-only, deadline-guarded query and return all rows.

    The deadline is the query budget; for state.db this is the documented
    budget (5s default, 10s search). Rows are capped by the caller with LIMIT
    clauses; no limit is imposed here to keep the SQL exact.
    """
    assert_read_only(sql)
    if timeout_ms is not None:
        _deadline_setter(con)(timeout_ms)
    cursor = con.execute(sql, params)
    return cursor.fetchall()


def compute_schema_fingerprint(con: sqlite3.Connection) -> str:
    """sha256 over ``'\\n'.join(sqlite_master.sql ORDER BY name)``.

    This is the ONLY reproducible serialization per verification report U-10.
    """
    import hashlib

    rows = [
        r[0] for r in con.execute("SELECT sql FROM sqlite_master ORDER BY name") if r[0]
    ]
    ddl = "\n".join(rows)
    return hashlib.sha256(ddl.encode("utf-8")).hexdigest()


class SourceStore:
    """Owns one read-only connection per source, lazily opened."""

    def __init__(self, spec: SourceSpec) -> None:
        self.spec = spec
        # sqlite3.Connection is not safe for concurrent use even with
        # check_same_thread=False. FastAPI runs sync routes in worker threads,
        # so serialize every operation on this store's shared connection.
        self._lock = threading.RLock()
        self._con: sqlite3.Connection | None = None
        self._fingerprint: str | None = None
        self._fingerprint_computed = False

    def _ensure(self) -> sqlite3.Connection:
        with self._lock:
            if self._con is None:
                self._con = open_read_only_connection(
                    self.spec.path, self.spec.query_budget_ms
                )
            return self._con

    def connection(self) -> sqlite3.Connection:
        return self._ensure()

    def fingerprint(self, recompute: bool = False) -> str:
        with self._lock:
            if recompute or not self._fingerprint_computed:
                self._fingerprint = compute_schema_fingerprint(self._ensure())
                self._fingerprint_computed = True
            return self._fingerprint or ""

    def close(self) -> None:
        with self._lock:
            if self._con is not None:
                try:
                    close_read_only_connection(self._con)
                finally:
                    self._con = None
            self._fingerprint = None
            self._fingerprint_computed = False

    def query(
        self,
        sql: str,
        params: Sequence = (),
        timeout_ms: int | None = None,
    ) -> list[sqlite3.Row]:
        """Every call opens its own short-lived connection.

        A `mode=ro` WAL reader held open for the process lifetime (the prior
        behaviour: one cached `self._con` reused forever) can pin a stale
        snapshot — a writer process (the issue/permit CLI scripts, which run
        as separate processes) commits are then invisible to this store until
        the connection is closed and reopened, no matter how long the adapter
        keeps running or how many times the underlying file changes. Reopening
        per query costs a few hundred microseconds on a local SQLite file,
        which is negligible next to `query_budget_ms`, and it guarantees every
        read reflects what was actually committed.
        """
        with self._lock:
            con = open_read_only_connection(self.spec.path, self.spec.query_budget_ms)
            try:
                return execute_bounded(
                    con, sql, params, timeout_ms or self.spec.query_budget_ms
                )
            finally:
                close_read_only_connection(con)

    def reachable(self) -> bool:
        try:
            with self._lock:
                con = self._ensure()
                con.execute("SELECT 1")
                return True
        except Exception:
            return False

    def tables(self) -> list[str]:
        rows = self.query(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
        return [r[0] for r in rows]

    def row_count(self, table: str) -> int:
        return int(self.query(f"SELECT COUNT(*) FROM {table}")[0][0])


def discover_board_paths(boards_dir: str) -> dict[str, str]:
    """Return {board_name: kanban.db path} for the multi-board kanban source.

    A board is any direct child directory of ``boards_dir`` that contains a
    ``kanban.db``. Underscore-prefixed directories (``_archived``) are
    skipped: they hold nested per-attempt databases, not live boards.
    """
    root = Path(boards_dir).expanduser()
    if not root.is_dir():
        return {}
    out: dict[str, str] = {}
    for child in sorted(root.iterdir()):
        if not child.is_dir():
            continue
        if child.name.startswith("_"):
            continue
        db_path = child / "kanban.db"
        if db_path.is_file():
            out[child.name] = str(db_path)
    return out


class KanbanBoardRegistry:
    """Read-only SourceStore-per-board registry for the multi-board source.

    ``default_board`` (settings.default_kanban_board, ``task``) preserves the
    legacy single-board contract. Unknown board names resolve to a 404-shaped
    ``KeyError``; ``all`` is reserved by the summary endpoint.
    """

    def __init__(
        self,
        boards_dir: str,
        default_board: str,
        spec_builder,
    ) -> None:
        self.boards_dir = str(Path(boards_dir).expanduser())
        self.default_board = default_board
        self._spec_builder = spec_builder
        self._paths: dict[str, str] = {}
        self._stores: dict[str, SourceStore] = {}
        self._lock = threading.Lock()

    def _refresh_paths(self) -> dict[str, str]:
        with self._lock:
            if not self._paths:
                self._paths = discover_board_paths(self.boards_dir)
            return dict(self._paths)

    def board_names(self) -> list[str]:
        return sorted(self._refresh_paths())

    def _store(self, board: str) -> SourceStore:
        with self._lock:
            store = self._stores.get(board)
            if store is None:
                path = self._paths.get(board)
                if path is None:
                    self._paths = discover_board_paths(self.boards_dir)
                    path = self._paths.get(board)
                    if path is None:
                        raise KeyError(board)
                store = SourceStore(self._spec_builder(board, path))
                self._stores[board] = store
            return store

    def default_store(self) -> SourceStore:
        return self._store(self.default_board)

    def resolve(self, board: str) -> SourceStore:
        """Resolve a board param: None/'' -> default board; else exact board."""
        if not board or board == "all":
            raise ValueError("board 'all' is only valid for the summary endpoint")
        if board == self.default_board:
            return self.default_store()
        if board not in self.board_names():
            raise KeyError(board)
        return self._store(board)

    def fingerprint(self, board: str) -> str:
        return self._store(board).fingerprint()

    def close(self) -> None:
        with self._lock:
            stores = list(self._stores.values())
            self._stores.clear()
        for store in stores:
            store.close()


def discover_profile_state_paths(profiles_dir: str) -> dict[str, str]:
    """Return {profile_name: state.db path} for every Hermes worker profile.

    A profile is any direct child directory of ``profiles_dir`` that contains
    a ``state.db``. A Kanban worker's real conversation is stored here, in
    ``~/.hermes/profiles/<profile>/state.db`` — never in the default
    ``~/.hermes/state.db`` that every other read in this adapter uses. See
    the ``worker-session`` resolver in ``queries.py`` for why this database
    set has to be searched at all.
    """
    root = Path(profiles_dir).expanduser()
    if not root.is_dir():
        return {}
    out: dict[str, str] = {}
    for child in sorted(root.iterdir()):
        if not child.is_dir():
            continue
        db_path = child / "state.db"
        if db_path.is_file():
            out[child.name] = str(db_path)
    return out


class StateProfileRegistry:
    """Read-only SourceStore-per-profile registry, mirroring KanbanBoardRegistry.

    Exists solely to back the worker-session resolver: it never serves any
    other read, and every store it opens is subject to the same allowlisted,
    read-only, per-query-connection contract as every other SourceStore.
    """

    def __init__(self, profiles_dir: str, spec_builder) -> None:
        self.profiles_dir = str(Path(profiles_dir).expanduser())
        self._spec_builder = spec_builder
        self._paths: dict[str, str] = {}
        self._stores: dict[str, SourceStore] = {}
        self._lock = threading.Lock()

    def _refresh_paths(self) -> dict[str, str]:
        with self._lock:
            if not self._paths:
                self._paths = discover_profile_state_paths(self.profiles_dir)
            return dict(self._paths)

    def profile_names(self) -> list[str]:
        return sorted(self._refresh_paths())

    def stores(self) -> list[tuple[str, SourceStore]]:
        names = self.profile_names()
        out: list[tuple[str, SourceStore]] = []
        with self._lock:
            for name in names:
                store = self._stores.get(name)
                if store is None:
                    store = SourceStore(self._spec_builder(name, self._paths[name]))
                    self._stores[name] = store
                out.append((name, store))
            return out

    def close(self) -> None:
        with self._lock:
            stores = list(self._stores.values())
            self._stores.clear()
        for store in stores:
            store.close()


@contextmanager
def read_only_source(path: str, timeout_ms: int) -> Iterator[sqlite3.Connection]:
    """Context-managed read-only connection (used by probes/tests)."""
    con = open_read_only_connection(path, timeout_ms)
    try:
        yield con
    finally:
        close_read_only_connection(con)


def path_exists(path: str) -> bool:
    return Path(path).exists()

"""Provider-bound request persistence for :class:`hermes_state.SessionDB`.

This stays in a mixin so request observability does not inline or duplicate the
schema, search, and portability modules inside ``hermes_state.py``.
"""

from __future__ import annotations

import copy
import hashlib
import json
import sqlite3
import time
from collections import OrderedDict
from typing import Any, Callable, Dict, List, Optional, Tuple


_PROVIDER_PAYLOAD_FULL = "full"
_PROVIDER_PAYLOAD_DELTA_V1 = "delta-v1"


def _provider_payload_json(value: Any) -> str:
    """Serialize provider-capture data in a stable compact representation."""
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    )


def _provider_payload_delta(previous: Any, current: Any) -> Dict[str, Any]:
    """Return a compact, exactly reversible structural delta."""
    operations: List[List[Any]] = []

    def _diff(old: Any, new: Any, path: List[Any]) -> None:
        if type(old) is not type(new):
            operations.append(["set", path, new])
            return
        if isinstance(old, dict):
            old_common = [key for key in old if key in new]
            new_common = [key for key in new if key in old]
            added = [key for key in new if key not in old]
            # JSON preserves insertion order. Replacing the object is the only
            # exact representation if retained keys were reordered or a new key
            # was inserted before an existing one.
            if old_common != new_common or list(new) != new_common + added:
                operations.append(["set", path, new])
                return
            for key in old:
                if key not in new:
                    operations.append(["remove", [*path, key]])
            for key in new:
                if key not in old:
                    operations.append(["set", [*path, key], new[key]])
                else:
                    _diff(old[key], new[key], [*path, key])
            return
        if isinstance(old, list):
            prefix = 0
            common = min(len(old), len(new))
            while prefix < common and old[prefix] == new[prefix]:
                prefix += 1
            suffix = 0
            while (
                suffix < len(old) - prefix
                and suffix < len(new) - prefix
                and old[len(old) - 1 - suffix] == new[len(new) - 1 - suffix]
            ):
                suffix += 1
            delete_count = len(old) - prefix - suffix
            insert_end = len(new) - suffix if suffix else len(new)
            inserted = new[prefix:insert_end]
            if delete_count or inserted:
                operations.append(
                    ["splice", path, prefix, delete_count, inserted]
                )
            return
        if old != new:
            operations.append(["set", path, new])

    _diff(previous, current, [])
    return {"version": 1, "operations": operations}


def _apply_provider_payload_delta(previous: Any, delta: Dict[str, Any]) -> Any:
    """Apply a ``delta-v1`` record without mutating the prior request."""
    if delta.get("version") != 1 or not isinstance(delta.get("operations"), list):
        raise ValueError("invalid provider payload delta")
    result = copy.deepcopy(previous)

    def _parent(document: Any, path: List[Any]) -> Tuple[Any, Any]:
        if not path:
            return None, None
        target = document
        for component in path[:-1]:
            target = target[component]
        return target, path[-1]

    for operation in delta["operations"]:
        if not isinstance(operation, list) or len(operation) < 2:
            raise ValueError("invalid provider payload delta operation")
        kind, path = operation[0], operation[1]
        if not isinstance(path, list):
            raise ValueError("invalid provider payload delta path")
        if kind == "set" and len(operation) == 3:
            value = copy.deepcopy(operation[2])
            if not path:
                result = value
            else:
                parent, key = _parent(result, path)
                parent[key] = value
        elif kind == "remove" and len(operation) == 2 and path:
            parent, key = _parent(result, path)
            del parent[key]
        elif kind == "splice" and len(operation) == 5:
            target = result
            for component in path:
                target = target[component]
            if not isinstance(target, list):
                raise ValueError("provider payload splice target is not a list")
            start, delete_count, inserted = operation[2:]
            if (
                not isinstance(start, int)
                or not isinstance(delete_count, int)
                or not isinstance(inserted, list)
            ):
                raise ValueError("invalid provider payload splice")
            target[start : start + delete_count] = copy.deepcopy(inserted)
        else:
            raise ValueError(f"unknown provider payload delta operation: {kind!r}")
    return result


class ProviderRequestMixin:
    """Persist and reconstruct exact payloads dispatched to model providers."""

    def _provider_request_cache(
        self,
    ) -> OrderedDict[str, Tuple[int, Dict[str, Any]]]:
        cache = getattr(self, "_provider_payload_cache", None)
        if cache is None:
            cache = OrderedDict()
            self._provider_payload_cache = cache
        return cache

    def append_llm_provider_request(
        self,
        session_id: str,
        *,
        payload: Dict[str, Any],
        transport: str,
        session_key: Optional[str] = None,
        api_request_id: Optional[str] = None,
        turn_id: Optional[str] = None,
        api_call_count: Optional[int] = None,
        attempt: int = 1,
        provider: Optional[str] = None,
        model: Optional[str] = None,
        api_mode: Optional[str] = None,
        captured_at: Optional[float] = None,
    ) -> int:
        """Persist a provider-visible request, delta-encoding repeated context."""
        full_payload_json = _provider_payload_json(payload)
        full_payload_bytes = full_payload_json.encode("utf-8")
        payload_sha256 = hashlib.sha256(full_payload_bytes).hexdigest()
        request_timestamp = (
            float(captured_at) if captured_at is not None else time.time()
        )

        def _do(conn):
            latest = conn.execute(
                """SELECT id, payload_json, payload_encoding, base_request_id
                   FROM llm_provider_requests
                   WHERE session_id = ? ORDER BY id DESC LIMIT 1""",
                (session_id,),
            ).fetchone()
            stored_payload_json = full_payload_json
            payload_encoding = _PROVIDER_PAYLOAD_FULL
            base_request_id = None
            if latest is not None:
                latest_id = int(latest["id"])
                cache = self._provider_request_cache()
                cached = cache.get(session_id)
                previous_payload = (
                    cached[1]
                    if cached is not None and cached[0] == latest_id
                    else self._load_provider_payload_at(conn, session_id, latest_id)
                )
                delta_json = _provider_payload_json(
                    _provider_payload_delta(previous_payload, payload)
                )
                if len(delta_json.encode("utf-8")) < len(full_payload_bytes):
                    stored_payload_json = delta_json
                    payload_encoding = _PROVIDER_PAYLOAD_DELTA_V1
                    base_request_id = latest_id

            cursor = conn.execute(
                """INSERT INTO llm_provider_requests (
                       session_id, session_key, api_request_id, turn_id,
                       api_call_count, attempt, captured_at, provider, model,
                       api_mode, transport, payload_json, payload_sha256,
                       payload_encoding, base_request_id
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    session_id,
                    session_key,
                    api_request_id,
                    turn_id,
                    api_call_count,
                    attempt,
                    request_timestamp,
                    provider,
                    model,
                    api_mode,
                    transport,
                    stored_payload_json,
                    payload_sha256,
                    payload_encoding,
                    base_request_id,
                ),
            )
            return int(cursor.lastrowid)

        row_id = self._execute_write(_do)
        with self._lock:
            cache = self._provider_request_cache()
            cache[session_id] = (row_id, copy.deepcopy(payload))
            cache.move_to_end(session_id)
            while len(cache) > 32:
                cache.popitem(last=False)
        return row_id

    @staticmethod
    def _decode_provider_payload(
        raw: str,
        encoding: Optional[str],
        previous_payload: Optional[Dict[str, Any]],
    ) -> Dict[str, Any]:
        encoded = json.loads(raw)
        if not encoding or encoding == _PROVIDER_PAYLOAD_FULL:
            if not isinstance(encoded, dict):
                raise ValueError("full provider payload is not an object")
            return encoded
        if encoding == _PROVIDER_PAYLOAD_DELTA_V1:
            if previous_payload is None or not isinstance(encoded, dict):
                raise ValueError("provider payload delta has no usable base")
            payload = _apply_provider_payload_delta(previous_payload, encoded)
            if not isinstance(payload, dict):
                raise ValueError("reconstructed provider payload is not an object")
            return payload
        raise ValueError(f"unsupported provider payload encoding: {encoding!r}")

    def _load_provider_payload_at(
        self,
        conn: sqlite3.Connection,
        session_id: str,
        request_id: int,
    ) -> Dict[str, Any]:
        """Reconstruct one request by walking backward to its nearest full row."""
        chain = []
        cursor = conn.execute(
            """SELECT id, payload_json, payload_encoding, base_request_id
               FROM llm_provider_requests
               WHERE session_id = ? AND id <= ?
               ORDER BY id DESC""",
            (session_id, request_id),
        )
        for row in cursor:
            chain.append(row)
            encoding = row["payload_encoding"] or _PROVIDER_PAYLOAD_FULL
            if encoding == _PROVIDER_PAYLOAD_FULL:
                break
        if not chain or (
            chain[-1]["payload_encoding"] or _PROVIDER_PAYLOAD_FULL
        ) != _PROVIDER_PAYLOAD_FULL:
            raise ValueError("provider payload delta chain has no full base")

        payload: Optional[Dict[str, Any]] = None
        previous_id: Optional[int] = None
        for row in reversed(chain):
            encoding = row["payload_encoding"] or _PROVIDER_PAYLOAD_FULL
            if (
                encoding == _PROVIDER_PAYLOAD_DELTA_V1
                and row["base_request_id"] != previous_id
            ):
                raise ValueError("provider payload delta chain is discontinuous")
            payload = self._decode_provider_payload(
                row["payload_json"], encoding, payload
            )
            previous_id = int(row["id"])
        if payload is None:
            raise ValueError("provider payload reconstruction produced no body")
        return payload

    def get_llm_provider_requests(self, session_id: str) -> List[Dict[str, Any]]:
        """Return provider-bound request snapshots in dispatch order."""
        with self._lock:
            rows = self._conn.execute(
                """SELECT * FROM llm_provider_requests
                   WHERE session_id = ? ORDER BY id ASC""",
                (session_id,),
            ).fetchall()
        result: List[Dict[str, Any]] = []
        payloads_by_id: Dict[int, Dict[str, Any]] = {}
        for row in rows:
            record = dict(row)
            raw = record.get("payload_json")
            if isinstance(raw, str):
                try:
                    encoding = (
                        record.get("payload_encoding") or _PROVIDER_PAYLOAD_FULL
                    )
                    base_id = record.get("base_request_id")
                    previous = (
                        payloads_by_id.get(int(base_id))
                        if base_id is not None
                        else None
                    )
                    payload = self._decode_provider_payload(raw, encoding, previous)
                    payloads_by_id[int(record["id"])] = payload
                    full_raw = (
                        raw
                        if encoding == _PROVIDER_PAYLOAD_FULL
                        else _provider_payload_json(payload)
                    )
                    record["payload_storage_json_raw"] = raw
                    record["payload_json_raw"] = full_raw
                    record["payload"] = payload
                    actual_sha = hashlib.sha256(full_raw.encode("utf-8")).hexdigest()
                    record["payload_integrity"] = (
                        "verified"
                        if actual_sha == record.get("payload_sha256")
                        else "hash_mismatch"
                    )
                except (json.JSONDecodeError, TypeError, ValueError, KeyError) as exc:
                    record["payload_parse_status"] = (
                        f"INVALID_{type(exc).__name__}: {exc}"
                    )
            result.append(record)
        return result

    def compact_llm_provider_requests(
        self,
        *,
        vacuum: bool = False,
        progress_cb: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> Dict[str, Any]:
        """Rewrite historical full snapshots into exact delta chains."""
        if self.read_only:
            raise RuntimeError("cannot compact provider requests in read-only mode")
        with self._lock:
            session_ids = [
                row[0]
                for row in self._conn.execute(
                    """SELECT DISTINCT session_id FROM llm_provider_requests
                       ORDER BY session_id"""
                ).fetchall()
            ]

        totals = {
            "sessions": len(session_ids),
            "requests": 0,
            "original_bytes": 0,
            "stored_bytes": 0,
            "delta_requests": 0,
            "full_requests": 0,
            "vacuumed": False,
        }

        for session_index, current_session_id in enumerate(session_ids, start=1):

            def _compact_session(conn):
                stats = {
                    "requests": 0,
                    "original_bytes": 0,
                    "stored_bytes": 0,
                    "delta_requests": 0,
                    "full_requests": 0,
                }
                previous_payload: Optional[Dict[str, Any]] = None
                previous_id: Optional[int] = None
                last_id = 0
                while True:
                    rows = conn.execute(
                        """SELECT id, payload_json, payload_encoding,
                                  base_request_id, payload_sha256
                           FROM llm_provider_requests
                           WHERE session_id = ? AND id > ?
                           ORDER BY id ASC LIMIT 50""",
                        (current_session_id, last_id),
                    ).fetchall()
                    if not rows:
                        break
                    for row in rows:
                        row_id = int(row["id"])
                        raw = row["payload_json"]
                        encoding = row["payload_encoding"] or _PROVIDER_PAYLOAD_FULL
                        base_id = row["base_request_id"]
                        if (
                            encoding == _PROVIDER_PAYLOAD_DELTA_V1
                            and base_id != previous_id
                        ):
                            raise ValueError(
                                "provider payload delta chain is discontinuous "
                                f"at row {row_id}"
                            )
                        payload = self._decode_provider_payload(
                            raw, encoding, previous_payload
                        )
                        full_raw = (
                            raw
                            if encoding == _PROVIDER_PAYLOAD_FULL
                            else _provider_payload_json(payload)
                        )
                        if hashlib.sha256(full_raw.encode("utf-8")).hexdigest() != row[
                            "payload_sha256"
                        ]:
                            raise ValueError(
                                f"provider payload hash mismatch at row {row_id}; "
                                "refusing to compact"
                            )
                        stored_raw = full_raw
                        stored_encoding = _PROVIDER_PAYLOAD_FULL
                        stored_base_id = None
                        if previous_payload is not None:
                            delta_raw = _provider_payload_json(
                                _provider_payload_delta(previous_payload, payload)
                            )
                            if len(delta_raw.encode("utf-8")) < len(
                                full_raw.encode("utf-8")
                            ):
                                stored_raw = delta_raw
                                stored_encoding = _PROVIDER_PAYLOAD_DELTA_V1
                                stored_base_id = previous_id

                        conn.execute(
                            """UPDATE llm_provider_requests
                               SET payload_json = ?, payload_encoding = ?,
                                   base_request_id = ? WHERE id = ?""",
                            (
                                stored_raw,
                                stored_encoding,
                                stored_base_id,
                                row_id,
                            ),
                        )
                        stats["requests"] += 1
                        stats["original_bytes"] += len(raw.encode("utf-8"))
                        stats["stored_bytes"] += len(stored_raw.encode("utf-8"))
                        stats[
                            "delta_requests"
                            if stored_encoding == _PROVIDER_PAYLOAD_DELTA_V1
                            else "full_requests"
                        ] += 1
                        previous_payload = payload
                        previous_id = row_id
                        last_id = row_id
                return stats

            session_stats = self._execute_write(_compact_session)
            for key in (
                "requests",
                "original_bytes",
                "stored_bytes",
                "delta_requests",
                "full_requests",
            ):
                totals[key] += session_stats[key]
            if progress_cb is not None:
                progress_cb(
                    {
                        **totals,
                        "session_index": session_index,
                        "session_id": current_session_id,
                    }
                )

        with self._lock:
            self._provider_request_cache().clear()
            if vacuum:
                self._conn.execute("VACUUM")
                totals["vacuumed"] = True
        return totals

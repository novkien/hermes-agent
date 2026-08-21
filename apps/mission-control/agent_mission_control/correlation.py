"""Correlation engine (Stage 5 item 5) — U-06 identifier map EXACTLY.

correlate(entity_type, entity_id) -> {nodes[], edges[], coverage:
complete|partial|unsupported}.

Supported pairs (native columns): task<->session (tasks.session_id),
task<->run, session<->thread (sessions.thread_id/chat_id),
tool_call<->message (messages.tool_call_id), issue<->task/session
(issue_occurrences.task_ref/session_ref), artifact<->task
(task_attachments.task_id), delegation<->session, api_request<->session/turn.

Partial pairs: run<->session (task_runs.metadata JSON worker_session_id),
permit<->issue (issue_title "Issue N" parse), tool_call<->task,
agent/profile<->task (assignee name).

Missing pairs -> label `unsupported`: permit<->task, permit<->session,
delegation<->task, issue<->tool_call.

NEVER infer a relation from timestamps alone; inferred edges labeled `inferred`.
"""
from __future__ import annotations

import json
import re
from typing import Any, Optional

# Entity kinds
TASK = "task"
RUN = "run"
SESSION = "session"
THREAD = "thread"
MESSAGE = "message"
TOOL_CALL = "tool_call"
ISSUE = "issue"
ARTIFACT = "artifact"
DELEGATION = "delegation"
API_REQUEST = "api_request"
PERMIT = "permit"
AGENT = "agent"
PROFILE = "profile"

UNSUPPORTED_PAIRS = {
    (PERMIT, TASK),
    (PERMIT, SESSION),
    (DELEGATION, TASK),
    (ISSUE, TOOL_CALL),
}

PAIR_NAMES = {
    (TASK, SESSION): "task<->session",
    (TASK, RUN): "task<->run",
    (SESSION, THREAD): "session<->thread",
    (TOOL_CALL, MESSAGE): "tool_call<->message",
    (ISSUE, TASK): "issue<->task",
    (ISSUE, SESSION): "issue<->session",
    (ARTIFACT, TASK): "artifact<->task",
    (DELEGATION, SESSION): "delegation<->session",
    (API_REQUEST, SESSION): "api_request<->session",
    (API_REQUEST, "turn"): "api_request<->turn",
    (RUN, SESSION): "run<->session",
    (PERMIT, ISSUE): "permit<->issue",
    (TOOL_CALL, TASK): "tool_call<->task",
    (AGENT, TASK): "agent<->task",
    (PROFILE, SESSION): "profile<->session",
}


class CorrelationEngine:
    """Consumes bounded row data (from adapter/API responses) and builds a graph.

    Data providers are injected as callables so tests can supply fixtures
    without live upstream access. All lookups are bounded (PK/limited lists).
    """

    def __init__(self, providers: Optional[dict] = None) -> None:
        # providers: {entity_kind: callable(entity_id) -> dict|None}
        self.providers = providers or {}

    async def _get(self, kind: str, entity_id: Any) -> Optional[dict]:
        """Providers are async so they can be live upstream calls. A missing
        provider degrades to None rather than failing the whole graph."""
        fn = self.providers.get(kind)
        if fn is None:
            return None
        try:
            return await fn(entity_id)
        except Exception:
            return None

    # ---- edge builders -----------------------------------------------------
    async def _task_edges(self, task: dict, nodes: dict, edges: list) -> None:
        tid = task.get("id")
        add_node(nodes, TASK, tid, task)
        session_id = task.get("session_id")
        if session_id:
            add_edge(edges, TASK, tid, SESSION, session_id, "tasks.session_id", "native")
            add_node(nodes, SESSION, session_id, {})
        run_id = task.get("current_run_id")
        if run_id:
            add_edge(edges, TASK, tid, RUN, str(run_id), "tasks.current_run_id", "native")
            add_node(nodes, RUN, str(run_id), {})
        assignee = task.get("assignee")
        if assignee:
            add_edge(edges, AGENT, assignee, TASK, tid, "tasks.assignee", "partial")
            add_node(nodes, AGENT, assignee, {"name": assignee})
        # runs list from provider
        runs = await self._get("task_runs", tid) or []
        for r in runs:
            rid = str(r.get("id"))
            add_edge(edges, TASK, tid, RUN, rid, "task_runs.task_id", "native")
            add_node(nodes, RUN, rid, {"status": r.get("status"), "outcome": r.get("outcome")})
            meta = r.get("metadata") or {}
            if isinstance(meta, str):
                try:
                    meta = json.loads(meta)
                except Exception:
                    meta = {}
            wsid = meta.get("worker_session_id")
            if wsid:
                add_edge(edges, RUN, rid, SESSION, wsid, "task_runs.metadata.worker_session_id", "inferred")
                add_node(nodes, SESSION, wsid, {})
        # attachments
        atts = await self._get("task_attachments", tid) or []
        for a in atts:
            aid = str(a.get("id"))
            add_edge(edges, ARTIFACT, aid, TASK, tid, "task_attachments.task_id", "native")
            add_node(nodes, ARTIFACT, aid, {"filename": a.get("filename", "")})
        # issue occurrences referencing this task
        occs = await self._get("issue_occurrences_by_task", tid) or []
        for o in occs:
            iid = str(o.get("issue_id"))
            add_edge(edges, ISSUE, iid, TASK, tid, "issue_occurrences.task_ref", "native")
            add_node(nodes, ISSUE, iid, {"severity": o.get("severity", ""), "status": o.get("status", "")})
            if o.get("session_ref"):
                add_edge(edges, ISSUE, iid, SESSION, o["session_ref"], "issue_occurrences.session_ref", "native")
                add_node(nodes, SESSION, o["session_ref"], {})

    async def _session_edges(self, session: dict, nodes: dict, edges: list) -> None:
        sid = session.get("id")
        add_node(nodes, SESSION, sid, session)
        thread_id = session.get("thread_id")
        if thread_id:
            add_edge(edges, SESSION, sid, THREAD, str(thread_id), "sessions.thread_id", "native")
            add_node(nodes, THREAD, str(thread_id), {})
        parent = session.get("parent_session_id")
        if parent:
            add_edge(edges, SESSION, sid, SESSION, parent, "sessions.parent_session_id", "native")
            add_node(nodes, SESSION, parent, {})
        profile = session.get("profile_name")
        if profile:
            add_edge(edges, PROFILE, profile, SESSION, sid, "sessions.profile_name", "partial")
            add_node(nodes, PROFILE, profile, {"name": profile})
        # delegations
        deps = await self._get("delegations_by_session", sid) or []
        for d in deps:
            did = d.get("delegation_id") or d.get("id")
            if did:
                add_edge(edges, DELEGATION, str(did), SESSION, sid, "async_delegations.origin_session", "native")
                add_node(nodes, DELEGATION, str(did), {"state": d.get("state", "")})
        # api requests
        reqs = await self._get("api_requests_by_session", sid) or []
        for r in reqs:
            rid = r.get("api_request_id")
            if rid:
                add_edge(edges, API_REQUEST, str(rid), SESSION, sid, "llm_provider_requests.session_id", "native")
                add_node(nodes, API_REQUEST, str(rid), {"model": r.get("model", "")})
                if r.get("turn_id"):
                    add_edge(edges, API_REQUEST, str(rid), "turn", str(r["turn_id"]), "llm_provider_requests.turn_id", "native")
                    add_node(nodes, "turn", str(r["turn_id"]), {})
        # messages with tool_call_id (bounded)
        msgs = await self._get("messages_by_session", sid) or []
        for m in msgs:
            tc = m.get("tool_call_id")
            if tc:
                add_edge(edges, TOOL_CALL, str(tc), MESSAGE, str(m.get("id")), "messages.tool_call_id", "native")
                add_node(nodes, TOOL_CALL, str(tc), {})
                add_node(nodes, MESSAGE, str(m.get("id")), {"role": m.get("role", "")})
        # issue occurrences referencing this session
        occs = await self._get("issue_occurrences_by_session", sid) or []
        for o in occs:
            iid = str(o.get("issue_id"))
            add_edge(edges, ISSUE, iid, SESSION, sid, "issue_occurrences.session_ref", "native")
            add_node(nodes, ISSUE, iid, {"severity": o.get("severity", ""), "status": o.get("status", "")})
            if o.get("task_ref"):
                add_edge(edges, ISSUE, iid, TASK, o["task_ref"], "issue_occurrences.task_ref", "native")
                add_node(nodes, TASK, o["task_ref"], {})

    async def _issue_edges(self, issue: dict, nodes: dict, edges: list) -> None:
        iid = str(issue.get("id"))
        add_node(nodes, ISSUE, iid, issue)
        for o in issue.get("occurrences", []) or []:
            if o.get("task_ref"):
                add_edge(edges, ISSUE, iid, TASK, o["task_ref"], "issue_occurrences.task_ref", "native")
                add_node(nodes, TASK, o["task_ref"], {})
            if o.get("session_ref"):
                add_edge(edges, ISSUE, iid, SESSION, o["session_ref"], "issue_occurrences.session_ref", "native")
                add_node(nodes, SESSION, o["session_ref"], {})
            if o.get("tool_ref"):
                add_edge(edges, ISSUE, iid, TOOL_CALL, o["tool_ref"], "issue_occurrences.tool_ref (NAME only)", "inferred")
                add_node(nodes, TOOL_CALL, o["tool_ref"], {})
        # permit -> issue via issue_title "Issue N"
        permits = await self._get("permits_for_issue", iid) or []
        for p in permits:
            pid = p.get("permit_id")
            if pid:
                add_edge(edges, PERMIT, str(pid), ISSUE, iid, "permits.issue_title parse", "inferred")
                add_node(nodes, PERMIT, str(pid), {"status": p.get("status", "")})

    # ---- public API ---------------------------------------------------------
    async def correlate(self, entity_type: str, entity_id: Any) -> dict:
        nodes: dict[tuple[str, str], dict] = {}
        edges: list[dict] = []
        supported_hits: set = set()
        partial_hits: set = set()

        if entity_type == TASK:
            task = await self._get("task", entity_id)
            if not task:
                return {"nodes": [], "edges": [], "coverage": "unsupported"}
            await self._task_edges(task, nodes, edges)
        elif entity_type == SESSION:
            session = await self._get("session", entity_id)
            if not session:
                return {"nodes": [], "edges": [], "coverage": "unsupported"}
            await self._session_edges(session, nodes, edges)
        elif entity_type == ISSUE:
            issue = await self._get("issue", entity_id)
            if not issue:
                return {"nodes": [], "edges": [], "coverage": "unsupported"}
            await self._issue_edges(issue, nodes, edges)
        elif entity_type == RUN:
            run = await self._get("run", entity_id)
            if not run:
                return {"nodes": [], "edges": [], "coverage": "unsupported"}
            rid = str(run.get("id"))
            tid = run.get("task_id")
            add_node(nodes, RUN, rid, run)
            if tid:
                add_edge(edges, TASK, tid, RUN, rid, "task_runs.task_id", "native")
                add_node(nodes, TASK, tid, {})
        else:
            return {"nodes": [], "edges": [], "coverage": "unsupported"}

        # coverage: complete if any native edge, partial if only inferred/partial
        for e in edges:
            pair = frozenset((e["source_type"], e["target_type"]))
            if e["kind"] in ("native",):
                supported_hits.add(pair)
            elif e["kind"] == "partial":
                partial_hits.add(pair)
            else:
                partial_hits.add(pair)

        unsupported = [p for p in UNSUPPORTED_PAIRS if any(
            frozenset((s, t)) == frozenset(p) for s, t in
            [(e["source_type"], e["target_type"]) for e in edges]
        )]
        if edges and (supported_hits or partial_hits):
            coverage = "complete" if supported_hits else "partial"
        else:
            coverage = "unsupported"
        return {
            "nodes": list(nodes.values()),
            "edges": edges,
            "coverage": coverage,
            "unsupported_pairs": [PAIR_NAMES.get(p, str(p)) for p in unsupported],
        }


def add_node(nodes: dict, kind: str, entity_id: Any, data: Optional[dict]) -> None:
    key = (kind, str(entity_id))
    if key not in nodes:
        nodes[key] = {"type": kind, "id": str(entity_id), **(data or {})}


def add_edge(
    edges: list,
    src_type: str, src_id: Any,
    tgt_type: str, tgt_id: Any,
    evidence: str,
    kind: str,
) -> None:
    edges.append({
        "source": f"{src_type}:{src_id}",
        "target": f"{tgt_type}:{tgt_id}",
        "source_type": src_type,
        "target_type": tgt_type,
        "evidence": evidence,
        "kind": kind,  # native | partial | inferred
    })


ISSUE_TITLE_RE = re.compile(r"[Ii]ssue\s+(\d+)")


def parse_permit_issue(issue_title: str) -> Optional[str]:
    """Permit<->issue partial link: 'Issue N' free-text parse."""
    m = ISSUE_TITLE_RE.search(issue_title or "")
    return m.group(1) if m else None

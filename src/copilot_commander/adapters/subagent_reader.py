"""Read-only parser for sub-agent activity from a session's events log.

Each Copilot CLI session writes a line-delimited ``events.jsonl`` file
in its session-state directory. Sub-agent invocations produce a pair
of events keyed by ``toolCallId``::

    {"type":"subagent.started","data":{"toolCallId":"...","agentName":"...",
     "agentDisplayName":"...","agentDescription":"..."}, ... }
    {"type":"subagent.completed","data":{"toolCallId":"..."}, ... }

The reader streams the file once, pairs start/complete events by id,
and returns a :class:`SubAgentTree`. Results are cached by
``(session_id, events.jsonl mtime)`` so the dashboard can call
:meth:`read` on every expand / poll without re-parsing a file whose
contents have not changed.

This is intentionally a separate adapter from
:class:`CopilotSessionStore`: session discovery wants cheap tail-only
reads across *every* session directory, while sub-agent tracking wants
a full scan of *one* session's events when the operator expands that
row. Mixing the two would either slow discovery down or pay for a
full scan on sessions no one is looking at.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from copilot_commander.adapters.copilot_session_store import SessionStoreRoot
from copilot_commander.domain.subagents import SubAgentSnapshot, SubAgentTree

_log = logging.getLogger(__name__)


class _SessionDirProvider(Protocol):
    """Minimal surface the reader needs from the session store.

    We intentionally don't depend on the concrete
    ``CopilotSessionStore`` type: the reader only needs to know which
    filesystem roots to probe, and taking a protocol keeps the unit
    tests trivial (a two-line stub replaces the whole store).
    """

    @property
    def session_state_dir(self) -> Path: ...

    @property
    def extra_roots(self) -> tuple[SessionStoreRoot, ...]: ...


@dataclass(slots=True)
class _CachedTree:
    mtime_ns: int
    tree: SubAgentTree


@dataclass(slots=True)
class SubAgentReader:
    """Parse sub-agent activity for a given session id, with mtime cache.

    ``recent_limit`` caps how many completed sub-agents we keep per
    tree. The dashboard only renders a handful anyway and parent
    sessions that have delegated hundreds of tasks shouldn't force the
    UI to hold that whole list in memory.
    """

    store: _SessionDirProvider
    recent_limit: int = 20
    _cache: dict[str, _CachedTree] = field(default_factory=dict, init=False, repr=False)

    def read(self, session_id: str) -> SubAgentTree | None:
        """Return the current sub-agent tree for ``session_id``.

        Returns ``None`` when the session id doesn't resolve to any of
        the configured roots (likely deleted or from a different host).
        Returns an empty tree when the session exists but has never
        spawned a sub-agent.
        """
        events_path = self._resolve_events_path(session_id)
        if events_path is None:
            return None
        try:
            mtime_ns = events_path.stat().st_mtime_ns
        except OSError:
            return None
        cached = self._cache.get(session_id)
        if cached is not None and cached.mtime_ns == mtime_ns:
            return cached.tree
        tree = _parse_events(
            session_id,
            events_path,
            recent_limit=self.recent_limit,
        )
        self._cache[session_id] = _CachedTree(mtime_ns=mtime_ns, tree=tree)
        return tree

    def invalidate(self, session_id: str | None = None) -> None:
        """Drop cached trees so the next ``read`` reparses from disk."""
        if session_id is None:
            self._cache.clear()
        else:
            self._cache.pop(session_id, None)

    def _resolve_events_path(self, session_id: str) -> Path | None:
        # Copilot CLI stores each session as ``<root>/<session_id>/``.
        # Probe every configured root so Windows-side sessions under
        # a secondary root are discoverable too.
        candidates = [SessionStoreRoot(self.store.session_state_dir, "local")]
        candidates.extend(self.store.extra_roots)
        for root in candidates:
            candidate = root.path / session_id / "events.jsonl"
            if candidate.is_file():
                return candidate
        return None


def _parse_events(
    session_id: str,
    events_path: Path,
    *,
    recent_limit: int,
) -> SubAgentTree:
    """Stream-parse one events.jsonl into a sub-agent tree.

    We track started events by ``toolCallId`` in insertion order and
    lift them into ``recent`` when the matching ``completed`` arrives.
    A session that dies mid-run leaves its last sub-agents in the
    running bucket, which is exactly what the dashboard should show:
    "these were in flight when we lost contact".

    In a single pass we also pick up the matching ``task`` tool events
    so the detail view can show prompt + result content without
    re-reading the file.
    """
    started: dict[str, SubAgentSnapshot] = {}
    completed: list[SubAgentSnapshot] = []
    task_details: dict[str, _TaskDetails] = {}
    try:
        with events_path.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                _apply_event(
                    event,
                    started=started,
                    completed=completed,
                    task_details=task_details,
                )
    except OSError as exc:
        _log.debug("failed to read subagent events from %s: %s", events_path, exc)

    running = tuple(_enrich(s, task_details) for s in reversed(started.values()))
    recent = tuple(
        _enrich(s, task_details)
        for s in sorted(completed, key=_completed_sort_key, reverse=True)[:recent_limit]
    )
    return SubAgentTree(
        session_id=session_id,
        running=running,
        recent=recent,
        scanned_at=datetime.now(UTC),
    )


@dataclass(slots=True)
class _TaskDetails:
    task_name: str | None = None
    agent_type: str | None = None
    prompt: str | None = None
    result_content: str | None = None
    success: bool | None = None


def _apply_event(
    event: dict[str, object],
    *,
    started: dict[str, SubAgentSnapshot],
    completed: list[SubAgentSnapshot],
    task_details: dict[str, _TaskDetails],
) -> None:
    event_type = event.get("type")
    data = event.get("data")
    if not isinstance(data, dict):
        return
    # Enrichment side-channel: capture task tool prompt/result so the
    # sub-agent detail view has meaningful input and output.
    if event_type == "tool.execution_start" and _as_str(data.get("toolName")) == "task":
        tcid = _as_str(data.get("toolCallId"))
        args = data.get("arguments")
        if tcid and isinstance(args, dict):
            detail = task_details.setdefault(tcid, _TaskDetails())
            detail.task_name = _as_str(args.get("name"))
            detail.agent_type = _as_str(args.get("agent_type"))
            detail.prompt = _as_str(args.get("prompt"))
        return
    if event_type == "tool.execution_complete" and _as_str(data.get("toolName")) == "task":
        tcid = _as_str(data.get("toolCallId"))
        if tcid:
            detail = task_details.setdefault(tcid, _TaskDetails())
            result = data.get("result")
            if isinstance(result, dict):
                detail.result_content = _as_str(result.get("content"))
            elif isinstance(result, str):
                detail.result_content = result or None
            success = data.get("success")
            if isinstance(success, bool):
                detail.success = success
        return

    if event_type not in ("subagent.started", "subagent.completed"):
        return
    tool_call_id = data.get("toolCallId")
    if not isinstance(tool_call_id, str) or not tool_call_id:
        return
    timestamp = _parse_iso(event.get("timestamp"))
    if timestamp is None:
        return

    if event_type == "subagent.started":
        agent_name = _as_str(data.get("agentName")) or "unknown"
        display_name = _as_str(data.get("agentDisplayName")) or agent_name
        description = _as_str(data.get("agentDescription"))
        started[tool_call_id] = SubAgentSnapshot(
            tool_call_id=tool_call_id,
            agent_name=agent_name,
            display_name=display_name,
            description=description,
            started_at=timestamp,
            completed_at=None,
        )
        return

    # subagent.completed
    existing = started.pop(tool_call_id, None)
    if existing is None:
        agent_name = _as_str(data.get("agentName")) or "unknown"
        display_name = _as_str(data.get("agentDisplayName")) or agent_name
        completed.append(
            SubAgentSnapshot(
                tool_call_id=tool_call_id,
                agent_name=agent_name,
                display_name=display_name,
                description=None,
                started_at=timestamp,
                completed_at=timestamp,
            )
        )
        return
    completed.append(
        SubAgentSnapshot(
            tool_call_id=existing.tool_call_id,
            agent_name=existing.agent_name,
            display_name=existing.display_name,
            description=existing.description,
            started_at=existing.started_at,
            completed_at=timestamp,
        )
    )


def _enrich(snapshot: SubAgentSnapshot, task_details: dict[str, _TaskDetails]) -> SubAgentSnapshot:
    detail = task_details.get(snapshot.tool_call_id)
    if detail is None:
        return snapshot
    return SubAgentSnapshot(
        tool_call_id=snapshot.tool_call_id,
        agent_name=snapshot.agent_name,
        display_name=snapshot.display_name,
        description=snapshot.description,
        started_at=snapshot.started_at,
        completed_at=snapshot.completed_at,
        task_name=detail.task_name,
        agent_type=detail.agent_type,
        prompt=detail.prompt,
        result_content=detail.result_content,
        success=detail.success,
    )


def _completed_sort_key(snapshot: SubAgentSnapshot) -> datetime:
    # completed_at is non-None for every element we place in `completed`
    # but keep this defensive for callers that might synthesize data.
    return snapshot.completed_at or snapshot.started_at


def _parse_iso(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    cleaned = value.strip()
    if cleaned.endswith("Z"):
        cleaned = cleaned[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(cleaned)
    except ValueError:
        return None


def _as_str(value: object) -> str | None:
    if isinstance(value, str) and value:
        return value
    return None


def _safe_iter_roots(primary: Path, extras: Iterable[SessionStoreRoot]) -> list[SessionStoreRoot]:
    # Kept for symmetry with CopilotSessionStore._iter_roots if future
    # code wants to pre-resolve candidates. Not currently used.
    roots = [SessionStoreRoot(primary, "local")]
    seen = {primary}
    for extra in extras:
        if extra.path in seen:
            continue
        seen.add(extra.path)
        roots.append(extra)
    return roots


__all__ = ["SubAgentReader"]

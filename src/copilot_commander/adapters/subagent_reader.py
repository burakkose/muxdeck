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
class _StreamState:
    """Incremental parse state for one session's events.jsonl.

    The reader keeps a byte offset into the file and the currently
    accumulated started/completed/task-detail maps. Each :meth:`read`
    call stats the file, decides whether it was rotated/truncated
    (inode change or size shrink) or just grew, and only consumes the
    newly appended bytes. A trailing incomplete line — Copilot CLI
    writes events.jsonl without fsync guarantees — is buffered in
    ``partial`` and re-joined on the next pass.
    """

    inode: int
    size: int
    mtime_ns: int
    offset: int
    partial: str
    started: dict[str, SubAgentSnapshot]
    completed: list[SubAgentSnapshot]
    task_details: dict[str, _TaskDetails]
    tree: SubAgentTree


@dataclass(slots=True)
class SubAgentReader:
    """Parse sub-agent activity for a given session id incrementally.

    ``recent_limit`` caps how many completed sub-agents we keep per
    tree. The dashboard only renders a handful anyway and parent
    sessions that have delegated hundreds of tasks shouldn't force the
    UI to hold that whole list in memory.

    Reads are O(new bytes) rather than O(file size): the reader
    maintains a per-session byte offset and tails only what was
    appended since the last call. A completely unchanged file returns
    the last built tree without re-opening it.
    """

    store: _SessionDirProvider
    recent_limit: int = 20
    # Keep enough completed entries in memory to survive arbitrary
    # reordering by completed_at when we sort for the `recent` slice,
    # but bound it so pathological sessions don't grow without limit.
    _completed_memory_factor: int = 8
    _state: dict[str, _StreamState] = field(default_factory=dict, init=False, repr=False)

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
            stat = events_path.stat()
        except OSError:
            return None

        state = self._state.get(session_id)
        rotated = state is not None and (stat.st_ino != state.inode or stat.st_size < state.offset)
        if rotated:
            state = None

        if state is not None and stat.st_mtime_ns == state.mtime_ns and stat.st_size == state.size:
            # Nothing new on disk — hand back the last tree. Identity
            # preservation matters: callers compare `tree is prev` to
            # decide whether to skip a repaint.
            return state.tree

        if state is None:
            state = _StreamState(
                inode=stat.st_ino,
                size=0,
                mtime_ns=0,
                offset=0,
                partial="",
                started={},
                completed=[],
                task_details={},
                tree=SubAgentTree(
                    session_id=session_id,
                    running=(),
                    recent=(),
                    scanned_at=datetime.now(UTC),
                ),
            )

        self._consume_new_bytes(events_path, state)
        state.size = stat.st_size
        state.mtime_ns = stat.st_mtime_ns
        state.inode = stat.st_ino

        self._trim_completed(state)
        state.tree = _build_tree(
            session_id,
            started=state.started,
            completed=state.completed,
            task_details=state.task_details,
            recent_limit=self.recent_limit,
        )
        self._state[session_id] = state
        return state.tree

    def invalidate(self, session_id: str | None = None) -> None:
        """Drop stream state so the next ``read`` reparses from scratch."""
        if session_id is None:
            self._state.clear()
        else:
            self._state.pop(session_id, None)

    def _consume_new_bytes(self, events_path: Path, state: _StreamState) -> None:
        """Read bytes from ``state.offset`` to EOF and apply events."""
        try:
            with events_path.open("r", encoding="utf-8", errors="replace") as fh:
                if state.offset:
                    fh.seek(state.offset)
                chunk = fh.read()
        except OSError as exc:
            _log.debug("failed to tail subagent events from %s: %s", events_path, exc)
            return

        if not chunk and not state.partial:
            return

        buffer = state.partial + chunk
        last_newline = buffer.rfind("\n")
        if last_newline == -1:
            # Entire buffer is one incomplete line; keep it for next tick.
            state.partial = buffer
            # offset intentionally unchanged — we'll re-read from the
            # same spot next time to try again.
            return

        complete_text = buffer[: last_newline + 1]
        state.partial = buffer[last_newline + 1 :]
        # Advance offset so next read starts at the byte immediately
        # after the last complete line we just consumed.
        state.offset += len(chunk) - len(state.partial)

        for raw_line in complete_text.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            _apply_event(
                event,
                started=state.started,
                completed=state.completed,
                task_details=state.task_details,
            )

    def _trim_completed(self, state: _StreamState) -> None:
        cap = max(self.recent_limit * self._completed_memory_factor, self.recent_limit)
        if len(state.completed) <= cap:
            return
        state.completed.sort(key=_completed_sort_key, reverse=True)
        del state.completed[cap:]

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


def _build_tree(
    session_id: str,
    *,
    started: dict[str, SubAgentSnapshot],
    completed: list[SubAgentSnapshot],
    task_details: dict[str, _TaskDetails],
    recent_limit: int,
) -> SubAgentTree:
    """Materialize a :class:`SubAgentTree` from current stream state.

    Called on every :meth:`SubAgentReader.read` that observed new
    bytes. Running sub-agents are returned newest-first (dict
    insertion order reversed); completed are sorted by completed_at
    and capped at ``recent_limit``.

    A session that dies mid-run leaves its last sub-agents in the
    running bucket, which is exactly what the dashboard should show:
    "these were in flight when we lost contact".
    """
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

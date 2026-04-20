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
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from muxdeck.adapters.copilot_session_store import SessionStoreRoot
from muxdeck.domain.subagents import (
    ReadAgentInteraction,
    SubAgentSnapshot,
    SubAgentTree,
)

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


_MAX_READ_INTERACTIONS_PER_TASK = 50
_READ_AGENT_RESULT_MAX_CHARS = 2000
_READ_AGENT_ARGS_MAX_CHARS = 200
_READ_AGENT_STATUS_RE = re.compile(r"\bstatus:\s*([a-z_]+)\b", re.IGNORECASE)


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
    # Background sub-agents stream output back to the parent via a
    # sequence of ``read_agent`` tool calls keyed by the task's ``name``
    # argument. We resolve that name back to the task's tool call id so
    # interactions land on the right sub-agent detail.
    task_name_to_tcid: dict[str, str]
    # ``tool.execution_start`` for ``read_agent`` races ahead of its
    # ``tool.execution_complete``. Buffer the half-parsed interaction
    # keyed by the read_agent tool call id until the result arrives.
    pending_read_agents: dict[str, _PendingReadAgent]
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
                task_name_to_tcid={},
                pending_read_agents={},
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
                task_name_to_tcid=state.task_name_to_tcid,
                pending_read_agents=state.pending_read_agents,
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
    running_candidates = tuple(_enrich(s, task_details) for s in reversed(started.values()))
    running = tuple(snapshot for snapshot in running_candidates if snapshot.is_running)
    recent = tuple(
        sorted(
            (
                *(snapshot for snapshot in running_candidates if not snapshot.is_running),
                *(_enrich(snapshot, task_details) for snapshot in completed),
            ),
            key=_completed_sort_key,
            reverse=True,
        )[:recent_limit]
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
    mode: str | None = None
    # For background tasks the parent's ``tool.execution_complete``
    # only holds a launch ack; the real agent output lives in the
    # sub-agent's own session log. Keep both so the UI can label them
    # correctly instead of pretending the ack is the real result.
    result_content: str | None = None
    result_detailed: str | None = None
    success: bool | None = None
    # Populated from ``subagent.completed`` / ``subagent.failed``.
    model: str | None = None
    total_tokens: int | None = None
    duration_ms: int | None = None
    total_tool_calls: int | None = None
    error_message: str | None = None
    # Correlated ``read_agent`` interactions for background sub-agents.
    read_interactions: list[ReadAgentInteraction] = field(default_factory=list)
    latest_agent_status: str | None = None
    latest_agent_status_at: datetime | None = None


@dataclass(slots=True)
class _PendingReadAgent:
    """A ``read_agent`` call observed on ``tool.execution_start`` whose
    completion event has not landed yet.

    We need both halves to produce a :class:`ReadAgentInteraction`:
    the start gives us the target ``agent_id`` and the wall-clock
    timestamp, the completion gives us the textual result.
    """

    target_agent_id: str
    timestamp: datetime
    arguments_summary: str


def _apply_event(
    event: dict[str, object],
    *,
    started: dict[str, SubAgentSnapshot],
    completed: list[SubAgentSnapshot],
    task_details: dict[str, _TaskDetails],
    task_name_to_tcid: dict[str, str],
    pending_read_agents: dict[str, _PendingReadAgent],
) -> None:
    event_type = event.get("type")
    data = event.get("data")
    if not isinstance(data, dict):
        return
    # Enrichment side-channel: capture task tool prompt/result so the
    # sub-agent detail view has meaningful input and output.
    if event_type == "tool.execution_start":
        tool_name = _as_str(data.get("toolName"))
        if tool_name == "task":
            tcid = _as_str(data.get("toolCallId"))
            args = data.get("arguments")
            if tcid and isinstance(args, dict):
                detail = task_details.setdefault(tcid, _TaskDetails())
                detail.task_name = _as_str(args.get("name"))
                detail.agent_type = _as_str(args.get("agent_type"))
                detail.prompt = _as_str(args.get("prompt"))
                detail.mode = _as_str(args.get("mode"))
                if detail.task_name is not None:
                    task_name_to_tcid[detail.task_name] = tcid
            return
        if tool_name == "read_agent":
            read_tcid = _as_str(data.get("toolCallId"))
            args = data.get("arguments")
            if not read_tcid or not isinstance(args, dict):
                return
            target = _as_str(args.get("agent_id"))
            if target is None:
                return
            timestamp = _parse_iso(event.get("timestamp"))
            if timestamp is None:
                return
            pending_read_agents[read_tcid] = _PendingReadAgent(
                target_agent_id=target,
                timestamp=timestamp,
                arguments_summary=_summarise_read_agent_args(args),
            )
            return
        return
    # ``tool.execution_complete`` does not carry ``toolName`` in the
    # current CLI — match by ``toolCallId`` against the tasks we
    # already recorded on ``tool.execution_start``. Without this
    # lookup the result_content branch was dead code and the detail
    # view never had any output to show.
    if event_type == "tool.execution_complete":
        tcid = _as_str(data.get("toolCallId"))
        if not tcid:
            return
        timestamp = _parse_iso(event.get("timestamp"))
        pending = pending_read_agents.pop(tcid, None)
        if pending is not None:
            _record_read_agent_completion(
                pending,
                data,
                observed_at=timestamp,
                task_name_to_tcid=task_name_to_tcid,
                task_details=task_details,
            )
            return
        if tcid in task_details:
            detail = task_details[tcid]
            result = data.get("result")
            if isinstance(result, dict):
                detail.result_content = _as_str(result.get("content"))
                detail.result_detailed = _as_str(result.get("detailedContent"))
            elif isinstance(result, str):
                detail.result_content = result or None
            success = data.get("success")
            if isinstance(success, bool):
                detail.success = success
        return

    if event_type not in ("subagent.started", "subagent.completed", "subagent.failed"):
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

    # subagent.completed / subagent.failed — both terminal.
    _record_terminal_metrics(
        data,
        task_details=task_details,
        tool_call_id=tool_call_id,
        failed=event_type == "subagent.failed",
    )

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


def _record_terminal_metrics(
    data: dict[str, object],
    *,
    task_details: dict[str, _TaskDetails],
    tool_call_id: str,
    failed: bool,
) -> None:
    """Copy metrics off a ``subagent.completed``/``subagent.failed`` event
    onto the matching task's detail record."""
    detail = task_details.setdefault(tool_call_id, _TaskDetails())
    detail.model = _as_str(data.get("model")) or detail.model
    total_tokens = _as_int(data.get("totalTokens"))
    if total_tokens is not None:
        detail.total_tokens = total_tokens
    duration_ms = _as_int(data.get("durationMs"))
    if duration_ms is not None:
        detail.duration_ms = duration_ms
    total_tool_calls = _as_int(data.get("totalToolCalls"))
    if total_tool_calls is not None:
        detail.total_tool_calls = total_tool_calls
    if failed:
        detail.success = False
        error = _as_str(data.get("error"))
        if error is not None:
            detail.error_message = error


def _record_read_agent_completion(
    pending: _PendingReadAgent,
    data: dict[str, object],
    *,
    observed_at: datetime | None,
    task_name_to_tcid: dict[str, str],
    task_details: dict[str, _TaskDetails],
) -> None:
    """Finalize a ``read_agent`` interaction and attach it to its task.

    The task it belongs to is the one whose ``task_name`` matches the
    ``agent_id`` the read_agent call was targeting — that is how the
    parent addresses its background children.
    """
    task_tcid = task_name_to_tcid.get(pending.target_agent_id)
    if task_tcid is None:
        return
    detail = task_details.setdefault(task_tcid, _TaskDetails())
    result_content = _extract_read_agent_result(data.get("result"))
    status = _extract_read_agent_status(data.get("result"))
    detail.read_interactions.append(
        ReadAgentInteraction(
            timestamp=pending.timestamp,
            arguments_summary=pending.arguments_summary,
            result_content=result_content,
        )
    )
    if status is not None:
        detail.latest_agent_status = status
        detail.latest_agent_status_at = observed_at or pending.timestamp
    if len(detail.read_interactions) > _MAX_READ_INTERACTIONS_PER_TASK:
        # Keep the most recent window so long-running coordinators
        # don't grow memory without bound.
        del detail.read_interactions[:-_MAX_READ_INTERACTIONS_PER_TASK]


def _extract_read_agent_result(result: object) -> str | None:
    if isinstance(result, dict):
        detailed = _as_str(result.get("detailedContent"))
        content = _as_str(result.get("content"))
        chosen = detailed or content
    elif isinstance(result, str):
        chosen = result or None
    else:
        chosen = None
    if chosen is None:
        return None
    if len(chosen) > _READ_AGENT_RESULT_MAX_CHARS:
        return chosen[: _READ_AGENT_RESULT_MAX_CHARS - 1].rstrip() + "…"
    return chosen


def _extract_read_agent_status(result: object) -> str | None:
    candidates: tuple[object, ...]
    if isinstance(result, dict):
        candidates = (result.get("content"), result.get("detailedContent"))
    elif isinstance(result, str):
        candidates = (result,)
    else:
        candidates = ()
    for candidate in candidates:
        if not isinstance(candidate, str) or not candidate:
            continue
        match = _READ_AGENT_STATUS_RE.search(candidate)
        if match is not None:
            return match.group(1).lower()
    return None


def _summarise_read_agent_args(args: dict[str, object]) -> str:
    """Format read_agent arguments as a compact one-line summary.

    We keep the shape close to the source (``agent_id=..., wait=...``)
    so operators who know the CLI recognise it, but cap length so the
    UI can render one-line entries without wrapping.
    """
    parts: list[str] = []
    agent_id = _as_str(args.get("agent_id"))
    if agent_id is not None:
        parts.append(f'agent_id="{agent_id}"')
    for key in ("wait", "timeout", "since_turn"):
        if key in args:
            raw = args[key]
            if isinstance(raw, bool):
                parts.append(f"{key}={'true' if raw else 'false'}")
            elif isinstance(raw, int | float | str):
                parts.append(f"{key}={raw}")
    summary = ", ".join(parts)
    if len(summary) > _READ_AGENT_ARGS_MAX_CHARS:
        return summary[: _READ_AGENT_ARGS_MAX_CHARS - 1].rstrip() + "…"
    return summary


def _enrich(snapshot: SubAgentSnapshot, task_details: dict[str, _TaskDetails]) -> SubAgentSnapshot:
    detail = task_details.get(snapshot.tool_call_id)
    if detail is None:
        return snapshot
    completed_at = snapshot.completed_at
    if completed_at is None and detail.latest_agent_status not in (None, "running"):
        completed_at = detail.latest_agent_status_at or snapshot.started_at
    return SubAgentSnapshot(
        tool_call_id=snapshot.tool_call_id,
        agent_name=snapshot.agent_name,
        display_name=snapshot.display_name,
        description=snapshot.description,
        started_at=snapshot.started_at,
        completed_at=completed_at,
        task_name=detail.task_name,
        agent_type=detail.agent_type,
        prompt=detail.prompt,
        mode=detail.mode,
        result_content=detail.result_detailed or detail.result_content,
        success=detail.success,
        read_interactions=tuple(detail.read_interactions),
        total_tokens=detail.total_tokens,
        duration_ms=detail.duration_ms,
        total_tool_calls=detail.total_tool_calls,
        model=detail.model,
        error_message=detail.error_message,
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


def _as_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
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

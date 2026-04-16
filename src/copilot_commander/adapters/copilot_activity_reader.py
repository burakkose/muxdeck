"""Read Copilot session ``events.jsonl`` to describe what an agent is doing.

The regex-based pane parser tries to extract "the agent is reading X"
from terminal text which is lossy and often stale. Copilot CLI writes
every tool call to ``events.jsonl`` as structured JSON events; that
stream is the authoritative source of truth.

This adapter tails events.jsonl incrementally (we remember each
session's last byte offset and only parse deltas) and produces a
small :class:`AgentActivity` value describing the current pending tool
call, the latest ``report_intent`` message, and whether the agent is
parked on an ``ask_user`` prompt. The dashboard controller uses this
view to render "editing foo.py" / "running pytest" / "waiting: which
DB?" lines instead of the old trimmed task title.
"""

from __future__ import annotations

import json
import logging
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Literal, Protocol

from copilot_commander.adapters.copilot_session_store import SessionStoreRoot

_log = logging.getLogger(__name__)

# How much of the tail we'll read on a fresh parse (i.e. first time we
# see this session, or after a truncate/rotate). events.jsonl files can
# grow to hundreds of MB; we only need the last chunk to identify
# current activity. 256 KB covers far more than any realistic batch of
# pending tool calls.
_FRESH_PARSE_TAIL_BYTES = 256 * 1024

# How many transcript lines we hold per session. The widget typically
# renders the last ~20, but we keep a larger buffer so tail-reads can
# surface an older message if the newest ones are dense tool calls
# with no prose. Bounded so long sessions can't grow state without
# limit.
_TRANSCRIPT_BUFFER_MAX = 200

TranscriptRole = Literal["user", "assistant"]


@dataclass(frozen=True, slots=True)
class TranscriptLine:
    """One line of real agent / operator speech extracted from events.jsonl.

    The parent ``assistant.message`` / ``user.message`` events carry
    markdown-ish prose in ``content``. We split on newlines and emit
    one TranscriptLine per non-empty line so the log preview widget
    can render them at its own granularity.
    """

    at: datetime
    role: TranscriptRole
    content: str
    sequence_no: int


@dataclass(frozen=True, slots=True)
class AgentActivity:
    """One agent's current activity, parsed from ``events.jsonl``."""

    # Most recent report_intent.intent string (short agent self-description).
    intent: str | None
    # Most recent *unmatched* tool.execution_start — the tool still running
    # right now. When multiple tools overlap, this is the newest of the
    # still-pending ones.
    tool_name: str | None
    tool_target: str | None
    # Human-friendly one-liner combining tool_name and tool_target,
    # e.g. "editing foo.py", "running pytest", "waiting: which DB?".
    summary: str | None
    # True when the pending tool is ask_user — i.e. the agent is blocked
    # on the operator. Dashboards can surface this as WAITING_INPUT.
    waiting_for_user: bool
    # Timestamp of the last event we ingested. None when the file is
    # empty or we haven't seen any events yet.
    latest_at: datetime | None

    @property
    def has_signal(self) -> bool:
        return self.summary is not None or self.intent is not None


@dataclass(slots=True)
class _PendingTool:
    """A tool call we've seen start but haven't seen complete."""

    tool_call_id: str
    tool_name: str
    target: str | None
    started_at: datetime


@dataclass(slots=True)
class _SessionState:
    """Incremental parse state for one session's events.jsonl."""

    path: Path
    inode: int = 0
    mtime_ns: int = 0
    last_size: int = 0
    last_offset: int = 0
    # First-bytes fingerprint — lets us detect in-place file replacement
    # (unlink + rewrite reusing the same inode, common on tmpfs) without
    # re-reading the entire file every tick. 128 bytes is more than
    # enough to cover the leading session.* event that never changes
    # for the same session.
    head_fingerprint: bytes = b""
    pending: dict[str, _PendingTool] = field(default_factory=dict)
    intent: str | None = None
    intent_at: datetime | None = None
    latest_at: datetime | None = None
    # A partial trailing line from a previous read we couldn't json-parse
    # yet — re-prepend on the next read.
    buffered_tail: str = ""
    # Rolling transcript of the last N assistant / user messages.
    # Bounded via ``deque(maxlen=...)`` so a long-running session with
    # thousands of turns can't grow reader memory without limit.
    transcript: deque[TranscriptLine] = field(
        default_factory=lambda: deque(maxlen=_TRANSCRIPT_BUFFER_MAX)
    )
    _transcript_seq: int = 0


class _SessionDirProvider(Protocol):
    @property
    def session_state_dir(self) -> Path: ...

    @property
    def extra_roots(self) -> tuple[SessionStoreRoot, ...]: ...


class CopilotActivityReader:
    """Incremental reader for the per-session ``events.jsonl`` tail."""

    def __init__(self, *, store: _SessionDirProvider) -> None:
        self._store = store
        self._state: dict[str, _SessionState] = {}

    def read(self, session_id: str) -> AgentActivity | None:
        """Return the latest activity for ``session_id``, or None.

        Returns None when the session has no known events file (e.g.
        fresh session that hasn't persisted anything yet, or the id
        was invented by the discovery pipeline and doesn't match a
        real Copilot session).
        """
        state = self._state.get(session_id)
        if state is None:
            resolved = self._resolve_events_path(session_id)
            if resolved is None:
                return None
            state = _SessionState(path=resolved)
            self._state[session_id] = state

        try:
            st = state.path.stat()
        except FileNotFoundError:
            # File was deleted — drop cached state so a new file (e.g.
            # session id reused with a fresh log) can be picked up.
            self._state.pop(session_id, None)
            return None
        except OSError as exc:
            _log.debug("activity reader stat failed for %s: %s", state.path, exc)
            return None

        rotated = (
            st.st_ino != state.inode
            or st.st_size < state.last_size
            or (state.mtime_ns != 0 and st.st_mtime_ns < state.mtime_ns)
        )
        # Fingerprint check catches the "unlink + recreate with a
        # different payload" case where the filesystem happens to reuse
        # the inode (common on tmpfs). Only pay this cost when mtime
        # advanced — on a pure append the file head didn't change.
        if (
            not rotated
            and state.head_fingerprint
            and state.mtime_ns != 0
            and st.st_mtime_ns != state.mtime_ns
        ):
            current_head = _read_head(state.path)
            if current_head != state.head_fingerprint:
                rotated = True

        if rotated:
            # Inode changed or file shrank — toss all derived state and
            # re-parse from the tail.
            state.inode = st.st_ino
            state.mtime_ns = st.st_mtime_ns
            state.last_size = 0
            state.last_offset = 0
            state.pending.clear()
            state.intent = None
            state.intent_at = None
            state.latest_at = None
            state.buffered_tail = ""
            state.head_fingerprint = b""
            state.transcript.clear()
            state._transcript_seq = 0

        if st.st_size == state.last_size and not rotated:
            # Nothing new. Return cached snapshot.
            return self._snapshot(state)

        self._ingest(state, file_size=st.st_size, fresh=rotated or state.last_size == 0)
        state.mtime_ns = st.st_mtime_ns
        if not state.head_fingerprint:
            state.head_fingerprint = _read_head(state.path)
        return self._snapshot(state)

    def invalidate(self, session_id: str | None = None) -> None:
        if session_id is None:
            self._state.clear()
        else:
            self._state.pop(session_id, None)

    # ── internals ────────────────────────────────────────────────────

    def _resolve_events_path(self, session_id: str) -> Path | None:
        candidates = [SessionStoreRoot(self._store.session_state_dir, "local")]
        candidates.extend(self._store.extra_roots)
        for root in candidates:
            candidate = root.path / session_id / "events.jsonl"
            if candidate.is_file():
                return candidate
        return None

    def _ingest(self, state: _SessionState, *, file_size: int, fresh: bool) -> None:
        """Read new bytes from the file and fold them into ``state``."""
        start = max(0, file_size - _FRESH_PARSE_TAIL_BYTES) if fresh else state.last_offset
        try:
            with state.path.open("rb") as fh:
                fh.seek(start)
                chunk = fh.read(file_size - start)
        except OSError as exc:
            _log.debug("activity reader read failed for %s: %s", state.path, exc)
            return

        text = state.buffered_tail + chunk.decode("utf-8", errors="replace")
        lines = text.split("\n")
        # The last element is whatever followed the final "\n" — may be
        # empty (clean newline boundary) or a partial line still being
        # written. Either way, hold it for the next read.
        state.buffered_tail = lines.pop()
        if fresh and start > 0 and lines:
            # The first "line" after a mid-file seek is almost certainly
            # a partial line — its start byte was chopped off. Skip it.
            # When start==0 we read the whole file and every line is
            # complete, so don't drop anything.
            lines.pop(0)

        for raw in lines:
            raw = raw.strip()
            if not raw:
                continue
            try:
                event = json.loads(raw)
            except json.JSONDecodeError:
                continue
            self._apply(state, event)

        state.last_offset = file_size
        state.last_size = file_size

    def _apply(self, state: _SessionState, event: dict[str, object]) -> None:
        event_type = event.get("type")
        data = event.get("data")
        if not isinstance(data, dict):
            return
        timestamp = _parse_iso(event.get("timestamp"))
        if timestamp is not None:
            state.latest_at = timestamp

        if event_type == "tool.execution_start":
            tool_name = _as_str(data.get("toolName"))
            tool_call_id = _as_str(data.get("toolCallId"))
            if not tool_name or not tool_call_id or timestamp is None:
                return
            args = data.get("arguments") if isinstance(data.get("arguments"), dict) else {}
            assert isinstance(args, dict)
            target = _format_target(tool_name, args)
            if tool_name == "report_intent":
                # Treat report_intent as intent-channel, not pending tool.
                intent = _as_str(args.get("intent"))
                if intent:
                    state.intent = intent
                    state.intent_at = timestamp
                return
            state.pending[tool_call_id] = _PendingTool(
                tool_call_id=tool_call_id,
                tool_name=tool_name,
                target=target,
                started_at=timestamp,
            )
            return

        if event_type == "tool.execution_complete":
            tool_call_id = _as_str(data.get("toolCallId"))
            if tool_call_id:
                state.pending.pop(tool_call_id, None)
            return

        if event_type == "assistant.message":
            self._record_transcript_content(
                state,
                role="assistant",
                content=data.get("content"),
                at=timestamp,
            )
            return

        if event_type == "user.message":
            self._record_transcript_content(
                state,
                role="user",
                content=data.get("content"),
                at=timestamp,
            )
            return

    def _record_transcript_content(
        self,
        state: _SessionState,
        *,
        role: TranscriptRole,
        content: object,
        at: datetime | None,
    ) -> None:
        """Split a message's content on newlines and append non-empty
        lines to the transcript deque.

        The CLI writes conversational content as markdown prose in a
        single ``content`` string. Splitting per line keeps the log
        preview widget's line-oriented rendering happy without
        changing its contract.
        """
        if at is None or not isinstance(content, str) or not content.strip():
            return
        for raw in content.splitlines():
            stripped = raw.strip()
            if not stripped:
                continue
            state._transcript_seq += 1
            state.transcript.append(
                TranscriptLine(
                    at=at,
                    role=role,
                    content=stripped,
                    sequence_no=state._transcript_seq,
                )
            )

    def read_transcript(self, session_id: str, *, limit: int = 40) -> tuple[TranscriptLine, ...]:
        """Return the last ``limit`` transcript lines for a session.

        Returns an empty tuple when the session is unknown or has no
        conversational content yet. Callers should fall back to the
        tmux log preview in that case.
        """
        # Force an ingest so the transcript reflects current disk state.
        # ``read()`` already has all the rotation / offset logic we need.
        self.read(session_id)
        state = self._state.get(session_id)
        if state is None or not state.transcript:
            return ()
        if limit <= 0:
            return ()
        # deque supports slicing via list(); it's O(N) but N is bounded
        # by _TRANSCRIPT_BUFFER_MAX (200) so this is cheap.
        return tuple(list(state.transcript)[-limit:])

    def _snapshot(self, state: _SessionState) -> AgentActivity:
        pending_tool: _PendingTool | None = None
        if state.pending:
            # Newest unmatched pending tool wins — when multiple tools
            # overlap, the freshest start is the one currently producing
            # output the user cares about.
            pending_tool = max(state.pending.values(), key=lambda p: p.started_at)
        summary = _format_summary(pending_tool, state.intent)
        waiting = pending_tool is not None and pending_tool.tool_name == "ask_user"
        return AgentActivity(
            intent=state.intent,
            tool_name=pending_tool.tool_name if pending_tool else None,
            tool_target=pending_tool.target if pending_tool else None,
            summary=summary,
            waiting_for_user=waiting,
            latest_at=state.latest_at,
        )


# ── formatting helpers ────────────────────────────────────────────────

# Maps tool → (argument key, verb). When verb is None we render
# "<tool>: <target>" to preserve the raw name.
_TOOL_VERBS: dict[str, tuple[str, str]] = {
    "view": ("path", "reading"),
    "edit": ("path", "editing"),
    "create": ("path", "creating"),
    "bash": ("description", "running"),
    "grep": ("pattern", "searching"),
    "rg": ("pattern", "searching"),
    "glob": ("pattern", "globbing"),
    "ask_user": ("question", "waiting"),
    "task": ("name", "delegating"),
    "read_bash": ("shellId", "reading shell"),
    "write_bash": ("input", "writing shell"),
    "stop_bash": ("shellId", "stopping shell"),
    "web_fetch": ("url", "fetching"),
    "web_search": ("query", "searching web"),
    "session_store_sql": ("description", "querying store"),
    "sql": ("description", "querying sql"),
    "read_agent": ("agent_id", "reading agent"),
}


def _format_target(tool_name: str, args: dict[str, object]) -> str | None:
    spec = _TOOL_VERBS.get(tool_name)
    if spec is None:
        return None
    key, _verb = spec
    value = args.get(key)
    if not isinstance(value, str) or not value.strip():
        return None
    cleaned = value.strip()
    # Paths get basenamed so the summary stays compact.
    if key == "path":
        cleaned = Path(cleaned).name or cleaned
    return _truncate(cleaned, 80)


def _format_summary(pending: _PendingTool | None, intent: str | None) -> str | None:
    if pending is not None:
        spec = _TOOL_VERBS.get(pending.tool_name)
        verb = spec[1] if spec is not None else pending.tool_name
        if pending.target:
            # "waiting: Which DB?" reads better than "waiting Which DB?".
            sep = ": " if verb.startswith("waiting") else " "
            return _truncate(f"{verb}{sep}{pending.target}", 120)
        return verb
    if intent:
        return _truncate(intent, 120)
    return None


def _truncate(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[: limit - 1].rstrip() + "…"


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


__all__ = ["AgentActivity", "CopilotActivityReader", "TranscriptLine"]


def _read_head(path: Path, size: int = 128) -> bytes:
    try:
        with path.open("rb") as fh:
            return fh.read(size)
    except OSError:
        return b""

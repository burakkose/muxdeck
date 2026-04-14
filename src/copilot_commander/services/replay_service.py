from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
import json
from typing import Literal, Protocol

from copilot_commander.domain.events import Event, LogChunk
from copilot_commander.domain.models import Session
from copilot_commander.parsers.copilot_output_parser import parse_copilot_output
from copilot_commander.services.session_service import SessionContextView, SessionReplayLookup


class ReplayLookupPort(Protocol):
    def assemble_session_context(self, session_id: str) -> SessionContextView: ...

    def lookup_for_replay(
        self,
        *,
        session_id: str | None = None,
        copilot_session_id: str | None = None,
        tmux_pane_id: str | None = None,
    ) -> SessionReplayLookup | None: ...


class ReplayStorePort(Protocol):
    def list_events_for_session(self, session_id: str, /) -> Sequence[Event]: ...

    def list_log_chunks(self, session_id: str, /) -> Sequence[LogChunk]: ...


@dataclass(frozen=True, slots=True)
class ReplayEntry:
    kind: Literal["event", "log"]
    timestamp: datetime
    ordinal: int
    session_id: str
    agent_id: str | None
    event: Event | None = None
    log_chunk: LogChunk | None = None


@dataclass(frozen=True, slots=True)
class ReplayJumpMarker:
    index: int
    timestamp: datetime
    label: str
    kind: str


@dataclass(frozen=True, slots=True)
class SessionReplay:
    session: Session
    context: SessionContextView
    entries: tuple[ReplayEntry, ...]
    jump_markers: tuple[ReplayJumpMarker, ...]


class ReplayService:
    def __init__(
        self,
        *,
        store: ReplayStorePort,
        sessions: ReplayLookupPort,
    ) -> None:
        self._store = store
        self._sessions = sessions

    def load_session_replay(self, session_id: str) -> SessionReplay:
        context = self._sessions.assemble_session_context(session_id)
        entries = self._build_entries(
            session_id=session_id,
            events=self._store.list_events_for_session(session_id),
            log_chunks=self._store.list_log_chunks(session_id),
        )
        replay = SessionReplay(
            session=context.session,
            context=context,
            entries=entries,
            jump_markers=(),
        )
        return SessionReplay(
            session=replay.session,
            context=replay.context,
            entries=replay.entries,
            jump_markers=self.build_jump_markers(replay),
        )

    def load_replay_by_locator(
        self,
        *,
        session_id: str | None = None,
        copilot_session_id: str | None = None,
        tmux_pane_id: str | None = None,
    ) -> SessionReplay:
        lookup = self._sessions.lookup_for_replay(
            session_id=session_id,
            copilot_session_id=copilot_session_id,
            tmux_pane_id=tmux_pane_id,
        )
        if lookup is None:
            missing = session_id or copilot_session_id or tmux_pane_id or "unknown"
            raise LookupError(f"no replayable session found for {missing}")
        return self.load_session_replay(lookup.session.id)

    def build_jump_markers(self, replay: SessionReplay) -> tuple[ReplayJumpMarker, ...]:
        markers: list[ReplayJumpMarker] = []
        for index, entry in enumerate(replay.entries):
            if entry.event is not None:
                markers.append(
                    ReplayJumpMarker(
                        index=index,
                        timestamp=entry.timestamp,
                        label=entry.event.kind,
                        kind="event",
                    )
                )
                continue
            if entry.log_chunk is None:
                continue
            parsed = parse_copilot_output(entry.log_chunk.content)
            for boundary in parsed.boundaries:
                markers.append(
                    ReplayJumpMarker(
                        index=index,
                        timestamp=entry.timestamp,
                        label=boundary.kind,
                        kind="boundary",
                    )
                )
            for issue in parsed.blocking_issues:
                markers.append(
                    ReplayJumpMarker(
                        index=index,
                        timestamp=entry.timestamp,
                        label=issue.kind,
                        kind="blocking",
                    )
                )
            for error in parsed.errors:
                markers.append(
                    ReplayJumpMarker(
                        index=index,
                        timestamp=entry.timestamp,
                        label=error.message,
                        kind="error",
                    )
                )
        return tuple(markers)

    def export_transcript_text(self, replay: SessionReplay) -> str:
        return "\n".join(self.export_transcript_lines(replay))

    def export_transcript_lines(self, replay: SessionReplay) -> tuple[str, ...]:
        lines: list[str] = []
        for entry in replay.entries:
            timestamp = entry.timestamp.isoformat()
            if entry.event is not None:
                payload = self._normalize_payload(entry.event.payload_json)
                lines.append(
                    f"{timestamp} EVENT {entry.event.kind} [{entry.event.severity}] {payload}"
                )
                continue
            if entry.log_chunk is None:
                continue
            header = f"{timestamp} LOG {entry.log_chunk.source}#{entry.log_chunk.sequence_no}"
            lines.append(header)
            for content_line in entry.log_chunk.content.splitlines():
                lines.append(f"  {content_line}")
        return tuple(lines)

    def _build_entries(
        self,
        *,
        session_id: str,
        events: Sequence[Event],
        log_chunks: Sequence[LogChunk],
    ) -> tuple[ReplayEntry, ...]:
        unsorted: list[tuple[datetime, int, int, ReplayEntry]] = []
        for event_index, event in enumerate(events):
            unsorted.append(
                (
                    event.occurred_at,
                    0,
                    event_index,
                    ReplayEntry(
                        kind="event",
                        timestamp=event.occurred_at,
                        ordinal=0,
                        session_id=session_id,
                        agent_id=event.agent_id,
                        event=event,
                    ),
                )
            )
        for chunk_index, chunk in enumerate(log_chunks):
            unsorted.append(
                (
                    chunk.captured_at,
                    1,
                    chunk_index,
                    ReplayEntry(
                        kind="log",
                        timestamp=chunk.captured_at,
                        ordinal=0,
                        session_id=session_id,
                        agent_id=chunk.agent_id,
                        log_chunk=chunk,
                    ),
                )
            )
        ordered = sorted(unsorted, key=lambda item: (item[0], item[1], item[2]))
        entries: list[ReplayEntry] = []
        for ordinal, (_, _, _, entry) in enumerate(ordered):
            entries.append(
                ReplayEntry(
                    kind=entry.kind,
                    timestamp=entry.timestamp,
                    ordinal=ordinal,
                    session_id=entry.session_id,
                    agent_id=entry.agent_id,
                    event=entry.event,
                    log_chunk=entry.log_chunk,
                )
            )
        return tuple(entries)

    def _normalize_payload(self, payload_json: str) -> str:
        try:
            payload = json.loads(payload_json)
        except json.JSONDecodeError:
            return payload_json
        return json.dumps(payload, sort_keys=True, separators=(",", ":"))


__all__ = [
    "ReplayEntry",
    "ReplayJumpMarker",
    "ReplayService",
    "SessionReplay",
]

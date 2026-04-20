from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol

from muxdeck.domain.events import Event, LogChunk
from muxdeck.domain.models import Session
from muxdeck.parsers.copilot_output_parser import parse_copilot_output
from muxdeck.services.session_service import SessionContextView, SessionReplayLookup


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


@dataclass(frozen=True, slots=True)
class MultiSessionReplay:
    sessions: tuple[Session, ...]
    contexts: tuple[SessionContextView, ...]
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

    def load_multi_session_replay(self, session_ids: Sequence[str]) -> MultiSessionReplay:
        if not session_ids:
            msg = "load_multi_session_replay requires at least one session id"
            raise ValueError(msg)
        seen: set[str] = set()
        ordered_ids: list[str] = []
        for session_id in session_ids:
            if session_id in seen:
                continue
            seen.add(session_id)
            ordered_ids.append(session_id)
        sessions: list[Session] = []
        contexts: list[SessionContextView] = []
        all_entries: list[ReplayEntry] = []
        for session_id in ordered_ids:
            context = self._sessions.assemble_session_context(session_id)
            sessions.append(context.session)
            contexts.append(context)
            entries = self._build_entries(
                session_id=session_id,
                events=self._store.list_events_for_session(session_id),
                log_chunks=self._store.list_log_chunks(session_id),
            )
            all_entries.extend(entries)
        merged = self._merge_and_reordinal(all_entries)
        replay = MultiSessionReplay(
            sessions=tuple(sessions),
            contexts=tuple(contexts),
            entries=merged,
            jump_markers=(),
        )
        return MultiSessionReplay(
            sessions=replay.sessions,
            contexts=replay.contexts,
            entries=replay.entries,
            jump_markers=self.build_multi_jump_markers(replay),
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
        return self._build_entry_markers(replay.entries)

    def build_multi_jump_markers(self, replay: MultiSessionReplay) -> tuple[ReplayJumpMarker, ...]:
        markers = list(self._build_entry_markers(replay.entries))
        distinct_agents = {entry.agent_id for entry in replay.entries if entry.agent_id is not None}
        if len(distinct_agents) <= 1:
            return tuple(sorted(markers, key=lambda marker: (marker.index, marker.kind)))
        previous_agent: str | None = None
        for index, entry in enumerate(replay.entries):
            current_agent = entry.agent_id
            if current_agent is None:
                continue
            if previous_agent is not None and current_agent != previous_agent:
                markers.append(
                    ReplayJumpMarker(
                        index=index,
                        timestamp=entry.timestamp,
                        label=f"{previous_agent}→{current_agent}",
                        kind="agent_switch",
                    )
                )
            previous_agent = current_agent
        return tuple(sorted(markers, key=lambda marker: (marker.index, marker.kind)))

    def _build_entry_markers(self, entries: Sequence[ReplayEntry]) -> tuple[ReplayJumpMarker, ...]:
        markers: list[ReplayJumpMarker] = []
        for index, entry in enumerate(entries):
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
            for activity in parsed.activity_markers:
                markers.append(
                    ReplayJumpMarker(
                        index=index,
                        timestamp=entry.timestamp,
                        label=activity.activity,
                        kind="activity",
                    )
                )
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
            for mutation in parsed.file_mutations:
                markers.append(
                    ReplayJumpMarker(
                        index=index,
                        timestamp=entry.timestamp,
                        label=mutation.path,
                        kind="file_edit",
                    )
                )
            for tool in parsed.tool_calls:
                markers.append(
                    ReplayJumpMarker(
                        index=index,
                        timestamp=entry.timestamp,
                        label=tool.name,
                        kind="tool_call",
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
        entries = list(
            self._iter_entries(session_id=session_id, events=events, log_chunks=log_chunks)
        )
        return self._merge_and_reordinal(entries)

    def _merge_and_reordinal(self, entries: Sequence[ReplayEntry]) -> tuple[ReplayEntry, ...]:
        ordered = sorted(
            enumerate(entries),
            key=lambda item: (
                item[1].timestamp,
                0 if item[1].kind == "event" else 1,
                item[1].session_id,
                item[0],
            ),
        )
        return tuple(
            ReplayEntry(
                kind=entry.kind,
                timestamp=entry.timestamp,
                ordinal=ordinal,
                session_id=entry.session_id,
                agent_id=entry.agent_id,
                event=entry.event,
                log_chunk=entry.log_chunk,
            )
            for ordinal, (_, entry) in enumerate(ordered)
        )

    def _iter_entries(
        self,
        *,
        session_id: str,
        events: Sequence[Event],
        log_chunks: Sequence[LogChunk],
    ) -> Sequence[ReplayEntry]:
        out: list[ReplayEntry] = []
        for event in events:
            out.append(
                ReplayEntry(
                    kind="event",
                    timestamp=event.occurred_at,
                    ordinal=0,
                    session_id=session_id,
                    agent_id=event.agent_id,
                    event=event,
                )
            )
        for chunk in log_chunks:
            out.append(
                ReplayEntry(
                    kind="log",
                    timestamp=chunk.captured_at,
                    ordinal=0,
                    session_id=session_id,
                    agent_id=chunk.agent_id,
                    log_chunk=chunk,
                )
            )
        return out

    def _normalize_payload(self, payload_json: str) -> str:
        try:
            payload = json.loads(payload_json)
        except json.JSONDecodeError:
            return payload_json
        return json.dumps(payload, sort_keys=True, separators=(",", ":"))


__all__ = [
    "MultiSessionReplay",
    "ReplayEntry",
    "ReplayJumpMarker",
    "ReplayService",
    "SessionReplay",
]

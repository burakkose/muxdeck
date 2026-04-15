from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Literal

from copilot_commander.services.replay_service import ReplayEntry, ReplayService, SessionReplay

ReplayExportFormat = Literal["text", "json"]


@dataclass(frozen=True, slots=True)
class ReplayJumpMarkerView:
    index: int
    timestamp: str
    label: str
    kind: str


@dataclass(frozen=True, slots=True)
class ReplayTranscriptEntryView:
    ordinal: int
    kind: str
    timestamp: str
    label: str
    severity: str | None
    lines: tuple[str, ...]
    is_selected: bool


@dataclass(frozen=True, slots=True)
class ReplayExportIntent:
    session_id: str
    format: ReplayExportFormat
    filename_hint: str
    content: str


@dataclass(frozen=True, slots=True)
class ReplayStateView:
    session_id: str
    agent_id: str
    task_title: str | None
    selected_index: int | None
    transcript: tuple[ReplayTranscriptEntryView, ...]
    jump_markers: tuple[ReplayJumpMarkerView, ...]


class ReplayController:
    def __init__(self, service: ReplayService) -> None:
        self._service = service

    def load_state(
        self,
        *,
        session_id: str | None = None,
        copilot_session_id: str | None = None,
        tmux_pane_id: str | None = None,
        selected_index: int | None = None,
    ) -> ReplayStateView:
        replay = self._service.load_replay_by_locator(
            session_id=session_id,
            copilot_session_id=copilot_session_id,
            tmux_pane_id=tmux_pane_id,
        )
        return self._build_state(replay, selected_index=selected_index)

    def jump_to_marker(self, state: ReplayStateView, marker_ordinal: int) -> ReplayStateView:
        marker = state.jump_markers[marker_ordinal]
        return ReplayStateView(
            session_id=state.session_id,
            agent_id=state.agent_id,
            task_title=state.task_title,
            selected_index=marker.index,
            transcript=tuple(
                ReplayTranscriptEntryView(
                    ordinal=entry.ordinal,
                    kind=entry.kind,
                    timestamp=entry.timestamp,
                    label=entry.label,
                    severity=entry.severity,
                    lines=entry.lines,
                    is_selected=entry.ordinal == marker.index,
                )
                for entry in state.transcript
            ),
            jump_markers=state.jump_markers,
        )

    def build_export_intent(
        self,
        state: ReplayStateView,
        *,
        export_format: ReplayExportFormat = "text",
    ) -> ReplayExportIntent:
        if export_format == "text":
            content = "\n".join(self._flatten_transcript(state.transcript))
            suffix = "txt"
        else:
            content = json.dumps(
                {
                    "session_id": state.session_id,
                    "agent_id": state.agent_id,
                    "task_title": state.task_title,
                    "selected_index": state.selected_index,
                    "transcript": [
                        {
                            "ordinal": entry.ordinal,
                            "kind": entry.kind,
                            "timestamp": entry.timestamp,
                            "label": entry.label,
                            "severity": entry.severity,
                            "lines": entry.lines,
                        }
                        for entry in state.transcript
                    ],
                    "jump_markers": [
                        {
                            "index": marker.index,
                            "timestamp": marker.timestamp,
                            "label": marker.label,
                            "kind": marker.kind,
                        }
                        for marker in state.jump_markers
                    ],
                },
                sort_keys=True,
                indent=2,
            )
            suffix = "json"
        return ReplayExportIntent(
            session_id=state.session_id,
            format=export_format,
            filename_hint=f"replay-{state.session_id}.{suffix}",
            content=content,
        )

    def _build_state(
        self,
        replay: SessionReplay,
        *,
        selected_index: int | None,
    ) -> ReplayStateView:
        transcript = tuple(
            self._build_transcript_entry(entry, selected_index=selected_index)
            for entry in replay.entries
        )
        markers = tuple(
            ReplayJumpMarkerView(
                index=marker.index,
                timestamp=marker.timestamp.isoformat(),
                label=marker.label,
                kind=marker.kind,
            )
            for marker in replay.jump_markers
        )
        return ReplayStateView(
            session_id=replay.session.id,
            agent_id=replay.session.agent_id,
            task_title=replay.session.task_title,
            selected_index=selected_index,
            transcript=transcript,
            jump_markers=markers,
        )

    def _build_transcript_entry(
        self,
        entry: ReplayEntry,
        *,
        selected_index: int | None,
    ) -> ReplayTranscriptEntryView:
        if entry.event is not None:
            label = entry.event.kind
            severity = entry.event.severity
            lines: tuple[str, ...] = (entry.event.payload_json,)
        elif entry.log_chunk is not None:
            label = f"{entry.log_chunk.source}#{entry.log_chunk.sequence_no}"
            severity = None
            lines = tuple(entry.log_chunk.content.splitlines())
        else:
            msg = "replay entry is missing both event and log chunk"
            raise ValueError(msg)
        return ReplayTranscriptEntryView(
            ordinal=entry.ordinal,
            kind=entry.kind,
            timestamp=entry.timestamp.isoformat(),
            label=label,
            severity=severity,
            lines=lines,
            is_selected=entry.ordinal == selected_index,
        )

    def _flatten_transcript(
        self,
        transcript: tuple[ReplayTranscriptEntryView, ...],
    ) -> tuple[str, ...]:
        lines: list[str] = []
        for entry in transcript:
            lines.append(f"{entry.timestamp} {entry.kind.upper()} {entry.label}")
            lines.extend(f"  {line}" for line in entry.lines)
        return tuple(lines)


__all__ = [
    "ReplayController",
    "ReplayExportIntent",
    "ReplayJumpMarkerView",
    "ReplayStateView",
    "ReplayTranscriptEntryView",
]

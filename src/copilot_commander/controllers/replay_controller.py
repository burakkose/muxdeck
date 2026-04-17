from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Literal

from copilot_commander.domain.replay_query import (
    build_chip_filter_text,
    parse_replay_query,
    query_matches,
)
from copilot_commander.parsers.copilot_output_parser import parse_copilot_output
from copilot_commander.services.replay_insights import (
    ReplayInsightsView,
    compute_replay_insights,
)
from copilot_commander.services.replay_service import ReplayEntry, ReplayService, SessionReplay

ReplayExportFormat = Literal["text", "json"]
ReplayPresentation = Literal["parsed", "raw"]

_ACTIVITY_MARKER_KINDS = frozenset({"activity"})
_PROBLEM_MARKER_KINDS = frozenset({"blocking", "error"})


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
    marker_kind: str | None
    lines: tuple[str, ...]
    is_selected: bool
    agent_id: str | None = None


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
    presentation: ReplayPresentation
    filter_text: str
    follow_latest: bool
    total_entries: int
    total_markers: int
    insights: ReplayInsightsView | None = None


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
        filter_text: str = "",
        presentation: ReplayPresentation = "parsed",
        follow_latest: bool = False,
    ) -> ReplayStateView:
        replay = self._service.load_replay_by_locator(
            session_id=session_id,
            copilot_session_id=copilot_session_id,
            tmux_pane_id=tmux_pane_id,
        )
        return self._build_state(
            replay,
            selected_index=selected_index,
            filter_text=filter_text,
            presentation=presentation,
            follow_latest=follow_latest,
        )

    def jump_to_marker(self, state: ReplayStateView, marker_ordinal: int) -> ReplayStateView:
        marker = state.jump_markers[marker_ordinal]
        return self._with_selection(state, marker.index)

    def jump_to_next_marker(self, state: ReplayStateView) -> ReplayStateView | None:
        return self._jump_relative_marker(state, direction=1)

    def jump_to_previous_marker(self, state: ReplayStateView) -> ReplayStateView | None:
        return self._jump_relative_marker(state, direction=-1)

    def jump_to_next_activity(self, state: ReplayStateView) -> ReplayStateView | None:
        return self._jump_relative_marker(state, direction=1, kinds=_ACTIVITY_MARKER_KINDS)

    def jump_to_next_problem(self, state: ReplayStateView) -> ReplayStateView | None:
        return self._jump_relative_marker(state, direction=1, kinds=_PROBLEM_MARKER_KINDS)

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
                    "presentation": state.presentation,
                    "filter_text": state.filter_text,
                    "follow_latest": state.follow_latest,
                    "transcript": [
                        {
                            "ordinal": entry.ordinal,
                            "kind": entry.kind,
                            "timestamp": entry.timestamp,
                            "label": entry.label,
                            "severity": entry.severity,
                            "marker_kind": entry.marker_kind,
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

    def _jump_relative_marker(
        self,
        state: ReplayStateView,
        *,
        direction: Literal[-1, 1],
        kinds: frozenset[str] | None = None,
    ) -> ReplayStateView | None:
        markers = tuple(
            marker for marker in state.jump_markers if kinds is None or marker.kind in kinds
        )
        if not markers:
            return None
        current_index = state.selected_index if state.selected_index is not None else -1
        if direction > 0:
            target = next(
                (marker for marker in markers if marker.index > current_index),
                markers[0],
            )
            return self._with_selection(state, target.index)
        target = next(
            (marker for marker in reversed(markers) if marker.index < current_index),
            markers[-1],
        )
        return self._with_selection(state, target.index)

    def _with_selection(
        self,
        state: ReplayStateView,
        selected_index: int | None,
    ) -> ReplayStateView:
        return ReplayStateView(
            session_id=state.session_id,
            agent_id=state.agent_id,
            task_title=state.task_title,
            selected_index=selected_index,
            transcript=tuple(
                ReplayTranscriptEntryView(
                    ordinal=entry.ordinal,
                    kind=entry.kind,
                    timestamp=entry.timestamp,
                    label=entry.label,
                    severity=entry.severity,
                    marker_kind=entry.marker_kind,
                    lines=entry.lines,
                    is_selected=entry.ordinal == selected_index,
                    agent_id=entry.agent_id,
                )
                for entry in state.transcript
            ),
            jump_markers=state.jump_markers,
            presentation=state.presentation,
            filter_text=state.filter_text,
            follow_latest=state.follow_latest,
            total_entries=state.total_entries,
            total_markers=state.total_markers,
            insights=state.insights,
        )

    def _build_state(
        self,
        replay: SessionReplay,
        *,
        selected_index: int | None,
        filter_text: str,
        presentation: ReplayPresentation,
        follow_latest: bool,
    ) -> ReplayStateView:
        query = parse_replay_query(filter_text)
        initial_selection = (
            replay.entries[-1].ordinal if follow_latest and replay.entries else selected_index
        )
        transcript = tuple(
            self._build_transcript_entry(
                entry,
                presentation=presentation,
                selected_index=initial_selection,
            )
            for entry in replay.entries
        )
        if not query.is_empty:
            transcript = tuple(entry for entry in transcript if query_matches(query, entry))
        resolved_selection = initial_selection
        if resolved_selection not in {entry.ordinal for entry in transcript}:
            if transcript:
                resolved_selection = (
                    transcript[-1].ordinal if follow_latest else transcript[0].ordinal
                )
            else:
                resolved_selection = None
        if resolved_selection != initial_selection:
            transcript = tuple(
                ReplayTranscriptEntryView(
                    ordinal=entry.ordinal,
                    kind=entry.kind,
                    timestamp=entry.timestamp,
                    label=entry.label,
                    severity=entry.severity,
                    marker_kind=entry.marker_kind,
                    lines=entry.lines,
                    is_selected=entry.ordinal == resolved_selection,
                    agent_id=entry.agent_id,
                )
                for entry in transcript
            )
        visible_ordinals = {entry.ordinal for entry in transcript}
        markers = tuple(
            ReplayJumpMarkerView(
                index=marker.index,
                timestamp=marker.timestamp.isoformat(),
                label=marker.label,
                kind=marker.kind,
            )
            for marker in replay.jump_markers
            if marker.index in visible_ordinals
        )
        insights = compute_replay_insights(replay.entries)
        return ReplayStateView(
            session_id=replay.session.id,
            agent_id=replay.session.agent_id,
            task_title=replay.session.task_title,
            selected_index=resolved_selection,
            transcript=transcript,
            jump_markers=markers,
            presentation=presentation,
            filter_text=filter_text,
            follow_latest=follow_latest,
            total_entries=len(replay.entries),
            total_markers=len(replay.jump_markers),
            insights=insights,
        )

    def _build_transcript_entry(
        self,
        entry: ReplayEntry,
        *,
        presentation: ReplayPresentation,
        selected_index: int | None,
    ) -> ReplayTranscriptEntryView:
        marker_kind: str | None = None
        if entry.event is not None:
            label = entry.event.kind
            severity = entry.event.severity
            lines: tuple[str, ...] = (self._normalize_json(entry.event.payload_json),)
        elif entry.log_chunk is not None:
            raw_lines = tuple(entry.log_chunk.content.splitlines())
            parsed_label, parsed_lines, marker_kind = self._build_parsed_log_view(entry)
            label = (
                parsed_label
                if presentation == "parsed"
                else f"{entry.log_chunk.source}#{entry.log_chunk.sequence_no}"
            )
            severity = self._severity_for_marker(marker_kind)
            lines = parsed_lines if presentation == "parsed" else raw_lines
            if not lines and not raw_lines:
                # Genuinely empty chunk — keep a hint for the detail
                # panel. Don't fire this when ``parsed_lines`` collapsed
                # to nothing because every parsed signal was redundant
                # with the label/kind columns; in that case the header
                # alone is cleaner than synthetic placeholder text.
                lines = ("(empty log chunk)",)
        else:
            msg = "replay entry is missing both event and log chunk"
            raise ValueError(msg)
        return ReplayTranscriptEntryView(
            ordinal=entry.ordinal,
            kind=entry.kind,
            timestamp=entry.timestamp.isoformat(),
            label=label,
            severity=severity,
            marker_kind=marker_kind,
            lines=lines,
            is_selected=entry.ordinal == selected_index,
            agent_id=entry.agent_id,
        )

    def _build_parsed_log_view(
        self,
        entry: ReplayEntry,
    ) -> tuple[str, tuple[str, ...], str | None]:
        if entry.log_chunk is None:
            msg = "expected log chunk"
            raise ValueError(msg)
        parsed = parse_copilot_output(entry.log_chunk.content)
        raw_lines = tuple(line for line in entry.log_chunk.content.splitlines() if line.strip())
        signals: list[tuple[str, str]] = []
        signals.extend(("error", error.message) for error in parsed.errors)
        signals.extend(("blocking", issue.kind) for issue in parsed.blocking_issues)
        signals.extend(("activity", marker.activity) for marker in parsed.activity_markers)
        signals.extend(("boundary", boundary.kind) for boundary in parsed.boundaries)
        if signals:
            label_kind, label = signals[0]
            signal_lines = tuple(f"{kind}: {value}" for kind, value in signals)
            # The transcript widget already displays ``marker_kind`` and
            # ``label`` as separate columns, so the first signal — which
            # is always ``f"{label_kind}: {label}"`` — is pure visual
            # noise when rendered as a preview/detail line. Drop that
            # redundant entry but keep any additional signals, which
            # carry distinct ``kind``/``value`` pairs worth surfacing.
            redundant = f"{label_kind}: {label}"
            lines = tuple(line for line in signal_lines if line != redundant)
            return label, lines, label_kind
        fallback = raw_lines[:3] if raw_lines else ("(no parsed markers)",)
        return f"{entry.log_chunk.source}#{entry.log_chunk.sequence_no}", fallback, None

    def _severity_for_marker(self, marker_kind: str | None) -> str | None:
        if marker_kind == "error":
            return "error"
        if marker_kind == "blocking":
            return "warning"
        return None

    @staticmethod
    def apply_errors_only_chip() -> str:
        """Filter-text snippet for the *errors only* quick filter."""

        return build_chip_filter_text("errors_only")

    @staticmethod
    def apply_activity_chip() -> str:
        """Filter-text snippet for the *activity only* quick filter."""

        return build_chip_filter_text("activity")

    @staticmethod
    def apply_tool_calls_chip() -> str:
        """Filter-text snippet for the *tool calls* quick filter."""

        return build_chip_filter_text("tool_calls")

    @staticmethod
    def clear_chips() -> str:
        """Filter-text snippet that clears all chips."""

        return build_chip_filter_text("clear")

    def _normalize_json(self, payload_json: str) -> str:
        try:
            payload = json.loads(payload_json)
        except json.JSONDecodeError:
            return payload_json
        return json.dumps(payload, sort_keys=True, indent=2)

    def _flatten_transcript(
        self,
        transcript: tuple[ReplayTranscriptEntryView, ...],
    ) -> tuple[str, ...]:
        lines: list[str] = []
        for entry in transcript:
            marker = f" {entry.marker_kind}" if entry.marker_kind else ""
            lines.append(f"{entry.timestamp} {entry.kind.upper()}{marker} {entry.label}")
            lines.extend(f"  {line}" for line in entry.lines)
        return tuple(lines)


__all__ = [
    "ReplayController",
    "ReplayExportIntent",
    "ReplayJumpMarkerView",
    "ReplayPresentation",
    "ReplayStateView",
    "ReplayTranscriptEntryView",
]

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Literal

from copilot_commander.parsers.copilot_output_parser import parse_copilot_output
from copilot_commander.services.annotations_service import AnnotationsService
from copilot_commander.services.replay_service import ReplayEntry, ReplayService, SessionReplay

ReplayExportFormat = Literal["text", "json", "markdown"]
ReplayPresentation = Literal["parsed", "raw"]

_ACTIVITY_MARKER_KINDS = frozenset({"activity"})
_PROBLEM_MARKER_KINDS = frozenset({"blocking", "error"})
_ANNOTATION_MARKER_KINDS = frozenset({"annotation"})

_BOOKMARK_GLYPH = "✱"
_NOTE_GLYPH = "✎"


@dataclass(frozen=True, slots=True)
class ReplayJumpMarkerView:
    index: int
    timestamp: str
    label: str
    kind: str


@dataclass(frozen=True, slots=True)
class ReplayAnnotationView:
    id: str
    ordinal: int
    kind: Literal["bookmark", "note"]
    body: str
    created_at: str


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
    annotation_glyph: str | None = None


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
    annotations: tuple[ReplayAnnotationView, ...] = ()


class ReplayController:
    def __init__(
        self,
        service: ReplayService,
        annotations: AnnotationsService | None = None,
    ) -> None:
        self._service = service
        self._annotations = annotations

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

    def list_annotations(self, session_id: str) -> tuple[ReplayAnnotationView, ...]:
        return tuple(self._fetch_annotation_views(session_id))

    def toggle_bookmark(self, session_id: str, ordinal: int) -> bool:
        if self._annotations is None:
            msg = "annotations service is not configured"
            raise RuntimeError(msg)
        return self._annotations.toggle_bookmark(session_id, ordinal)

    def add_note(self, session_id: str, ordinal: int, body: str) -> ReplayAnnotationView:
        if self._annotations is None:
            msg = "annotations service is not configured"
            raise RuntimeError(msg)
        annotation = self._annotations.add_note(session_id, ordinal, body)
        return ReplayAnnotationView(
            id=annotation.id,
            ordinal=annotation.ordinal,
            kind=annotation.kind,
            body=annotation.body,
            created_at=annotation.created_at.isoformat(),
        )

    def delete_annotation(self, annotation_id: str) -> bool:
        if self._annotations is None:
            msg = "annotations service is not configured"
            raise RuntimeError(msg)
        return self._annotations.delete(annotation_id)

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

    def jump_to_next_annotation(self, state: ReplayStateView) -> ReplayStateView | None:
        return self._jump_relative_marker(state, direction=1, kinds=_ANNOTATION_MARKER_KINDS)

    def build_export_intent(
        self,
        state: ReplayStateView,
        *,
        export_format: ReplayExportFormat = "text",
        range: tuple[int, int] | None = None,  # noqa: A002 - intentional parameter name
        include_annotations: bool = False,
    ) -> ReplayExportIntent:
        transcript = self._slice_transcript(state.transcript, range)
        annotations = self._slice_annotations(state.annotations, range)
        if export_format == "text":
            content = "\n".join(
                self._flatten_transcript(
                    transcript,
                    annotations=annotations if include_annotations else (),
                )
            )
            suffix = "txt"
        elif export_format == "markdown":
            content = self._render_markdown(
                state,
                transcript=transcript,
                annotations=annotations if include_annotations else (),
                ordinal_range=range,
            )
            suffix = "md"
        else:
            payload: dict[str, object] = {
                "session_id": state.session_id,
                "agent_id": state.agent_id,
                "task_title": state.task_title,
                "selected_index": state.selected_index,
                "presentation": state.presentation,
                "filter_text": state.filter_text,
                "follow_latest": state.follow_latest,
                "ordinal_range": list(range) if range is not None else None,
                "transcript": [
                    {
                        "ordinal": entry.ordinal,
                        "kind": entry.kind,
                        "timestamp": entry.timestamp,
                        "label": entry.label,
                        "severity": entry.severity,
                        "marker_kind": entry.marker_kind,
                        "lines": list(entry.lines),
                        "annotation_glyph": entry.annotation_glyph,
                    }
                    for entry in transcript
                ],
                "jump_markers": [
                    {
                        "index": marker.index,
                        "timestamp": marker.timestamp,
                        "label": marker.label,
                        "kind": marker.kind,
                    }
                    for marker in state.jump_markers
                    if range is None or range[0] <= marker.index <= range[1]
                ],
            }
            if include_annotations:
                payload["annotations"] = [
                    {
                        "id": annotation.id,
                        "ordinal": annotation.ordinal,
                        "kind": annotation.kind,
                        "body": annotation.body,
                        "created_at": annotation.created_at,
                    }
                    for annotation in annotations
                ]
            content = json.dumps(payload, sort_keys=True, indent=2)
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
                    annotation_glyph=entry.annotation_glyph,
                )
                for entry in state.transcript
            ),
            jump_markers=state.jump_markers,
            presentation=state.presentation,
            filter_text=state.filter_text,
            follow_latest=state.follow_latest,
            total_entries=state.total_entries,
            total_markers=state.total_markers,
            annotations=state.annotations,
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
        annotation_views = tuple(self._fetch_annotation_views(replay.session.id))
        glyphs_by_ordinal = self._glyphs_by_ordinal(annotation_views)
        query = filter_text.strip().casefold()
        initial_selection = (
            replay.entries[-1].ordinal if follow_latest and replay.entries else selected_index
        )
        transcript = tuple(
            self._build_transcript_entry(
                entry,
                presentation=presentation,
                selected_index=initial_selection,
                annotation_glyph=glyphs_by_ordinal.get(entry.ordinal),
            )
            for entry in replay.entries
        )
        if query:
            transcript = tuple(entry for entry in transcript if query in self._search_blob(entry))
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
                    annotation_glyph=entry.annotation_glyph,
                )
                for entry in transcript
            )
        visible_ordinals = {entry.ordinal for entry in transcript}
        markers: list[ReplayJumpMarkerView] = [
            ReplayJumpMarkerView(
                index=marker.index,
                timestamp=marker.timestamp.isoformat(),
                label=marker.label,
                kind=marker.kind,
            )
            for marker in replay.jump_markers
            if marker.index in visible_ordinals
        ]
        markers.extend(
            ReplayJumpMarkerView(
                index=annotation.ordinal,
                timestamp=annotation.created_at,
                label=self._annotation_marker_label(annotation),
                kind="annotation",
            )
            for annotation in annotation_views
            if annotation.ordinal in visible_ordinals
        )
        all_marker_count = len(replay.jump_markers) + len(annotation_views)
        return ReplayStateView(
            session_id=replay.session.id,
            agent_id=replay.session.agent_id,
            task_title=replay.session.task_title,
            selected_index=resolved_selection,
            transcript=transcript,
            jump_markers=tuple(markers),
            presentation=presentation,
            filter_text=filter_text,
            follow_latest=follow_latest,
            total_entries=len(replay.entries),
            total_markers=all_marker_count,
            annotations=annotation_views,
        )

    def _fetch_annotation_views(self, session_id: str) -> list[ReplayAnnotationView]:
        if self._annotations is None:
            return []
        return [
            ReplayAnnotationView(
                id=annotation.id,
                ordinal=annotation.ordinal,
                kind=annotation.kind,
                body=annotation.body,
                created_at=annotation.created_at.isoformat(),
            )
            for annotation in self._annotations.list_for_session(session_id)
        ]

    def _glyphs_by_ordinal(
        self,
        annotations: tuple[ReplayAnnotationView, ...] | list[ReplayAnnotationView],
    ) -> dict[int, str]:
        glyphs: dict[int, str] = {}
        for annotation in annotations:
            glyph = _BOOKMARK_GLYPH if annotation.kind == "bookmark" else _NOTE_GLYPH
            existing = glyphs.get(annotation.ordinal)
            if existing == _BOOKMARK_GLYPH:
                # Prefer bookmark glyph when both kinds attach to the same ordinal.
                continue
            glyphs[annotation.ordinal] = glyph
        return glyphs

    def _annotation_marker_label(self, annotation: ReplayAnnotationView) -> str:
        if annotation.kind == "bookmark":
            return "bookmark"
        body = annotation.body.strip().splitlines()[0] if annotation.body.strip() else "(empty)"
        return f"note: {body}"

    def _slice_transcript(
        self,
        transcript: tuple[ReplayTranscriptEntryView, ...],
        ordinal_range: tuple[int, int] | None,
    ) -> tuple[ReplayTranscriptEntryView, ...]:
        if ordinal_range is None:
            return transcript
        low, high = sorted(ordinal_range)
        return tuple(entry for entry in transcript if low <= entry.ordinal <= high)

    def _slice_annotations(
        self,
        annotations: tuple[ReplayAnnotationView, ...],
        ordinal_range: tuple[int, int] | None,
    ) -> tuple[ReplayAnnotationView, ...]:
        if ordinal_range is None:
            return annotations
        low, high = sorted(ordinal_range)
        return tuple(annotation for annotation in annotations if low <= annotation.ordinal <= high)

    def _build_transcript_entry(
        self,
        entry: ReplayEntry,
        *,
        presentation: ReplayPresentation,
        selected_index: int | None,
        annotation_glyph: str | None,
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
            annotation_glyph=annotation_glyph,
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

    def _search_blob(self, entry: ReplayTranscriptEntryView) -> str:
        parts = (
            entry.timestamp,
            entry.kind,
            entry.label,
            entry.severity or "",
            entry.marker_kind or "",
            *entry.lines,
        )
        return "\n".join(parts).casefold()

    def _normalize_json(self, payload_json: str) -> str:
        try:
            payload = json.loads(payload_json)
        except json.JSONDecodeError:
            return payload_json
        return json.dumps(payload, sort_keys=True, indent=2)

    def _flatten_transcript(
        self,
        transcript: tuple[ReplayTranscriptEntryView, ...],
        *,
        annotations: tuple[ReplayAnnotationView, ...] = (),
    ) -> tuple[str, ...]:
        annotations_by_ordinal: dict[int, list[ReplayAnnotationView]] = {}
        for annotation in annotations:
            annotations_by_ordinal.setdefault(annotation.ordinal, []).append(annotation)
        lines: list[str] = []
        for entry in transcript:
            marker = f" {entry.marker_kind}" if entry.marker_kind else ""
            lines.append(f"{entry.timestamp} {entry.kind.upper()}{marker} {entry.label}")
            lines.extend(f"  {line}" for line in entry.lines)
            for annotation in annotations_by_ordinal.get(entry.ordinal, ()):
                lines.append(self._format_annotation_text(annotation))
        return tuple(lines)

    def _format_annotation_text(self, annotation: ReplayAnnotationView) -> str:
        if annotation.kind == "bookmark":
            return "  ★ bookmark"
        body = annotation.body.strip() or "(empty note)"
        return f"  ✎ note: {body}"

    def _render_markdown(
        self,
        state: ReplayStateView,
        *,
        transcript: tuple[ReplayTranscriptEntryView, ...],
        annotations: tuple[ReplayAnnotationView, ...],
        ordinal_range: tuple[int, int] | None,
    ) -> str:
        annotations_by_ordinal: dict[int, list[ReplayAnnotationView]] = {}
        for annotation in annotations:
            annotations_by_ordinal.setdefault(annotation.ordinal, []).append(annotation)
        lines: list[str] = []
        title = state.task_title or state.session_id
        lines.append(f"## Replay slice — {title}")
        meta = [f"agent `{state.agent_id}`", f"session `{state.session_id}`"]
        if ordinal_range is not None:
            low, high = sorted(ordinal_range)
            meta.append(f"ordinals `{low}`-`{high}`")
        lines.append("_" + ", ".join(meta) + "_")
        lines.append("")
        for entry in transcript:
            header_kind = entry.marker_kind or entry.kind
            header = f"### #{entry.ordinal} · {entry.timestamp} · {header_kind} — {entry.label}"
            lines.append(header)
            if entry.severity:
                lines.append(f"_severity: {entry.severity}_")
            if entry.lines:
                lines.append("```")
                lines.extend(entry.lines)
                lines.append("```")
            for annotation in annotations_by_ordinal.get(entry.ordinal, ()):
                lines.append(self._format_annotation_markdown(annotation))
            lines.append("")
        return "\n".join(lines).rstrip() + "\n"

    def _format_annotation_markdown(self, annotation: ReplayAnnotationView) -> str:
        if annotation.kind == "bookmark":
            return "> ★ Bookmark"
        body = annotation.body.strip() or "(empty note)"
        return "> Note: " + body.replace("\n", "\n> ")


__all__ = [
    "ReplayAnnotationView",
    "ReplayController",
    "ReplayExportFormat",
    "ReplayExportIntent",
    "ReplayJumpMarkerView",
    "ReplayPresentation",
    "ReplayStateView",
    "ReplayTranscriptEntryView",
]

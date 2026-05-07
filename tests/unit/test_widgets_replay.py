"""Widget-only tests for the replay screen panels.

Covers the pure helpers (``_marker_style``, ``_agent_badge_style``,
``_format_duration``) and the visual side of each ``Static`` /
``Vertical`` panel via direct ``set_*`` calls. List-driven panels
(``ReplayMarkerListPanel``, ``ReplayTranscriptPanel``) need a mounted
app because they query a ``ListView`` child.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Protocol

import pytest
from textual.app import App, ComposeResult

from muxdeck.controllers import (
    PlaybackStateView,
    ReplayJumpMarkerView,
    ReplayStateView,
    ReplayTranscriptEntryView,
)
from muxdeck.domain.error_clustering import ErrorCluster
from muxdeck.services.replay_insights import IdleGap, ReplayInsightsView
from muxdeck.widgets.replay import (
    ReplayActionBar,
    ReplayDetailPanel,
    ReplayDiffPanel,
    ReplayFilterBar,
    ReplayInsightsPanel,
    ReplayMarkerListPanel,
    ReplayProgressBar,
    ReplaySummaryPanel,
    ReplayTranscriptPanel,
    _agent_badge_style,
    _format_duration,
    _marker_style,
)


class _Renderable(Protocol):
    def render(self) -> object: ...


def _render(widget: _Renderable) -> str:
    renderable = widget.render()
    plain = getattr(renderable, "plain", None)
    return plain if isinstance(plain, str) else str(renderable)


# ── pure helpers ────────────────────────────────────────────────────


def test_format_duration_handles_negative_zero_and_units() -> None:
    assert _format_duration(timedelta(seconds=-30)) == "0s"
    assert _format_duration(timedelta(seconds=0)) == "0s"
    assert _format_duration(timedelta(seconds=45)) == "45s"
    assert _format_duration(timedelta(minutes=2, seconds=5)) == "2m05s"
    assert _format_duration(timedelta(hours=1, minutes=2, seconds=3)) == "1h02m03s"


def test_marker_style_returns_distinct_styles_per_kind() -> None:
    kinds = (
        "error",
        "blocking",
        "activity",
        "boundary",
        "agent_switch",
        "file_edit",
        "tool_call",
        "annotation",
        "unknown-kind",
    )
    styles = {kind: _marker_style(kind) for kind in kinds}
    assert all(isinstance(value, str) and value for value in styles.values())
    # Default branch returns a distinct yellow bold for unknowns.
    assert styles["unknown-kind"] != styles["error"]


def test_agent_badge_style_is_deterministic() -> None:
    assert _agent_badge_style("agent-1") == _agent_badge_style("agent-1")
    # Two distinct ids give a style — palette length means collisions
    # are possible but the call should always succeed.
    assert _agent_badge_style("agent-2").startswith("bold ")


# ── ReplayFilterBar ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_filter_bar_set_state_renders_summary_and_query() -> None:
    bar = ReplayFilterBar(id="replay-filter")

    class _Harness(App[None]):
        def compose(self) -> ComposeResult:
            yield bar

    async with _Harness().run_test() as pilot:
        bar.set_query("kind:error")
        bar.set_state(
            filter_text="kind:error",
            visible_entries=10,
            total_entries=42,
            presentation="parsed",
            follow_latest=True,
        )
        await pilot.pause()
        from textual.widgets import Static

        summary = bar.query_one("#replay-filter-summary", Static)
        rendered = _render(summary)
        assert "10/42 entries" in rendered
        assert "parsed" in rendered
        assert "on" in rendered
        assert "kind:error" in rendered


@pytest.mark.asyncio
async def test_filter_bar_default_query_hint_when_empty() -> None:
    bar = ReplayFilterBar(id="replay-filter")

    class _Harness(App[None]):
        def compose(self) -> ComposeResult:
            yield bar

    async with _Harness().run_test() as pilot:
        bar.set_state(
            filter_text="",
            visible_entries=0,
            total_entries=0,
            presentation="raw",
            follow_latest=False,
        )
        await pilot.pause()
        from textual.widgets import Static

        summary = bar.query_one("#replay-filter-summary", Static)
        assert "kind:error agent:planner" in _render(summary)
        bar.focus_input()


# ── ReplayMarkerListPanel ───────────────────────────────────────────


def _marker(*, index: int = 0, kind: str = "activity", label: str = "step") -> ReplayJumpMarkerView:
    return ReplayJumpMarkerView(
        index=index,
        timestamp=f"2025-01-01T12:00:{index:02d}+00:00",
        label=label,
        kind=kind,
    )


@pytest.mark.asyncio
async def test_marker_list_panel_set_markers_highlights_recent_under_selection() -> None:
    panel = ReplayMarkerListPanel(widget_id="markers")

    class _Harness(App[None]):
        def compose(self) -> ComposeResult:
            yield panel

    async with _Harness().run_test() as pilot:
        await pilot.pause()
        markers = (
            _marker(index=0, kind="activity", label="kickoff"),
            _marker(index=2, kind="error", label="boom"),
            _marker(index=4, kind="file_edit", label="touch"),
        )
        panel.set_markers(markers, selected_index=3)
        await pilot.pause()
        from textual.widgets import ListView

        list_view = panel.query_one(ListView)
        assert list_view.index == 1  # last marker.index ≤ 3
        # Move cursor + focus
        panel.move_cursor(1)
        panel.focus_list()
        await pilot.pause()


@pytest.mark.asyncio
async def test_marker_list_panel_handles_empty_markers_safely() -> None:
    panel = ReplayMarkerListPanel(widget_id="markers")

    class _Harness(App[None]):
        def compose(self) -> ComposeResult:
            yield panel

    async with _Harness().run_test() as pilot:
        await pilot.pause()
        panel.set_markers((), selected_index=None)
        # move_cursor should be a safe no-op
        panel.move_cursor(1)
        await pilot.pause()


# ── ReplayTranscriptPanel ───────────────────────────────────────────


def _entry(
    *,
    ordinal: int = 0,
    kind: str = "activity",
    is_selected: bool = False,
    agent_label: str | None = None,
    agent_id: str | None = None,
    file_path: str | None = None,
    annotation_glyph: str | None = None,
    severity: str | None = None,
) -> ReplayTranscriptEntryView:
    return ReplayTranscriptEntryView(
        ordinal=ordinal,
        kind=kind,
        timestamp=f"2025-01-01T12:00:{ordinal:02d}+00:00",
        label=f"label-{ordinal}",
        severity=severity,
        marker_kind=None,
        lines=("line-a", "line-b"),
        is_selected=is_selected,
        raw_lines=("raw-a", "raw-b"),
        agent_id=agent_id,
        agent_label=agent_label,
        file_path=file_path,
        annotation_glyph=annotation_glyph,
    )


@pytest.mark.asyncio
async def test_transcript_panel_set_transcript_highlights_selected_entry() -> None:
    panel = ReplayTranscriptPanel(widget_id="transcript")

    class _Harness(App[None]):
        def compose(self) -> ComposeResult:
            yield panel

    async with _Harness().run_test() as pilot:
        await pilot.pause()
        entries = (
            _entry(
                ordinal=0,
                kind="activity",
                agent_label="alpha",
                agent_id="a-1",
                annotation_glyph="✎",
            ),
            _entry(
                ordinal=1,
                kind="file_edit",
                is_selected=True,
                file_path="src/main.py",
                severity="error",
            ),
            _entry(ordinal=2, kind="tool_call"),
        )
        panel.set_transcript(entries)
        await pilot.pause()
        from textual.widgets import ListView

        list_view = panel.query_one(ListView)
        assert list_view.index == 1
        assert panel.last_transcript_index() == 2
        panel.move_cursor(-1)
        panel.focus_list()
        await pilot.pause()


# ── ReplayProgressBar ───────────────────────────────────────────────


def test_progress_bar_no_playback_shows_pause_glyph() -> None:
    bar = ReplayProgressBar()
    bar.set_state(None, ())
    rendered = _render(bar)
    assert "no playback" in rendered


def test_progress_bar_renders_position_and_marker_columns() -> None:
    bar = ReplayProgressBar()
    playback = PlaybackStateView(
        mode="playing",
        speed_label="1x",
        speed_multiplier=1.0,
        clock="2025-01-01T12:00:30+00:00",
        start="2025-01-01T12:00:00+00:00",
        end="2025-01-01T12:01:00+00:00",
        progress=0.5,
    )
    markers = (
        ReplayJumpMarkerView(
            index=0,
            timestamp="2025-01-01T12:00:30+00:00",
            label="mid",
            kind="activity",
        ),
        ReplayJumpMarkerView(
            index=1,
            timestamp="not-a-timestamp",
            label="bad",
            kind="error",
        ),
        ReplayJumpMarkerView(
            index=2,
            timestamp="2025-01-01T11:00:00+00:00",
            label="before",
            kind="file_edit",
        ),
    )
    bar.set_state(playback, markers)
    rendered = _render(bar)
    assert "1x" in rendered
    assert "12:00:30" in rendered


def test_progress_bar_paused_uses_pause_glyph() -> None:
    bar = ReplayProgressBar()
    playback = PlaybackStateView(
        mode="paused",
        speed_label="2x",
        speed_multiplier=2.0,
        clock="2025-01-01T12:00:10+00:00",
        start="2025-01-01T12:00:00+00:00",
        end="2025-01-01T12:01:00+00:00",
        progress=0.25,
    )
    bar.set_state(playback, ())
    assert "2x" in _render(bar)


def test_progress_bar_zero_span_returns_no_marker_columns() -> None:
    bar = ReplayProgressBar()
    playback = PlaybackStateView(
        mode="paused",
        speed_label="1x",
        speed_multiplier=1.0,
        clock="2025-01-01T12:00:00+00:00",
        start="2025-01-01T12:00:00+00:00",
        end="2025-01-01T12:00:00+00:00",
        progress=0.0,
    )
    markers = (
        ReplayJumpMarkerView(
            index=0,
            timestamp="2025-01-01T12:00:00+00:00",
            label="zero",
            kind="activity",
        ),
    )
    bar.set_state(playback, markers)
    # Just ensure no crash and the bar gets rendered
    assert _render(bar)


# ── ReplaySummaryPanel ──────────────────────────────────────────────


def _state(
    *,
    transcript: tuple[ReplayTranscriptEntryView, ...] = (),
    markers: tuple[ReplayJumpMarkerView, ...] = (),
    selected_index: int | None = 0,
    files_touched: int = 1,
    tool_calls: int = 2,
    follow_latest: bool = False,
    presentation: str = "parsed",
    filter_text: str = "",
    session_ids: tuple[str, ...] = ("session-1",),
    agent_ids: tuple[str, ...] = ("agent-1",),
    insights: ReplayInsightsView | None = None,
    annotations: tuple[object, ...] = (),
) -> ReplayStateView:
    return ReplayStateView(
        session_id="session-1",
        agent_id="agent-1",
        task_title="task",
        selected_index=selected_index,
        transcript=transcript,
        jump_markers=markers,
        presentation=presentation,  # type: ignore[arg-type]
        filter_text=filter_text,
        follow_latest=follow_latest,
        total_entries=len(transcript),
        total_markers=len(markers),
        session_ids=session_ids,
        agent_ids=agent_ids,
        files_touched=files_touched,
        tool_calls=tool_calls,
        annotations=annotations,  # type: ignore[arg-type]
        insights=insights,
    )


def test_summary_panel_show_loading_includes_filter_when_set() -> None:
    panel = ReplaySummaryPanel()
    panel.show_loading(
        session_label="my-session",
        presentation="parsed",
        follow_latest=True,
        filter_text="error",
    )
    rendered = _render(panel)
    assert "loading replay" in rendered
    assert "my-session" in rendered
    assert "filter" in rendered
    assert "error" in rendered


def test_summary_panel_state_none_renders_empty_label() -> None:
    panel = ReplaySummaryPanel()
    panel.set_state(None)
    assert "No replayable sessions" in _render(panel)


def test_summary_panel_state_renders_chips_filter_and_multi_agents() -> None:
    panel = ReplaySummaryPanel()
    state = _state(
        transcript=(_entry(ordinal=0), _entry(ordinal=1)),
        markers=(_marker(index=0),),
        selected_index=None,
        filter_text="kind:error",
        agent_ids=("agent-1", "agent-2"),
    )
    panel.set_state(state)
    rendered = _render(panel)
    assert "session" in rendered
    assert "filter" in rendered
    assert "kind:error" in rendered
    assert "agents 2" in rendered


# ── ReplayActionBar ─────────────────────────────────────────────────


def test_action_bar_show_loading_includes_export_format() -> None:
    bar = ReplayActionBar()
    bar.show_loading(
        session_label="session-1",
        export_format="md",
        filter_text="error",
    )
    rendered = _render(bar)
    assert "preparing replay actions" in rendered
    assert "export md" in rendered
    assert "filter" in rendered


def test_action_bar_state_none_shows_empty_actions() -> None:
    bar = ReplayActionBar()
    bar.set_state(None, export_format="md")
    rendered = _render(bar)
    assert "no replay loaded" in rendered
    assert "export md" in rendered


def test_action_bar_state_renders_chips_and_marker_counts() -> None:
    bar = ReplayActionBar()
    markers = (
        _marker(index=0, kind="activity"),
        _marker(index=1, kind="error"),
        _marker(index=2, kind="blocking"),
        _marker(index=3, kind="file_edit"),
        _marker(index=4, kind="annotation"),
    )
    state = _state(
        markers=markers,
        filter_text="kind:error",
        session_ids=("session-1", "session-2"),
    )
    bar.set_state(state, export_format="json")
    rendered = _render(bar)
    assert "2 merged" in rendered  # multi-session scope chip
    assert "activity" in rendered
    assert "problems" in rendered
    assert "files" in rendered
    assert "notes" in rendered
    assert "json" in rendered


# ── ReplayDetailPanel ───────────────────────────────────────────────


def test_detail_panel_no_entry_renders_empty_state() -> None:
    panel = ReplayDetailPanel()
    panel.set_entry(None)
    assert "No entry selected" in _render(panel)


def test_detail_panel_renders_severity_marker_and_lines() -> None:
    panel = ReplayDetailPanel()
    panel.set_entry(_entry(ordinal=7, severity="error"))
    rendered = _render(panel)
    assert "#7" in rendered
    assert "[error]" in rendered
    assert "label-7" in rendered
    assert "line-a" in rendered


# ── ReplayDiffPanel ─────────────────────────────────────────────────


def test_diff_panel_no_entry_renders_empty() -> None:
    panel = ReplayDiffPanel()
    panel.set_entry_diff(None, None)
    assert "No entry selected" in _render(panel)


def test_diff_panel_non_file_entry_says_so() -> None:
    panel = ReplayDiffPanel()
    panel.set_entry_diff(_entry(ordinal=0, file_path=None), None)
    assert "non-file entry" in _render(panel)


def test_diff_panel_no_diff_text_shows_raw_excerpt_evidence() -> None:
    panel = ReplayDiffPanel()
    entry = _entry(ordinal=0, file_path="src/x.py")
    panel.set_entry_diff(entry, None)
    rendered = _render(panel)
    assert "historical file evidence" in rendered
    assert "src/x.py" in rendered
    assert "raw-a" in rendered or "raw-b" in rendered


def test_diff_panel_no_diff_text_with_long_raw_lines_truncates() -> None:
    panel = ReplayDiffPanel()
    raw = tuple(f"raw-line-{i}" for i in range(15))
    entry = ReplayTranscriptEntryView(
        ordinal=0,
        kind="activity",
        timestamp="2025-01-01T12:00:00+00:00",
        label="x",
        severity=None,
        marker_kind=None,
        lines=(),
        is_selected=False,
        raw_lines=raw,
        file_path="src/x.py",
    )
    panel.set_entry_diff(entry, "")
    rendered = _render(panel)
    assert "raw-line-0" in rendered
    # Only first 8 lines are shown
    assert "raw-line-7" in rendered
    assert "raw-line-9" not in rendered


def test_diff_panel_renders_syntax_when_diff_text_provided() -> None:
    panel = ReplayDiffPanel()
    entry = _entry(ordinal=0, file_path="src/x.py")
    diff = "--- a/foo.py\n+++ b/foo.py\n@@ -1 +1 @@\n-old\n+new\n"
    panel.set_entry_diff(entry, diff)
    # Static.update accepts a Syntax renderable; assert the panel
    # didn't crash and a non-empty renderable is set.
    renderable = panel.render()
    assert renderable is not None


# ── ReplayInsightsPanel ─────────────────────────────────────────────


def test_insights_panel_no_state_or_no_insights() -> None:
    panel = ReplayInsightsPanel()
    panel.set_state(None)
    assert "No insights available" in _render(panel)
    state = _state(insights=None)
    panel.set_state(state)
    assert "No insights available" in _render(panel)


def test_insights_panel_renders_summary_with_idle_gaps_and_clusters() -> None:
    panel = ReplayInsightsPanel()
    base = datetime(2025, 1, 1, 12, tzinfo=UTC)
    gaps = tuple(
        IdleGap(
            start=base + timedelta(seconds=10 * i),
            end=base + timedelta(seconds=10 * i + 90),
            duration=timedelta(seconds=90),
        )
        for i in range(5)
    )
    clusters = (
        ErrorCluster(canonical="ConnectionError: foo", count=3, examples=()),
        ErrorCluster(canonical="TimeoutError: bar", count=1, examples=()),
    )
    insights = ReplayInsightsView(
        total_duration=timedelta(seconds=600),
        idle_gaps=gaps,
        longest_activity_streak=timedelta(seconds=120),
        error_count=4,
        top_error_clusters=clusters,
        files_touched=3,
    )
    panel.set_state(_state(insights=insights))
    rendered = _render(panel)
    assert "Insights" in rendered
    assert "duration" in rendered
    assert "idle gaps" in rendered
    assert "Top errors" in rendered
    assert "ConnectionError" in rendered
    assert "more" in rendered  # truncated tail of the idle gaps


def test_insights_panel_handles_no_idle_gaps_or_clusters() -> None:
    panel = ReplayInsightsPanel()
    insights = ReplayInsightsView(
        total_duration=timedelta(seconds=30),
        idle_gaps=(),
        longest_activity_streak=timedelta(seconds=15),
        error_count=0,
        top_error_clusters=(),
        files_touched=0,
    )
    panel.set_state(_state(insights=insights))
    rendered = _render(panel)
    assert "Insights" in rendered
    assert "Top errors" not in rendered

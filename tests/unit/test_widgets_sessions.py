"""Extra widget tests for the sessions screen.

Complements ``test_sessions_widgets.py`` by exercising the
``SessionListPanel`` (mounted), ``SessionSummaryBar`` counts and
loading paths, and the no-detail / loading branches of
``SessionActionBar``.
"""

from __future__ import annotations

from typing import Protocol

import pytest
from textual.app import App, ComposeResult

from muxdeck.controllers.sessions_controller import (
    SessionDetailView,
    SessionListItemView,
)
from muxdeck.ui_preferences import UiGlyphs, UiPreferences
from muxdeck.widgets.sessions import (
    SessionActionBar,
    SessionDetailPanel,
    SessionListPanel,
    SessionSummaryBar,
    _session_status_glyph,
)


class _Renderable(Protocol):
    def render(self) -> object: ...


def _render(widget: _Renderable) -> str:
    renderable = widget.render()
    plain = getattr(renderable, "plain", None)
    return plain if isinstance(plain, str) else str(renderable)


def _item(
    *,
    session_id: str = "session-1",
    summary: str = "Review changes",
    status: str = "active",
    branch: str = "feature/x",
    origin: str = "local",
    last_event_type: str = "agent.updated",
    cwd: str = "/repo/wt",
) -> SessionListItemView:
    return SessionListItemView(
        session_id=session_id,
        summary=summary,
        repository="repo",
        branch=branch,
        status=status,
        status_glyph="🟢",
        updated="2m ago",
        created="20m ago",
        checkpoint_count=2,
        last_event_type=last_event_type,
        cwd=cwd,
        is_resumable=True,
        origin=origin,
    )


def _detail(
    *,
    status: str = "active",
    is_resumable: bool = True,
    origin: str = "local",
    premium_requests: str | None = None,
    usage_available: bool = True,
) -> SessionDetailView:
    return SessionDetailView(
        session_id="session-1",
        summary="Review",
        repository="repo",
        branch="feature/x",
        cwd="/repo/wt",
        git_root="/repo",
        status=status,
        status_glyph="🟢",
        created_at="20m ago",
        updated_at="2m ago",
        last_event_type="agent.updated",
        last_event_at="2m ago",
        checkpoint_count=2,
        is_resumable=is_resumable,
        resume_command="copilot --resume=session-1",
        origin=origin,
        usage_summary="500 tok",
        usage_badge="500 tok",
        usage_available=usage_available,
        premium_requests=premium_requests,
    )


# ── _session_status_glyph ───────────────────────────────────────────


def test_session_status_glyph_returns_rich_or_ascii_per_preference() -> None:
    rich_prefs = UiPreferences()
    assert _session_status_glyph("active", preferences=rich_prefs) == "🟢"
    ascii_prefs = UiPreferences(glyphs=UiGlyphs.ASCII)
    assert _session_status_glyph("active", preferences=ascii_prefs) == "o"


def test_session_status_glyph_falls_back_for_unknown_status() -> None:
    rich_prefs = UiPreferences()
    assert _session_status_glyph("mystery", preferences=rich_prefs) == "⚫"
    ascii_prefs = UiPreferences(glyphs=UiGlyphs.ASCII)
    assert _session_status_glyph("mystery", preferences=ascii_prefs) == "?"


# ── SessionListPanel ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_panel_set_sessions_and_select_by_id() -> None:
    panel = SessionListPanel(widget_id="session-list")

    class _Harness(App[None]):
        def compose(self) -> ComposeResult:
            yield panel

    items = (
        _item(session_id="s-1", summary="alpha"),
        _item(session_id="s-2", summary="beta", origin="windows"),
        _item(session_id="s-3", summary="gamma", status="completed"),
    )
    async with _Harness().run_test() as pilot:
        await pilot.pause()
        panel.set_sessions(items, selected_session_id="s-2", notify=True)
        await pilot.pause()
        assert panel.selected_index == 1
        assert panel.get_selected_id() == "s-2"


@pytest.mark.asyncio
async def test_list_panel_handles_missing_selected_id_via_clamp() -> None:
    panel = SessionListPanel(widget_id="session-list")

    class _Harness(App[None]):
        def compose(self) -> ComposeResult:
            yield panel

    items = (_item(session_id="s-1"),)
    async with _Harness().run_test() as pilot:
        await pilot.pause()
        panel.selected_index = 99  # out of bounds
        panel.set_sessions(items, selected_session_id=None, notify=False)
        await pilot.pause()
        assert panel.selected_index == 0


@pytest.mark.asyncio
async def test_list_panel_get_selected_id_none_when_empty() -> None:
    panel = SessionListPanel(widget_id="session-list")
    assert panel.get_selected_id() is None


@pytest.mark.asyncio
async def test_list_panel_move_cursor_clamps_and_returns_session_id() -> None:
    panel = SessionListPanel(widget_id="session-list")

    class _Harness(App[None]):
        def compose(self) -> ComposeResult:
            yield panel

    items = (_item(session_id=f"s-{i}", summary=f"summary-{i}") for i in range(4))
    items_tuple = tuple(items)
    async with _Harness().run_test() as pilot:
        await pilot.pause()
        panel.set_sessions(items_tuple, selected_session_id="s-0", notify=False)
        await pilot.pause()
        result = panel.move_cursor(2)
        assert result == "s-2"
        # Already at the edge — no-op
        assert panel.move_cursor(99) == "s-3"
        # Already at edge again — no-op returns the current id
        assert panel.move_cursor(0) == "s-3"
        panel.focus_list()


def test_list_panel_move_cursor_no_op_when_empty() -> None:
    panel = SessionListPanel(widget_id="session-list")
    assert panel.move_cursor(1) is None


# ── SessionSummaryBar ───────────────────────────────────────────────


def test_summary_bar_set_counts_renders_status_glyphs() -> None:
    bar = SessionSummaryBar()
    bar.set_counts(total=10, active=4, unclosed=2, completed=4)
    rendered = _render(bar)
    assert "10" in rendered
    assert "4" in rendered
    assert "active" in rendered
    assert "unclosed" in rendered
    assert "completed" in rendered


def test_summary_bar_show_loading_includes_filter_chip() -> None:
    bar = SessionSummaryBar()
    bar.show_loading(filter_text="alpha", show_completed=True)
    rendered = _render(bar)
    assert "loading" in rendered
    assert "alpha" in rendered
    assert "shown" in rendered


def test_summary_bar_show_loading_completed_hidden_branch() -> None:
    bar = SessionSummaryBar()
    bar.show_loading(filter_text="", show_completed=False)
    rendered = _render(bar)
    assert "hidden" in rendered


# ── SessionActionBar ────────────────────────────────────────────────


def test_action_bar_show_loading_renders_export_format_and_filter() -> None:
    bar = SessionActionBar()
    bar.show_loading(filter_text="error", show_completed=False)
    rendered = _render(bar)
    assert "preparing actions" in rendered
    assert "show completed" in rendered
    assert "filter" in rendered
    assert "error" in rendered


def test_action_bar_show_loading_no_filter_no_filter_chip() -> None:
    bar = SessionActionBar()
    bar.show_loading(filter_text="", show_completed=True)
    rendered = _render(bar)
    assert "preparing actions" in rendered
    assert "hide completed" in rendered


def test_action_bar_no_detail_renders_filter_chip() -> None:
    bar = SessionActionBar()
    bar.set_state(
        None,
        has_live_pane=False,
        filter_text="alpha",
        show_completed=True,
    )
    rendered = _render(bar)
    assert "no session selected" in rendered
    assert "alpha" in rendered


def test_action_bar_no_detail_skips_filter_chip_when_blank() -> None:
    bar = SessionActionBar()
    bar.set_state(
        None,
        has_live_pane=False,
        filter_text="",
        show_completed=True,
    )
    rendered = _render(bar)
    assert "no session selected" in rendered


def test_action_bar_set_state_renders_premium_chip_and_windows_chip() -> None:
    bar = SessionActionBar()
    bar.set_state(
        _detail(origin="windows", premium_requests="3 req", usage_available=True),
        has_live_pane=True,
        filter_text="error",
        show_completed=False,
    )
    rendered = _render(bar)
    assert "premium" in rendered
    assert "host" in rendered
    assert "windows" in rendered
    assert "filter" in rendered
    assert "completed" in rendered


# ── SessionDetailPanel ──────────────────────────────────────────────


def test_detail_panel_no_detail_returns_no_session_label() -> None:
    panel = SessionDetailPanel()
    panel.set_detail(None)
    rendered = _render(panel)
    assert "No session selected" in rendered


def test_detail_panel_renders_windows_origin_marker_and_resume() -> None:
    panel = SessionDetailPanel()
    panel.set_detail(_detail(origin="windows"))
    rendered = _render(panel)
    assert "[win]" in rendered
    assert "Resume" in rendered
    assert "copilot --resume=session-1" in rendered


def test_detail_panel_completed_session_shows_clean_shutdown_message() -> None:
    panel = SessionDetailPanel()
    panel.set_detail(_detail(is_resumable=False, status="completed"))
    rendered = _render(panel)
    assert "Session completed cleanly" in rendered


def test_detail_panel_with_premium_requests_renders_premium_field() -> None:
    panel = SessionDetailPanel()
    panel.set_detail(_detail(premium_requests="3 req"))
    rendered = _render(panel)
    assert "Premium" in rendered
    assert "3 req" in rendered

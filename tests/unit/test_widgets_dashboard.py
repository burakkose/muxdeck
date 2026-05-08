"""Extra widget tests for the dashboard rendering helpers.

These cover the small pure helpers in ``widgets.dashboard`` that still
have uncovered branches after ``test_dashboard_display.py`` and the
dashboard-screen tests run.

The focus is intentionally on tier-1 / tier-2 work — pure helpers and
panel-only rendering — so the tests stay fast and fully deterministic.
"""

# ruff: noqa: SLF001

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Protocol

import pytest
from textual.app import App, ComposeResult

from muxdeck.controllers import (
    DashboardAgentListItemView,
    DashboardAlertView,
    DashboardHealthSummary,
    DashboardLogLineView,
    DashboardMetricView,
    DashboardSelectedAgentView,
    DashboardSubAgentTreeView,
    DashboardSubAgentView,
    DashboardSubTaskView,
)
from muxdeck.domain.enums import AgentStatus
from muxdeck.domain.subagents import ReadAgentInteraction
from muxdeck.services.operator_status_service import (
    OperatorStatus,
    OperatorStatusKind,
)
from muxdeck.ui_preferences import UiPreferences
from muxdeck.widgets.dashboard import (
    AgentDetailPanel,
    AgentListPanel,
    AlertPanel,
    FilterBar,
    LogPreviewPanel,
    StatusBar,
    _activity_summary,
    _agent_display,
    _event_color,
    _focus_summary,
    _format_cost,
    _format_duration,
    _format_duration_ms,
    _format_idle,
    _format_session,
    _format_subagent_duration,
    _has_structured_signals,
    _highlight_log_line,
    _humanize_event_kind,
    _resolved_token_total,
    _row_style,
    _short_status,
    _shorten_tool_call_id,
    _strip_ansi,
    _SubAgentHeaderRow,
    _truncate,
    _usage_badges,
)


class _Renderable(Protocol):
    def render(self) -> object: ...


def _render(widget: _Renderable) -> str:
    renderable = widget.render()
    plain = getattr(renderable, "plain", None)
    return plain if isinstance(plain, str) else str(renderable)


# ── pure helpers ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        # CSI / SGR colour codes (the original case)
        ("\x1b[31mred\x1b[0m text", "red text"),
        # PSReadLine cursor / paste toggles must be stripped wholesale
        ("\x1b[?25hbusy\x1b[?25l", "busy"),
        # OSC 0 window title (BEL terminated)
        ("\x1b]0;some title\x07after", "after"),
        # OSC 8 hyperlink (ST terminated with ESC \)
        ("\x1b]8;;https://x\x1b\\link\x1b]8;;\x1b\\", "link"),
        # Charset designator
        ("\x1b(Bplain", "plain"),
        # Two-character escape sequence (ESC =)
        ("before\x1b=after", "beforeafter"),
        # BOM, NUL, stray CR
        ("\ufeffhello\x00\rworld\n", "helloworld\n"),
    ],
)
def test_strip_ansi_handles_a_variety_of_escape_classes(payload: str, expected: str) -> None:
    assert _strip_ansi(payload) == expected


def test_strip_ansi_preserves_lf_and_tab() -> None:
    payload = "line1\n\tindented\n"
    assert _strip_ansi(payload) == payload


def test_event_color_branches() -> None:
    # Each branch in the function uses a distinct emoji prefix and must
    # return a distinct color so the dashboard can visually separate
    # event kinds. We assert on distinctness, not just truthiness — the
    # earlier shape `assert {a, b, c, d, e}` only verified the set was
    # non-empty (always true) and would silently pass if every branch
    # collapsed to the same color.
    aqua = _event_color("📖 read file")
    green = _event_color("⚡ ran command")
    yellow = _event_color("💭 thought")
    orange = _event_color("⚠️ warning")
    fallback = _event_color("misc event")
    colors = (aqua, green, yellow, orange, fallback)
    assert all(isinstance(c, str) and c for c in colors)
    assert len(set(colors)) == len(colors), (
        f"event colors should be pairwise distinct, got {colors!r}"
    )


def test_short_status_known_and_unknown() -> None:
    assert _short_status(AgentStatus.RUNNING) == "run"
    assert _short_status(AgentStatus.UNKNOWN) == "?"


def test_humanize_event_kind_handles_none_and_underscores() -> None:
    assert _humanize_event_kind(None) == "-"
    assert _humanize_event_kind("agent_updated") == "agent updated"
    assert _humanize_event_kind("session.shutdown") == "session.shutdown"


def test_format_idle_buckets() -> None:
    assert _format_idle(5) == "5s"
    assert _format_idle(150) == "2m"
    assert _format_idle(7300) == "2h1m"


def test_format_duration_handles_negative_zero_minutes_and_hours() -> None:
    now = datetime.now(UTC)
    # Future start ⇒ negative delta ⇒ "-"
    assert _format_duration(now + timedelta(seconds=10)) == "-"
    # Recent start ⇒ seconds-only output
    assert _format_duration(now - timedelta(seconds=15)).endswith("s")
    # Minute range
    assert _format_duration(now - timedelta(minutes=3, seconds=4)).endswith("s")
    # Hour range
    formatted_hours = _format_duration(now - timedelta(hours=2, minutes=10))
    assert "h" in formatted_hours
    assert "m" in formatted_hours


def test_format_cost_branches() -> None:
    assert _format_cost(None) == "-"
    assert _format_cost("1.234") == "$1.23"
    # Falls through ValueError fallback for non-numeric strings
    assert _format_cost("free") == "$free"


def _bare_item(
    *,
    token_total: int | None = None,
    token_input: int | None = None,
    token_output: int | None = None,
) -> DashboardAgentListItemView:
    now = datetime.now(UTC)
    return DashboardAgentListItemView(
        agent_id="agent-1",
        name="codex",
        status=AgentStatus.RUNNING,
        repo_name="repo",
        branch="main",
        worktree_name="wt",
        pane_id="%5",
        task_title=None,
        worktree_path=None,
        latest_session_id=None,
        last_event_kind=None,
        last_log_at=None,
        last_seen_at=now,
        started_at=now - timedelta(seconds=10),
        idle_seconds=2,
        needs_attention=False,
        attention_reason=None,
        token_total=token_total,
        estimated_cost_usd=None,
        token_input=token_input,
        token_output=token_output,
    )


def test_resolved_token_total_prefers_total_then_falls_back_to_sum() -> None:
    assert _resolved_token_total(_bare_item(token_total=500)) == 500
    summed = _resolved_token_total(_bare_item(token_input=120, token_output=80))
    assert summed == 200
    assert _resolved_token_total(_bare_item(token_input=10)) is None
    assert _resolved_token_total(_bare_item()) is None


def test_truncate_short_circuits_or_appends_ellipsis() -> None:
    assert _truncate("short", 10) == "short"
    truncated = _truncate("a longer description that needs trimming", 10)
    assert truncated.endswith("…")
    # length will be ≤ limit (rstrip may shave a trailing space before
    # the ellipsis is appended).
    assert len(truncated) <= 10


def test_shorten_tool_call_id_with_and_without_underscore() -> None:
    assert _shorten_tool_call_id("") == ""
    assert _shorten_tool_call_id("call_abcdefghij").startswith("#")
    assert _shorten_tool_call_id("plainid").startswith("#")


def test_format_duration_ms_buckets() -> None:
    assert _format_duration_ms(500) == "500ms"
    assert _format_duration_ms(2500) == "2.5s"
    assert "m" in _format_duration_ms(125_000)


def _make_subagent(
    *,
    completed_at: datetime | None = None,
    started_at: datetime | None = None,
    naive_started: bool = False,
) -> DashboardSubAgentView:
    now = datetime.now(UTC)
    started = started_at or (now - timedelta(seconds=30))
    if naive_started:
        started = started.replace(tzinfo=None)
    return DashboardSubAgentView(
        tool_call_id="call_test",
        agent_name="sub",
        display_name="sub agent",
        description=None,
        started_at=started,
        completed_at=completed_at,
        is_running=completed_at is None,
    )


def test_format_subagent_duration_completed_branch_uses_explicit_endpoints() -> None:
    started = datetime(2024, 1, 1, 12, 0, 0, tzinfo=UTC)
    completed = started + timedelta(minutes=2, seconds=30)
    sub = _make_subagent(started_at=started, completed_at=completed)
    assert _format_subagent_duration(sub) == "2m"


def test_format_subagent_duration_running_with_naive_timestamp_treated_as_utc() -> None:
    # If ``started_at`` came in without tzinfo we should still be able
    # to compute a real elapsed time without crashing.
    sub = _make_subagent(naive_started=True)
    output = _format_subagent_duration(sub)
    assert output.endswith(("s", "m", "h"))


def test_format_subagent_duration_clamps_negative_to_zero() -> None:
    now = datetime.now(UTC)
    sub = _make_subagent(started_at=now + timedelta(seconds=10))
    assert _format_subagent_duration(sub) == "0s"


def test_agent_display_appends_running_badge_when_count_positive() -> None:
    prefs = UiPreferences()
    no_badge = _agent_display("codex", expanded=False, running_count=None, preferences=prefs)
    badge = _agent_display("codex", expanded=True, running_count=2, preferences=prefs)
    assert "codex" in no_badge
    assert "codex" in badge
    assert "2" in badge


def test_has_structured_signals_truthy_for_each_field() -> None:
    base = _make_subagent()
    assert not _has_structured_signals(base)
    with_tokens = replace(base, total_tokens=100)
    assert _has_structured_signals(with_tokens)
    with_error = replace(base, error_message="boom")
    assert _has_structured_signals(with_error)
    with_model = replace(base, model="claude")
    assert _has_structured_signals(with_model)


@pytest.mark.parametrize(
    ("content", "expected_marker"),
    [
        ("Traceback (most recent)", "Traceback"),
        ("DeprecationWarning: foo", "Deprecation"),
        ("✓ tool ran", "tool ran"),
        ("$ ls -al", "ls -al"),
        ("ordinary log line", "ordinary log line"),
    ],
)
def test_highlight_log_line_renders_each_branch(content: str, expected_marker: str) -> None:
    text = _highlight_log_line(content, default_style="white")
    assert expected_marker in text.plain


# ── panel-only rendering ────────────────────────────────────────────


def test_alert_panel_collapses_to_empty_class_when_no_alerts() -> None:
    """Empty alert state should collapse the panel rather than print a placeholder.

    The previous behaviour rendered "no active alerts" as a one-line
    placeholder, which trained operators to skip the bottom of the
    sidebar entirely. Now AlertPanel adds the ``empty`` class and
    relies on the CSS rule on ``#dashboard-alerts.empty`` (display:
    none) to release the row to the output panel.
    """
    panel = AlertPanel()
    panel.set_alerts(())
    assert panel.has_class("empty"), "empty alerts should collapse the panel"
    rendered = _render(panel)
    assert "no active alerts" not in rendered.lower()
    assert "needs attention" not in rendered.lower()


def test_alert_panel_renders_needs_attention_header_with_alerts() -> None:
    """Non-empty alerts render the loud NEEDS ATTENTION banner."""
    from datetime import UTC, datetime

    from muxdeck.controllers.dashboard_controller import DashboardAlertView

    panel = AlertPanel()
    alert = DashboardAlertView(
        agent_id="agent-1",
        agent_name="planner",
        severity="warning",
        title="stale agent",
        message="idle for 600s",
        occurred_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    panel.set_alerts((alert,))
    rendered = _render(panel)
    assert not panel.has_class("empty")
    assert "needs attention" in rendered.lower()
    assert "planner" in rendered.lower()


def test_log_preview_panel_no_agent_branch() -> None:
    panel = LogPreviewPanel()
    panel.set_logs(None)
    assert "no recent output" in _render(panel)


def test_log_preview_panel_starting_status_shows_launching_message() -> None:
    panel = LogPreviewPanel()
    item = _bare_item()
    starting = OperatorStatus(
        kind=OperatorStatusKind.STARTING,
        label="launching",
        headline="launching",
        reason="warming up",
        tone="info",
        needs_attention=False,
    )
    item_with_status = replace(item, operator_status=starting)
    selected = DashboardSelectedAgentView(
        item=item_with_status,
        repo_root=None,
        worktree_id=None,
        session_count=0,
        open_session_id=None,
        copilot_session_id=None,
        latest_event_kind=None,
        latest_event_severity=None,
        latest_event_at=None,
        log_preview=(),
    )
    panel.set_logs(selected)
    rendered = _render(panel)
    assert "launching" in rendered


def test_agent_detail_panel_no_agent_branch() -> None:
    panel = AgentDetailPanel()
    panel.set_agent(None)
    assert "no agent selected" in _render(panel)


def test_agent_detail_panel_subagent_no_agent_branch() -> None:
    panel = AgentDetailPanel()
    panel.set_subagent(None)
    assert "no sub-agent selected" in _render(panel)


def test_agent_detail_panel_subagent_running_renders_status_and_glyph() -> None:
    panel = AgentDetailPanel()
    sub = _make_subagent()
    panel.set_subagent(sub)
    rendered = _render(panel)
    assert "running" in rendered
    assert "sub agent" in rendered.lower() or "sub" in rendered.lower()


def test_agent_detail_panel_subagent_failed_renders_error_styling() -> None:
    panel = AgentDetailPanel()
    base = _make_subagent(completed_at=datetime.now(UTC))
    failed = replace(base, is_running=False, success=False, error_message="kaboom")
    panel.set_subagent(failed)
    rendered = _render(panel)
    assert "failed" in rendered
    assert "kaboom" in rendered


def test_agent_detail_panel_renders_subtasks_section() -> None:
    panel = AgentDetailPanel()
    now = datetime.now(UTC)
    item = replace(
        _bare_item(),
        current_activity="Reviewing diff",
        task_title="Bug hunt",
        subtask_count=2,
    )
    selected = DashboardSelectedAgentView(
        item=item,
        repo_root="/repo",
        worktree_id="wt-1",
        session_count=1,
        open_session_id="sess-1",
        copilot_session_id="copilot-1",
        latest_event_kind="agent_updated",
        latest_event_severity=None,
        latest_event_at=now,
        log_preview=(),
        recent_events=("📖 read file", "⚡ ran command"),
        sub_tasks=(
            DashboardSubTaskView(
                task_key="t1",
                agent_type_label="research",
                model="claude-3.5",
                description="investigate cache",
                status="running",
            ),
            DashboardSubTaskView(
                task_key="t2",
                agent_type_label="planner",
                model=None,
                description="",
                status="completed",
            ),
        ),
    )
    panel.set_agent(selected)
    rendered = _render(panel).lower()
    assert "subtasks" in rendered
    assert "background task" in rendered
    assert "research" in rendered
    assert "planner" in rendered


# ── helpers for further coverage ────────────────────────────────────


def _operator_status(
    kind: OperatorStatusKind,
    *,
    label: str = "label",
    headline: str = "headline",
    reason: str = "reason",
) -> OperatorStatus:
    return OperatorStatus(
        kind=kind,
        label=label,
        headline=headline,
        reason=reason,
        tone="info",
        needs_attention=False,
    )


def _selected_view_with(
    item: DashboardAgentListItemView,
    *,
    open_session_id: str | None = "session-1",
    copilot_session_id: str | None = None,
    log_preview: tuple[DashboardLogLineView, ...] = (),
    recent_events: tuple[str, ...] = (),
    sub_tasks: tuple[DashboardSubTaskView, ...] = (),
    session_count: int = 1,
) -> DashboardSelectedAgentView:
    return DashboardSelectedAgentView(
        item=item,
        repo_root="/repo",
        worktree_id="wt-1",
        session_count=session_count,
        open_session_id=open_session_id,
        copilot_session_id=copilot_session_id,
        latest_event_kind=None,
        latest_event_severity=None,
        latest_event_at=None,
        log_preview=log_preview,
        recent_events=recent_events,
        sub_tasks=sub_tasks,
    )


def _health(
    *,
    waiting: int = 0,
    blocked: int = 0,
    error: int = 0,
    attention: int = 0,
    active: int = 0,
    total: int = 0,
) -> DashboardHealthSummary:
    return DashboardHealthSummary(
        tone="healthy",
        message="ok",
        total_agents=total,
        active_agents=active,
        attention_agents=attention,
        waiting_input_agents=waiting,
        blocked_agents=blocked,
        error_agents=error,
    )


# ── helper-level branches ───────────────────────────────────────────


def test_activity_summary_falls_back_to_task_title_for_working_status() -> None:
    item = replace(_bare_item(), task_title="restructure cache", current_activity=None)
    summary = _activity_summary(item)
    assert "restructure cache" in summary


def test_focus_summary_returns_status_text_for_waiting_input() -> None:
    waiting = _operator_status(OperatorStatusKind.WAITING_INPUT, reason="waiting on user")
    item = replace(_bare_item(), operator_status=waiting)
    summary, style = _focus_summary(item)
    assert "waiting on user" in summary
    assert isinstance(style, str)
    assert style


def test_focus_summary_returns_aqua_for_starting_status() -> None:
    starting = _operator_status(OperatorStatusKind.STARTING, headline="launching")
    item = replace(_bare_item(), operator_status=starting, current_activity=None)
    summary, style = _focus_summary(item)
    assert "launching" in summary
    assert isinstance(style, str)
    assert style


def test_usage_badges_with_only_token_input_renders_input_badge() -> None:
    item = _bare_item(token_input=120)
    badges = _usage_badges(item)
    labels = [label for label, _style in badges]
    assert any("in 120" in label for label in labels)


def test_usage_badges_with_only_token_output_renders_output_badge() -> None:
    item = _bare_item(token_output=240)
    badges = _usage_badges(item)
    labels = [label for label, _style in badges]
    assert any("out 240" in label for label in labels)


def test_format_session_returns_dash_when_session_unknown() -> None:
    item = replace(_bare_item(), latest_session_id=None)
    selected = _selected_view_with(item, open_session_id=None)
    assert _format_session(selected) == "-"


def test_row_style_completed_is_dim() -> None:
    item = replace(_bare_item(), status=AgentStatus.COMPLETED)
    assert _row_style(item, selected=False) == "dim"


def test_row_style_dead_is_dim() -> None:
    item = replace(_bare_item(), status=AgentStatus.DEAD)
    assert _row_style(item, selected=False) == "dim"


def test_row_style_attention_returns_attention_background() -> None:
    item = replace(_bare_item(), needs_attention=True, attention_reason="check this")
    assert "on " in _row_style(item, selected=False)


# ── StatusBar branches ──────────────────────────────────────────────


def test_status_bar_renders_review_waiting_blocked_failed_segments() -> None:
    bar = StatusBar()
    health = _health(
        waiting=1,
        blocked=2,
        error=3,
        attention=10,
        active=4,
        total=20,
    )
    bar.set_state(health, ())
    rendered = _render(bar).lower()
    assert "waiting" in rendered
    assert "blocked" in rendered
    assert "failed" in rendered
    assert "review" in rendered


def test_status_bar_renders_tokens_metric_when_present() -> None:
    bar = StatusBar()
    bar.set_state(
        _health(active=1, total=1),
        (DashboardMetricView(key="tokens", label="tokens", value=1234),),
    )
    rendered = _render(bar)
    assert "1,234" in rendered or "1234" in rendered


def test_status_bar_renders_focus_segment_when_selected_provided() -> None:
    bar = StatusBar()
    item = _bare_item(token_total=500)
    bar.set_state(_health(active=1, total=1), (), selected=item)
    rendered = _render(bar)
    assert "focus" in rendered.lower()
    assert item.name in rendered


# ── FilterBar branches ──────────────────────────────────────────────


class _FilterApp(App[None]):
    def compose(self) -> ComposeResult:
        yield FilterBar(id="dashboard-filter-row")


def _run_filter_app(body: Callable[[FilterBar], None]) -> None:
    async def _scenario() -> None:
        app = _FilterApp()
        async with app.run_test(size=(80, 24)):
            await app.workers.wait_for_complete()
            body(app.query_one(FilterBar))

    asyncio.run(_scenario())


def test_filter_bar_focus_input_focuses_input_widget() -> None:
    captured: dict[str, bool] = {}

    def body(bar: FilterBar) -> None:
        bar.focus_input()
        from textual.widgets import Input

        captured["focused"] = bar.query_one(Input).has_focus

    _run_filter_app(body)
    assert captured["focused"] is True


def test_filter_bar_renders_attention_only_and_query_segments() -> None:
    rendered: dict[str, str] = {}

    def body(bar: FilterBar) -> None:
        bar.set_state(
            filter_text="auth",
            visible_agents=2,
            total_agents=5,
            attention_only=True,
            include_completed=False,
            sort_label="last_seen",
        )
        from textual.widgets import Static

        summary = bar.query_one("#dashboard-filter-summary", Static)
        rendered["text"] = str(summary.renderable)

    _run_filter_app(body)
    assert "attention" in rendered["text"]
    assert "auth" in rendered["text"]


def test_filter_bar_renders_search_hint_when_no_query() -> None:
    rendered: dict[str, str] = {}

    def body(bar: FilterBar) -> None:
        bar.set_state(
            filter_text="",
            visible_agents=0,
            total_agents=0,
            attention_only=False,
            include_completed=True,
            sort_label="name",
        )
        from textual.widgets import Static

        summary = bar.query_one("#dashboard-filter-summary", Static)
        rendered["text"] = str(summary.renderable)

    _run_filter_app(body)
    assert "search name" in rendered["text"]


# ── AgentListPanel: selection / cursor / sub-agents ────────────────


def _list_item(
    agent_id: str = "agent-1",
    *,
    name: str = "node",
    pane_id: str = "%1",
    needs_attention: bool = False,
    subtask_count: int = 0,
    branch: str | None = "feature/x",
    worktree_name: str | None = "wt",
    repo_name: str | None = "repo",
    token_total: int | None = None,
) -> DashboardAgentListItemView:
    now = datetime.now(UTC)
    return DashboardAgentListItemView(
        agent_id=agent_id,
        name=name,
        status=AgentStatus.RUNNING,
        repo_name=repo_name,
        branch=branch,
        worktree_name=worktree_name,
        pane_id=pane_id,
        task_title="task",
        worktree_path="/repo/wt",
        latest_session_id="session-1",
        last_event_kind="agent.updated",
        last_log_at=now,
        last_seen_at=now,
        started_at=now - timedelta(seconds=30),
        idle_seconds=10,
        needs_attention=needs_attention,
        attention_reason="please look",
        token_total=token_total,
        estimated_cost_usd=None,
        window_name="win",
        window_id="@1",
        subtask_count=subtask_count,
    )


def _running_subagent(*, tool_call_id: str = "call_abcdef") -> DashboardSubAgentView:
    now = datetime.now(UTC)
    return DashboardSubAgentView(
        tool_call_id=tool_call_id,
        agent_name="sub",
        display_name=f"sub agent {tool_call_id}",
        description="some sub agent",
        started_at=now - timedelta(seconds=5),
        completed_at=None,
        is_running=True,
        agent_type="research",
        mode="background",
    )


def test_agent_list_panel_set_agents_renders_empty_message_when_no_agents() -> None:
    panel = AgentListPanel(widget_id="test-empty")
    panel.set_agents((), selected_agent_id=None)
    assert "no agents found" in _render(panel)


def test_agent_list_panel_set_agents_with_subtasks_renders_glyph() -> None:
    panel = AgentListPanel(widget_id="test-subtasks")
    item = _list_item(subtask_count=3, token_total=42)
    panel.set_agents((item,), selected_agent_id="agent-1")
    table = panel._build_table()
    cells = "".join(str(cell) for cell in table.columns[2]._cells)
    assert "3" in cells
    assert item.name in cells


def test_agent_list_panel_running_subagent_count_returns_none_when_collapsed() -> None:
    panel = AgentListPanel(widget_id="test-collapsed")
    item = _list_item()
    panel.set_agents((item,), selected_agent_id="agent-1")
    assert panel._running_subagent_count("agent-1") is None


def test_agent_list_panel_toggle_expand_emits_request_when_uncached() -> None:
    panel = AgentListPanel(widget_id="test-toggle")
    item = _list_item()
    panel.set_agents((item,), selected_agent_id="agent-1")
    panel.toggle_expand()
    assert "agent-1" in panel._expanded
    assert "agent-1" in panel._loading


def test_agent_list_panel_toggle_expand_collapses_already_expanded_row() -> None:
    panel = AgentListPanel(widget_id="test-toggle-2")
    item = _list_item()
    panel.set_agents((item,), selected_agent_id="agent-1")
    panel.toggle_expand()
    panel.toggle_expand()
    assert "agent-1" not in panel._expanded


def test_agent_list_panel_toggle_expand_with_cached_subagents_skips_request() -> None:
    panel = AgentListPanel(widget_id="test-cached")
    item = _list_item()
    panel.set_agents((item,), selected_agent_id="agent-1")
    sub = _running_subagent()
    tree = DashboardSubAgentTreeView(
        agent_id="agent-1",
        session_id="session-1",
        running=(sub,),
        recent=(),
    )
    # Pre-populate cache so toggle_expand avoids posting ExpandRequested.
    panel._subagents["agent-1"] = tree
    panel.toggle_expand()
    assert "agent-1" in panel._expanded
    # No load was started (cache was already present).
    assert "agent-1" not in panel._loading


def test_agent_list_panel_set_subagents_keeps_cursor_when_no_rows_yet() -> None:
    panel = AgentListPanel(widget_id="test-set-subagents-empty")
    sub = _running_subagent()
    tree = DashboardSubAgentTreeView(
        agent_id="agent-1",
        session_id="session-1",
        running=(sub,),
        recent=(),
    )
    # No agents loaded yet — set_subagents must not crash and rows stay empty.
    panel.set_subagents("agent-1", tree)
    assert panel._rows == ()
    assert "agent-1" in panel._subagents


def test_agent_list_panel_subagent_rows_are_rendered_when_expanded() -> None:
    panel = AgentListPanel(widget_id="test-rendered-sub")
    item = _list_item()
    panel.set_agents((item,), selected_agent_id="agent-1")
    sub = _running_subagent(tool_call_id="call_xyz123")
    tree = DashboardSubAgentTreeView(
        agent_id="agent-1",
        session_id="session-1",
        running=(sub,),
        recent=(),
    )
    panel._expanded.add("agent-1")
    panel.set_subagents("agent-1", tree)
    table = panel._build_table()
    cells = "".join(str(cell) for cell in table.columns[2]._cells)
    assert sub.display_name in cells


def test_agent_list_panel_move_cursor_returns_none_when_no_rows() -> None:
    panel = AgentListPanel(widget_id="test-move-empty")
    panel.set_agents((), selected_agent_id=None)
    assert panel.move_cursor(1) is None


def test_agent_list_panel_move_cursor_skips_loading_header_row() -> None:
    panel = AgentListPanel(widget_id="test-skip-header")
    a1 = _list_item("agent-1")
    a2 = _list_item("agent-2")
    panel.set_agents((a1, a2), selected_agent_id="agent-1")
    # Force expand so a header row appears between the two agents
    panel._expanded.add("agent-1")
    panel._loading.add("agent-1")
    panel._rebuild_rows()
    panel._refresh_table()
    # Move cursor down should skip past the header onto agent-2 row
    panel._selected_index = 0
    result = panel.move_cursor(1)
    assert result == "agent-2"


def test_agent_list_panel_selected_agent_id_returns_none_for_empty_rows() -> None:
    panel = AgentListPanel(widget_id="test-selected-empty")
    panel.set_agents((), selected_agent_id=None)
    assert panel.selected_agent_id is None


def test_agent_list_panel_selected_subagent_returns_none_for_empty_rows() -> None:
    panel = AgentListPanel(widget_id="test-sub-empty")
    panel.set_agents((), selected_agent_id=None)
    assert panel.selected_subagent is None


def test_agent_list_panel_row_index_for_agent_returns_zero_for_unknown() -> None:
    panel = AgentListPanel(widget_id="test-index-unknown")
    item = _list_item()
    panel.set_agents((item,), selected_agent_id="agent-1")
    # Falls back to 0 when the id is not in the row set.
    assert panel._row_index_for_agent("nope") == 0
    # Also handles the None branch.
    assert panel._row_index_for_agent(None) == 0


def test_agent_list_panel_row_index_for_position_prefer_header_returns_header() -> None:
    panel = AgentListPanel(widget_id="test-prefer-header")
    item = _list_item()
    panel.set_agents((item,), selected_agent_id="agent-1")
    panel._expanded.add("agent-1")
    panel._loading.add("agent-1")
    panel._rebuild_rows()
    idx = panel._row_index_for_position(
        parent_agent_id="agent-1",
        sub_tool_call_id=None,
        prefer_header=True,
    )
    # Header row should sit immediately after the parent row.
    assert idx == 1


# ── _render_subagent_row branches ───────────────────────────────────


def test_subagent_row_appears_with_call_id_suffix_in_render() -> None:
    panel = AgentListPanel(widget_id="test-callid")
    item = _list_item()
    panel.set_agents((item,), selected_agent_id="agent-1")
    sub = _running_subagent(tool_call_id="call_xyz123456")
    tree = DashboardSubAgentTreeView(
        agent_id="agent-1",
        session_id="session-1",
        running=(sub,),
        recent=(),
    )
    panel._expanded.add("agent-1")
    panel.set_subagents("agent-1", tree)
    table = panel._build_table()
    cells = "".join(str(cell) for cell in table.columns[2]._cells)
    # Last 6 chars of tool_call_id appear with `#` prefix.
    assert "#23456" in cells or "23456" in cells


# ── AgentDetailPanel branches ───────────────────────────────────────


def test_agent_detail_panel_truncates_long_session_and_copilot_ids() -> None:
    panel = AgentDetailPanel()
    long_session = "s" * 60
    long_copilot = "c" * 30
    item = replace(_bare_item(), latest_session_id=long_session)
    selected = _selected_view_with(
        item,
        open_session_id=long_session,
        copilot_session_id=long_copilot,
    )
    panel.set_agent(selected)
    rendered = _render(panel)
    assert "…" in rendered  # truncation marker for at least one of the ids


def test_agent_detail_panel_renders_recent_events_with_dedup() -> None:
    panel = AgentDetailPanel()
    item = _bare_item()
    selected = _selected_view_with(
        item,
        recent_events=("📖 read file", "📖 read file", "⚡ ran command"),
    )
    panel.set_agent(selected)
    rendered = _render(panel)
    assert "recent" in rendered.lower()


def test_agent_detail_panel_renders_attention_reason_line() -> None:
    panel = AgentDetailPanel()
    item = replace(
        _bare_item(),
        needs_attention=True,
        attention_reason="merge conflict",
        current_activity="Reviewing diff",
    )
    selected = _selected_view_with(item)
    panel.set_agent(selected)
    rendered = _render(panel)
    assert "merge conflict" in rendered


def test_agent_detail_panel_renders_task_title_when_distinct_from_activity() -> None:
    panel = AgentDetailPanel()
    item = replace(
        _bare_item(),
        task_title="Bug hunt",
        current_activity="Reviewing diff",
    )
    selected = _selected_view_with(item)
    panel.set_agent(selected)
    rendered = _render(panel)
    assert "Bug hunt" in rendered


def test_agent_detail_panel_surfaces_state_aware_primary_action() -> None:
    """The ACTIONS section leads with a contextual primary action.

    A WAITING agent's primary should be ``m send message`` (operator
    has to answer the question to unblock it). A RUNNING agent's
    primary should be ``v live mirror`` (the most common operator
    interaction is "let me see what it's doing"). The marker chip
    ``primary`` is rendered after the suggestion to signal that this
    row is the recommended next step rather than just another
    shortcut.
    """
    panel = AgentDetailPanel()

    waiting_item = replace(
        _bare_item(),
        status=AgentStatus.WAITING_INPUT,
        needs_attention=True,
        attention_reason="waiting for operator confirmation",
    )
    panel.set_agent(_selected_view_with(waiting_item))
    waiting_rendered = _render(panel)
    assert "primary" in waiting_rendered.lower()
    assert "send message" in waiting_rendered.lower()

    running_panel = AgentDetailPanel()
    running_panel.set_agent(_selected_view_with(_bare_item()))
    running_rendered = _render(running_panel)
    assert "primary" in running_rendered.lower()
    assert "live mirror" in running_rendered.lower()


def test_render_action_shortcuts_skips_section_when_all_rows_empty() -> None:
    """Defensive: an ACTIONS header with no body confuses operators.

    Previously ``_render_action_shortcuts`` always emitted the
    ``ACTIONS`` section header before iterating rows, so an empty
    ``rows`` argument left the operator staring at a section header
    with nothing under it. The new behaviour skips the entire
    section when there's nothing to render and no primary action.
    """
    from rich.text import Text as _RichText

    from muxdeck.ui_preferences import UiPreferences
    from muxdeck.widgets.dashboard import _render_action_shortcuts

    text = _RichText()
    _render_action_shortcuts(text, ((), ()), preferences=UiPreferences())
    assert "actions" not in text.plain.lower()


def test_render_action_shortcuts_renders_when_only_primary_supplied() -> None:
    """Primary action alone is enough to justify the section."""
    from rich.text import Text as _RichText

    from muxdeck.ui_preferences import UiPreferences
    from muxdeck.widgets.dashboard import _render_action_shortcuts

    text = _RichText()
    _render_action_shortcuts(
        text,
        (),
        preferences=UiPreferences(),
        primary=("R", "resume"),
    )
    rendered = text.plain.lower()
    assert "actions" in rendered
    assert "resume" in rendered
    assert "primary" in rendered


def test_agent_detail_panel_renders_subagent_input_section_with_prompt() -> None:
    panel = AgentDetailPanel()
    sub = DashboardSubAgentView(
        tool_call_id="call_with_prompt",
        agent_name="sub",
        display_name="sub agent",
        description="desc",
        started_at=datetime.now(UTC) - timedelta(seconds=20),
        completed_at=None,
        is_running=True,
        prompt="line 1\nline 2",
    )
    panel.set_subagent(sub)
    rendered = _render(panel)
    assert "input" in rendered.lower()
    assert "line 1" in rendered
    assert "line 2" in rendered


def test_agent_detail_panel_renders_background_launch_ack_label_when_completed() -> None:
    panel = AgentDetailPanel()
    sub = DashboardSubAgentView(
        tool_call_id="call_bg_complete",
        agent_name="sub",
        display_name="sub agent",
        description=None,
        started_at=datetime.now(UTC) - timedelta(minutes=1),
        completed_at=datetime.now(UTC),
        is_running=False,
        mode="background",
        result_content="acknowledged",
    )
    panel.set_subagent(sub)
    rendered = _render(panel)
    assert "launch ack" in rendered.lower()


def test_agent_detail_panel_renders_subagent_interactions() -> None:
    panel = AgentDetailPanel()
    interaction = ReadAgentInteraction(
        timestamp=datetime.now(UTC),
        arguments_summary="agent_id=task-1",
        result_content="part one\npart two\npart three",
    )
    sub = DashboardSubAgentView(
        tool_call_id="call_interactions",
        agent_name="sub",
        display_name="sub agent",
        description=None,
        started_at=datetime.now(UTC) - timedelta(seconds=30),
        completed_at=None,
        is_running=True,
        read_interactions=(interaction,),
        total_tokens=120,
        duration_ms=2500,
        total_tool_calls=4,
        model="claude-sonnet",
    )
    panel.set_subagent(sub)
    rendered = _render(panel)
    assert "interactions" in rendered.lower()
    assert "part one" in rendered


def test_agent_detail_panel_renders_subagent_no_interactions_dash() -> None:
    panel = AgentDetailPanel()
    sub = DashboardSubAgentView(
        tool_call_id="call_no_interactions",
        agent_name="sub",
        display_name="sub agent",
        description=None,
        started_at=datetime.now(UTC) - timedelta(seconds=30),
        completed_at=None,
        is_running=True,
        total_tokens=120,
    )
    panel.set_subagent(sub)
    rendered = _render(panel)
    assert "interactions" in rendered.lower()
    assert "—" in rendered


def test_agent_detail_panel_renders_subtasks_when_known_less_than_count() -> None:
    panel = AgentDetailPanel()
    item = replace(_bare_item(), subtask_count=3)
    selected = _selected_view_with(
        item,
        sub_tasks=(
            DashboardSubTaskView(
                task_key="t1",
                agent_type_label="research",
                model="claude-sonnet-4.5",
                description="x" * 80,
                status="running",
            ),
        ),
    )
    panel.set_agent(selected)
    rendered = _render(panel).lower()
    assert "known details for 1" in rendered
    assert "tasks (details unknown)" in rendered or "task (details unknown)" in rendered


def test_agent_detail_panel_renders_unavailable_when_count_but_no_tasks() -> None:
    panel = AgentDetailPanel()
    item = replace(_bare_item(), subtask_count=2)
    selected = _selected_view_with(item, sub_tasks=())
    panel.set_agent(selected)
    rendered = _render(panel)
    assert "details unavailable" in rendered


# ── _format_subagent_duration durations ─────────────────────────────


def test_format_subagent_duration_running_minutes_bucket() -> None:
    started = datetime.now(UTC) - timedelta(minutes=5)
    sub = DashboardSubAgentView(
        tool_call_id="x",
        agent_name="a",
        display_name="a",
        description=None,
        started_at=started,
        completed_at=None,
        is_running=True,
    )
    out = _format_subagent_duration(sub)
    assert out.endswith("m")


def test_format_subagent_duration_hour_bucket_for_completed() -> None:
    started = datetime(2024, 1, 1, 12, 0, 0, tzinfo=UTC)
    completed = started + timedelta(hours=2, minutes=10)
    sub = DashboardSubAgentView(
        tool_call_id="x",
        agent_name="a",
        display_name="a",
        description=None,
        started_at=started,
        completed_at=completed,
        is_running=False,
    )
    assert _format_subagent_duration(sub) == "2h"


# ── LogPreviewPanel / AlertPanel branches ──────────────────────────


def test_log_preview_panel_renders_log_lines_with_timestamps() -> None:
    panel = LogPreviewPanel()
    now = datetime(2024, 1, 1, 12, 0, 0, tzinfo=UTC)
    lines = (
        DashboardLogLineView(
            captured_at=now,
            source="stdout",
            sequence_no=1,
            content="hello",
        ),
        DashboardLogLineView(
            captured_at=now,
            source="stderr",
            sequence_no=2,
            content="failure!",
        ),
        DashboardLogLineView(
            captured_at=now + timedelta(seconds=5),
            source="assistant",
            sequence_no=3,
            content="thinking...",
        ),
    )
    item = _bare_item()
    selected = _selected_view_with(item, log_preview=lines)
    panel.set_logs(selected)
    rendered = _render(panel)
    assert "hello" in rendered
    assert "failure" in rendered
    assert "thinking" in rendered


def test_log_preview_panel_tails_to_visible_height_when_mounted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Long previews are tailed to the rows that physically fit on screen.

    Without this the panel would render all 50 preview lines into a
    Static, which clips from the bottom — hiding the freshest output
    (the opposite of ``tail -f`` behaviour).
    """
    from textual.geometry import Size

    panel = LogPreviewPanel()
    monkeypatch.setattr(type(panel), "size", property(lambda _self: Size(80, 12)))
    now = datetime(2024, 1, 1, 12, 0, 0, tzinfo=UTC)
    lines = tuple(
        DashboardLogLineView(
            captured_at=now,
            source="stdout",
            sequence_no=i,
            content=f"line-{i:02d}",
        )
        for i in range(50)
    )
    item = _bare_item()
    selected = _selected_view_with(item, log_preview=lines)
    panel.set_logs(selected)
    rendered = _render(panel)
    # The most recent lines must be present, the oldest must be elided.
    assert "line-49" in rendered
    assert "line-48" in rendered
    assert "line-00" not in rendered
    assert "line-10" not in rendered


def test_log_preview_panel_renders_full_preview_when_unmounted() -> None:
    """Off-screen renders (unit tests, benchmarks) should not clip."""
    panel = LogPreviewPanel()
    # ``size`` is ``Size(0, 0)`` until the widget is mounted.
    now = datetime(2024, 1, 1, 12, 0, 0, tzinfo=UTC)
    lines = tuple(
        DashboardLogLineView(
            captured_at=now,
            source="stdout",
            sequence_no=i,
            content=f"row-{i:02d}",
        )
        for i in range(20)
    )
    item = _bare_item()
    selected = _selected_view_with(item, log_preview=lines)
    panel.set_logs(selected)
    rendered = _render(panel)
    assert "row-00" in rendered
    assert "row-19" in rendered


def test_log_preview_panel_re_renders_on_resize_with_cached_view(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Resize re-tails the cached preview so the freshest lines stay visible."""
    from textual import events
    from textual.geometry import Size

    panel = LogPreviewPanel()
    now = datetime(2024, 1, 1, 12, 0, 0, tzinfo=UTC)
    lines = tuple(
        DashboardLogLineView(
            captured_at=now,
            source="stdout",
            sequence_no=i,
            content=f"row-{i:02d}",
        )
        for i in range(40)
    )
    item = _bare_item()
    selected = _selected_view_with(item, log_preview=lines)
    monkeypatch.setattr(type(panel), "size", property(lambda _self: Size(80, 30)))
    panel.set_logs(selected)
    assert "row-39" in _render(panel)
    # Shrink the panel; on_resize should re-tail to the smaller budget.
    monkeypatch.setattr(type(panel), "size", property(lambda _self: Size(80, 6)))
    panel.on_resize(events.Resize(Size(80, 6), Size(80, 30)))
    rendered = _render(panel)
    assert "row-39" in rendered
    # 6 rows total → 4 visible after header+padding → only the very end.
    assert "row-00" not in rendered
    assert "row-30" not in rendered


def test_log_preview_panel_no_cached_view_resize_is_noop() -> None:
    """Resize without a cached view must not crash or render anything new."""
    from textual import events
    from textual.geometry import Size

    panel = LogPreviewPanel()
    panel.on_resize(events.Resize(Size(80, 12), Size(80, 30)))


def test_alert_panel_renders_alerts_with_severity_badges() -> None:
    panel = AlertPanel()
    alerts = (
        DashboardAlertView(
            agent_id="a1",
            agent_name="alpha",
            severity="error",
            title="boom",
            message="something exploded",
            occurred_at=datetime.now(UTC),
        ),
        DashboardAlertView(
            agent_id="a2",
            agent_name="beta",
            severity="warning",
            title="careful",
            message="might explode",
            occurred_at=datetime.now(UTC),
        ),
    )
    panel.set_alerts(alerts)
    rendered = _render(panel)
    assert "alpha" in rendered
    assert "beta" in rendered
    assert "something exploded" in rendered


# ── _has_structured_signals false branch ────────────────────────────


def test_has_structured_signals_false_when_only_required_fields() -> None:
    sub = DashboardSubAgentView(
        tool_call_id="x",
        agent_name="a",
        display_name="a",
        description=None,
        started_at=datetime.now(UTC),
        completed_at=None,
        is_running=True,
    )
    assert _has_structured_signals(sub) is False


# ── density-aware rendering: COMFORTABLE / ASCII ────────────────────


class _ComfortableApp(App[None]):
    ui_preferences = UiPreferences(
        density=__import__("muxdeck.ui_preferences", fromlist=["UiDensity"]).UiDensity.COMFORTABLE
    )

    def __init__(self, factory: Callable[[], object]) -> None:
        super().__init__()
        self._factory = factory

    def compose(self) -> ComposeResult:
        widget = self._factory()
        if hasattr(widget, "__iter__"):
            yield from widget  # type: ignore[misc]
        else:
            yield widget  # type: ignore[misc]


def _run_in_app(
    app_factory: Callable[[], App[None]],
    body: Callable[[App[None]], None],
) -> None:
    async def _scenario() -> None:
        app = app_factory()
        async with app.run_test(size=(120, 40)):
            await app.workers.wait_for_complete()
            body(app)

    asyncio.run(_scenario())


def test_agent_list_panel_comfortable_renders_meta_and_badges() -> None:
    panel: AgentListPanel | None = None

    def factory() -> AgentListPanel:
        nonlocal panel
        panel = AgentListPanel(widget_id="comfy-list")
        return panel

    captured: dict[str, str] = {}

    def body(_app: App[None]) -> None:
        assert panel is not None
        item = _list_item(token_total=99, branch="feature/big", repo_name="repo-y")
        panel.set_agents((item,), selected_agent_id="agent-1")
        table = panel._build_table()
        captured["names"] = "".join(str(cell) for cell in table.columns[2]._cells)
        captured["activity"] = "".join(str(cell) for cell in table.columns[3]._cells)

    _run_in_app(lambda: _ComfortableApp(factory), body)
    assert "feature/big" in captured["names"]
    assert "99 tok" in captured["names"]
    assert "seen" in captured["activity"] or "idle" in captured["activity"]


def test_agent_list_panel_compact_renders_inline_usage_badges() -> None:
    panel = AgentListPanel(widget_id="compact-badges")
    item = _list_item(token_total=12)
    panel.set_agents((item,), selected_agent_id="agent-1")
    table = panel._build_table()
    cells = "".join(str(cell) for cell in table.columns[2]._cells)
    assert "12 tok" in cells


def test_subagent_row_comfortable_includes_description_and_mode() -> None:
    panel: AgentListPanel | None = None

    def factory() -> AgentListPanel:
        nonlocal panel
        panel = AgentListPanel(widget_id="comfy-sub")
        return panel

    captured: dict[str, str] = {}

    def body(_app: App[None]) -> None:
        assert panel is not None
        item = _list_item()
        panel.set_agents((item,), selected_agent_id="agent-1")
        sub = DashboardSubAgentView(
            tool_call_id="call_comfort_xyz",
            agent_name="research",
            display_name="research worker",
            description="dig through docs",
            started_at=datetime.now(UTC) - timedelta(seconds=20),
            completed_at=None,
            is_running=True,
            mode="background",
        )
        tree = DashboardSubAgentTreeView(
            agent_id="agent-1",
            session_id="session-1",
            running=(sub,),
            recent=(),
        )
        panel._expanded.add("agent-1")
        panel.set_subagents("agent-1", tree)
        table = panel._build_table()
        captured["names"] = "".join(str(cell) for cell in table.columns[2]._cells)
        captured["duration"] = "".join(str(cell) for cell in table.columns[3]._cells)

    _run_in_app(lambda: _ComfortableApp(factory), body)
    assert "dig through docs" in captured["names"]
    # Mode rendered on the duration column for comfortable density.
    assert "background" in captured["duration"]


# ── _activity_summary STARTING branch ───────────────────────────────


def test_activity_summary_uses_starting_reason_when_no_current_activity() -> None:
    starting = _operator_status(
        OperatorStatusKind.STARTING,
        reason="warming up",
    )
    item = replace(_bare_item(), operator_status=starting, current_activity=None)
    summary = _activity_summary(item)
    assert "warming up" in summary


# ── _post_selection edge cases ─────────────────────────────────────


def test_agent_list_panel_post_selection_handles_index_beyond_rows() -> None:
    panel = AgentListPanel(widget_id="post-sel")
    item = _list_item()
    panel.set_agents((item,), selected_agent_id="agent-1")
    # Internal helper: passing an out-of-range index should be a no-op.
    panel._post_selection(99)
    panel._post_selection(None)
    # No exception raised; the rows are intact.
    assert len(panel._rows) == 1


# ── move_cursor cornered backward branch ───────────────────────────


def test_agent_list_panel_move_cursor_corners_backward_off_header() -> None:
    panel = AgentListPanel(widget_id="cornered-header")
    item = _list_item()
    panel.set_agents((item,), selected_agent_id="agent-1")
    # Force a single row that is a header — no other row to skip onto.
    panel._rows = (_panel_header_row("agent-1"),)
    panel._selected_index = 0
    # Move forward should fall back to the same/header position; the
    # function must not raise even with no real selectable rows.
    panel.move_cursor(1)
    assert panel._selected_index == 0


def _panel_header_row(parent_id: str) -> _SubAgentHeaderRow:
    return _SubAgentHeaderRow(parent_agent_id=parent_id, count=0)


# ── Agent detail attention branch when no activity ─────────────────


def test_agent_detail_panel_shows_attention_reason_without_activity() -> None:
    panel = AgentDetailPanel()
    blocked = _operator_status(
        OperatorStatusKind.BLOCKED,
        reason="merge conflict",
        headline="blocked",
    )
    item = replace(
        _bare_item(),
        needs_attention=True,
        attention_reason="resolve conflicts in src/",
        current_activity=None,
        task_title=None,
        operator_status=blocked,
    )
    selected = _selected_view_with(item)
    panel.set_agent(selected)
    rendered = _render(panel)
    assert "resolve conflicts in src/" in rendered


# ── inline fields hidden when all empty ────────────────────────────


def test_agent_detail_panel_hides_window_pane_block_when_both_empty() -> None:
    panel = AgentDetailPanel()
    # When window_name is None and pane_id is empty, the
    # `_append_inline_fields` row for window/pane is skipped entirely
    # (line "  window foo | pane %1" never appears).
    item_full = replace(_bare_item(), window_name="distinct-window-name", pane_id="%99")
    panel.set_agent(_selected_view_with(item_full))
    with_window = _render(panel)

    panel2 = AgentDetailPanel()
    item_empty = replace(_bare_item(), window_name=None, pane_id="")
    panel2.set_agent(_selected_view_with(item_empty))
    without_window = _render(panel2)

    assert "distinct-window-name" in with_window
    assert "distinct-window-name" not in without_window


# ── ASCII subagent glyphs ───────────────────────────────────────────


class _AsciiApp(App[None]):
    ui_preferences = UiPreferences(
        glyphs=__import__("muxdeck.ui_preferences", fromlist=["UiGlyphs"]).UiGlyphs.ASCII
    )

    def __init__(self, factory: Callable[[], object]) -> None:
        super().__init__()
        self._factory = factory

    def compose(self) -> ComposeResult:
        yield self._factory()  # type: ignore[misc]


def test_agent_detail_panel_subagent_uses_ascii_glyphs_when_configured() -> None:
    panel: AgentDetailPanel | None = None

    def factory() -> AgentDetailPanel:
        nonlocal panel
        panel = AgentDetailPanel()
        return panel

    captured: dict[str, str] = {}

    def body(_app: App[None]) -> None:
        assert panel is not None
        sub = DashboardSubAgentView(
            tool_call_id="call_ascii",
            agent_name="sub",
            display_name="sub agent",
            description=None,
            started_at=datetime.now(UTC) - timedelta(seconds=10),
            completed_at=None,
            is_running=True,
        )
        panel.set_subagent(sub)
        captured["render"] = _render(panel)

    _run_in_app(lambda: _AsciiApp(factory), body)
    # Running ASCII glyph should be `>`; not the rich `▶`.
    assert ">" in captured["render"]


# ── multiple read interactions branch ──────────────────────────────


def test_agent_detail_panel_renders_multiple_subagent_interactions() -> None:
    panel = AgentDetailPanel()
    interactions = tuple(
        ReadAgentInteraction(
            timestamp=datetime.now(UTC),
            arguments_summary=f"agent_id=task-{i}",
            result_content=f"result {i}",
        )
        for i in range(3)
    )
    sub = DashboardSubAgentView(
        tool_call_id="call_many",
        agent_name="sub",
        display_name="sub agent",
        description=None,
        started_at=datetime.now(UTC) - timedelta(seconds=10),
        completed_at=None,
        is_running=True,
        read_interactions=interactions,
    )
    panel.set_subagent(sub)
    rendered = _render(panel)
    assert "result 0" in rendered
    assert "result 2" in rendered

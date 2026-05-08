"""Widget-only tests for the attention screen panels.

These tests exercise the four ``Static``-based panels that render the
attention queue. They poke ``set_*`` APIs that don't require a mounted
Textual app and read the rendered content directly from
``Widget.render()``. Tests that touch ``_post_selection`` mount the
panel inside a scratch :class:`textual.app.App`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from io import StringIO
from typing import Protocol

import pytest
from rich.console import Console
from rich.table import Table
from textual.app import App, ComposeResult

from muxdeck.controllers import (
    AttentionFilterState,
    AttentionItemView,
    AttentionSelectedItemView,
    AttentionSummaryView,
    DashboardAgentListItemView,
    DashboardSelectedAgentView,
)
from muxdeck.domain.enums import AgentStatus
from muxdeck.services.operator_status_service import OperatorStatus, OperatorStatusKind
from muxdeck.widgets.attention import (
    AttentionActivityPanel,
    AttentionDetailPanel,
    AttentionListPanel,
    AttentionSummaryBar,
    _status_style,
)

_TS = datetime(2025, 1, 1, 12, 30, 45, tzinfo=UTC)


class _Renderable(Protocol):
    def render(self) -> object: ...


def _render(widget: _Renderable) -> str:
    # Static widgets cache their last update on ``widget.renderable``;
    # that's the canonical post-update Text and gives us the visible
    # plain content without going through Rich's screen renderer.
    rendered = getattr(widget, "renderable", None)
    plain = getattr(rendered, "plain", None)
    if isinstance(plain, str):
        return plain
    renderable = widget.render()
    plain = getattr(renderable, "plain", None)
    if isinstance(plain, str):
        return plain
    inner = getattr(renderable, "_renderable", None)
    inner_plain = getattr(inner, "plain", None)
    if isinstance(inner_plain, str):
        return inner_plain
    # Widgets that paint a ``rich.console.Group`` (used to combine the
    # table with scroll indicators) reach here. Drive a real
    # :class:`rich.console.Console` against the inner renderable so we
    # see the materialised text.
    target = inner if inner is not None else (rendered if rendered is not None else renderable)
    buffer = StringIO()
    Console(file=buffer, width=200, force_terminal=False, color_system=None).print(target)
    return buffer.getvalue()


def _render_table(table: Table) -> str:
    buffer = StringIO()
    Console(file=buffer, width=200, force_terminal=False, color_system=None).print(table)
    return buffer.getvalue()


def _operator_status(
    *,
    kind: OperatorStatusKind = OperatorStatusKind.WAITING_INPUT,
    label: str = "needs input",
    headline: str = "needs operator input",
    reason: str = "blocked on review",
    is_critical: bool = False,
) -> OperatorStatus:
    tone = (
        "warning"
        if kind in {OperatorStatusKind.WAITING_INPUT, OperatorStatusKind.STALE}
        else (
            "error" if kind in {OperatorStatusKind.BLOCKED, OperatorStatusKind.FAILED} else "info"
        )
    )
    return OperatorStatus(
        kind=kind,
        label=label,
        headline=headline,
        reason=reason,
        tone=tone,  # type: ignore[arg-type]
        needs_attention=kind != OperatorStatusKind.COMPLETED,
        is_critical=is_critical,
    )


def _item(
    *,
    alert_id: str = "alert-1",
    agent_id: str = "agent-1",
    agent_name: str = "node",
    severity: str = "warning",
    operator_status: OperatorStatus | None = None,
    message: str = "needs review",
    branch: str | None = "feature/x",
    worktree_name: str | None = "wt",
    task_title: str | None = "audit",
    pane_id: str = "%1",
    unread: bool = False,
) -> AttentionItemView:
    return AttentionItemView(
        alert_id=alert_id,
        agent_id=agent_id,
        agent_name=agent_name,
        severity=severity,  # type: ignore[arg-type]
        operator_status=operator_status or _operator_status(),
        message=message,
        occurred_at=_TS,
        branch=branch,
        worktree_name=worktree_name,
        task_title=task_title,
        pane_id=pane_id,
        unread=unread,
    )


def _agent_view(*, recent: tuple[str, ...] = ()) -> DashboardSelectedAgentView:
    item = DashboardAgentListItemView(
        agent_id="agent-1",
        name="node",
        status=AgentStatus.WAITING_INPUT,
        repo_name="repo",
        branch="feature/x",
        worktree_name="wt",
        pane_id="%1",
        task_title="audit",
        worktree_path="/repo/wt",
        latest_session_id="session-1",
        last_event_kind="agent.updated",
        last_log_at=_TS,
        last_seen_at=_TS,
        started_at=_TS,
        idle_seconds=10,
        needs_attention=True,
        attention_reason="needs input",
        token_total=1234,
        estimated_cost_usd="0.42",
    )
    return DashboardSelectedAgentView(
        item=item,
        repo_root="/repo",
        worktree_id="wt-1",
        session_count=1,
        open_session_id="session-1",
        copilot_session_id=None,
        latest_event_kind="agent.updated",
        latest_event_severity="warning",
        latest_event_at=_TS,
        log_preview=(),
        recent_events=recent,
    )


# ── _status_style ───────────────────────────────────────────────────


def test_status_style_returns_a_style_string_for_each_kind() -> None:
    """``_status_style`` must return a non-empty style for every kind
    in the lookup. STARTING is intentionally not present and must
    raise ``KeyError`` (the prior version of this test ``try``/``except``-d
    without an assertion, which would silently pass if STARTING ever
    started returning a value).
    """
    for kind in OperatorStatusKind:
        if kind is OperatorStatusKind.STARTING:
            with pytest.raises(KeyError):
                _status_style(kind)
            continue
        style = _status_style(kind)
        assert isinstance(style, str)
        assert style


# ── AttentionSummaryBar ─────────────────────────────────────────────


def test_summary_bar_shows_counts_and_unread_filter_chip() -> None:
    bar = AttentionSummaryBar()
    bar.set_state(
        AttentionSummaryView(total_items=5, unread_items=3, critical_items=1),
        AttentionFilterState(unread_only=True),
    )
    rendered = _render(bar)
    assert "5" in rendered
    assert "3" in rendered
    assert "1" in rendered
    assert "filtered unread" in rendered


def test_summary_bar_skips_filter_chip_when_no_filter_active() -> None:
    bar = AttentionSummaryBar()
    bar.set_state(
        AttentionSummaryView(total_items=0, unread_items=0, critical_items=0),
        AttentionFilterState(unread_only=False),
    )
    rendered = _render(bar)
    assert "filtered unread" not in rendered
    assert "active" in rendered


# ── AttentionListPanel ──────────────────────────────────────────────


def test_list_panel_shows_empty_state_when_no_items() -> None:
    panel = AttentionListPanel(widget_id="attention-list")
    panel.set_items((), selected_agent_id=None)
    rendered = _render(panel)
    assert "no unread attention items" in rendered


def test_list_panel_renders_rows_and_unread_marker() -> None:
    panel = AttentionListPanel(widget_id="attention-list")
    items = (
        _item(agent_id="a-1", agent_name="alpha", unread=True, severity="error"),
        _item(agent_id="a-2", agent_name="beta", unread=False, severity="info"),
    )
    # Bypass set_items (which posts a selection message) and exercise
    # _build_table directly so we don't need a running app.
    panel._items = items
    panel._selected_index = 1
    rendered = _render_table(panel._build_table())
    assert "alpha" in rendered
    assert "beta" in rendered
    assert "●" in rendered


@pytest.mark.asyncio
async def test_list_panel_falls_back_to_clamped_index_when_selection_missing() -> None:
    """When ``selected_agent_id`` doesn't match any item, the panel
    must clamp ``_selected_index`` against the new ``len(items) - 1``.

    The earlier test re-implemented the production expression inline
    and asserted on its own copy — production was never invoked, so a
    bug in the real ``set_items`` would not have been detected. This
    version mounts the panel, drives ``set_items`` directly, and reads
    the resulting ``_selected_index`` after the truncation.
    """
    panel = AttentionListPanel(widget_id="attention-list")

    class _Harness(App[None]):
        def compose(self) -> ComposeResult:
            yield panel

    seed_items = (
        _item(agent_id="a-1"),
        _item(agent_id="a-2"),
        _item(agent_id="a-3"),
    )
    async with _Harness().run_test() as pilot:
        panel.set_items(seed_items, selected_agent_id="a-3")
        await pilot.pause()
        assert panel._selected_index == 2  # exact-match selection
        # Now shrink the list and pass an unknown id. With 1 item left
        # the clamp must produce index 0; the earlier index 2 is no
        # longer valid.
        panel.set_items((_item(agent_id="a-1"),), selected_agent_id="never")
        await pilot.pause()
        assert panel._selected_index == 0


def test_list_panel_move_cursor_no_op_when_empty() -> None:
    """Removing the empty-items guard would cause ``move_cursor`` to
    invoke ``focus()``/``_refresh_table()``/``_post_selection`` on an
    empty list. Spy on those side-effects so the guard's absence would
    surface as an unexpected call (the earlier test only inspected the
    final ``_selected_index`` which is 0 either way).
    """
    panel = AttentionListPanel(widget_id="attention-list")
    refresh_calls: list[bool] = []
    post_calls: list[int] = []
    panel._refresh_table = lambda: refresh_calls.append(True)  # type: ignore[method-assign]
    panel._post_selection = lambda index: post_calls.append(index)  # type: ignore[method-assign]
    panel.move_cursor(1)
    assert panel._selected_index == 0
    assert refresh_calls == [], "guard removed: _refresh_table fired on empty list"
    assert post_calls == [], "guard removed: _post_selection fired on empty list"


def test_list_panel_renders_branch_dash_when_missing() -> None:
    panel = AttentionListPanel(widget_id="attention-list")
    panel._items = (_item(branch=None),)
    panel._selected_index = 0
    rendered = _render_table(panel._build_table())
    assert "-" in rendered


@pytest.mark.asyncio
async def test_list_panel_set_items_inside_app_posts_selection() -> None:
    panel = AttentionListPanel(widget_id="attention-list")

    class _Harness(App[None]):
        def compose(self) -> ComposeResult:
            yield panel

    items = (_item(agent_id="a-1"), _item(agent_id="a-2"), _item(agent_id="a-3"))
    async with _Harness().run_test() as pilot:
        panel.set_items(items, selected_agent_id="a-2")
        await pilot.pause()
        assert panel._selected_index == 1


@pytest.mark.asyncio
async def test_list_panel_move_cursor_inside_app_clamps_to_bounds() -> None:
    panel = AttentionListPanel(widget_id="attention-list")

    class _Harness(App[None]):
        def compose(self) -> ComposeResult:
            yield panel

    items = (_item(agent_id="a-1"), _item(agent_id="a-2"), _item(agent_id="a-3"))
    async with _Harness().run_test() as pilot:
        panel.set_items(items, selected_agent_id="a-1")
        await pilot.pause()
        panel.move_cursor(1)
        assert panel._selected_index == 1
        panel.move_cursor(50)
        assert panel._selected_index == 2
        panel.move_cursor(-99)
        assert panel._selected_index == 0
        panel.focus_list()


@pytest.mark.asyncio
async def test_list_panel_windows_long_inboxes_to_keep_selection_visible() -> None:
    """Long inboxes must clip rendered rows around the cursor and surface
    ``↑/↓ N more`` indicators so cursor moves never disappear off-screen.
    """

    panel = AttentionListPanel(widget_id="attention-list")

    class _Harness(App[None]):
        CSS = "AttentionListPanel { height: 8; }"

        def compose(self) -> ComposeResult:
            yield panel

    items = tuple(_item(agent_id=f"a-{i}", agent_name=f"agent-{i}") for i in range(40))
    async with _Harness().run_test(size=(160, 12)) as pilot:
        await pilot.pause()
        panel.set_items(items, selected_agent_id="a-20")
        await pilot.pause()
        rendered = _render(panel)
        # Selected row in view + scroll indicators top and bottom.
        assert "agent-20" in rendered
        assert "more above" in rendered
        assert "more below" in rendered
        # Far-away rows are clipped.
        assert "agent-0 " not in rendered
        assert "agent-39" not in rendered
        # Move to the bottom — viewport must follow the cursor.
        panel.move_cursor(50)
        await pilot.pause()
        rendered = _render(panel)
        assert "agent-39" in rendered
        assert "agent-0 " not in rendered


# ── AttentionDetailPanel ────────────────────────────────────────────


def test_detail_panel_shows_empty_state_when_no_selection() -> None:
    panel = AttentionDetailPanel()
    panel.set_item(None)
    rendered = _render(panel)
    assert "triage" in rendered
    assert "no attention item selected" in rendered


def test_detail_panel_renders_full_item_with_fields_and_recent_events() -> None:
    panel = AttentionDetailPanel()
    panel.set_item(
        AttentionSelectedItemView(
            item=_item(unread=True),
            agent=_agent_view(
                recent=("event 1", "event 2", "event 3", "event 4", "event 5"),
            ),
        )
    )
    rendered = _render(panel)
    assert "node" in rendered
    assert "needs operator input" in rendered
    assert "feature/x" in rendered
    assert "audit" in rendered
    assert "%1" in rendered
    assert "yes" in rendered  # unread → yes
    assert "recent" in rendered
    # Should only render the last 4 events from the tail (recent[-4:])
    assert "event 2" in rendered
    assert "event 5" in rendered
    assert "event 1" not in rendered


def test_detail_panel_skips_blank_value_fields() -> None:
    panel = AttentionDetailPanel()
    panel.set_item(
        AttentionSelectedItemView(
            item=_item(branch=None, worktree_name=None, task_title=None),
            agent=_agent_view(),
        )
    )
    rendered = _render(panel)
    assert "branch" not in rendered.lower().split()
    assert "worktree" not in rendered.lower().split()


def test_detail_panel_renders_unread_no_when_item_read() -> None:
    """The unread row must render as ``"unread   no"`` (label + value)
    when the item is read.

    The earlier assertion ``assert "no" in rendered`` would pass even
    if the unread row was missing entirely, because the default
    ``agent_name="node"`` happens to contain the substring ``"no"``.
    Match on the labelled row instead.
    """
    panel = AttentionDetailPanel()
    panel.set_item(
        AttentionSelectedItemView(
            item=_item(unread=False),
            agent=_agent_view(),
        )
    )
    rendered = _render(panel)
    # The label is left-padded to 8 chars; assert the labelled row
    # actually appears, not just the literal "no" anywhere.
    assert "unread" in rendered
    # Find the line carrying the unread row and verify its value is
    # exactly "no", not absent or "yes".
    unread_lines = [line for line in rendered.splitlines() if "unread" in line]
    assert unread_lines, f"unread row missing from detail panel: {rendered!r}"
    assert any(line.strip().endswith("no") for line in unread_lines), (
        f"unread row should end in 'no' for read item: {unread_lines!r}"
    )


# ── AttentionActivityPanel ──────────────────────────────────────────


def test_activity_panel_shows_empty_inbox_state() -> None:
    panel = AttentionActivityPanel()
    panel.set_items(())
    rendered = _render(panel)
    assert "queue" in rendered
    assert "inbox clear" in rendered


def test_activity_panel_renders_items_with_unread_marker() -> None:
    panel = AttentionActivityPanel()
    items = tuple(
        _item(
            agent_id=f"a-{i}",
            agent_name=f"agent-{i}",
            unread=(i % 2 == 0),
        )
        for i in range(8)
    )
    panel.set_items(items)
    rendered = _render(panel)
    assert "agent-0" in rendered
    # Cap at 6 entries
    assert "agent-6" not in rendered
    assert "new " in rendered
    assert "needs operator input" in rendered

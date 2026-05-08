"""Tests for the AttentionScreen behaviour."""

from __future__ import annotations

import asyncio
import unittest
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import cast

from textual.app import App, ComposeResult

from muxdeck.app import MuxdeckRuntime
from muxdeck.controllers.attention_controller import (
    AttentionFilterState,
    AttentionItemView,
    AttentionSelectedItemView,
    AttentionState,
    AttentionSummaryView,
)
from muxdeck.controllers.dashboard_controller import (
    DashboardAgentListItemView,
    DashboardSelectedAgentView,
)
from muxdeck.domain.enums import AgentStatus
from muxdeck.screens.attention import AttentionScreen
from muxdeck.services.attention_service import AttentionNotification
from muxdeck.services.operator_status_service import (
    OperatorStatus,
    OperatorStatusKind,
)
from muxdeck.widgets.attention import AttentionListPanel
from muxdeck.widgets.common import KeyHintFooter

_NOW = datetime(2025, 1, 1, 12, 0, tzinfo=UTC)


def _operator_status(*, needs_attention: bool = True) -> OperatorStatus:
    return OperatorStatus(
        kind=OperatorStatusKind.WAITING_INPUT,
        label="waiting",
        headline="needs you",
        reason="waiting for input",
        tone="warning",
        needs_attention=needs_attention,
    )


def _list_item(agent_id: str, *, name: str | None = None) -> DashboardAgentListItemView:
    return DashboardAgentListItemView(
        agent_id=agent_id,
        name=name or agent_id,
        status=AgentStatus.RUNNING,
        repo_name="repo",
        branch="main",
        worktree_name="wt",
        pane_id="%1",
        task_title="task",
        worktree_path="/wt",
        latest_session_id=None,
        last_event_kind=None,
        last_log_at=None,
        last_seen_at=_NOW,
        started_at=_NOW,
        idle_seconds=0,
        needs_attention=True,
        attention_reason="needs input",
        token_total=None,
        estimated_cost_usd=None,
        operator_status=_operator_status(),
    )


def _attention_item(agent_id: str, *, unread: bool = True) -> AttentionItemView:
    return AttentionItemView(
        alert_id=f"{agent_id}:waiting_input",
        agent_id=agent_id,
        agent_name=agent_id,
        severity="warning",
        operator_status=_operator_status(),
        message="needs input",
        occurred_at=_NOW,
        branch="main",
        worktree_name="wt",
        task_title="task",
        pane_id="%1",
        unread=unread,
    )


def _selected(agent_id: str) -> AttentionSelectedItemView:
    item = _attention_item(agent_id)
    list_item = _list_item(agent_id)
    return AttentionSelectedItemView(
        item=item,
        agent=DashboardSelectedAgentView(
            item=list_item,
            repo_root="/repo",
            worktree_id="wt-1",
            session_count=0,
            open_session_id=None,
            copilot_session_id=None,
            latest_event_kind=None,
            latest_event_severity=None,
            latest_event_at=None,
            log_preview=(),
        ),
    )


def _state(
    *,
    items: tuple[AttentionItemView, ...] = (),
    selected_agent_id: str | None = None,
    selected_item: AttentionSelectedItemView | None = None,
    notifications: tuple[AttentionNotification, ...] = (),
    filters: AttentionFilterState | None = None,
    summary: AttentionSummaryView | None = None,
) -> AttentionState:
    return AttentionState(
        generated_at=_NOW,
        filters=filters or AttentionFilterState(),
        summary=summary
        or AttentionSummaryView(
            total_items=len(items),
            unread_items=sum(1 for item in items if item.unread),
            critical_items=0,
        ),
        items=items,
        selected_agent_id=selected_agent_id,
        selected_item=selected_item,
        notifications=notifications,
    )


@dataclass(slots=True)
class _RecordingController:
    state: AttentionState
    mark_read_calls: list[str] = field(default_factory=list)
    mark_all_read_calls: int = 0
    build_state_calls: list[tuple[AttentionFilterState, str | None, int]] = field(
        default_factory=list
    )

    def build_state(
        self,
        *,
        filters: AttentionFilterState,
        selected_agent_id: str | None,
        preview_line_limit: int,
    ) -> AttentionState:
        self.build_state_calls.append((filters, selected_agent_id, preview_line_limit))
        return self.state

    def mark_read(self, alert_id: str) -> None:
        self.mark_read_calls.append(alert_id)

    def mark_all_read(self) -> None:
        self.mark_all_read_calls += 1


class _MinimalGeneral:
    log_preview_lines = 8
    discovery_interval_sec = 2


class _MinimalConfig:
    general = _MinimalGeneral()


class _Harness(App[None]):
    def __init__(self, runtime: MuxdeckRuntime) -> None:
        super().__init__()
        self._runtime = runtime
        self.bell_count = 0
        self.notify_calls: list[tuple[str, str | None, str]] = []

    def compose(self) -> ComposeResult:
        return iter(())

    def bell(self) -> None:  # type: ignore[override]
        self.bell_count += 1

    def notify(  # type: ignore[override]
        self,
        message: str,
        *,
        title: str | None = None,
        severity: str = "information",
        timeout: float | None = None,
        markup: bool = True,
    ) -> None:
        del timeout, markup
        self.notify_calls.append((message, title, severity))


def _runtime_with(controller: object | None) -> MuxdeckRuntime:
    return cast(
        MuxdeckRuntime,
        type(
            "_FakeRuntime",
            (),
            {"attention": controller, "config": _MinimalConfig()},
        )(),
    )


class AttentionRefreshTests(unittest.TestCase):
    def test_refresh_without_controller_marks_inbox_unavailable(self) -> None:
        async def scenario() -> str:
            runtime = _runtime_with(None)
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = AttentionScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                screen.refresh_data()
                await pilot.pause()
                return app.screen.query_one(KeyHintFooter).status

        assert asyncio.run(scenario()) == "attention inbox unavailable"

    def test_refresh_with_empty_inbox_marks_inbox_clear(self) -> None:
        async def scenario() -> str:
            controller = _RecordingController(state=_state())
            runtime = _runtime_with(controller)
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = AttentionScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                screen.refresh_data()
                await pilot.pause()
                return app.screen.query_one(KeyHintFooter).status

        assert asyncio.run(scenario()) == "attention inbox clear"

    def test_refresh_with_items_summarizes_in_status(self) -> None:
        async def scenario() -> str:
            items = (_attention_item("a"), _attention_item("b", unread=False))
            controller = _RecordingController(
                state=_state(
                    items=items,
                    selected_agent_id="a",
                    selected_item=_selected("a"),
                    summary=AttentionSummaryView(total_items=2, unread_items=1, critical_items=1),
                ),
            )
            runtime = _runtime_with(controller)
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = AttentionScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                screen.refresh_data()
                await pilot.pause()
                return app.screen.query_one(KeyHintFooter).status

        assert asyncio.run(scenario()) == "2 attention · 1 unread · 1 critical"


class AttentionNotificationDispatchTests(unittest.TestCase):
    def test_emit_notifications_sends_first_with_summary_suffix(self) -> None:
        async def scenario() -> tuple[int, list[tuple[str, str | None, str]]]:
            items = (_attention_item("a"),)
            notifications = (
                AttentionNotification(
                    alert_id="a:1", severity="error", title="A failed", message="boom"
                ),
                AttentionNotification(
                    alert_id="b:1", severity="warning", title="B slow", message="lag"
                ),
            )
            controller = _RecordingController(
                state=_state(
                    items=items,
                    selected_agent_id="a",
                    selected_item=_selected("a"),
                    notifications=notifications,
                ),
            )
            runtime = _runtime_with(controller)
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = AttentionScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                screen.refresh_data()
                await pilot.pause()
            return app.bell_count, app.notify_calls

        bell_count, calls = asyncio.run(scenario())
        # Two refresh runs hit the bell because on_show + explicit refresh both
        # see the same notifications. We only assert that at least one bell
        # was rung and the notify payload is consistent with the notifications.
        assert bell_count >= 1
        # Each call describes the head notification and includes the +1 more
        # critical suffix because there are 2 notifications.
        assert all(payload == ("boom (+1 more critical)", "A failed", "error") for payload in calls)
        assert calls  # at least one call was made

    def test_emit_notifications_with_single_notification_omits_suffix(self) -> None:
        async def scenario() -> list[tuple[str, str | None, str]]:
            notifications = (
                AttentionNotification(
                    alert_id="a:1",
                    severity="info",
                    title="info title",
                    message="info body",
                ),
            )
            controller = _RecordingController(
                state=_state(notifications=notifications),
            )
            runtime = _runtime_with(controller)
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = AttentionScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                screen.refresh_data()
                await pilot.pause()
            return app.notify_calls

        calls = asyncio.run(scenario())
        # 'info' severity maps to 'information'.
        assert all(call == ("info body", "info title", "information") for call in calls)
        assert calls


class AttentionSelectionAndCursorTests(unittest.TestCase):
    def test_attention_selected_message_with_same_id_is_a_noop(self) -> None:
        async def scenario() -> int:
            items = (_attention_item("a"),)
            controller = _RecordingController(
                state=_state(
                    items=items,
                    selected_agent_id="a",
                    selected_item=_selected("a"),
                ),
            )
            runtime = _runtime_with(controller)
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = AttentionScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                screen.refresh_data()
                await pilot.pause()
                before = len(controller.build_state_calls)
                screen.on_attention_list_panel_attention_selected(
                    AttentionListPanel.AttentionSelected("a"),
                )
                await pilot.pause()
            return len(controller.build_state_calls) - before

        # Same agent_id should NOT trigger another build_state.
        assert asyncio.run(scenario()) == 0

    def test_attention_selected_message_with_new_id_triggers_refresh(self) -> None:
        async def scenario() -> int:
            items = (_attention_item("a"), _attention_item("b"))
            controller = _RecordingController(
                state=_state(
                    items=items,
                    selected_agent_id="a",
                    selected_item=_selected("a"),
                ),
            )
            runtime = _runtime_with(controller)
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = AttentionScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                screen.refresh_data()
                await pilot.pause()
                before = len(controller.build_state_calls)
                screen.on_attention_list_panel_attention_selected(
                    AttentionListPanel.AttentionSelected("b"),
                )
                await pilot.pause()
            return len(controller.build_state_calls) - before

        assert asyncio.run(scenario()) >= 1

    def test_cursor_actions_call_panel_move_cursor(self) -> None:
        async def scenario() -> int:
            items = (_attention_item("a"), _attention_item("b"), _attention_item("c"))
            controller = _RecordingController(
                state=_state(
                    items=items,
                    selected_agent_id="a",
                    selected_item=_selected("a"),
                ),
            )
            runtime = _runtime_with(controller)
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = AttentionScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                screen.refresh_data()
                # Note: do NOT pause between cursor moves; otherwise the
                # AttentionSelected message would re-run refresh_data and
                # snap the panel back to the controller's selected_agent_id.
                screen.action_cursor_down()
                screen.action_cursor_down()
                screen.action_cursor_up()
                panel = app.screen.query_one(AttentionListPanel)
            return panel._selected_index  # type: ignore[attr-defined]

        # Down twice + up once = +1 from start.
        assert asyncio.run(scenario()) == 1


class AttentionToggleAndMarkTests(unittest.TestCase):
    def test_toggle_unread_flips_filter_and_updates_status(self) -> None:
        async def scenario() -> tuple[bool, bool]:
            controller = _RecordingController(state=_state())
            runtime = _runtime_with(controller)
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = AttentionScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                screen.action_toggle_unread()
                await pilot.pause()
                first = screen._filters.unread_only
                screen.action_toggle_unread()
                await pilot.pause()
                second = screen._filters.unread_only
            return first, second

        first, second = asyncio.run(scenario())
        assert first is True
        assert second is False

    def test_mark_selected_read_without_controller(self) -> None:
        async def scenario() -> str:
            runtime = _runtime_with(None)
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = AttentionScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                screen.action_mark_selected_read()
                await pilot.pause()
                return app.screen.query_one(KeyHintFooter).status

        assert asyncio.run(scenario()) == "attention inbox unavailable"

    def test_mark_selected_read_no_selection(self) -> None:
        async def scenario() -> str:
            controller = _RecordingController(state=_state())
            runtime = _runtime_with(controller)
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = AttentionScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                screen.refresh_data()
                await pilot.pause()
                screen.action_mark_selected_read()
                await pilot.pause()
                return app.screen.query_one(KeyHintFooter).status

        assert asyncio.run(scenario()) == "no attention item selected"

    def test_mark_selected_read_with_selection(self) -> None:
        async def scenario() -> list[str]:
            selected = _selected("a")
            controller = _RecordingController(
                state=_state(
                    items=(_attention_item("a"),),
                    selected_agent_id="a",
                    selected_item=selected,
                ),
            )
            runtime = _runtime_with(controller)
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = AttentionScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                screen.refresh_data()
                await pilot.pause()
                screen.action_mark_selected_read()
                await pilot.pause()
            return controller.mark_read_calls

        calls = asyncio.run(scenario())
        assert calls == ["a:waiting_input"]

    def test_mark_all_read_without_controller(self) -> None:
        async def scenario() -> str:
            runtime = _runtime_with(None)
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = AttentionScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                screen.action_mark_all_read()
                await pilot.pause()
                return app.screen.query_one(KeyHintFooter).status

        assert asyncio.run(scenario()) == "attention inbox unavailable"

    def test_mark_all_read_calls_controller(self) -> None:
        async def scenario() -> int:
            controller = _RecordingController(state=_state())
            runtime = _runtime_with(controller)
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = AttentionScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                screen.action_mark_all_read()
                await pilot.pause()
            return controller.mark_all_read_calls

        assert asyncio.run(scenario()) == 1


class AttentionPreviewLineLimitTests(unittest.TestCase):
    def test_preview_line_limit_caps_at_200(self) -> None:
        controller = _RecordingController(state=_state())

        class _GeneralBig:
            log_preview_lines = 500
            discovery_interval_sec = 2

        class _ConfigBig:
            general = _GeneralBig()

        runtime = cast(
            MuxdeckRuntime,
            type(
                "_FakeRuntime",
                (),
                {"attention": controller, "config": _ConfigBig()},
            )(),
        )

        async def scenario() -> int:
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = AttentionScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                screen.refresh_data()
                await pilot.pause()
            assert all(call[2] == 200 for call in controller.build_state_calls), (
                controller.build_state_calls
            )
            return controller.build_state_calls[-1][2]

        assert asyncio.run(scenario()) == 200

    def test_preview_line_limit_uses_configured_value_when_below_cap(self) -> None:
        controller = _RecordingController(state=_state())

        class _GeneralSmall:
            log_preview_lines = 5
            discovery_interval_sec = 2

        class _ConfigSmall:
            general = _GeneralSmall()

        runtime = cast(
            MuxdeckRuntime,
            type(
                "_FakeRuntime",
                (),
                {"attention": controller, "config": _ConfigSmall()},
            )(),
        )

        async def scenario() -> int:
            app = _Harness(runtime)
            async with app.run_test(size=(160, 60)) as pilot:
                screen = AttentionScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                screen.refresh_data()
                await pilot.pause()
            return controller.build_state_calls[-1][2]

        assert asyncio.run(scenario()) == 5

from __future__ import annotations

from datetime import UTC, datetime
from typing import Protocol, cast

import pytest
from textual.widgets import Input, Static

from copilot_commander.app import CommanderApp, CommanderRuntime
from copilot_commander.controllers import (
    DashboardAgentListItemView,
    DashboardFilterState,
    DashboardHealthSummary,
    DashboardSelectedAgentView,
    DashboardSort,
    DashboardState,
)
from copilot_commander.domain.enums import AgentStatus
from copilot_commander.domain.models import Session
from copilot_commander.widgets.common import KeyHintFooter, TabBar

_TIMESTAMP = datetime(2025, 1, 1, 12, tzinfo=UTC)


class _FakeConfig:
    class General:
        discovery_interval_sec = 60
        log_preview_lines = 8
        idle_threshold_sec = 300

    general = General()


class _FakeStore:
    def __init__(self) -> None:
        self.sessions: dict[str, Session] = {
            "session-1": Session(id="session-1", agent_id="agent-1", created_at=_TIMESTAMP),
        }

    def get_session(self, session_id: str) -> Session | None:
        return self.sessions.get(session_id)

    def list_sessions(self, agent_id: str | None = None) -> tuple[Session, ...]:
        sessions = tuple(self.sessions.values())
        if agent_id is None:
            return sessions
        return tuple(session for session in sessions if session.agent_id == agent_id)


class _FakeDashboardController:
    def build_state(self, **kwargs: object) -> DashboardState:
        del kwargs
        item = DashboardAgentListItemView(
            agent_id="agent-1",
            name="Agent",
            status=AgentStatus.RUNNING,
            repo_name="repo",
            branch="main",
            worktree_name="wt",
            pane_id="%1",
            task_title="triage",
            worktree_path="/repo/wt",
            latest_session_id="session-1",
            last_event_kind="agent.updated",
            last_log_at=_TIMESTAMP,
            last_seen_at=_TIMESTAMP,
            started_at=_TIMESTAMP,
            idle_seconds=0,
            needs_attention=False,
            attention_reason=None,
            token_total=0,
            estimated_cost_usd="0.00",
        )
        return DashboardState(
            generated_at=_TIMESTAMP,
            metrics=(),
            filters=DashboardFilterState(),
            sort=DashboardSort(),
            health=DashboardHealthSummary(
                tone="healthy",
                message="ok",
                total_agents=1,
                active_agents=1,
                attention_agents=0,
                waiting_input_agents=0,
                blocked_agents=0,
                error_agents=0,
            ),
            alerts=(),
            agents=(item,),
            selected_agent_id=item.agent_id,
            selected_agent=DashboardSelectedAgentView(
                item=item,
                repo_root="/repo",
                worktree_id="wt-1",
                session_count=1,
                open_session_id="session-1",
                copilot_session_id=None,
                latest_event_kind="agent.updated",
                latest_event_severity="info",
                latest_event_at=_TIMESTAMP,
                log_preview=(),
            ),
        )


def _build_runtime() -> CommanderRuntime:
    return cast(
        CommanderRuntime,
        type(
            "FakeRuntime",
            (),
            {
                "config": _FakeConfig(),
                "store": _FakeStore(),
                "dashboard": _FakeDashboardController(),
                "worktrees": object(),
                "replay": object(),
                "agents": object(),
                "synchronizer": None,
                "sync_store": None,
                "attention": None,
                "operations": None,
                "fleet": None,
            },
        )(),
    )


class _WidgetWithRender(Protocol):
    def render(self) -> object: ...


class _HelpActions(Protocol):
    def action_focus_filter(self) -> None: ...


def _render_plain(widget: _WidgetWithRender) -> str:
    renderable = widget.render()
    plain = getattr(renderable, "plain", None)
    return plain if isinstance(plain, str) else str(renderable)


@pytest.mark.asyncio
async def test_ui_mode_toggles_update_classes_subtitle_tabbar_and_footer() -> None:
    app = CommanderApp(_build_runtime())

    async with app.run_test() as pilot:
        await pilot.pause()

        app.action_toggle_density()
        app.action_toggle_glyphs()
        app.action_toggle_contrast()
        app.action_toggle_decorations()
        app.action_toggle_log_wrap()
        await pilot.pause()

        assert app.sub_title == "Operator Console · comfy · ascii · high · plain · wrap"
        assert {
            "ux-density-comfortable",
            "ux-glyphs-ascii",
            "ux-contrast-high",
            "ux-decor-reduced",
            "ux-wrap-logs",
        } <= set(app.classes)

        app.screen.query_one("#dashboard-filter-input", Input).focus()
        await pilot.pause()

        tab_text = _render_plain(app.screen.query_one(TabBar))
        footer_text = _render_plain(app.screen.query_one(KeyHintFooter))

        for badge in ("comfy", "ascii", "high", "plain", "wrap"):
            assert badge in tab_text
            assert badge in footer_text
        assert "focus" in footer_text
        assert "dashboard filter" in footer_text


@pytest.mark.asyncio
async def test_system_commands_and_help_escape_flow() -> None:
    app = CommanderApp(_build_runtime())

    async with app.run_test() as pilot:
        await pilot.pause()

        titles = {command.title for command in app.get_system_commands(app.screen)}
        assert {
            "Open help",
            "Toggle comfortable density",
            "Toggle simple glyphs",
            "Toggle high contrast",
            "Toggle reduced decoration",
            "Toggle log wrap",
            "Focus filter",
            "Toggle attention filter",
            "Toggle completed items",
        } <= titles
        assert "Reset UI modes" not in titles

        app.action_toggle_density()
        await pilot.pause()

        titles = {command.title for command in app.get_system_commands(app.screen)}
        assert "Reset UI modes" in titles

        app.action_show_help()
        await pilot.pause()

        filter_input = app.screen.query_one("#help-filter-input", Input)
        content = app.screen.query_one("#help-content", Static)

        cast(_HelpActions, app.screen).action_focus_filter()
        await pilot.pause()
        assert filter_input.has_focus is True

        filter_input.value = "contrast"
        await pilot.pause()
        assert "high contrast" in _render_plain(content).lower()

        await pilot.press("escape")
        await pilot.pause()
        assert filter_input.has_focus is False
        assert filter_input.value == "contrast"

        await pilot.press("escape")
        await pilot.pause()
        assert filter_input.value == ""

        await pilot.press("escape")
        await pilot.pause()
        assert app.screen.__class__.__name__ == "DashboardScreen"

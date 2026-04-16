# ruff: noqa: ANN201

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from typing import Literal, cast

from copilot_commander.app import CommanderApp, CommanderRuntime
from copilot_commander.controllers.attention_controller import AttentionController
from copilot_commander.controllers.dashboard_controller import (
    DashboardAlertView,
    DashboardFilterState,
    DashboardHealthSummary,
    DashboardSort,
    DashboardState,
)
from copilot_commander.services.attention_service import AttentionInboxService


class _FakeDashboardController:
    def build_state(
        self,
        *,
        filters: DashboardFilterState | None = None,
        selected_agent_id: str | None = None,
        preview_line_limit: int = 8,
        alert_limit: int = 20,
    ) -> DashboardState:
        del filters, selected_agent_id, preview_line_limit, alert_limit
        return _empty_state()

    def build_selected_agent_view(self, *_args: object, **_kwargs: object) -> object:
        raise NotImplementedError


class _RecordingNotifier:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, Literal["low", "normal", "critical"]]] = []

    def notify(
        self,
        title: str,
        body: str,
        urgency: Literal["low", "normal", "critical"],
    ) -> None:
        self.calls.append((title, body, urgency))


class _MinimalConfig:
    class General:
        discovery_interval_sec = 2

    general = General()


def _empty_state() -> DashboardState:
    return DashboardState(
        generated_at=datetime(2025, 1, 1, 12, tzinfo=UTC),
        metrics=(),
        filters=DashboardFilterState(),
        sort=DashboardSort(),
        health=DashboardHealthSummary(
            tone="ok",
            message="",
            total_agents=0,
            active_agents=0,
            attention_agents=0,
            waiting_input_agents=0,
            blocked_agents=0,
            error_agents=0,
        ),
        alerts=(),
        agents=(),
        selected_agent_id=None,
        selected_agent=None,
    )


def _state_with_alerts(*alerts: DashboardAlertView) -> DashboardState:
    return replace(_empty_state(), alerts=tuple(alerts))


def _alert(
    alert_id: str,
    severity: Literal["info", "warning", "error"] = "error",
    *,
    title: str = "failed",
    message: str = "boom",
) -> DashboardAlertView:
    return DashboardAlertView(
        agent_id=alert_id.split(":", 1)[0],
        agent_name=alert_id.split(":", 1)[0],
        severity=severity,
        title=title,
        message=message,
        occurred_at=datetime(2025, 1, 1, 12, tzinfo=UTC),
        alert_id=alert_id,
    )


def _make_runtime(
    *, attention: AttentionController | None, notifier: _RecordingNotifier | None
) -> CommanderRuntime:
    return cast(
        CommanderRuntime,
        type(
            "FakeRuntime",
            (),
            {
                "config": _MinimalConfig(),
                "store": object(),
                "dashboard": object(),
                "worktrees": object(),
                "replay": object(),
                "agents": object(),
                "synchronizer": None,
                "sync_store": None,
                "attention": attention,
                "notifier": notifier,
            },
        )(),
    )


def test_dispatch_attention_notifications_calls_notifier_with_mapped_urgency() -> None:
    attention = AttentionController(_FakeDashboardController(), AttentionInboxService())
    notifier = _RecordingNotifier()
    app = CommanderApp(_make_runtime(attention=attention, notifier=notifier))

    state = _state_with_alerts(
        _alert("agent-1:failed", "error", title="agent-1 failed", message="exit 1"),
        _alert("agent-2:stuck", "error", title="agent-2 stuck", message="no output"),
        _alert("agent-3:stale", "warning", title="agent-3 warning", message="slow"),
    )

    app._dispatch_attention_notifications(state)

    # Only critical signals produce notifications; map "error" → "critical".
    assert notifier.calls == [
        ("agent-1 failed", "exit 1", "critical"),
        ("agent-2 stuck", "no output", "critical"),
    ]
    # Tab badge reflects unread signals in the inbox (including warnings).
    assert app.tab_badges.get("attention") == 3


def test_dispatch_attention_notifications_does_not_renotify_same_ids() -> None:
    attention = AttentionController(_FakeDashboardController(), AttentionInboxService())
    notifier = _RecordingNotifier()
    app = CommanderApp(_make_runtime(attention=attention, notifier=notifier))

    state = _state_with_alerts(
        _alert("agent-1:failed", "error"),
    )
    app._dispatch_attention_notifications(state)
    app._dispatch_attention_notifications(state)

    assert len(notifier.calls) == 1


def test_dispatch_attention_is_noop_without_attention_controller() -> None:
    notifier = _RecordingNotifier()
    app = CommanderApp(_make_runtime(attention=None, notifier=notifier))
    app._dispatch_attention_notifications(_empty_state())
    assert notifier.calls == []


def test_dispatch_attention_does_nothing_when_state_is_none() -> None:
    attention = AttentionController(_FakeDashboardController(), AttentionInboxService())
    notifier = _RecordingNotifier()
    app = CommanderApp(_make_runtime(attention=attention, notifier=notifier))
    app._dispatch_attention_notifications(None)
    assert notifier.calls == []


def test_set_tab_badge_updates_and_clears() -> None:
    app = CommanderApp(_make_runtime(attention=None, notifier=None))
    app.set_tab_badge("attention", 2)
    assert app.tab_badges == {"attention": 2}
    app.set_tab_badge("attention", 0)
    assert app.tab_badges == {}

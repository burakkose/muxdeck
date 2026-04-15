"""Tests for safety controls: restart, stop-all, and emergency features."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from copilot_commander.controllers.agent_controller import (
    AgentIntentView,
    AgentTargetView,
)
from copilot_commander.domain.enums import AgentStatus
from copilot_commander.services.action_service import (
    ActionResult,
    TmuxActionService,
)

# -------------------------------------------------------------------
# Fakes
# -------------------------------------------------------------------


@dataclass
class SendKeysCall:
    target_pane: str
    keys: Sequence[str]
    literal: bool
    append_enter: bool


@dataclass
class FakeTmux:
    """Minimal fake satisfying TmuxOperations protocol."""

    existing_panes: set[str] = field(default_factory=set)
    select_pane_calls: list[str] = field(default_factory=list)
    send_keys_calls: list[SendKeysCall] = field(default_factory=list)

    def pane_exists(self, target_pane: str, /) -> bool:
        return target_pane in self.existing_panes

    def select_pane(self, target_pane: str, /) -> None:
        self.select_pane_calls.append(target_pane)

    def send_keys(
        self,
        target_pane: str,
        keys: Sequence[str],
        /,
        *,
        literal: bool = False,
        append_enter: bool = False,
    ) -> None:
        self.send_keys_calls.append(
            SendKeysCall(target_pane, keys, literal, append_enter),
        )

    def capture_pane(
        self,
        target_pane: str,
        /,
        *,
        start_line: str | int | None = None,
        end_line: str | int | None = None,
        join_wrapped_lines: bool = False,
    ) -> str:
        return ""


# -------------------------------------------------------------------
# Helpers
# -------------------------------------------------------------------

_TARGET = AgentTargetView(
    agent_id="agent-1",
    name="test-agent",
    status=AgentStatus.RUNNING,
    pane_target="%1",
    worktree_path=None,
    repo_root=None,
    branch=None,
    latest_session_id=None,
)


def _restart_intent(
    pane: str = "%1",
    *,
    task_title: str = "",
    model: str | None = None,
) -> AgentIntentView:
    target = AgentTargetView(
        agent_id="agent-1",
        name="test-agent",
        status=AgentStatus.RUNNING,
        pane_target=pane,
        worktree_path=None,
        repo_root=None,
        branch=None,
        latest_session_id=None,
    )
    meta: list[tuple[str, str]] = [
        ("pane_target", pane),
        ("cwd", "/home/user/repo"),
    ]
    if task_title:
        meta.append(("task_title", task_title))
    if model is not None:
        meta.append(("model", model))
    return AgentIntentView(
        kind="restart",
        agent=target,
        label="Restart agent",
        metadata=tuple(meta),
    )


# -------------------------------------------------------------------
# Restart execution
# -------------------------------------------------------------------


class TestRestartExecution:
    """Tests for the restart intent in execute_intent."""

    def test_restart_sends_interrupt_then_command(self) -> None:
        tmux = FakeTmux(existing_panes={"%1"})
        svc = TmuxActionService(tmux)
        intent = _restart_intent("%1", task_title="fix bug")

        result = svc.execute_intent(intent)

        assert result.success is True
        assert "restart" in result.message.lower()
        assert len(tmux.send_keys_calls) == 2
        # First call: Ctrl-C interrupt
        assert list(tmux.send_keys_calls[0].keys) == ["C-c"]
        assert tmux.send_keys_calls[0].literal is False
        # Second call: copilot --resume (has task_title)
        second = tmux.send_keys_calls[1]
        assert "copilot" in second.keys[0].lower()
        assert "--resume" in second.keys[0]
        assert second.literal is True
        assert second.append_enter is True

    def test_restart_without_task_sends_plain_copilot(self) -> None:
        tmux = FakeTmux(existing_panes={"%1"})
        svc = TmuxActionService(tmux)
        intent = _restart_intent("%1", task_title="")

        result = svc.execute_intent(intent)

        assert result.success is True
        second = tmux.send_keys_calls[1]
        assert second.keys[0] == "copilot"
        assert "--resume" not in second.keys[0]

    def test_restart_no_task_title_in_metadata(self) -> None:
        """When task_title key is absent, defaults to plain copilot."""
        tmux = FakeTmux(existing_panes={"%1"})
        svc = TmuxActionService(tmux)
        intent = AgentIntentView(
            kind="restart",
            agent=_TARGET,
            label="Restart",
            metadata=(("pane_target", "%1"),),
        )

        result = svc.execute_intent(intent)

        assert result.success is True
        second = tmux.send_keys_calls[1]
        assert second.keys[0] == "copilot"

    def test_restart_pane_not_found(self) -> None:
        tmux = FakeTmux(existing_panes=set())
        svc = TmuxActionService(tmux)
        intent = _restart_intent("%99")

        result = svc.execute_intent(intent)

        assert result.success is False
        assert "%99" in result.message
        assert tmux.send_keys_calls == []

    def test_restart_result_has_correct_pane_id(self) -> None:
        tmux = FakeTmux(existing_panes={"%3"})
        svc = TmuxActionService(tmux)
        intent = _restart_intent("%3", task_title="deploy")

        result = svc.execute_intent(intent)

        assert result.pane_id == "%3"

    def test_restart_falls_back_to_agent_pane_target(self) -> None:
        """When metadata has no pane_target, use agent's pane."""
        tmux = FakeTmux(existing_panes={"%1"})
        svc = TmuxActionService(tmux)
        intent = AgentIntentView(
            kind="restart",
            agent=_TARGET,
            label="Restart",
            metadata=(),
        )

        result = svc.execute_intent(intent)

        assert result.success is True
        assert result.pane_id == "%1"


# -------------------------------------------------------------------
# Stop all agents
# -------------------------------------------------------------------


class TestStopAllAgents:
    """Tests for the stop_all_agents batch-interrupt method."""

    def test_stop_all_interrupts_each_pane(self) -> None:
        tmux = FakeTmux(existing_panes={"%1", "%2", "%3"})
        svc = TmuxActionService(tmux)

        results = svc.stop_all_agents(["%1", "%2", "%3"])

        assert len(results) == 3
        assert all(r.success for r in results)
        panes = [c.target_pane for c in tmux.send_keys_calls]
        assert set(panes) == {"%1", "%2", "%3"}
        for call in tmux.send_keys_calls:
            assert list(call.keys) == ["C-c"]

    def test_stop_all_handles_missing_panes(self) -> None:
        tmux = FakeTmux(existing_panes={"%1"})
        svc = TmuxActionService(tmux)

        results = svc.stop_all_agents(["%1", "%99"])

        assert results[0].success is True
        assert results[1].success is False
        assert "%99" in results[1].message

    def test_stop_all_empty_list(self) -> None:
        tmux = FakeTmux()
        svc = TmuxActionService(tmux)

        results = svc.stop_all_agents([])

        assert results == []
        assert tmux.send_keys_calls == []

    def test_stop_all_returns_action_results(self) -> None:
        tmux = FakeTmux(existing_panes={"%5"})
        svc = TmuxActionService(tmux)

        results = svc.stop_all_agents(["%5"])

        assert len(results) == 1
        assert isinstance(results[0], ActionResult)
        assert results[0].pane_id == "%5"

    def test_stop_all_preserves_order(self) -> None:
        tmux = FakeTmux(existing_panes={"%a", "%b", "%c"})
        svc = TmuxActionService(tmux)

        results = svc.stop_all_agents(["%a", "%b", "%c"])

        assert [r.pane_id for r in results] == ["%a", "%b", "%c"]

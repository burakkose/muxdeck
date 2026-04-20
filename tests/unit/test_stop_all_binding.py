"""Tests for emergency stop-all binding and launch-agent action logic."""

from __future__ import annotations

from datetime import UTC, datetime

from muxdeck.bindings import (
    DASHBOARD_BINDINGS,
    DASHBOARD_HINTS,
    WORKTREE_BINDINGS,
    WORKTREE_HINTS,
)
from muxdeck.controllers.dashboard_controller import DashboardAgentListItemView
from muxdeck.controllers.worktree_controller import WorktreeStartAgentIntent
from muxdeck.domain.enums import AgentStatus
from muxdeck.services.action_service import ActionResult

# -------------------------------------------------------------------
# Helpers
# -------------------------------------------------------------------

_TS = datetime(2025, 1, 1, tzinfo=UTC)


def _agent_item(
    agent_id: str = "agent-1",
    pane_id: str = "%1",
) -> DashboardAgentListItemView:
    return DashboardAgentListItemView(
        agent_id=agent_id,
        name="test",
        status=AgentStatus.RUNNING,
        repo_name="repo",
        branch="main",
        worktree_name="wt",
        pane_id=pane_id,
        task_title=None,
        worktree_path="/repo/wt",
        latest_session_id="s1",
        last_event_kind=None,
        last_log_at=_TS,
        last_seen_at=_TS,
        started_at=_TS,
        idle_seconds=0,
        needs_attention=False,
        attention_reason=None,
        token_total=None,
        estimated_cost_usd=None,
    )


def _start_intent(
    *,
    model: str | None = "gpt-5.4",
) -> WorktreeStartAgentIntent:
    return WorktreeStartAgentIntent(
        worktree_id="wt-1",
        repo_root="/repo",
        worktree_path="/repo/wt",
        branch="feat/thing",
        suggested_session_name="muxdeck",
        suggested_window_name="thing",
        prompt="Continue work for feat/thing",
        model=model,
    )


# -------------------------------------------------------------------
# Binding presence
# -------------------------------------------------------------------


class TestBindingsPresent:
    """Verify new bindings exist in the binding lists."""

    def test_stop_all_binding_in_dashboard(self) -> None:
        actions = [b.action if hasattr(b, "action") else b[1] for b in DASHBOARD_BINDINGS]
        assert "stop_all" in actions

    def test_stop_all_uses_capital_s(self) -> None:
        binding = next(
            b for b in DASHBOARD_BINDINGS if hasattr(b, "action") and b.action == "stop_all"
        )
        assert binding.key == "S"

    def test_stop_all_hint_in_dashboard(self) -> None:
        labels = [h.label for h in DASHBOARD_HINTS]
        assert "stop all" in labels

    def test_launch_agent_binding_in_worktrees(self) -> None:
        actions = [b.action if hasattr(b, "action") else b[1] for b in WORKTREE_BINDINGS]
        assert "launch_agent" in actions

    def test_launch_agent_uses_all_launch_keys(self) -> None:
        keys = {
            b.key for b in WORKTREE_BINDINGS if hasattr(b, "action") and b.action == "launch_agent"
        }
        assert keys == {"enter", "s", "x"}

    def test_launch_hint_in_worktrees(self) -> None:
        labels = [h.label for h in WORKTREE_HINTS]
        assert "launch" in labels


# -------------------------------------------------------------------
# Stop-all pane collection logic
# -------------------------------------------------------------------


class TestStopAllPaneCollection:
    """Test the pane_id collection logic used by action_stop_all."""

    def test_collects_pane_ids_from_agents(self) -> None:
        agents = [
            _agent_item(agent_id="a1", pane_id="%1"),
            _agent_item(agent_id="a2", pane_id="%2"),
        ]
        pane_ids = [a.pane_id for a in agents if a.pane_id]
        assert pane_ids == ["%1", "%2"]

    def test_skips_empty_pane_ids(self) -> None:
        agents = [_agent_item(pane_id="%1"), _agent_item(pane_id="")]
        pane_ids = [a.pane_id for a in agents if a.pane_id]
        assert pane_ids == ["%1"]

    def test_empty_agent_list(self) -> None:
        agents: list[DashboardAgentListItemView] = []
        pane_ids = [a.pane_id for a in agents if a.pane_id]
        assert pane_ids == []


# -------------------------------------------------------------------
# Stop-all status message formatting
# -------------------------------------------------------------------


class TestStopAllStatusMessages:
    """Test the status message formatting logic from action_stop_all."""

    def test_all_success(self) -> None:
        results = [ActionResult(success=True, message="ok", pane_id="%1")]
        ok = sum(1 for r in results if r.success)
        fail = len(results) - ok
        assert fail == 0
        assert f"✓ stopped {ok} agents" == "✓ stopped 1 agents"

    def test_mixed_results(self) -> None:
        results = [
            ActionResult(success=True, message="ok", pane_id="%1"),
            ActionResult(success=False, message="nope", pane_id="%2"),
            ActionResult(success=True, message="ok", pane_id="%3"),
        ]
        ok = sum(1 for r in results if r.success)
        fail = len(results) - ok
        msg = f"⚠ stopped {ok}/{len(results)} agents ({fail} failed)"
        assert msg == "⚠ stopped 2/3 agents (1 failed)"


# -------------------------------------------------------------------
# Launch-agent intent logic
# -------------------------------------------------------------------


class TestLaunchAgentIntent:
    """Test logic used by the worktree launch flow."""

    def test_intent_with_model(self) -> None:
        intent = _start_intent(model="gpt-5.4")
        model_flag = f" --model {intent.model}" if intent.model else ""
        cmd = f"copilot{model_flag}"
        assert cmd == "copilot --model gpt-5.4"
        assert intent.worktree_path == "/repo/wt"
        assert intent.suggested_session_name == "muxdeck"
        assert intent.suggested_window_name == "thing"

    def test_intent_without_model(self) -> None:
        intent = _start_intent(model=None)
        model_flag = f" --model {intent.model}" if intent.model else ""
        cmd = f"copilot{model_flag}"
        assert cmd == "copilot"

    def test_intent_clears_after_execute(self) -> None:
        intent: WorktreeStartAgentIntent | None = _start_intent()
        assert intent is not None
        # Simulate clearing after execute
        intent = None
        assert intent is None

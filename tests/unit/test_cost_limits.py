# ruff: noqa: E402,I001

"""Tests for cost/runtime runaway detection in the dashboard controller."""

from __future__ import annotations

import sys
import unittest
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from copilot_commander.config import GeneralConfig
from copilot_commander.controllers.dashboard_controller import (
    DashboardController,
    _check_runaway,
)
from copilot_commander.adapters.sqlite_store import SessionContextRecord
from copilot_commander.domain.enums import AgentStatus
from copilot_commander.domain.events import Event, LogChunk
from copilot_commander.domain.models import Agent, Session, Worktree


class InMemoryDashboardStore:
    def __init__(self) -> None:
        self.agents: dict[str, Agent] = {}
        self.sessions: dict[str, Session] = {}
        self.contexts: dict[str, SessionContextRecord] = {}
        self.events: list[Event] = []
        self.logs: list[LogChunk] = []
        self.worktrees: dict[str, Worktree] = {}

    def list_agents(self) -> tuple[Agent, ...]:
        return tuple(sorted(self.agents.values(), key=lambda a: a.last_seen_at, reverse=True))

    def list_sessions(self, agent_id: str | None = None, /) -> tuple[Session, ...]:
        sessions = tuple(sorted(self.sessions.values(), key=lambda s: s.created_at, reverse=True))
        if agent_id is None:
            return sessions
        return tuple(s for s in sessions if s.agent_id == agent_id)

    def get_session_context(self, session_id: str, /) -> SessionContextRecord | None:
        return self.contexts.get(session_id)

    def list_events_for_session(self, session_id: str, /) -> tuple[Event, ...]:
        return tuple(e for e in self.events if e.session_id == session_id)

    def list_log_chunks(self, session_id: str, /) -> tuple[LogChunk, ...]:
        return tuple(c for c in self.logs if c.session_id == session_id)

    def get_worktree(self, worktree_id: str, /) -> Worktree | None:
        return self.worktrees.get(worktree_id)


class CheckRunawayTests(unittest.TestCase):
    """Unit tests for the pure _check_runaway helper."""

    def _make_agent(
        self,
        *,
        status: AgentStatus = AgentStatus.RUNNING,
        estimated_cost_usd: Decimal | None = None,
        started_at: datetime | None = None,
    ) -> Agent:
        return Agent(
            id="agent-1",
            name="test-agent",
            tmux_session_name="s",
            tmux_window_id="@1",
            tmux_pane_id="%1",
            cwd="/repo",
            status=status,
            started_at=started_at or datetime(2025, 1, 1, 12, 0, tzinfo=UTC),
            last_seen_at=datetime(2025, 1, 1, 13, 0, tzinfo=UTC),
            estimated_cost_usd=estimated_cost_usd,
        )

    def test_no_limits_returns_none(self) -> None:
        agent = self._make_agent(estimated_cost_usd=Decimal("10.00"))
        now = datetime(2025, 1, 1, 15, 0, tzinfo=UTC)
        result = _check_runaway(agent, now=now, max_cost_usd=None, max_runtime_minutes=None)
        assert result is None

    def test_cost_under_limit_returns_none(self) -> None:
        agent = self._make_agent(estimated_cost_usd=Decimal("1.50"))
        now = datetime(2025, 1, 1, 13, 0, tzinfo=UTC)
        result = _check_runaway(
            agent, now=now, max_cost_usd=Decimal("5.00"), max_runtime_minutes=None
        )
        assert result is None

    def test_cost_exceeds_limit(self) -> None:
        agent = self._make_agent(estimated_cost_usd=Decimal("7.50"))
        now = datetime(2025, 1, 1, 13, 0, tzinfo=UTC)
        result = _check_runaway(
            agent, now=now, max_cost_usd=Decimal("5.00"), max_runtime_minutes=None
        )
        assert result is not None
        assert "cost" in result
        assert "$7.50" in result
        assert "$5.00" in result

    def test_runtime_under_limit_returns_none(self) -> None:
        started = datetime(2025, 1, 1, 12, 0, tzinfo=UTC)
        agent = self._make_agent(started_at=started)
        now = started + timedelta(minutes=10)
        result = _check_runaway(agent, now=now, max_cost_usd=None, max_runtime_minutes=30)
        assert result is None

    def test_runtime_exceeds_limit(self) -> None:
        started = datetime(2025, 1, 1, 12, 0, tzinfo=UTC)
        agent = self._make_agent(started_at=started)
        now = started + timedelta(minutes=45)
        result = _check_runaway(agent, now=now, max_cost_usd=None, max_runtime_minutes=30)
        assert result is not None
        assert "runtime" in result
        assert "45m" in result
        assert "30m" in result

    def test_completed_agent_skipped(self) -> None:
        agent = self._make_agent(
            status=AgentStatus.COMPLETED,
            estimated_cost_usd=Decimal("100.00"),
        )
        now = datetime(2025, 1, 1, 20, 0, tzinfo=UTC)
        result = _check_runaway(agent, now=now, max_cost_usd=Decimal("1.00"), max_runtime_minutes=1)
        assert result is None

    def test_dead_agent_skipped(self) -> None:
        agent = self._make_agent(
            status=AgentStatus.DEAD,
            estimated_cost_usd=Decimal("100.00"),
        )
        now = datetime(2025, 1, 1, 20, 0, tzinfo=UTC)
        result = _check_runaway(agent, now=now, max_cost_usd=Decimal("1.00"), max_runtime_minutes=1)
        assert result is None

    def test_cost_checked_before_runtime(self) -> None:
        """When both limits are exceeded, cost takes precedence."""
        started = datetime(2025, 1, 1, 12, 0, tzinfo=UTC)
        agent = self._make_agent(
            started_at=started,
            estimated_cost_usd=Decimal("10.00"),
        )
        now = started + timedelta(minutes=120)
        result = _check_runaway(
            agent,
            now=now,
            max_cost_usd=Decimal("5.00"),
            max_runtime_minutes=30,
        )
        assert result is not None
        assert "cost" in result

    def test_no_cost_data_skips_cost_check(self) -> None:
        agent = self._make_agent(estimated_cost_usd=None)
        now = datetime(2025, 1, 1, 13, 0, tzinfo=UTC)
        result = _check_runaway(
            agent, now=now, max_cost_usd=Decimal("5.00"), max_runtime_minutes=None
        )
        assert result is None


class DashboardControllerRunawayIntegrationTests(unittest.TestCase):
    """Integration test: DashboardController with cost/runtime limits."""

    def test_agent_flagged_as_needs_attention_when_cost_exceeds(self) -> None:
        store = InMemoryDashboardStore()
        now = datetime(2025, 1, 1, 12, 30, tzinfo=UTC)
        store.agents["agent-1"] = Agent(
            id="agent-1",
            name="Expensive",
            tmux_session_name="s",
            tmux_window_id="@1",
            tmux_pane_id="%1",
            cwd="/repo",
            status=AgentStatus.RUNNING,
            started_at=datetime(2025, 1, 1, 12, 0, tzinfo=UTC),
            last_seen_at=now,
            estimated_cost_usd=Decimal("8.00"),
            needs_attention=False,
            attention_reason=None,
        )
        controller = DashboardController(
            store,
            clock=lambda: now,
            max_cost_usd=Decimal("5.00"),
        )
        state = controller.build_state()
        assert len(state.agents) == 1
        agent_view = state.agents[0]
        assert agent_view.needs_attention is True
        assert agent_view.attention_reason is not None
        assert "cost" in agent_view.attention_reason

    def test_agent_not_flagged_when_under_limits(self) -> None:
        store = InMemoryDashboardStore()
        now = datetime(2025, 1, 1, 12, 10, tzinfo=UTC)
        store.agents["agent-1"] = Agent(
            id="agent-1",
            name="Cheap",
            tmux_session_name="s",
            tmux_window_id="@1",
            tmux_pane_id="%1",
            cwd="/repo",
            status=AgentStatus.RUNNING,
            started_at=datetime(2025, 1, 1, 12, 0, tzinfo=UTC),
            last_seen_at=now,
            estimated_cost_usd=Decimal("1.00"),
            needs_attention=False,
            attention_reason=None,
        )
        controller = DashboardController(
            store,
            clock=lambda: now,
            max_cost_usd=Decimal("5.00"),
            max_runtime_minutes=60,
        )
        state = controller.build_state()
        assert len(state.agents) == 1
        assert state.agents[0].needs_attention is False


class GeneralConfigLimitTests(unittest.TestCase):
    """Test that GeneralConfig accepts cost/runtime limit fields."""

    def test_defaults_are_none(self) -> None:
        config = GeneralConfig()
        assert config.max_cost_usd is None
        assert config.max_runtime_minutes is None

    def test_explicit_values(self) -> None:
        config = GeneralConfig(max_cost_usd=Decimal("10.00"), max_runtime_minutes=60)
        assert config.max_cost_usd == Decimal("10.00")
        assert config.max_runtime_minutes == 60

    def test_negative_runtime_rejected(self) -> None:
        import pytest

        from copilot_commander.exceptions import ConfigValidationError

        with pytest.raises(ConfigValidationError):
            GeneralConfig(max_runtime_minutes=-5)

    def test_zero_runtime_rejected(self) -> None:
        import pytest

        from copilot_commander.exceptions import ConfigValidationError

        with pytest.raises(ConfigValidationError):
            GeneralConfig(max_runtime_minutes=0)


if __name__ == "__main__":
    unittest.main()

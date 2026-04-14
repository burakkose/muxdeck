# ruff: noqa: E402,I001,PT009

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
import sys
import unittest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from copilot_commander.adapters.sqlite_store import SessionContextRecord
from copilot_commander.controllers.dashboard_controller import (
    DashboardController,
    DashboardFilterState,
    DashboardSort,
)
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
        return tuple(
            sorted(
                self.agents.values(),
                key=lambda agent: agent.last_seen_at,
                reverse=True,
            )
        )

    def list_sessions(self, agent_id: str | None = None, /) -> tuple[Session, ...]:
        sessions = tuple(
            sorted(
                self.sessions.values(),
                key=lambda session: session.created_at,
                reverse=True,
            )
        )
        if agent_id is None:
            return sessions
        return tuple(session for session in sessions if session.agent_id == agent_id)

    def get_session_context(self, session_id: str, /) -> SessionContextRecord | None:
        return self.contexts.get(session_id)

    def list_events_for_session(self, session_id: str, /) -> tuple[Event, ...]:
        return tuple(event for event in self.events if event.session_id == session_id)

    def list_log_chunks(self, session_id: str, /) -> tuple[LogChunk, ...]:
        return tuple(chunk for chunk in self.logs if chunk.session_id == session_id)

    def get_worktree(self, worktree_id: str, /) -> Worktree | None:
        return self.worktrees.get(worktree_id)


class DashboardControllerTests(unittest.TestCase):
    def test_build_state_applies_filters_and_assembles_selected_detail(self) -> None:
        store = InMemoryDashboardStore()
        observed_at = datetime(2025, 1, 1, 12, 5, tzinfo=UTC)
        store.agents["agent-1"] = Agent(
            id="agent-1",
            name="Reviewer",
            tmux_session_name="muxdeck",
            tmux_window_id="@1",
            tmux_pane_id="%1",
            cwd="/repo/worktrees/review",
            repo_root="/repo",
            worktree_path="/repo/worktrees/review",
            branch="task/review",
            task_title="Review",
            status=AgentStatus.WAITING_INPUT,
            started_at=datetime(2025, 1, 1, 12, tzinfo=UTC),
            last_activity_at=datetime(2025, 1, 1, 12, 4, tzinfo=UTC),
            last_seen_at=observed_at,
            idle_seconds=65,
            needs_attention=True,
            attention_reason="waiting for confirmation input",
            token_input=10,
            token_output=20,
            token_total=30,
            estimated_cost_usd=Decimal("0.250000"),
        )
        store.agents["agent-2"] = Agent(
            id="agent-2",
            name="Done",
            tmux_session_name="muxdeck",
            tmux_window_id="@2",
            tmux_pane_id="%2",
            cwd="/repo",
            repo_root="/repo",
            branch="task/done",
            task_title="Done",
            status=AgentStatus.COMPLETED,
            started_at=datetime(2025, 1, 1, 11, tzinfo=UTC),
            last_seen_at=datetime(2025, 1, 1, 11, 30, tzinfo=UTC),
        )
        store.sessions["session-1"] = Session(
            id="session-1",
            agent_id="agent-1",
            task_title="Review",
            created_at=datetime(2025, 1, 1, 12, tzinfo=UTC),
        )
        store.contexts["session-1"] = SessionContextRecord(
            session_id="session-1",
            agent_id="agent-1",
            worktree_id="worktree-1",
            tmux_pane_id="%1",
            worktree_path="/repo/worktrees/review",
            repo_root="/repo",
            branch="task/review",
            updated_at=observed_at,
        )
        store.worktrees["worktree-1"] = Worktree(
            id="worktree-1",
            repo_root="/repo",
            path="/repo/worktrees/review",
            branch="task/review",
            base_branch="main",
            last_seen_at=observed_at,
        )
        store.events.append(
            Event(
                id="event-1",
                occurred_at=observed_at,
                agent_id="agent-1",
                session_id="session-1",
                kind="agent.blocking_issue",
                severity="warning",
                payload_json='{"kind":"waiting_for_confirmation"}',
            )
        )
        store.logs.append(
            LogChunk(
                id="log-1",
                agent_id="agent-1",
                session_id="session-1",
                source="stdout",
                sequence_no=0,
                captured_at=observed_at,
                content="alpha\nbeta\ngamma",
            )
        )

        controller = DashboardController(store, clock=lambda: observed_at)
        state = controller.build_state(
            filters=DashboardFilterState(attention_only=True, text_query="review"),
            sort=DashboardSort(field="name", descending=False),
            selected_agent_id="agent-1",
            preview_line_limit=2,
        )

        self.assertEqual([item.agent_id for item in state.agents], ["agent-1"])
        self.assertEqual(state.health.tone, "warning")
        self.assertEqual([metric.value for metric in state.metrics], [2, 1, 1, 1])
        self.assertEqual(len(state.alerts), 1)
        self.assertEqual(state.selected_agent_id, "agent-1")
        assert state.selected_agent is not None
        self.assertEqual(state.selected_agent.worktree_id, "worktree-1")
        self.assertEqual(
            [line.content for line in state.selected_agent.log_preview],
            ["beta", "gamma"],
        )


if __name__ == "__main__":
    unittest.main()

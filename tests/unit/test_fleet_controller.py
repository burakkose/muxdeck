# ruff: noqa: E402,I001,PT009

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
import sys
import unittest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from copilot_commander.adapters.copilot_session_store import CopilotLocalSession
from copilot_commander.adapters.sqlite_store import SessionContextRecord
from copilot_commander.controllers.fleet_controller import FleetController, FleetFilterState
from copilot_commander.domain.enums import AgentStatus
from copilot_commander.domain.events import Event
from copilot_commander.domain.models import Agent, Session, Worktree


class InMemoryFleetStore:
    def __init__(self) -> None:
        self.agents: dict[str, Agent] = {}
        self.worktrees: dict[str, Worktree] = {}
        self.sessions: dict[str, Session] = {}
        self.events: list[Event] = []
        self.contexts: dict[str, SessionContextRecord] = {}

    def list_agents(self) -> tuple[Agent, ...]:
        return tuple(
            sorted(self.agents.values(), key=lambda agent: agent.last_seen_at, reverse=True)
        )

    def list_worktrees(
        self,
        /,
        *,
        repo_root: str | None = None,
        assigned_agent_id: str | None = None,
    ) -> tuple[Worktree, ...]:
        worktrees = tuple(self.worktrees.values())
        if repo_root is not None:
            worktrees = tuple(worktree for worktree in worktrees if worktree.repo_root == repo_root)
        if assigned_agent_id is not None:
            worktrees = tuple(
                worktree
                for worktree in worktrees
                if worktree.assigned_agent_id == assigned_agent_id
            )
        return worktrees

    def list_sessions(self, agent_id: str | None = None, /) -> tuple[Session, ...]:
        sessions = tuple(
            sorted(self.sessions.values(), key=lambda session: session.created_at, reverse=True)
        )
        if agent_id is None:
            return sessions
        return tuple(session for session in sessions if session.agent_id == agent_id)

    def list_events(
        self,
        /,
        *,
        agent_id: str | None = None,
        session_id: str | None = None,
    ) -> tuple[Event, ...]:
        events = tuple(self.events)
        if agent_id is not None:
            events = tuple(event for event in events if event.agent_id == agent_id)
        if session_id is not None:
            events = tuple(event for event in events if event.session_id == session_id)
        return events

    def get_session_context(self, session_id: str, /) -> SessionContextRecord | None:
        return self.contexts.get(session_id)


class FakeLocalSessionStore:
    def __init__(self, sessions: tuple[CopilotLocalSession, ...]) -> None:
        self._sessions = sessions

    def discover(self, *, force: bool = False) -> tuple[CopilotLocalSession, ...]:
        return self._sessions


class FleetControllerTests(unittest.TestCase):
    def test_build_state_groups_repo_health_helpers_and_search(self) -> None:
        observed_at = datetime(2025, 2, 1, 12, tzinfo=UTC)
        store = InMemoryFleetStore()
        store.agents["agent-1"] = Agent(
            id="agent-1",
            name="Planner",
            tmux_session_name="muxdeck",
            tmux_window_id="@1",
            tmux_pane_id="%1",
            cwd="/repo-a/worktrees/planner",
            repo_root="/repo-a",
            worktree_path="/repo-a/worktrees/planner",
            branch="task/planner",
            task_title="Plan fleet search surface",
            copilot_session_id="copilot-1",
            status=AgentStatus.WAITING_INPUT,
            started_at=observed_at - timedelta(hours=2),
            last_seen_at=observed_at,
            idle_seconds=180,
            needs_attention=True,
            attention_reason="waiting for operator review",
            token_total=320,
            estimated_cost_usd=Decimal("1.25"),
        )
        store.agents["agent-2"] = Agent(
            id="agent-2",
            name="Indexer",
            tmux_session_name="muxdeck",
            tmux_window_id="@2",
            tmux_pane_id="%2",
            cwd="/repo-b/worktrees/indexer",
            repo_root="/repo-b",
            worktree_path="/repo-b/worktrees/indexer",
            branch="task/indexer",
            task_title="Index runtime history",
            copilot_session_id="copilot-2",
            status=AgentStatus.RUNNING,
            started_at=observed_at - timedelta(hours=1),
            last_seen_at=observed_at - timedelta(minutes=5),
            idle_seconds=20,
        )
        store.worktrees["wt-1"] = Worktree(
            id="wt-1",
            repo_root="/repo-a",
            path="/repo-a/worktrees/planner",
            branch="task/planner",
            is_dirty=True,
            assigned_agent_id="agent-1",
            last_seen_at=observed_at,
        )
        store.worktrees["wt-2"] = Worktree(
            id="wt-2",
            repo_root="/repo-b",
            path="/repo-b/worktrees/indexer",
            branch="task/indexer",
            locked=True,
            assigned_agent_id="agent-2",
            last_seen_at=observed_at,
        )
        store.sessions["session-1"] = Session(
            id="session-1",
            agent_id="agent-1",
            copilot_session_id="copilot-1",
            task_title="Plan fleet search surface",
            created_at=observed_at - timedelta(minutes=50),
        )
        store.sessions["session-2"] = Session(
            id="session-2",
            agent_id="agent-2",
            copilot_session_id="copilot-2",
            task_title="Index runtime history",
            created_at=observed_at - timedelta(minutes=15),
        )
        store.contexts["session-1"] = SessionContextRecord(
            session_id="session-1",
            agent_id="agent-1",
            worktree_id="wt-1",
            worktree_path="/repo-a/worktrees/planner",
            repo_root="/repo-a",
            branch="task/planner",
            updated_at=observed_at,
        )
        store.contexts["session-2"] = SessionContextRecord(
            session_id="session-2",
            agent_id="agent-2",
            worktree_id="wt-2",
            worktree_path="/repo-b/worktrees/indexer",
            repo_root="/repo-b",
            branch="task/indexer",
            updated_at=observed_at,
        )
        store.events.extend(
            [
                Event(
                    id="event-1",
                    occurred_at=observed_at - timedelta(minutes=2),
                    agent_id="agent-1",
                    session_id="session-1",
                    kind="agent.waiting_input",
                    severity="warning",
                    payload_json='{"message":"waiting"}',
                ),
                Event(
                    id="event-2",
                    occurred_at=observed_at - timedelta(minutes=1),
                    agent_id="agent-2",
                    session_id="session-2",
                    kind="agent.progress",
                    severity="info",
                    payload_json='{"message":"indexing"}',
                ),
            ]
        )
        local_store = FakeLocalSessionStore(
            (
                CopilotLocalSession(
                    session_id="copilot-1",
                    cwd=Path("/repo-a/worktrees/planner"),
                    git_root=Path("/repo-a"),
                    repository="repo-a",
                    branch="task/planner",
                    summary="Plan fleet search surface",
                    created_at=observed_at - timedelta(hours=2),
                    updated_at=observed_at,
                    last_event_type="tool.execution_complete",
                    last_event_at=observed_at,
                    is_cleanly_closed=False,
                ),
                CopilotLocalSession(
                    session_id="orphan-local",
                    cwd=Path("/repo-a/worktrees/orphan"),
                    git_root=Path("/repo-a"),
                    repository="repo-a",
                    branch="task/orphan",
                    summary="Investigate orphan session",
                    created_at=observed_at - timedelta(hours=1),
                    updated_at=observed_at - timedelta(minutes=10),
                    last_event_type="tool.execution_complete",
                    last_event_at=observed_at - timedelta(minutes=10),
                    is_cleanly_closed=False,
                ),
            )
        )

        controller = FleetController(store, local_sessions=local_store, clock=lambda: observed_at)
        state = controller.build_state(filters=FleetFilterState(text_query="planner"))

        self.assertEqual(state.total_groups, 1)
        self.assertEqual(state.total_visible_agents, 1)
        self.assertEqual(state.health.tone, "warning")
        self.assertEqual(state.health.orphan_local_sessions, 1)
        self.assertEqual(state.groups[0].repo_label, "repo-a")
        self.assertEqual(state.groups[0].attention_count, 1)
        self.assertEqual(state.groups[0].dirty_worktree_count, 1)
        self.assertEqual(state.groups[0].orphan_local_session_count, 1)
        self.assertEqual(state.search_hits[0].kind, "agent")
        helper_queries = {helper.query for helper in state.search_helpers}
        self.assertIn("attention", helper_queries)
        self.assertIn("dirty", helper_queries)
        self.assertIn("unclosed", helper_queries)
        self.assertEqual(
            [metric.label for metric in state.history_metrics],
            ["repos", "24h sessions", "24h events", "tokens"],
        )
        history_by_label = {metric.label: metric for metric in state.history_metrics}
        self.assertEqual(history_by_label["repos"].detail, "1 dirty · 1 worktrees")
        self.assertEqual(history_by_label["24h sessions"].value, "1")
        self.assertEqual(history_by_label["24h events"].value, "1")
        resources_by_label = {resource.label: resource for resource in state.resources}
        self.assertEqual(resources_by_label["repos"].value, "1")
        self.assertEqual(resources_by_label["worktrees"].value, "1")
        self.assertEqual(resources_by_label["runtime"].value, "1")
        self.assertEqual(resources_by_label["local sessions"].value, "2")

    def test_build_state_attention_only_hides_healthy_agents(self) -> None:
        observed_at = datetime(2025, 2, 1, 12, tzinfo=UTC)
        store = InMemoryFleetStore()
        store.agents["agent-1"] = Agent(
            id="agent-1",
            name="Planner",
            tmux_session_name="muxdeck",
            tmux_window_id="@1",
            tmux_pane_id="%1",
            cwd="/repo/worktrees/planner",
            repo_root="/repo",
            worktree_path="/repo/worktrees/planner",
            branch="task/planner",
            task_title="Plan insights",
            status=AgentStatus.WAITING_INPUT,
            started_at=observed_at - timedelta(hours=2),
            last_seen_at=observed_at,
            idle_seconds=30,
            needs_attention=True,
            attention_reason="waiting",
        )
        store.agents["agent-2"] = Agent(
            id="agent-2",
            name="Indexer",
            tmux_session_name="muxdeck",
            tmux_window_id="@2",
            tmux_pane_id="%2",
            cwd="/repo/worktrees/indexer",
            repo_root="/repo",
            worktree_path="/repo/worktrees/indexer",
            branch="task/indexer",
            task_title="Index history",
            status=AgentStatus.RUNNING,
            started_at=observed_at - timedelta(hours=1),
            last_seen_at=observed_at,
            idle_seconds=15,
        )
        controller = FleetController(store, clock=lambda: observed_at)

        state = controller.build_state(filters=FleetFilterState(attention_only=True))

        self.assertEqual(state.total_visible_agents, 1)
        self.assertEqual(state.groups[0].agents[0].name, "Planner")

    def test_build_state_surfaces_orphan_local_repo_without_visible_agents(self) -> None:
        observed_at = datetime(2025, 2, 1, 12, tzinfo=UTC)
        store = InMemoryFleetStore()
        local_store = FakeLocalSessionStore(
            (
                CopilotLocalSession(
                    session_id="orphan-local",
                    cwd=Path("/repo-z/worktrees/orphan"),
                    git_root=Path("/repo-z"),
                    repository="repo-z",
                    branch="task/orphan",
                    summary="Investigate orphan session",
                    created_at=observed_at - timedelta(hours=1),
                    updated_at=observed_at,
                    last_event_type="tool.execution_complete",
                    last_event_at=observed_at,
                    is_cleanly_closed=False,
                ),
            )
        )

        controller = FleetController(store, local_sessions=local_store, clock=lambda: observed_at)

        state = controller.build_state(filters=FleetFilterState(include_completed=False))

        self.assertEqual(state.total_visible_agents, 0)
        self.assertEqual(state.total_groups, 1)
        self.assertEqual(state.groups[0].repo_label, "repo-z")
        self.assertEqual(state.groups[0].agent_count, 0)
        self.assertEqual(state.groups[0].local_session_count, 1)
        self.assertEqual(state.groups[0].orphan_local_session_count, 1)
        self.assertTrue(state.local_sessions[0].is_orphan)
        self.assertEqual(state.local_sessions[0].repo_label, "repo-z")

    def test_build_state_attention_only_keeps_local_session_drift_groups(self) -> None:
        observed_at = datetime(2025, 2, 1, 12, tzinfo=UTC)
        store = InMemoryFleetStore()
        local_store = FakeLocalSessionStore(
            (
                CopilotLocalSession(
                    session_id="orphan-local",
                    cwd=Path("/repo-z/worktrees/orphan"),
                    git_root=Path("/repo-z"),
                    repository="repo-z",
                    branch="task/orphan",
                    summary="Investigate orphan session",
                    created_at=observed_at - timedelta(hours=1),
                    updated_at=observed_at,
                    last_event_type="tool.execution_complete",
                    last_event_at=observed_at,
                    is_cleanly_closed=False,
                ),
            )
        )

        controller = FleetController(store, local_sessions=local_store, clock=lambda: observed_at)

        state = controller.build_state(filters=FleetFilterState(attention_only=True))

        self.assertEqual(state.total_groups, 1)
        self.assertEqual(state.groups[0].repo_label, "repo-z")

    def test_build_state_hide_done_hides_closed_local_sessions(self) -> None:
        observed_at = datetime(2025, 2, 1, 12, tzinfo=UTC)
        store = InMemoryFleetStore()
        local_store = FakeLocalSessionStore(
            (
                CopilotLocalSession(
                    session_id="open-local",
                    cwd=Path("/repo-z/worktrees/open"),
                    git_root=Path("/repo-z"),
                    repository="repo-z",
                    branch="task/open",
                    summary="Investigate active local session",
                    created_at=observed_at - timedelta(hours=1),
                    updated_at=observed_at,
                    last_event_type="tool.execution_complete",
                    last_event_at=observed_at,
                    is_cleanly_closed=False,
                ),
                CopilotLocalSession(
                    session_id="closed-local",
                    cwd=Path("/repo-z/worktrees/closed"),
                    git_root=Path("/repo-z"),
                    repository="repo-z",
                    branch="task/closed",
                    summary="Finished local session",
                    created_at=observed_at - timedelta(days=1),
                    updated_at=observed_at - timedelta(days=1),
                    last_event_type="session.shutdown",
                    last_event_at=observed_at - timedelta(days=1),
                    is_cleanly_closed=True,
                ),
            )
        )

        controller = FleetController(store, local_sessions=local_store, clock=lambda: observed_at)

        state = controller.build_state(filters=FleetFilterState(include_completed=False))

        self.assertEqual(state.total_groups, 1)
        self.assertEqual(len(state.local_sessions), 1)
        self.assertEqual(state.local_sessions[0].session_id, "open-local")
        self.assertEqual(state.groups[0].local_session_count, 1)
        self.assertEqual(state.groups[0].orphan_local_session_count, 1)

    def test_build_state_query_matches_session_context_fields(self) -> None:
        observed_at = datetime(2025, 2, 1, 12, tzinfo=UTC)
        store = InMemoryFleetStore()
        store.agents["agent-1"] = Agent(
            id="agent-1",
            name="Planner",
            tmux_session_name="muxdeck",
            tmux_window_id="@1",
            tmux_pane_id="%1",
            cwd="/repo-a/worktrees/planner",
            repo_root="/repo-a",
            worktree_path="/repo-a/worktrees/planner",
            branch="task/planner",
            task_title="Plan fleet search surface",
            status=AgentStatus.RUNNING,
            started_at=observed_at - timedelta(hours=1),
            last_seen_at=observed_at,
            idle_seconds=5,
        )
        store.sessions["session-1"] = Session(
            id="session-1",
            agent_id="agent-1",
            copilot_session_id="copilot-1",
            task_title="Track session context",
            created_at=observed_at - timedelta(minutes=30),
        )
        store.contexts["session-1"] = SessionContextRecord(
            session_id="session-1",
            agent_id="agent-1",
            worktree_id="wt-1",
            worktree_path="/repo-a/worktrees/context-only",
            repo_root="/repo-a",
            branch="feature/context-only",
            updated_at=observed_at,
        )
        controller = FleetController(store, clock=lambda: observed_at)

        state = controller.build_state(filters=FleetFilterState(text_query="context-only"))

        self.assertEqual(state.total_groups, 1)
        self.assertEqual(state.groups[0].repo_label, "repo-a")
        self.assertEqual(state.groups[0].open_session_count, 1)
        self.assertEqual(len(state.search_hits), 1)
        self.assertEqual(state.search_hits[0].kind, "session")
        self.assertIn("feature/context-only", state.search_hits[0].detail)
        self.assertIn("/repo-a/worktrees/context-only", state.search_hits[0].detail)

    def test_build_state_groups_agent_and_local_session_into_same_story_lane(self) -> None:
        observed_at = datetime(2025, 2, 1, 12, tzinfo=UTC)
        store = InMemoryFleetStore()
        store.agents["agent-1"] = Agent(
            id="agent-1",
            name="Planner",
            tmux_session_name="muxdeck",
            tmux_window_id="@1",
            tmux_pane_id="%1",
            cwd="/repo-a/worktrees/planner",
            repo_root="/repo-a",
            worktree_path="/repo-a/worktrees/planner",
            branch="task/planner",
            task_title="Story lane focus",
            status=AgentStatus.WAITING_INPUT,
            started_at=observed_at - timedelta(hours=1),
            last_seen_at=observed_at,
            idle_seconds=120,
            needs_attention=True,
            attention_reason="waiting for operator reply",
        )
        store.sessions["session-1"] = Session(
            id="session-1",
            agent_id="agent-1",
            copilot_session_id="copilot-1",
            task_title="Story lane focus",
            created_at=observed_at - timedelta(minutes=30),
        )
        local_store = FakeLocalSessionStore(
            (
                CopilotLocalSession(
                    session_id="local-1",
                    cwd=Path("/repo-a/worktrees/orphan"),
                    git_root=Path("/repo-a"),
                    repository="repo-a",
                    branch="task/planner",
                    summary="Story lane focus",
                    created_at=observed_at - timedelta(minutes=40),
                    updated_at=observed_at - timedelta(minutes=5),
                    last_event_type="tool.execution_complete",
                    last_event_at=observed_at - timedelta(minutes=5),
                    is_cleanly_closed=False,
                ),
            )
        )

        controller = FleetController(store, local_sessions=local_store, clock=lambda: observed_at)

        state = controller.build_state()

        self.assertEqual(len(state.story_lanes), 1)
        story = state.story_lanes[0]
        self.assertEqual(story.story_label, "Story lane focus")
        self.assertEqual(story.live_agent_count, 1)
        self.assertEqual(story.waiting_agent_count, 1)
        self.assertEqual(story.open_session_count, 1)
        self.assertEqual(story.local_session_count, 1)
        self.assertEqual(story.orphan_local_session_count, 1)
        self.assertEqual(story.inbox_count, 2)
        self.assertEqual(story.next_action, "reply")
        self.assertEqual(
            [item.suggested_action for item in state.response_inbox],
            ["reply", "recover"],
        )
        self.assertTrue(all(item.story_key == story.story_key for item in state.response_inbox))

    def test_build_state_surfaces_resume_inbox_for_open_session_without_live_agent(self) -> None:
        observed_at = datetime(2025, 2, 1, 12, tzinfo=UTC)
        store = InMemoryFleetStore()
        store.sessions["session-1"] = Session(
            id="session-1",
            agent_id="agent-1",
            copilot_session_id="copilot-1",
            task_title="Resume abandoned work",
            created_at=observed_at - timedelta(minutes=20),
        )
        store.contexts["session-1"] = SessionContextRecord(
            session_id="session-1",
            agent_id="agent-1",
            worktree_id="wt-1",
            worktree_path="/repo-a/worktrees/resume",
            repo_root="/repo-a",
            branch="task/resume",
            updated_at=observed_at,
        )

        controller = FleetController(store, clock=lambda: observed_at)

        state = controller.build_state()

        self.assertEqual(len(state.story_lanes), 1)
        story = state.story_lanes[0]
        self.assertEqual(story.story_label, "Resume abandoned work")
        self.assertEqual(story.live_agent_count, 0)
        self.assertEqual(story.open_session_count, 1)
        self.assertEqual(story.inbox_count, 1)
        self.assertEqual(story.next_action, "resume")
        self.assertEqual(len(state.response_inbox), 1)
        self.assertEqual(state.response_inbox[0].suggested_action, "resume")
        self.assertEqual(
            state.response_inbox[0].reason,
            "tracked session is open without a visible live agent",
        )


if __name__ == "__main__":
    unittest.main()

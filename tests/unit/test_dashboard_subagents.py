"""Unit tests for DashboardController.load_subagents (lazy sub-agent tree)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from copilot_commander.adapters.sqlite_store import SessionContextRecord
from copilot_commander.controllers.dashboard_controller import DashboardController
from copilot_commander.domain.enums import AgentStatus
from copilot_commander.domain.events import Event, LogChunk
from copilot_commander.domain.models import Agent, Session, Worktree
from copilot_commander.domain.subagents import ReadAgentInteraction, SubAgentSnapshot, SubAgentTree


@dataclass
class _SingleAgentStore:
    """The bare minimum a DashboardStorePort needs for these tests."""

    agent: Agent | None
    latest_session: Session | None = None

    def list_agents(self) -> tuple[Agent, ...]:
        return (self.agent,) if self.agent is not None else ()

    def list_sessions(self, agent_id: str | None = None, /) -> tuple[Session, ...]:
        if self.latest_session is None:
            return ()
        if agent_id is not None and self.latest_session.agent_id != agent_id:
            return ()
        return (self.latest_session,)

    def get_latest_session_for_agent(self, agent_id: str, /) -> Session | None:
        if self.latest_session and self.latest_session.agent_id == agent_id:
            return self.latest_session
        return None

    def count_sessions_for_agent(self, agent_id: str, /) -> int:
        return 1 if self.get_latest_session_for_agent(agent_id) else 0

    def get_open_session_for_agent(self, agent_id: str, /) -> Session | None:
        return self.get_latest_session_for_agent(agent_id)

    def get_session_context(self, session_id: str, /) -> SessionContextRecord | None:
        return None

    def list_events_for_session(self, session_id: str, /) -> tuple[Event, ...]:
        return ()

    def get_latest_event_for_session(self, session_id: str, /) -> Event | None:
        return None

    def list_log_chunks(self, session_id: str, /) -> tuple[LogChunk, ...]:
        return ()

    def get_latest_log_chunk(self, session_id: str, /) -> LogChunk | None:
        return None

    def list_recent_log_chunks(
        self, session_id: str, /, *, limit: int = 20
    ) -> tuple[LogChunk, ...]:
        return ()

    def get_worktree(self, worktree_id: str, /) -> Worktree | None:
        return None


@dataclass
class _FakeReader:
    trees: dict[str, SubAgentTree] = field(default_factory=dict)
    calls: list[str] = field(default_factory=list)

    def read(self, session_id: str) -> SubAgentTree | None:
        self.calls.append(session_id)
        return self.trees.get(session_id)


def _make_agent(agent_id: str, *, copilot_session_id: str | None = None) -> Agent:
    return Agent(
        id=agent_id,
        name=agent_id,
        tmux_session_name="muxdeck",
        tmux_window_id="@1",
        tmux_pane_id="%1",
        cwd="/repo",
        repo_root="/repo",
        branch="main",
        task_title="Task",
        copilot_session_id=copilot_session_id,
        status=AgentStatus.RUNNING,
        started_at=datetime(2026, 1, 1, tzinfo=UTC),
        last_seen_at=datetime(2026, 1, 1, 0, 1, tzinfo=UTC),
    )


class TestLoadSubagents:
    def test_returns_empty_tree_when_reader_not_configured(self) -> None:
        store = _SingleAgentStore(agent=_make_agent("a1", copilot_session_id="sess"))
        controller = DashboardController(store, subagent_reader=None)
        tree = controller.load_subagents("a1")
        assert tree.agent_id == "a1"
        assert tree.session_id is None
        assert tree.is_empty

    def test_returns_empty_tree_when_agent_missing(self) -> None:
        store = _SingleAgentStore(agent=None)
        controller = DashboardController(store, subagent_reader=_FakeReader())
        tree = controller.load_subagents("nonexistent")
        assert tree.is_empty
        assert tree.session_id is None

    def test_returns_empty_tree_when_no_session_id(self) -> None:
        store = _SingleAgentStore(agent=_make_agent("a1", copilot_session_id=None))
        reader = _FakeReader()
        controller = DashboardController(store, subagent_reader=reader)
        tree = controller.load_subagents("a1")
        assert tree.is_empty
        assert tree.session_id is None
        # Reader not consulted — no session to resolve.
        assert reader.calls == []

    def test_falls_back_to_latest_session_copilot_id(self) -> None:
        """When Agent.copilot_session_id is unset, the controller should

        still find a tree via the linked Session row.
        """
        agent = _make_agent("a1", copilot_session_id=None)
        latest = Session(
            id="session-1",
            agent_id="a1",
            task_title="t",
            copilot_session_id="copilot-sess",
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
        store = _SingleAgentStore(agent=agent, latest_session=latest)
        reader = _FakeReader(
            trees={
                "copilot-sess": SubAgentTree(
                    session_id="copilot-sess",
                    running=(
                        SubAgentSnapshot(
                            tool_call_id="call_r",
                            agent_name="explore",
                            display_name="Explore Agent",
                            description=None,
                            started_at=datetime(2026, 1, 1, tzinfo=UTC),
                            completed_at=None,
                        ),
                    ),
                    recent=(),
                    scanned_at=datetime(2026, 1, 1, 1, tzinfo=UTC),
                )
            }
        )
        controller = DashboardController(store, subagent_reader=reader)
        tree = controller.load_subagents("a1")
        assert tree.session_id == "copilot-sess"
        assert len(tree.running) == 1
        assert tree.running[0].agent_name == "explore"
        assert tree.running[0].is_running is True
        assert reader.calls == ["copilot-sess"]

    def test_maps_running_and_recent_to_views(self) -> None:
        agent = _make_agent("a1", copilot_session_id="sess-42")
        store = _SingleAgentStore(agent=agent)
        running = SubAgentSnapshot(
            tool_call_id="call_run",
            agent_name="general-purpose",
            display_name="General Purpose Agent",
            description="desc",
            started_at=datetime(2026, 1, 1, tzinfo=UTC),
            completed_at=None,
        )
        done = SubAgentSnapshot(
            tool_call_id="call_done",
            agent_name="code-review",
            display_name="Code Review Agent",
            description=None,
            started_at=datetime(2026, 1, 1, tzinfo=UTC),
            completed_at=datetime(2026, 1, 1, 0, 0, 8, tzinfo=UTC),
        )
        reader = _FakeReader(
            trees={
                "sess-42": SubAgentTree(
                    session_id="sess-42",
                    running=(running,),
                    recent=(done,),
                    scanned_at=datetime(2026, 1, 1, 1, tzinfo=UTC),
                )
            }
        )
        controller = DashboardController(store, subagent_reader=reader)
        tree = controller.load_subagents("a1")
        assert tree.session_id == "sess-42"
        assert len(tree.running) == 1
        assert tree.running[0].tool_call_id == "call_run"
        assert tree.running[0].is_running is True
        assert tree.running[0].completed_at is None
        assert len(tree.recent) == 1
        assert tree.recent[0].tool_call_id == "call_done"
        assert tree.recent[0].is_running is False
        assert tree.recent[0].completed_at is not None

    def test_view_propagates_read_interactions_and_metrics(self) -> None:
        agent = _make_agent("a1", copilot_session_id="sess-k")
        store = _SingleAgentStore(agent=agent)
        interaction = ReadAgentInteraction(
            timestamp=datetime(2026, 1, 1, 0, 1, tzinfo=UTC),
            arguments_summary='agent_id="risk-reviewer", wait=true',
            result_content="progress update",
        )
        snapshot = SubAgentSnapshot(
            tool_call_id="call_bg",
            agent_name="general-purpose",
            display_name="General Purpose Agent",
            description=None,
            started_at=datetime(2026, 1, 1, tzinfo=UTC),
            completed_at=datetime(2026, 1, 1, 0, 2, 4, tzinfo=UTC),
            task_name="risk-reviewer",
            mode="background",
            read_interactions=(interaction,),
            total_tokens=133463,
            duration_ms=124335,
            total_tool_calls=18,
            model="claude-sonnet-4.5",
            error_message="AbortError",
        )
        reader = _FakeReader(
            trees={
                "sess-k": SubAgentTree(
                    session_id="sess-k",
                    running=(),
                    recent=(snapshot,),
                    scanned_at=datetime(2026, 1, 1, 1, tzinfo=UTC),
                )
            }
        )
        controller = DashboardController(store, subagent_reader=reader)
        tree = controller.load_subagents("a1")
        assert len(tree.recent) == 1
        view = tree.recent[0]
        assert view.read_interactions == (interaction,)
        assert view.total_tokens == 133463
        assert view.duration_ms == 124335
        assert view.total_tool_calls == 18
        assert view.model == "claude-sonnet-4.5"
        assert view.error_message == "AbortError"
        assert view.mode == "background"
        assert view.task_name == "risk-reviewer"

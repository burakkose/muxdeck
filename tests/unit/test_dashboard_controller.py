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

from muxdeck.adapters.copilot_activity_reader import AgentActivity, TranscriptLine
from muxdeck.adapters.copilot_session_resolver import CopilotSessionResolution
from muxdeck.adapters.sqlite_store import SessionContextRecord
from muxdeck.controllers.dashboard_controller import (
    DashboardController,
    DashboardFilterState,
    DashboardSort,
)
from muxdeck.domain.enums import AgentStatus
from muxdeck.domain.events import Event, LogChunk
from muxdeck.domain.models import Agent, Session, Worktree


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

    def get_latest_session_for_agent(self, agent_id: str, /) -> Session | None:
        sessions = self.list_sessions(agent_id)
        return sessions[0] if sessions else None

    def count_sessions_for_agent(self, agent_id: str, /) -> int:
        return len(self.list_sessions(agent_id))

    def get_open_session_for_agent(self, agent_id: str, /) -> Session | None:
        return next(
            (s for s in self.list_sessions(agent_id) if s.ended_at is None),
            None,
        )

    def get_session_context(self, session_id: str, /) -> SessionContextRecord | None:
        return self.contexts.get(session_id)

    def list_events_for_session(self, session_id: str, /) -> tuple[Event, ...]:
        return tuple(event for event in self.events if event.session_id == session_id)

    def get_latest_event_for_session(self, session_id: str, /) -> Event | None:
        events = self.list_events_for_session(session_id)
        return events[-1] if events else None

    def list_log_chunks(self, session_id: str, /) -> tuple[LogChunk, ...]:
        return tuple(chunk for chunk in self.logs if chunk.session_id == session_id)

    def get_latest_log_chunk(self, session_id: str, /) -> LogChunk | None:
        chunks = self.list_log_chunks(session_id)
        return chunks[-1] if chunks else None

    def list_recent_log_chunks(
        self, session_id: str, /, *, limit: int = 20
    ) -> tuple[LogChunk, ...]:
        chunks = self.list_log_chunks(session_id)
        return chunks[-limit:]

    def get_worktree(self, worktree_id: str, /) -> Worktree | None:
        return self.worktrees.get(worktree_id)


class StubActivityReader:
    def __init__(
        self,
        activity: AgentActivity | None,
        *,
        transcript: tuple[TranscriptLine, ...] = (),
    ) -> None:
        self.activity = activity
        self.transcript = transcript
        self.calls: list[str] = []
        self.transcript_calls: list[tuple[str, int]] = []

    def read(self, session_id: str) -> AgentActivity | None:
        self.calls.append(session_id)
        return self.activity

    def read_transcript(self, session_id: str, *, limit: int = 40) -> tuple[TranscriptLine, ...]:
        self.transcript_calls.append((session_id, limit))
        return self.transcript[-limit:]


class StubSessionResolver:
    def __init__(self, session_id: str | None = None, *, ambiguous: bool = False) -> None:
        state = "missing"
        if session_id is not None:
            state = "resolved"
        elif ambiguous:
            state = "ambiguous"
        self.resolution = CopilotSessionResolution(session_id=session_id, state=state)
        self.calls: list[int | None] = []

    def resolve(self, pane_pid: int | None, /) -> CopilotSessionResolution:
        self.calls.append(pane_pid)
        return self.resolution

    def resolve_for_pid(self, pane_pid: int | None, /) -> str | None:
        return self.resolve(pane_pid).session_id


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
        self.assertEqual([metric.value for metric in state.metrics], [2, 1, 1, 1, 30])
        self.assertEqual(len(state.alerts), 1)
        self.assertEqual(state.selected_agent_id, "agent-1")
        assert state.selected_agent is not None
        self.assertEqual(state.selected_agent.worktree_id, "worktree-1")
        self.assertEqual(state.selected_agent.item.token_input, 10)
        self.assertEqual(state.selected_agent.item.token_output, 20)
        self.assertEqual(state.selected_agent.item.token_total, 30)
        self.assertEqual(
            [line.content for line in state.selected_agent.log_preview],
            ["beta", "gamma"],
        )

    def test_build_state_derives_token_total_from_input_and_output(self) -> None:
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
            status=AgentStatus.RUNNING,
            started_at=datetime(2025, 1, 1, 12, tzinfo=UTC),
            last_activity_at=datetime(2025, 1, 1, 12, 4, tzinfo=UTC),
            last_seen_at=observed_at,
            token_input=10,
            token_output=20,
            estimated_cost_usd=Decimal("0.250000"),
        )

        controller = DashboardController(store, clock=lambda: observed_at)
        state = controller.build_state(selected_agent_id="agent-1")

        self.assertEqual([metric.value for metric in state.metrics], [1, 1, 0, 0, 30])
        self.assertEqual(state.agents[0].token_total, 30)
        assert state.selected_agent is not None
        self.assertEqual(state.selected_agent.item.token_total, 30)

    def test_terminal_status_agents_do_not_produce_alerts(self) -> None:
        """DEAD / COMPLETED agents carrying stale attention flags are

        suppressed from the alert feed. They remain in the agent list
        (so the history is visible) but they are not actionable signals
        and must not add noise to the dashboard.
        """
        store = InMemoryDashboardStore()
        observed_at = datetime(2025, 1, 1, 12, 5, tzinfo=UTC)
        store.agents["agent-live"] = Agent(
            id="agent-live",
            name="Live",
            tmux_session_name="muxdeck",
            tmux_window_id="@1",
            tmux_pane_id="%1",
            cwd="/repo",
            repo_root="/repo",
            branch="main",
            task_title="Live task",
            status=AgentStatus.WAITING_INPUT,
            started_at=datetime(2025, 1, 1, 12, tzinfo=UTC),
            last_activity_at=observed_at,
            last_seen_at=observed_at,
            idle_seconds=30,
            needs_attention=True,
            attention_reason="waiting for confirmation input",
        )
        store.agents["agent-dead"] = Agent(
            id="agent-dead",
            name="Dead",
            tmux_session_name="muxdeck",
            tmux_window_id="@2",
            tmux_pane_id="%2",
            cwd="/repo",
            repo_root="/repo",
            branch="main",
            task_title="Dead task",
            status=AgentStatus.DEAD,
            started_at=datetime(2025, 1, 1, 11, tzinfo=UTC),
            last_seen_at=datetime(2025, 1, 1, 11, 30, tzinfo=UTC),
            needs_attention=True,
            attention_reason="tmux pane no longer exists",
        )
        store.agents["agent-done"] = Agent(
            id="agent-done",
            name="Done",
            tmux_session_name="muxdeck",
            tmux_window_id="@3",
            tmux_pane_id="%3",
            cwd="/repo",
            repo_root="/repo",
            branch="main",
            task_title="Done task",
            status=AgentStatus.COMPLETED,
            started_at=datetime(2025, 1, 1, 11, tzinfo=UTC),
            last_seen_at=datetime(2025, 1, 1, 11, 45, tzinfo=UTC),
            needs_attention=True,
            attention_reason="reviewed tool failure",
        )

        controller = DashboardController(store, clock=lambda: observed_at)
        state = controller.build_state()

        alert_ids = [alert.agent_id for alert in state.alerts]
        self.assertEqual(alert_ids, ["agent-live"])
        self.assertEqual(state.health.error_agents, 0)
        self.assertEqual(state.health.tone, "warning")

    def test_build_state_uses_latest_session_copilot_id_for_activity(self) -> None:
        store = InMemoryDashboardStore()
        observed_at = datetime(2025, 1, 1, 12, tzinfo=UTC)
        store.agents["agent-1"] = Agent(
            id="agent-1",
            name="Planner",
            tmux_session_name="muxdeck",
            tmux_window_id="@1",
            tmux_pane_id="%1",
            cwd="/repo",
            repo_root="/repo",
            branch="main",
            task_title="Plan dashboard",
            status=AgentStatus.RUNNING,
            started_at=observed_at,
            last_seen_at=observed_at,
        )
        store.sessions["session-1"] = Session(
            id="session-1",
            agent_id="agent-1",
            task_title="Plan dashboard",
            copilot_session_id="copilot-123",
            created_at=observed_at,
        )
        activity_reader = StubActivityReader(
            AgentActivity(
                intent="Inspecting dashboard",
                tool_name="view",
                tool_target="dashboard.py",
                summary="Inspecting dashboard widgets",
                waiting_for_user=False,
                latest_at=observed_at,
            )
        )

        controller = DashboardController(
            store,
            clock=lambda: observed_at,
            activity_reader=activity_reader,
        )
        state = controller.build_state()

        self.assertEqual(activity_reader.calls, ["copilot-123"])
        self.assertEqual(state.agents[0].current_activity, "Inspecting dashboard widgets")

    def test_build_state_uses_session_resolver_for_activity_and_transcript_preview(self) -> None:
        store = InMemoryDashboardStore()
        observed_at = datetime(2025, 1, 1, 12, tzinfo=UTC)
        store.agents["agent-1"] = Agent(
            id="agent-1",
            name="Planner",
            tmux_session_name="muxdeck",
            tmux_window_id="@1",
            tmux_pane_id="%1",
            cwd="/repo",
            repo_root="/repo",
            branch="main",
            task_title="Plan dashboard",
            pid=4242,
            status=AgentStatus.RUNNING,
            started_at=observed_at,
            last_seen_at=observed_at,
        )
        store.sessions["session-1"] = Session(
            id="session-1",
            agent_id="agent-1",
            task_title="Plan dashboard",
            created_at=observed_at,
        )
        activity_reader = StubActivityReader(
            AgentActivity(
                intent="Inspecting dashboard",
                tool_name="view",
                tool_target="dashboard.py",
                summary="Inspecting dashboard widgets",
                waiting_for_user=False,
                latest_at=observed_at,
            ),
            transcript=(
                TranscriptLine(
                    at=observed_at,
                    role="assistant",
                    content="live transcript line",
                    sequence_no=1,
                ),
            ),
        )
        resolver = StubSessionResolver("copilot-live")

        controller = DashboardController(
            store,
            clock=lambda: observed_at,
            activity_reader=activity_reader,
            session_resolver=resolver,
        )
        state = controller.build_state(selected_agent_id="agent-1", preview_line_limit=1)

        self.assertEqual(resolver.calls, [4242, 4242])
        self.assertEqual(activity_reader.calls, ["copilot-live"])
        self.assertEqual(activity_reader.transcript_calls, [("copilot-live", 1)])
        self.assertEqual(state.agents[0].current_activity, "Inspecting dashboard widgets")
        assert state.selected_agent is not None
        self.assertEqual(
            [line.content for line in state.selected_agent.log_preview],
            ["live transcript line"],
        )

    def test_build_state_skips_stale_session_fallback_when_live_resolution_is_ambiguous(
        self,
    ) -> None:
        store = InMemoryDashboardStore()
        observed_at = datetime(2025, 1, 1, 12, tzinfo=UTC)
        store.agents["agent-1"] = Agent(
            id="agent-1",
            name="Planner",
            tmux_session_name="muxdeck",
            tmux_window_id="@1",
            tmux_pane_id="%1",
            cwd="/repo",
            repo_root="/repo",
            branch="main",
            task_title="Plan dashboard",
            pid=4242,
            copilot_session_id="stale-agent-session",
            status=AgentStatus.RUNNING,
            started_at=observed_at,
            last_seen_at=observed_at,
        )
        store.sessions["session-1"] = Session(
            id="session-1",
            agent_id="agent-1",
            task_title="Plan dashboard",
            copilot_session_id="stale-session-row",
            created_at=observed_at,
        )
        activity_reader = StubActivityReader(
            AgentActivity(
                intent="Inspecting dashboard",
                tool_name="view",
                tool_target="dashboard.py",
                summary="Inspecting dashboard widgets",
                waiting_for_user=False,
                latest_at=observed_at,
            ),
            transcript=(
                TranscriptLine(
                    at=observed_at,
                    role="assistant",
                    content="wrong transcript line",
                    sequence_no=1,
                ),
            ),
        )
        resolver = StubSessionResolver(ambiguous=True)

        controller = DashboardController(
            store,
            clock=lambda: observed_at,
            activity_reader=activity_reader,
            session_resolver=resolver,
        )
        state = controller.build_state(selected_agent_id="agent-1", preview_line_limit=1)

        self.assertEqual(resolver.calls, [4242, 4242])
        self.assertEqual(activity_reader.calls, [])
        self.assertEqual(activity_reader.transcript_calls, [])
        assert state.selected_agent is not None
        self.assertIsNone(state.selected_agent.copilot_session_id)

    def test_build_state_recomputes_live_idle_seconds_from_last_activity(self) -> None:
        store = InMemoryDashboardStore()
        started_at = datetime(2025, 1, 1, 12, 0, tzinfo=UTC)
        last_activity_at = datetime(2025, 1, 1, 12, 4, tzinfo=UTC)
        observed_at = datetime(2025, 1, 1, 12, 5, 15, tzinfo=UTC)
        store.agents["agent-1"] = Agent(
            id="agent-1",
            name="Planner",
            tmux_session_name="muxdeck",
            tmux_window_id="@1",
            tmux_pane_id="%1",
            cwd="/repo",
            repo_root="/repo",
            branch="main",
            task_title="Plan dashboard",
            status=AgentStatus.RUNNING,
            started_at=started_at,
            last_activity_at=last_activity_at,
            last_seen_at=last_activity_at,
            idle_seconds=0,
        )

        controller = DashboardController(store, clock=lambda: observed_at)
        state = controller.build_state(selected_agent_id="agent-1")

        self.assertEqual(state.agents[0].idle_seconds, 75)
        assert state.selected_agent is not None
        self.assertEqual(state.selected_agent.item.idle_seconds, 75)

    def test_log_preview_dedupes_static_scrollback_across_snapshots(self) -> None:
        # Each tmux_capture LogChunk is a complete pane snapshot, not
        # an incremental delta — so a long-lived agent accumulates
        # many snapshots that share the same static scrollback prefix
        # (the original ``copilot`` command, the launch banner, etc.).
        # Without dedup, ``lines[-N:]`` surfaces that frozen prefix
        # because it dominates the flattened line stream. The preview
        # must show the *cumulative live churn* — content that's new
        # in later snapshots — not the same banner over and over.
        store = InMemoryDashboardStore()
        observed_at = datetime(2025, 6, 1, 12, 0, tzinfo=UTC)
        store.agents["agent-pwsh"] = Agent(
            id="agent-pwsh",
            name="pwsh-agent",
            tmux_session_name="muxdeck",
            tmux_window_id="@1",
            tmux_pane_id="%9",
            cwd="/repo",
            status=AgentStatus.RUNNING,
            started_at=observed_at,
            last_seen_at=observed_at,
        )
        store.sessions["session-pwsh"] = Session(
            id="session-pwsh",
            agent_id="agent-pwsh",
            created_at=observed_at,
        )
        # First snapshot: shell prompt + the operator's `copilot` command
        # + the banner that copilot prints on launch.
        snapshot_one = (
            "PS C:\\repo> copilot\n"
            "  banner-line-1\n"
            "  banner-line-2\n"
            "  banner-line-3\n"
            "Copilot ready. > \n"
        )
        # Second snapshot: same scrollback above, plus a new live line
        # at the bottom (typical TUI churn).
        snapshot_two = snapshot_one + "● Read main.py\n  done\n"
        # Third snapshot: same scrollback + more new churn.
        snapshot_three = snapshot_two + "◐ Thinking (1.2 KiB)\n"
        for sequence_no, content in enumerate((snapshot_one, snapshot_two, snapshot_three)):
            store.logs.append(
                LogChunk(
                    id=f"log-{sequence_no}",
                    agent_id="agent-pwsh",
                    session_id="session-pwsh",
                    source="tmux_capture",
                    sequence_no=sequence_no,
                    captured_at=observed_at,
                    content=content,
                )
            )

        controller = DashboardController(store, clock=lambda: observed_at)
        state = controller.build_state(
            selected_agent_id="agent-pwsh",
            preview_line_limit=5,
        )

        assert state.selected_agent is not None
        contents = [line.content for line in state.selected_agent.log_preview]
        # The frozen prefix must NOT dominate the preview…
        self.assertNotIn("PS C:\\repo> copilot", contents)
        self.assertNotIn("banner-line-1", contents)
        # …and the live tail from the most recent snapshots MUST be
        # what surfaces, including content unique to later snapshots.
        self.assertIn("● Read main.py", contents)
        self.assertIn("◐ Thinking (1.2 KiB)", contents)
        # The live tail is the *last* thing in the preview, since
        # dedup preserves chronological insertion order.
        self.assertEqual(contents[-1], "◐ Thinking (1.2 KiB)")


if __name__ == "__main__":
    unittest.main()

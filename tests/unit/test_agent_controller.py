# ruff: noqa: E402,I001,PT009

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
import sys
import unittest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from muxdeck.adapters.sqlite_store import SessionContextRecord
from muxdeck.controllers.agent_controller import AgentController
from muxdeck.domain.enums import AgentStatus
from muxdeck.domain.models import Agent, Session
from muxdeck.services.session_service import SessionBundle


class FakeAgentStore:
    def __init__(self, agent: Agent) -> None:
        self.agent = agent
        self.sessions: dict[str, Session] = {}
        self.contexts: dict[str, SessionContextRecord] = {}

    def get_agent(self, agent_id: str, /) -> Agent | None:
        return self.agent if self.agent.id == agent_id else None

    def upsert_agent(self, agent: Agent, /) -> None:
        self.agent = agent

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

    def get_agent_by_pane_id(self, pane_id: str, /) -> Agent | None:
        if self.agent is None:
            return None
        return self.agent if self.agent.tmux_pane_id == pane_id else None

    def get_agent_by_copilot_session_id(self, session_id: str, /) -> Agent | None:
        if self.agent is None:
            return None
        return self.agent if self.agent.copilot_session_id == session_id else None

    def get_worktree(self, worktree_id: str, /) -> None:
        del worktree_id


class FakeSessionService:
    def __init__(self, store: FakeAgentStore, now: datetime) -> None:
        self._store = store
        self._now = now

    def create_session(self, agent_id: str, **_: object) -> SessionBundle:
        session = Session(id="session-created", agent_id=agent_id, created_at=self._now)
        self._store.sessions[session.id] = session
        context = SessionContextRecord(
            session_id=session.id,
            agent_id=agent_id,
            tmux_pane_id="%1",
            worktree_path="/repo/worktrees/task",
            repo_root="/repo",
            branch="task/example",
            updated_at=self._now,
        )
        self._store.contexts[session.id] = context
        return SessionBundle(
            session=session,
            context=context,
            agent=self._store.agent,
            worktree=None,
        )

    def end_session(
        self,
        session_id: str,
        *,
        exit_reason: str,
        ended_at: datetime | None = None,
        **_: object,
    ) -> SessionBundle:
        session = self._store.sessions[session_id]
        ended = Session(
            id=session.id,
            agent_id=session.agent_id,
            task_title=session.task_title,
            created_at=session.created_at,
            ended_at=ended_at or self._now,
            exit_reason=exit_reason,
        )
        self._store.sessions[session_id] = ended
        context = self._store.contexts[session_id]
        return SessionBundle(
            session=ended,
            context=context,
            agent=self._store.agent,
            worktree=None,
        )


class AgentControllerTests(unittest.TestCase):
    def test_adopt_mark_complete_and_intents_use_agent_context(self) -> None:
        now = datetime(2025, 1, 1, 12, tzinfo=UTC)
        agent = Agent(
            id="agent-1",
            name="Planner",
            tmux_session_name="muxdeck",
            tmux_window_id="@1",
            tmux_pane_id="%1",
            cwd="/repo/worktrees/task",
            repo_root="/repo",
            worktree_path="/repo/worktrees/task",
            branch="task/example",
            task_title="Example",
            status=AgentStatus.RUNNING,
            started_at=now,
            last_seen_at=now,
        )
        store = FakeAgentStore(agent)
        sessions = FakeSessionService(store, now)
        controller = AgentController(store, sessions, clock=lambda: now)

        adopted = controller.adopt("agent-1")
        restart = controller.restart_intent("agent-1", model="gpt-5.4")
        send_input = controller.send_input_intent("agent-1", "Continue")
        complete = controller.mark_complete("agent-1")
        rename_window = controller.rename_window_intent("agent-1", new_name="planner-ui")
        move_window = controller.move_to_window_intent(
            "agent-1",
            new_window_name="planner-ui",
        )
        kill_pane = controller.kill_pane_intent("agent-1")

        self.assertTrue(adopted.session_created)
        self.assertEqual(adopted.session_id, "session-created")
        self.assertEqual(restart.metadata[-1], ("model", "gpt-5.4"))
        self.assertEqual(send_input.prompt, "Continue")
        self.assertTrue(complete.session_ended)
        pane_intent = controller.open_pane_intent("agent-1")
        worktree_intent = controller.open_worktree_intent("agent-1")
        self.assertEqual(
            pane_intent.metadata,
            (
                ("pane_target", "%1"),
                ("window_target", "@1"),
                ("session_target", "muxdeck"),
            ),
        )
        self.assertEqual(pane_intent.agent.tmux_session_name, "muxdeck")
        self.assertEqual(pane_intent.agent.tmux_window_id, "@1")
        self.assertEqual(worktree_intent.metadata, (("path", "/repo/worktrees/task"),))
        self.assertEqual(
            rename_window.metadata,
            (("window_target", "@1"), ("window_name", "planner-ui")),
        )
        self.assertEqual(
            move_window.metadata,
            (
                ("pane_target", "%1"),
                ("session_target", "muxdeck"),
                ("new_window_name", "planner-ui"),
            ),
        )
        self.assertEqual(kill_pane.metadata, (("pane_target", "%1"),))

    def test_mark_complete_without_session(self) -> None:
        now = datetime(2025, 1, 1, 12, tzinfo=UTC)
        agent = Agent(
            id="agent-1",
            name="test",
            tmux_pane_id="%1",
            tmux_window_id="@1",
            tmux_session_name="muxdeck",
            cwd="/repo",
            branch="main",
            status=AgentStatus.RUNNING,
        )
        store = FakeAgentStore(agent)
        sessions = FakeSessionService(store, now)
        controller = AgentController(store, sessions, clock=lambda: now)

        result = controller.mark_complete("agent-1")

        self.assertFalse(result.session_ended)
        self.assertIsNone(result.session_id)

    def test_restart_intent_includes_model(self) -> None:
        now = datetime(2025, 1, 1, 12, tzinfo=UTC)
        agent = Agent(
            id="agent-1",
            name="test",
            tmux_pane_id="%1",
            tmux_window_id="@1",
            tmux_session_name="muxdeck",
            cwd="/repo",
            branch="main",
        )
        store = FakeAgentStore(agent)
        sessions = FakeSessionService(store, now)
        controller = AgentController(store, sessions, clock=lambda: now)

        intent = controller.restart_intent("agent-1", model="gpt-5.4")

        self.assertIn(("model", "gpt-5.4"), intent.metadata)

    def test_move_to_window_intent_with_session(self) -> None:
        now = datetime(2025, 1, 1, 12, tzinfo=UTC)
        agent = Agent(
            id="agent-1",
            name="test",
            tmux_pane_id="%1",
            tmux_window_id="@1",
            tmux_session_name="muxdeck",
            cwd="/repo",
            branch="main",
        )
        store = FakeAgentStore(agent)
        sessions = FakeSessionService(store, now)
        controller = AgentController(store, sessions, clock=lambda: now)

        intent = controller.move_to_window_intent("agent-1", target_window="@2")

        self.assertIn(("session_target", "muxdeck"), intent.metadata)

    def test_adopt_creates_new_session_with_task_title(self) -> None:
        now = datetime(2025, 1, 1, 12, tzinfo=UTC)
        agent = Agent(
            id="agent-1",
            name="test",
            tmux_pane_id="%1",
            tmux_window_id="@1",
            tmux_session_name="muxdeck",
            cwd="/repo",
            branch="main",
        )
        store = FakeAgentStore(agent)
        sessions = FakeSessionService(store, now)
        controller = AgentController(store, sessions, clock=lambda: now)

        result = controller.adopt("agent-1", task_title="important-task")

        self.assertTrue(result.session_created)


class SeedResumedSessionTests(unittest.TestCase):
    """Regression coverage for ``AgentController.seed_resumed_session``.

    Seeding runs after ``SessionsScreen.action_resume_session`` opens a
    fresh tmux window for a resumed Copilot session. It writes the
    initial ``Agent`` row up front so the dashboard immediately shows
    the *correct* repo/branch/name and so the monitoring loop has a
    record to fold pane data into. The two cases pinned here:

    * Brand-new pane id → a fresh ``Agent`` is created in ``STARTING``
      state with ``started_at == now()`` and every seeded field carried
      through verbatim (especially ``copilot_session_id`` so the
      session→pane link survives the next monitoring sync).
    * Pane id already present → the existing ``Agent`` row is updated
      in place and its original ``started_at`` is preserved so the
      session age does not "reset" if the operator accidentally
      resumes the same session twice through muxdeck.
    """

    def _make_controller(
        self,
        store: FakeAgentStore,
        now: datetime,
    ) -> AgentController:
        sessions = FakeSessionService(store, now)
        return AgentController(store, sessions, clock=lambda: now)

    def test_seed_creates_new_agent_record_with_starting_status(self) -> None:
        now = datetime(2025, 1, 1, 12, tzinfo=UTC)

        class EmptyStore(FakeAgentStore):
            def __init__(self) -> None:
                self.agent = None  # type: ignore[assignment]
                self.sessions: dict[str, Session] = {}
                self.contexts: dict[str, SessionContextRecord] = {}
                self.upserts: list[Agent] = []

            def get_agent(self, agent_id: str, /) -> Agent | None:
                return None

            def get_agent_by_pane_id(self, pane_id: str, /) -> Agent | None:
                return None

            def upsert_agent(self, agent: Agent, /) -> None:
                self.agent = agent
                self.upserts.append(agent)

        store = EmptyStore()
        controller = self._make_controller(store, now)  # type: ignore[arg-type]

        seeded = controller.seed_resumed_session(
            copilot_session_id="resumed-windows-session",
            tmux_pane_id="%9",
            tmux_session_name="muxdeck",
            tmux_window_id="@9",
            tmux_window_name="session abc12345",
            pane_tty="/dev/pts/4",
            pane_pid=4242,
            cwd=r"C:\src\CosmosDB",
            repo_root=r"C:\src\CosmosDB",
            worktree_path=r"C:\src\CosmosDB",
            branch="users/example/perf",
            name="CosmosDB",
        )

        # Identity + linkage carried verbatim.
        self.assertEqual(seeded.copilot_session_id, "resumed-windows-session")
        self.assertEqual(seeded.tmux_pane_id, "%9")
        self.assertEqual(seeded.cwd, r"C:\src\CosmosDB")
        self.assertEqual(seeded.repo_root, r"C:\src\CosmosDB")
        self.assertEqual(seeded.branch, "users/example/perf")
        self.assertEqual(seeded.name, "CosmosDB")
        # STARTING is the documented seed state — promotion happens
        # when monitoring observes real activity in the pane.
        self.assertIs(seeded.status, AgentStatus.STARTING)
        self.assertEqual(seeded.started_at, now)
        self.assertEqual(seeded.last_seen_at, now)
        # Exactly one upsert performed, and the controller hands back
        # the same row it persisted.
        self.assertEqual(len(store.upserts), 1)
        self.assertIs(store.upserts[0], seeded)

    def test_seed_preserves_started_at_when_pane_already_known(self) -> None:
        original_start = datetime(2024, 12, 31, 9, tzinfo=UTC)
        now = datetime(2025, 1, 1, 12, tzinfo=UTC)
        existing = Agent(
            id="agent-existing",
            name="old-name",
            tmux_session_name="muxdeck",
            tmux_window_id="@9",
            tmux_pane_id="%9",
            cwd="/home/burakkose/muxdeck",
            repo_root="/home/burakkose/muxdeck",
            worktree_path="/home/burakkose/muxdeck",
            branch="main",
            status=AgentStatus.RUNNING,
            started_at=original_start,
            last_seen_at=original_start,
            copilot_session_id="prior-session",
        )

        class PinnedStore(FakeAgentStore):
            def __init__(self, agent: Agent) -> None:
                super().__init__(agent)
                self.upserts: list[Agent] = []

            def get_agent_by_pane_id(self, pane_id: str, /) -> Agent | None:
                return self.agent if self.agent.tmux_pane_id == pane_id else None

            def upsert_agent(self, agent: Agent, /) -> None:
                self.agent = agent
                self.upserts.append(agent)

        store = PinnedStore(existing)
        controller = self._make_controller(store, now)

        seeded = controller.seed_resumed_session(
            copilot_session_id="resumed-windows-session",
            tmux_pane_id="%9",
            tmux_session_name="muxdeck",
            tmux_window_id="@9",
            tmux_window_name="session abc12345",
            pane_tty="/dev/pts/4",
            pane_pid=4242,
            cwd=r"C:\src\CosmosDB",
            repo_root=r"C:\src\CosmosDB",
            worktree_path=r"C:\src\CosmosDB",
            branch="users/example/perf",
            name="CosmosDB",
        )

        # Re-seeding must NOT mint a fresh id or reset the clock —
        # the row identity is anchored on tmux_pane_id and the age
        # is anchored on the original ``started_at``.
        self.assertEqual(seeded.id, "agent-existing")
        self.assertEqual(seeded.started_at, original_start)
        # The new metadata replaces the stale fields.
        self.assertEqual(seeded.copilot_session_id, "resumed-windows-session")
        self.assertEqual(seeded.cwd, r"C:\src\CosmosDB")
        self.assertEqual(seeded.name, "CosmosDB")
        # last_seen_at advances to "now" so the row counts as fresh.
        self.assertEqual(seeded.last_seen_at, now)
        self.assertIs(seeded.status, AgentStatus.STARTING)


if __name__ == "__main__":
    unittest.main()

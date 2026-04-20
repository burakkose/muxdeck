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


if __name__ == "__main__":
    unittest.main()

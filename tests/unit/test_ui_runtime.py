from __future__ import annotations

from datetime import UTC, datetime
from typing import cast

from copilot_commander.app import CommanderApp, CommanderRuntime
from copilot_commander.domain.models import Session


class FakeStore:
    def __init__(self) -> None:
        timestamp = datetime(2025, 1, 1, 12, tzinfo=UTC)
        self._sessions = (
            Session(id="session-1", agent_id="agent-1", created_at=timestamp),
            Session(id="session-2", agent_id="agent-2", created_at=timestamp),
        )

    def get_session(self, session_id: str) -> Session | None:
        return next((session for session in self._sessions if session.id == session_id), None)

    def list_sessions(self, agent_id: str | None = None) -> tuple[Session, ...]:
        if agent_id is None:
            return self._sessions
        return tuple(session for session in self._sessions if session.agent_id == agent_id)


class FakeConfig:
    class General:
        discovery_interval_sec = 2

    general = General()


class FakeRuntime:
    def __init__(self) -> None:
        self.config = FakeConfig()
        self.store = FakeStore()
        self.dashboard = object()
        self.worktrees = object()
        self.replay = object()
        self.agents = object()


def test_resolve_replay_session_prefers_selected_agent() -> None:
    app = CommanderApp(cast(CommanderRuntime, FakeRuntime()))
    app.selected_agent_id = "agent-2"
    app.selected_session_id = "session-1"

    assert app.resolve_replay_session_id() == "session-2"


def test_resolve_replay_session_keeps_existing_session() -> None:
    app = CommanderApp(cast(CommanderRuntime, FakeRuntime()))

    assert app.resolve_replay_session_id("session-1") == "session-1"

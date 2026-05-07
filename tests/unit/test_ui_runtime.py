from __future__ import annotations

from datetime import UTC, datetime
from typing import cast

from muxdeck.app import MuxdeckApp, MuxdeckRuntime
from muxdeck.domain.models import Session


class FakeStore:
    def __init__(self) -> None:
        timestamp = datetime(2025, 1, 1, 12, tzinfo=UTC)
        self._sessions = (
            Session(
                id="session-1",
                agent_id="agent-1",
                created_at=timestamp,
                copilot_session_id="copilot-1",
            ),
            Session(
                id="session-2",
                agent_id="agent-2",
                created_at=timestamp,
                copilot_session_id="copilot-2",
            ),
        )

    def get_session(self, session_id: str) -> Session | None:
        return next((session for session in self._sessions if session.id == session_id), None)

    def get_session_by_copilot_session_id(self, copilot_id: str) -> Session | None:
        return next(
            (session for session in self._sessions if session.copilot_session_id == copilot_id),
            None,
        )

    def list_sessions(self, agent_id: str | None = None) -> tuple[Session, ...]:
        if agent_id is None:
            return self._sessions
        return tuple(session for session in self._sessions if session.agent_id == agent_id)


class _EmptyStore:
    def get_session(self, _session_id: str) -> Session | None:
        return None

    def get_session_by_copilot_session_id(self, _copilot_id: str) -> Session | None:
        return None

    def list_sessions(self, _agent_id: str | None = None) -> tuple[Session, ...]:
        return ()


class FakeConfig:
    class General:
        discovery_interval_sec = 2

    general = General()


class FakeRuntime:
    def __init__(self, *, store: object | None = None) -> None:
        self.config = FakeConfig()
        self.store = store if store is not None else FakeStore()
        self.dashboard = object()
        self.worktrees = object()
        self.replay = object()
        self.agents = object()
        self.synchronizer = None
        self.sync_store = None


def test_resolve_replay_session_prefers_selected_agent() -> None:
    app = MuxdeckApp(cast(MuxdeckRuntime, FakeRuntime()))
    app.selected_agent_id = "agent-2"
    app.selected_session_id = "session-1"

    assert app.resolve_replay_session_id() == "session-2"


def test_resolve_replay_session_keeps_existing_session() -> None:
    app = MuxdeckApp(cast(MuxdeckRuntime, FakeRuntime()))

    assert app.resolve_replay_session_id("session-1") == "session-1"


def test_resolve_replay_session_resolves_copilot_session_id() -> None:
    app = MuxdeckApp(cast(MuxdeckRuntime, FakeRuntime()))

    assert app.resolve_replay_session_id("copilot-2") == "session-2"


def test_resolve_replay_session_falls_back_to_selected_session_internal() -> None:
    app = MuxdeckApp(cast(MuxdeckRuntime, FakeRuntime()))
    app.selected_session_id = "session-2"

    assert app.resolve_replay_session_id() == "session-2"


def test_resolve_replay_session_falls_back_to_selected_session_copilot() -> None:
    app = MuxdeckApp(cast(MuxdeckRuntime, FakeRuntime()))
    app.selected_session_id = "copilot-1"

    assert app.resolve_replay_session_id() == "session-1"


def test_resolve_replay_session_returns_first_session_when_no_state() -> None:
    app = MuxdeckApp(cast(MuxdeckRuntime, FakeRuntime()))

    assert app.resolve_replay_session_id() == "session-1"


def test_resolve_replay_session_returns_none_when_store_empty() -> None:
    app = MuxdeckApp(cast(MuxdeckRuntime, FakeRuntime(store=_EmptyStore())))

    assert app.resolve_replay_session_id() is None


def test_resolve_replay_session_returns_none_when_only_unknown_inputs_with_empty_store() -> None:
    app = MuxdeckApp(cast(MuxdeckRuntime, FakeRuntime(store=_EmptyStore())))
    app.selected_session_id = "missing"
    app.selected_agent_id = "missing-agent"

    assert app.resolve_replay_session_id("missing-session") is None


def test_remember_helpers_update_state() -> None:
    app = MuxdeckApp(cast(MuxdeckRuntime, FakeRuntime()))
    app.remember_agent_selection("agent-9")
    app.remember_worktree_selection("wt-9")
    app.remember_session_selection("session-9")

    assert app.selected_agent_id == "agent-9"
    assert app.selected_worktree_id == "wt-9"
    assert app.selected_session_id == "session-9"


def test_set_tab_badge_clamps_negative_to_zero_and_drops_entry() -> None:
    app = MuxdeckApp(cast(MuxdeckRuntime, FakeRuntime()))
    app.set_tab_badge("dashboard", 3)
    assert app.tab_badges["dashboard"] == 3

    # Re-setting with the same count is a no-op.
    app.set_tab_badge("dashboard", 3)
    assert app.tab_badges["dashboard"] == 3

    # Setting to zero (or negative) clears the entry.
    app.set_tab_badge("dashboard", 0)
    assert "dashboard" not in app.tab_badges

    app.set_tab_badge("dashboard", -1)
    assert "dashboard" not in app.tab_badges

"""Tests for session search/filter in SessionsController."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from copilot_commander.adapters.copilot_session_store import CopilotLocalSession
from copilot_commander.controllers.sessions_controller import SessionsController


def _session(
    session_id: str = "test-id",
    *,
    repository: str = "user/repo",
    branch: str = "main",
    summary: str = "Test",
    is_cleanly_closed: bool = False,
) -> CopilotLocalSession:
    now = datetime.now(UTC)
    return CopilotLocalSession(
        session_id=session_id,
        cwd=Path("/home/user/test"),
        git_root=Path("/home/user/test"),
        repository=repository,
        branch=branch,
        summary=summary,
        created_at=now - timedelta(hours=2),
        updated_at=now,
        last_event_type=None,
        last_event_at=now,
        checkpoint_count=0,
        is_cleanly_closed=is_cleanly_closed,
    )


class FakeSessionStore:
    """Minimal fake that acts like CopilotSessionStore."""

    def __init__(self, sessions: list[CopilotLocalSession]) -> None:
        self._sessions = sessions

    def discover(self, *, force: bool = False) -> list[CopilotLocalSession]:
        return list(self._sessions)

    def get_session(self, session_id: str) -> CopilotLocalSession | None:
        for s in self._sessions:
            if s.session_id == session_id:
                return s
        return None


# ── filter by different terms ───────────────────────────────────────


class TestFilterByDifferentTerms:
    def test_filter_by_summary(self) -> None:
        sessions = [
            _session("s1", summary="Fix authentication bug"),
            _session("s2", summary="Add styling"),
        ]
        ctrl = SessionsController(FakeSessionStore(sessions))  # type: ignore[arg-type]
        state = ctrl.build_state(filter_text="authentication")
        assert len(state.sessions) == 1
        assert state.sessions[0].session_id == "s1"

    def test_filter_by_repository(self) -> None:
        sessions = [
            _session("s1", repository="acme/frontend"),
            _session("s2", repository="acme/backend"),
        ]
        ctrl = SessionsController(FakeSessionStore(sessions))  # type: ignore[arg-type]
        state = ctrl.build_state(filter_text="frontend")
        assert len(state.sessions) == 1
        assert state.sessions[0].session_id == "s1"

    def test_filter_by_branch(self) -> None:
        sessions = [
            _session("s1", branch="feat/login"),
            _session("s2", branch="fix/typo"),
        ]
        ctrl = SessionsController(FakeSessionStore(sessions))  # type: ignore[arg-type]
        state = ctrl.build_state(filter_text="login")
        assert len(state.sessions) == 1
        assert state.sessions[0].session_id == "s1"

    def test_filter_by_session_id(self) -> None:
        sessions = [
            _session("abc-123"),
            _session("xyz-789"),
        ]
        ctrl = SessionsController(FakeSessionStore(sessions))  # type: ignore[arg-type]
        state = ctrl.build_state(filter_text="abc-123")
        assert len(state.sessions) == 1
        assert state.sessions[0].session_id == "abc-123"

    def test_filter_matches_multiple(self) -> None:
        sessions = [
            _session("s1", summary="Fix auth", repository="acme/auth"),
            _session("s2", summary="Add auth tests", repository="acme/tests"),
            _session("s3", summary="Add styling"),
        ]
        ctrl = SessionsController(FakeSessionStore(sessions))  # type: ignore[arg-type]
        state = ctrl.build_state(filter_text="auth")
        assert len(state.sessions) == 2
        ids = {s.session_id for s in state.sessions}
        assert ids == {"s1", "s2"}


# ── case-insensitive search ─────────────────────────────────────────


class TestCaseInsensitiveSearch:
    def test_lowercase_query_matches_mixed_case(self) -> None:
        sessions = [_session("s1", summary="Fix Authentication Bug")]
        ctrl = SessionsController(FakeSessionStore(sessions))  # type: ignore[arg-type]
        state = ctrl.build_state(filter_text="authentication")
        assert len(state.sessions) == 1

    def test_uppercase_query_matches(self) -> None:
        sessions = [_session("s1", summary="Fix authentication bug")]
        ctrl = SessionsController(FakeSessionStore(sessions))  # type: ignore[arg-type]
        state = ctrl.build_state(filter_text="AUTHENTICATION")
        assert len(state.sessions) == 1

    def test_mixed_case_query_matches(self) -> None:
        sessions = [_session("s1", summary="Fix authentication bug")]
        ctrl = SessionsController(FakeSessionStore(sessions))  # type: ignore[arg-type]
        state = ctrl.build_state(filter_text="Auth")
        assert len(state.sessions) == 1


# ── empty filter returns all ────────────────────────────────────────


class TestEmptyFilterReturnsAll:
    def test_empty_string_returns_all(self) -> None:
        sessions = [
            _session("s1", summary="First"),
            _session("s2", summary="Second"),
            _session("s3", summary="Third"),
        ]
        ctrl = SessionsController(FakeSessionStore(sessions))  # type: ignore[arg-type]
        state = ctrl.build_state(filter_text="")
        assert len(state.sessions) == 3

    def test_no_filter_returns_all(self) -> None:
        sessions = [
            _session("s1", summary="First"),
            _session("s2", summary="Second"),
        ]
        ctrl = SessionsController(FakeSessionStore(sessions))  # type: ignore[arg-type]
        state = ctrl.build_state()
        assert len(state.sessions) == 2

    def test_whitespace_only_matches_nothing(self) -> None:
        sessions = [
            _session("s1", summary="First"),
            _session("s2", summary="Second"),
        ]
        ctrl = SessionsController(FakeSessionStore(sessions))  # type: ignore[arg-type]
        # Whitespace-only filter is treated as a literal search, not ignored
        state = ctrl.build_state(filter_text="   ")
        assert len(state.sessions) == 0

    def test_no_match_returns_empty(self) -> None:
        sessions = [
            _session("s1", summary="First"),
            _session("s2", summary="Second"),
        ]
        ctrl = SessionsController(FakeSessionStore(sessions))  # type: ignore[arg-type]
        state = ctrl.build_state(filter_text="nonexistent_term_xyz")
        assert len(state.sessions) == 0

    def test_counts_reflect_all_sessions_not_filtered(self) -> None:
        sessions = [
            _session("s1", summary="First"),
            _session("s2", summary="Second"),
        ]
        ctrl = SessionsController(FakeSessionStore(sessions))  # type: ignore[arg-type]
        state = ctrl.build_state(filter_text="First")
        # total_count should be all sessions, not filtered ones
        assert state.total_count == 2
        assert len(state.sessions) == 1

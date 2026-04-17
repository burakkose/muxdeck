"""Tests for SessionsController — view model building from local sessions."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from copilot_commander.adapters.copilot_session_store import (
    CopilotLocalSession,
)
from copilot_commander.controllers.sessions_controller import (
    SessionsController,
    _relative_time,
    _session_status,
    _status_glyph,
)

# ── helpers ─────────────────────────────────────────────────────


def _session(
    session_id: str = "test-id",
    *,
    repository: str = "user/repo",
    branch: str = "main",
    summary: str = "Test",
    is_cleanly_closed: bool = False,
    updated_at: datetime | None = None,
    checkpoint_count: int = 0,
    last_event_type: str | None = None,
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
        updated_at=updated_at or now,
        last_event_type=last_event_type,
        last_event_at=now,
        checkpoint_count=checkpoint_count,
        is_cleanly_closed=is_cleanly_closed,
    )


# ── _relative_time ──────────────────────────────────────────────


def test_relative_time_none() -> None:
    assert _relative_time(None) == "—"


def test_relative_time_seconds() -> None:
    dt = datetime.now(UTC) - timedelta(seconds=30)
    assert _relative_time(dt) == "30s ago"


def test_relative_time_minutes() -> None:
    dt = datetime.now(UTC) - timedelta(minutes=5)
    assert _relative_time(dt) == "5m ago"


def test_relative_time_hours() -> None:
    dt = datetime.now(UTC) - timedelta(hours=3)
    assert _relative_time(dt) == "3h ago"


def test_relative_time_days() -> None:
    dt = datetime.now(UTC) - timedelta(days=7)
    assert _relative_time(dt) == "7d ago"


def test_relative_time_old_date() -> None:
    dt = datetime(2024, 6, 15, tzinfo=UTC)
    result = _relative_time(dt)
    assert result == "2024-06-15"


# ── _session_status ─────────────────────────────────────────────


def test_session_status_active() -> None:
    s = _session("active-1")
    assert _session_status(s, frozenset({"active-1"})) == "active"


def test_session_status_unclosed() -> None:
    s = _session("dead-1", is_cleanly_closed=False)
    assert _session_status(s, frozenset()) == "unclosed"


def test_session_status_completed() -> None:
    s = _session("done-1", is_cleanly_closed=True)
    assert _session_status(s, frozenset()) == "completed"


# ── _status_glyph ──────────────────────────────────────────────


def test_status_glyphs() -> None:
    assert _status_glyph("active") == "🟢"
    assert _status_glyph("unclosed") == "🔴"
    assert _status_glyph("completed") == "⚪"
    assert _status_glyph("unknown") == "⚫"


# ── SessionsController ─────────────────────────────────────────


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


def test_controller_build_state_basic() -> None:
    sessions = [
        _session("s1", summary="First", is_cleanly_closed=False),
        _session("s2", summary="Second", is_cleanly_closed=True),
    ]
    ctrl = SessionsController(FakeSessionStore(sessions))  # type: ignore[arg-type]
    state = ctrl.build_state()
    assert state.total_count == 2
    assert state.unclosed_count == 1
    assert state.completed_count == 1
    assert len(state.sessions) == 2


def test_controller_build_state_with_active() -> None:
    sessions = [_session("s1", summary="Active")]
    ctrl = SessionsController(FakeSessionStore(sessions))  # type: ignore[arg-type]
    state = ctrl.build_state(live_session_ids=frozenset({"s1"}))
    assert state.active_count == 1
    assert state.sessions[0].status == "active"


def test_controller_hide_completed() -> None:
    sessions = [
        _session("s1", is_cleanly_closed=False),
        _session("s2", is_cleanly_closed=True),
    ]
    ctrl = SessionsController(FakeSessionStore(sessions))  # type: ignore[arg-type]
    state = ctrl.build_state(show_completed=False)
    assert len(state.sessions) == 1
    assert state.sessions[0].session_id == "s1"


def test_controller_filter_text() -> None:
    sessions = [
        _session("s1", summary="Fix authentication", repository="user/auth"),
        _session("s2", summary="Add styling", repository="user/ui"),
    ]
    ctrl = SessionsController(FakeSessionStore(sessions))  # type: ignore[arg-type]
    state = ctrl.build_state(filter_text="auth")
    assert len(state.sessions) == 1
    assert state.sessions[0].session_id == "s1"


def test_controller_selected_detail() -> None:
    sessions = [_session("s1", summary="Selected", checkpoint_count=5)]
    ctrl = SessionsController(FakeSessionStore(sessions))  # type: ignore[arg-type]
    state = ctrl.build_state(selected_session_id="s1")
    assert state.selected is not None
    assert state.selected.session_id == "s1"
    assert state.selected.checkpoint_count == 5
    assert state.selected.resume_command == "copilot --resume=s1"


def test_controller_selected_not_found() -> None:
    ctrl = SessionsController(FakeSessionStore([]))  # type: ignore[arg-type]
    state = ctrl.build_state(selected_session_id="nonexistent")
    assert state.selected is None


# ── Windows-origin sessions ─────────────────────────────────────


def test_windows_session_flows_through_detail_view() -> None:
    store = FakeSessionStore(
        [
            CopilotLocalSession(
                session_id="win-abc",
                cwd=Path("/mnt/c/Users/alice/proj"),
                git_root=Path("/mnt/c/Users/alice/proj"),
                repository="alice/proj",
                branch="main",
                summary="work",
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
                origin="windows",
                windows_cwd="C:\\Users\\alice\\proj",
                windows_git_root="C:\\Users\\alice\\proj",
            )
        ]
    )
    ctrl = SessionsController(store)  # type: ignore[arg-type]
    state = ctrl.build_state(show_completed=True, selected_session_id="win-abc")

    assert state.sessions[0].origin == "windows"
    assert state.selected is not None
    assert state.selected.origin == "windows"
    # Detail should prefer the Windows path so the user can copy it.
    assert state.selected.cwd == "C:\\Users\\alice\\proj"
    # Resume command must wrap in pwsh.
    cmd = state.selected.resume_command
    assert cmd.startswith("pwsh.exe -NoExit -Command")
    assert "Set-Location -LiteralPath 'C:\\Users\\alice\\proj'" in cmd
    assert "copilot --resume=win-abc" in cmd


def test_local_session_resume_command_is_plain() -> None:
    store = FakeSessionStore([_session(session_id="lin-abc")])
    ctrl = SessionsController(store)  # type: ignore[arg-type]
    state = ctrl.build_state(selected_session_id="lin-abc")

    assert state.selected is not None
    assert state.selected.origin == "local"
    assert state.selected.resume_command == "copilot --resume=lin-abc"

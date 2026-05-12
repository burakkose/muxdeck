"""Tests for SessionsController — view model building from local sessions."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from muxdeck.adapters.copilot_session_store import (
    CopilotLocalSession,
    CopilotSessionUsage,
)
from muxdeck.controllers.sessions_controller import (
    SessionsController,
    _format_usage_count,
    _relative_time,
    _session_status,
    _status_glyph,
    _summary_for,
    _usage_view_for,
)

# ── helpers ─────────────────────────────────────────────────────


def _session(
    session_id: str = "test-id",
    *,
    repository: str = "user/repo",
    branch: str = "main",
    name: str | None = None,
    summary: str = "Test",
    is_cleanly_closed: bool = False,
    updated_at: datetime | None = None,
    checkpoint_count: int = 0,
    last_event_type: str | None = None,
    usage: CopilotSessionUsage | None = None,
) -> CopilotLocalSession:
    now = datetime.now(UTC)
    return CopilotLocalSession(
        session_id=session_id,
        cwd=Path("/home/user/test"),
        git_root=Path("/home/user/test"),
        repository=repository,
        branch=branch,
        name=name,
        summary=summary,
        created_at=now - timedelta(hours=2),
        updated_at=updated_at or now,
        last_event_type=last_event_type,
        last_event_at=now,
        checkpoint_count=checkpoint_count,
        usage=usage,
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
    assert state.selected_session_id == "s1"
    assert state.selected is not None
    assert state.selected.session_id == "s1"


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


def test_controller_filter_text_searches_name_field() -> None:
    """Filter haystack must include the canonical ``name`` title.

    Newer Copilot CLI sessions only carry ``name`` (no
    ``summary``); searching by the visible title would otherwise
    miss them entirely even though that title is what the operator
    sees in the table.
    """

    sessions = [
        _session("s1", name="Build Configuration Subscriber", summary="", repository="x/y"),
        _session("s2", name="Refactor Storage", summary="", repository="x/z"),
    ]
    ctrl = SessionsController(FakeSessionStore(sessions))  # type: ignore[arg-type]
    state = ctrl.build_state(filter_text="configuration")
    assert [item.session_id for item in state.sessions] == ["s1"]


def test_controller_selected_detail() -> None:
    sessions = [_session("s1", summary="Selected", checkpoint_count=5)]
    ctrl = SessionsController(FakeSessionStore(sessions))  # type: ignore[arg-type]
    state = ctrl.build_state(selected_session_id="s1")
    assert state.selected is not None
    assert state.selected.session_id == "s1"
    assert state.selected.checkpoint_count == 5
    assert state.selected.resume_command == "copilot --resume=s1"
    assert state.selected.usage_summary == "pending (recorded on clean shutdown)"
    assert state.selected.usage_badge == "pending"
    assert state.selected.usage_available is False


def test_controller_selected_detail_surfaces_usage() -> None:
    sessions = [
        _session(
            "s1",
            summary="Selected",
            checkpoint_count=5,
            is_cleanly_closed=True,
            usage=CopilotSessionUsage(
                input_tokens=1200,
                output_tokens=345,
                cache_read_tokens=1000,
                premium_requests=4,
            ),
        )
    ]
    ctrl = SessionsController(FakeSessionStore(sessions))  # type: ignore[arg-type]
    state = ctrl.build_state(selected_session_id="s1")

    assert state.selected is not None
    assert state.selected.usage_summary == "1,200 in · 345 out · 1,000 cached · 2,545 total"
    assert state.selected.usage_badge == "2.5k tok"
    assert state.selected.usage_available is True
    assert state.selected.premium_requests == "4 req"


def test_controller_selected_not_found() -> None:
    ctrl = SessionsController(FakeSessionStore([]))  # type: ignore[arg-type]
    state = ctrl.build_state(selected_session_id="nonexistent")
    assert state.selected is None


def test_controller_falls_back_to_first_visible_selection() -> None:
    sessions = [
        _session("s1", summary="Selected"),
        _session("s2", summary="Other"),
    ]
    ctrl = SessionsController(FakeSessionStore(sessions))  # type: ignore[arg-type]
    state = ctrl.build_state(selected_session_id="missing")
    assert state.selected_session_id == "s1"
    assert state.selected is not None
    assert state.selected.session_id == "s1"


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


# ── _summary_for edge cases ────────────────────────────────────────


def test_summary_for_with_all_fallbacks() -> None:
    """_summary_for falls back to cwd when no summary/repo/branch."""

    s = CopilotLocalSession(
        session_id="test",
        cwd=Path("/home/user/myproject"),
        git_root=Path("/home/user/myproject"),
        repository="",
        branch="",
        summary="",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    result = _summary_for(s)
    assert result == "myproject"


def test_summary_for_prefers_name_over_summary() -> None:
    """``name`` (Copilot CLI's canonical session title) wins over ``summary``.

    Newer Copilot CLI sessions write the session title to the
    ``name`` field; some sessions still carry an older ``summary``
    alongside it. Operators reported sessions appearing as nameless
    rows because the controller only consulted ``summary``. The
    canonical ``name`` must take precedence so newer sessions and
    explicitly user-named sessions surface their real title.
    """

    s = CopilotLocalSession(
        session_id="test",
        cwd=Path("/home/user/myproject"),
        git_root=Path("/home/user/myproject"),
        repository="user/repo",
        branch="main",
        name="Build Configuration Subscriber",
        summary="legacy autosummary text",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    result = _summary_for(s)
    assert result == "Build Configuration Subscriber"


def test_summary_for_falls_back_to_summary_without_name() -> None:
    """Older sessions without a ``name`` still surface ``summary``.

    Pre-name-field sessions on disk only carry ``summary``. The
    controller must keep treating that as a valid display label so
    historical rows do not regress to the cwd/session-id fallbacks.
    """

    s = CopilotLocalSession(
        session_id="test",
        cwd=Path("/home/user/myproject"),
        git_root=Path("/home/user/myproject"),
        repository="user/repo",
        branch="main",
        name=None,
        summary="legacy summary text",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    result = _summary_for(s)
    assert result == "legacy summary text"


def test_summary_for_prefers_explicit_summary() -> None:
    """_summary_for prefers explicit summary over all fallbacks."""

    s = CopilotLocalSession(
        session_id="test",
        cwd=Path("/home/user/myproject"),
        git_root=Path("/home/user/myproject"),
        repository="user/repo",
        branch="main",
        summary="My Custom Task",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    result = _summary_for(s)
    assert result == "My Custom Task"


def test_summary_for_uses_repo_and_branch() -> None:
    """_summary_for uses repo + branch when summary is empty."""

    s = CopilotLocalSession(
        session_id="test",
        cwd=Path("/home/user/myproject"),
        git_root=Path("/home/user/myproject"),
        repository="user/repo",
        branch="feature/auth",
        summary="",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    result = _summary_for(s)
    assert result == "user/repo · feature/auth"


def test_summary_for_uses_repo_alone() -> None:
    """_summary_for uses repo alone when branch is empty."""

    s = CopilotLocalSession(
        session_id="test",
        cwd=Path("/home/user/myproject"),
        git_root=Path("/home/user/myproject"),
        repository="user/repo",
        branch="",
        summary="",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    result = _summary_for(s)
    assert result == "user/repo"


def test_summary_for_fallback_to_session_id_when_cwd_missing() -> None:
    """_summary_for uses session id short form when cwd is None."""

    s = CopilotLocalSession(
        session_id="abc1234567890",
        cwd=None,
        git_root=Path("/home/user"),
        repository="",
        branch="",
        summary="",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    result = _summary_for(s)
    # session_id[:8] = "abc12345"
    assert result == "session abc12345"


# ── _format_usage_count ────────────────────────────────────────────


def test_format_usage_count_millions() -> None:
    assert _format_usage_count(1_000_000) == "1.0M"
    assert _format_usage_count(2_500_000) == "2.5M"


def test_format_usage_count_thousands() -> None:
    assert _format_usage_count(1000) == "1.0k"
    assert _format_usage_count(5500) == "5.5k"


def test_format_usage_count_plain() -> None:
    assert _format_usage_count(100) == "100"
    assert _format_usage_count(999) == "999"


# ── _usage_view_for ───────────────────────────────────────────────


def test_usage_view_for_with_no_usage() -> None:
    """_usage_view_for returns appropriate text when usage is None."""

    s = _session("test", usage=None, is_cleanly_closed=False)
    summary, badge, available, premium = _usage_view_for(s)

    assert summary == "pending (recorded on clean shutdown)"
    assert badge == "pending"
    assert available is False
    assert premium is None


def test_usage_view_for_unclosed_with_no_usage() -> None:
    """_usage_view_for for unclosed session with no usage data."""

    s = _session("test", usage=None, is_cleanly_closed=True)
    summary, badge, available, premium = _usage_view_for(s)

    assert summary == "not recorded in session state"
    assert badge == "n/a"
    assert available is False


def test_usage_view_for_with_full_usage_data() -> None:
    """_usage_view_for formats full usage including cache tokens."""

    usage = CopilotSessionUsage(
        input_tokens=1000,
        output_tokens=500,
        cache_read_tokens=200,
        cache_write_tokens=100,
        premium_requests=3,
    )
    s = _session("test", usage=usage, is_cleanly_closed=True)
    summary, badge, available, premium = _usage_view_for(s)

    assert "1,000 in" in summary
    assert "500 out" in summary
    assert "300 cached" in summary
    assert "1,800 total" in summary
    assert available is True
    assert premium == "3 req"


def test_usage_view_for_with_only_output_tokens() -> None:
    """_usage_view_for handles partial usage data."""

    usage = CopilotSessionUsage(
        output_tokens=250,
    )
    s = _session("test", usage=usage, is_cleanly_closed=True)
    summary, badge, available, premium = _usage_view_for(s)

    assert "250 out" in summary
    assert available is True

"""Controller for the Sessions browser screen."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from copilot_commander.adapters.copilot_session_store import (
        CopilotLocalSession,
        CopilotSessionStore,
    )


def _relative_time(dt: datetime | None) -> str:
    if dt is None:
        return "—"
    now = datetime.now(UTC)
    delta = now - dt
    seconds = int(delta.total_seconds())
    if seconds < 0:
        return "just now"
    if seconds < 60:
        return f"{seconds}s ago"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h ago"
    days = hours // 24
    if days < 30:
        return f"{days}d ago"
    return dt.strftime("%Y-%m-%d")


def _session_status(session: CopilotLocalSession, live_session_ids: frozenset[str]) -> str:
    """Determine display status for a session."""
    if session.session_id in live_session_ids:
        return "active"
    if not session.is_cleanly_closed:
        return "unclosed"
    return "completed"


def _summary_for(session: CopilotLocalSession) -> str:
    """Pick a human-useful summary for a local Copilot session row.

    Copilot CLI does not always write a ``summary`` into ``workspace.yaml``,
    so the old "(no summary)" placeholder dominated the list and made it
    impossible to tell rows apart. Fall back to cheaper signals already
    parsed from disk: the repository slug, the cwd tail, and finally a
    short form of the session id so every row still renders distinctly.
    """
    if session.summary:
        return session.summary
    if session.repository and session.branch:
        return f"{session.repository} · {session.branch}"
    if session.repository:
        return session.repository
    if session.cwd is not None:
        tail = session.cwd.name or str(session.cwd)
        if tail:
            return tail
    return f"session {session.session_id[:8]}"


def _resume_command_for(session: CopilotLocalSession) -> str:
    """Shape of the resume invocation shown in the UI.

    Windows-side sessions (started from ``pwsh`` on the Windows host)
    cannot be resumed from the WSL shell directly, because the Copilot
    CLI that owns them lives on the Windows ``PATH``. Surface an
    explicit pwsh wrapper so copying the command from the UI still
    works when someone pastes it into any tmux pane.
    """
    if session.origin == "windows":
        if session.windows_cwd:
            escaped = session.windows_cwd.replace("'", "''")
            return (
                f'pwsh.exe -NoExit -Command "Set-Location -LiteralPath '
                f"'{escaped}'; copilot --resume={session.session_id}\""
            )
        return f'pwsh.exe -NoExit -Command "copilot --resume={session.session_id}"'
    return f"copilot --resume={session.session_id}"


def _status_glyph(status: str) -> str:
    return {
        "active": "🟢",
        "unclosed": "🔴",
        "completed": "⚪",
    }.get(status, "⚫")


@dataclass(frozen=True, slots=True)
class SessionListItemView:
    """View model for a session in the list."""

    session_id: str
    summary: str
    repository: str
    branch: str
    status: str
    status_glyph: str
    updated: str
    created: str
    checkpoint_count: int
    last_event_type: str
    cwd: str
    is_resumable: bool
    origin: str = "local"


@dataclass(frozen=True, slots=True)
class SessionDetailView:
    """View model for the selected session detail panel."""

    session_id: str
    summary: str
    repository: str
    branch: str
    cwd: str
    git_root: str
    status: str
    status_glyph: str
    created_at: str
    updated_at: str
    last_event_type: str
    last_event_at: str
    checkpoint_count: int
    is_resumable: bool
    resume_command: str
    origin: str = "local"
    windows_cwd: str | None = None


@dataclass(frozen=True, slots=True)
class SessionsState:
    """Complete view model for the sessions screen."""

    sessions: tuple[SessionListItemView, ...]
    selected: SessionDetailView | None
    total_count: int
    active_count: int
    unclosed_count: int
    completed_count: int


class SessionsController:
    """Builds view models from CopilotSessionStore data."""

    def __init__(self, session_store: CopilotSessionStore) -> None:
        self._store = session_store

    def build_state(
        self,
        *,
        live_session_ids: frozenset[str] = frozenset(),
        selected_session_id: str | None = None,
        filter_text: str = "",
        show_completed: bool = True,
    ) -> SessionsState:
        raw_sessions = self._store.discover()

        items: list[SessionListItemView] = []
        active_count = 0
        unclosed_count = 0
        completed_count = 0

        for s in raw_sessions:
            status = _session_status(s, live_session_ids)
            if status == "active":
                active_count += 1
            elif status == "unclosed":
                unclosed_count += 1
            else:
                completed_count += 1

            if not show_completed and status == "completed":
                continue

            # Text filter
            if filter_text:
                needle = filter_text.lower()
                haystack = " ".join(
                    str(v) for v in (s.summary, s.repository, s.branch, s.session_id) if v
                ).lower()
                if needle not in haystack:
                    continue

            items.append(
                SessionListItemView(
                    session_id=s.session_id,
                    summary=_summary_for(s),
                    repository=s.repository or "—",
                    branch=s.branch or "—",
                    status=status,
                    status_glyph=_status_glyph(status),
                    updated=_relative_time(s.updated_at),
                    created=_relative_time(s.created_at),
                    checkpoint_count=s.checkpoint_count,
                    last_event_type=s.last_event_type or "—",
                    cwd=str(s.cwd) if s.cwd else "—",
                    is_resumable=not s.is_cleanly_closed or s.session_id in live_session_ids,
                    origin=s.origin,
                )
            )

        # Build selected detail
        selected: SessionDetailView | None = None
        if selected_session_id:
            raw = self._store.get_session(selected_session_id)
            if raw is not None:
                status = _session_status(raw, live_session_ids)
                selected = SessionDetailView(
                    session_id=raw.session_id,
                    summary=_summary_for(raw),
                    repository=raw.repository or "—",
                    branch=raw.branch or "—",
                    cwd=(raw.windows_cwd or (str(raw.cwd) if raw.cwd else "—")),
                    git_root=(raw.windows_git_root or (str(raw.git_root) if raw.git_root else "—")),
                    status=status,
                    status_glyph=_status_glyph(status),
                    created_at=_relative_time(raw.created_at),
                    updated_at=_relative_time(raw.updated_at),
                    last_event_type=raw.last_event_type or "—",
                    last_event_at=_relative_time(raw.last_event_at),
                    checkpoint_count=raw.checkpoint_count,
                    is_resumable=not raw.is_cleanly_closed or raw.session_id in live_session_ids,
                    resume_command=_resume_command_for(raw),
                    origin=raw.origin,
                    windows_cwd=raw.windows_cwd,
                )

        return SessionsState(
            sessions=tuple(items),
            selected=selected,
            total_count=len(raw_sessions),
            active_count=active_count,
            unclosed_count=unclosed_count,
            completed_count=completed_count,
        )

    def get_session_detail(
        self,
        session_id: str | None,
        *,
        live_session_ids: frozenset[str] = frozenset(),
    ) -> SessionDetailView | None:
        """Build the detail view for a single session by ID.

        Called from the UI thread on every j/k cursor move, so it must
        never trigger filesystem work. The store lookup uses
        ``warm_only=True``: if the id is not in the in-memory index
        we return None (the previous detail stays on-screen) rather
        than blocking on a full rescan.
        """
        if session_id is None:
            return None
        raw = self._store.get_session(session_id, warm_only=True)
        if raw is None:
            return None
        status = _session_status(raw, live_session_ids)
        return SessionDetailView(
            session_id=raw.session_id,
            summary=_summary_for(raw),
            repository=raw.repository or "—",
            branch=raw.branch or "—",
            cwd=(raw.windows_cwd or (str(raw.cwd) if raw.cwd else "—")),
            git_root=(raw.windows_git_root or (str(raw.git_root) if raw.git_root else "—")),
            status=status,
            status_glyph=_status_glyph(status),
            created_at=_relative_time(raw.created_at),
            updated_at=_relative_time(raw.updated_at),
            last_event_type=raw.last_event_type or "—",
            last_event_at=_relative_time(raw.last_event_at),
            checkpoint_count=raw.checkpoint_count,
            is_resumable=not raw.is_cleanly_closed or raw.session_id in live_session_ids,
            resume_command=_resume_command_for(raw),
            origin=raw.origin,
            windows_cwd=raw.windows_cwd,
        )


__all__ = [
    "SessionDetailView",
    "SessionListItemView",
    "SessionsController",
    "SessionsState",
]

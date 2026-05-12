"""Controller for the Sessions browser screen."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from muxdeck.adapters.copilot_session_store import (
        CopilotLocalSession,
        CopilotSessionStore,
        CopilotSessionUsage,
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
    """Pick a human-useful label for a local Copilot session row.

    Copilot CLI populates two related fields in ``workspace.yaml``:

    * ``name`` -- the canonical session title surfaced by the CLI
      itself (set explicitly via ``/name`` or auto-generated). Newer
      sessions only carry this field.
    * ``summary`` -- a legacy field present on older sessions; when
      both fields exist they are typically identical.

    Prefer ``name`` so newer sessions stop showing as nameless rows.
    Fall back to ``summary`` for historical sessions, then to cheaper
    signals already parsed from disk so every row still renders
    distinctly even when neither label is present.
    """
    if session.name:
        return session.name
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


def _format_usage_count(value: int) -> str:
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    if value >= 1_000:
        return f"{value / 1_000:.1f}k"
    return str(value)


def _premium_requests_text(usage: CopilotSessionUsage | None) -> str | None:
    if usage is None or usage.premium_requests is None:
        return None
    return f"{usage.premium_requests} req"


def _usage_view_for(session: CopilotLocalSession) -> tuple[str, str, bool, str | None]:
    usage = session.usage
    premium_requests = _premium_requests_text(usage)
    if usage is None or usage.total_tokens is None:
        if not session.is_cleanly_closed:
            return ("pending (recorded on clean shutdown)", "pending", False, premium_requests)
        return ("not recorded in session state", "n/a", False, premium_requests)

    cache_tokens = (usage.cache_read_tokens or 0) + (usage.cache_write_tokens or 0)
    parts: list[str] = []
    if usage.input_tokens is not None:
        parts.append(f"{usage.input_tokens:,} in")
    if usage.output_tokens is not None:
        parts.append(f"{usage.output_tokens:,} out")
    if cache_tokens > 0:
        parts.append(f"{cache_tokens:,} cached")
    total_tokens = usage.total_tokens
    parts.append(f"{total_tokens:,} total")
    return (
        " · ".join(parts),
        f"{_format_usage_count(total_tokens)} tok",
        True,
        premium_requests,
    )


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
    usage_summary: str = "not recorded in session state"
    usage_badge: str = "n/a"
    usage_available: bool = False
    premium_requests: str | None = None


@dataclass(frozen=True, slots=True)
class SessionsState:
    """Complete view model for the sessions screen."""

    sessions: tuple[SessionListItemView, ...]
    selected: SessionDetailView | None
    total_count: int
    active_count: int
    unclosed_count: int
    completed_count: int
    selected_session_id: str | None = None


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
                    str(v) for v in (s.name, s.summary, s.repository, s.branch, s.session_id) if v
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

        visible_session_ids = {item.session_id for item in items}
        resolved_selected_session_id = selected_session_id
        if resolved_selected_session_id not in visible_session_ids:
            resolved_selected_session_id = items[0].session_id if items else None

        # Build selected detail
        selected: SessionDetailView | None = None
        if resolved_selected_session_id:
            raw = self._store.get_session(resolved_selected_session_id)
            if raw is not None:
                status = _session_status(raw, live_session_ids)
                (
                    usage_summary,
                    usage_badge,
                    usage_available,
                    premium_requests,
                ) = _usage_view_for(raw)
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
                    usage_summary=usage_summary,
                    usage_badge=usage_badge,
                    usage_available=usage_available,
                    premium_requests=premium_requests,
                )

        return SessionsState(
            sessions=tuple(items),
            selected=selected,
            total_count=len(raw_sessions),
            active_count=active_count,
            unclosed_count=unclosed_count,
            completed_count=completed_count,
            selected_session_id=resolved_selected_session_id,
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
        usage_summary, usage_badge, usage_available, premium_requests = _usage_view_for(raw)
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
            usage_summary=usage_summary,
            usage_badge=usage_badge,
            usage_available=usage_available,
            premium_requests=premium_requests,
        )


__all__ = [
    "SessionDetailView",
    "SessionListItemView",
    "SessionsController",
    "SessionsState",
]

from __future__ import annotations

from typing import Protocol

from muxdeck.controllers.sessions_controller import SessionDetailView
from muxdeck.widgets.sessions import SessionActionBar, SessionDetailPanel


class _WidgetWithRender(Protocol):
    def render(self) -> object: ...


def _render(widget: _WidgetWithRender) -> str:
    renderable = widget.render()
    plain = getattr(renderable, "plain", None)
    return plain if isinstance(plain, str) else str(renderable)


def test_detail_panel_renders_usage_and_premium_requests() -> None:
    panel = SessionDetailPanel()
    panel.set_detail(
        SessionDetailView(
            session_id="session-1",
            summary="Review logs",
            repository="repo",
            branch="task/reviewer",
            cwd="/repo/reviewer",
            git_root="/repo",
            status="completed",
            status_glyph="⚪",
            created_at="20m ago",
            updated_at="2m ago",
            last_event_type="session.shutdown",
            last_event_at="2m ago",
            checkpoint_count=1,
            is_resumable=False,
            resume_command="copilot --resume=session-1",
            usage_summary="1,000 in · 250 out · 100 cached · 1,350 total",
            usage_badge="1.4k tok",
            usage_available=True,
            premium_requests="2 req",
        )
    )

    rendered = _render(panel).lower()
    assert "usage" in rendered
    assert "1,350 total" in rendered
    assert "premium" in rendered
    assert "2 req" in rendered


def test_action_bar_surfaces_pending_usage_for_open_sessions() -> None:
    bar = SessionActionBar()
    bar.set_state(
        SessionDetailView(
            session_id="session-1",
            summary="Review logs",
            repository="repo",
            branch="task/reviewer",
            cwd="/repo/reviewer",
            git_root="/repo",
            status="active",
            status_glyph="🟢",
            created_at="20m ago",
            updated_at="2m ago",
            last_event_type="agent.updated",
            last_event_at="2m ago",
            checkpoint_count=1,
            is_resumable=True,
            resume_command="copilot --resume=session-1",
            usage_summary="pending (recorded on clean shutdown)",
            usage_badge="pending",
            usage_available=False,
        ),
        has_live_pane=False,
        filter_text="",
        show_completed=True,
    )

    rendered = _render(bar).lower()
    assert "usage pending" in rendered
    assert "resume" in rendered

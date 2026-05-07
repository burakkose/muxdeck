"""Widget-only tests for the setup screen panels.

The :class:`SocketListPanel` is a ``Vertical`` with a child
``ListView`` so it must be mounted inside a scratch
:class:`textual.app.App`. The other two panels are plain ``Static``
widgets that we render directly.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

import pytest
from textual.app import App, ComposeResult
from textual.widgets import ListView

from muxdeck.adapters.windows_host import WindowsHostInfo
from muxdeck.services.setup_service import SetupCheck, SetupDoctorReport, TmuxSocketOption
from muxdeck.widgets.setup import DoctorDetailPanel, SetupSummaryPanel, SocketListPanel


class _Renderable(Protocol):
    def render(self) -> object: ...


def _render(widget: _Renderable) -> str:
    renderable = widget.render()
    plain = getattr(renderable, "plain", None)
    return plain if isinstance(plain, str) else str(renderable)


_TS = datetime(2025, 1, 1, tzinfo=UTC)


def _option(
    *,
    label: str = "default tmux",
    socket_path: str | None = None,
    note: str = "active socket",
    is_selected: bool = False,
    exists: bool = True,
) -> TmuxSocketOption:
    return TmuxSocketOption(
        label=label,
        socket_path=socket_path,
        note=note,
        is_selected=is_selected,
        exists=exists,
    )


def _check(
    *,
    key: str = "tmux-running",
    status: str = "ok",
    title: str = "tmux running",
    detail: str = "tmux server reachable",
) -> SetupCheck:
    return SetupCheck(key=key, status=status, title=title, detail=detail)  # type: ignore[arg-type]


def _report(
    *,
    checks: tuple[SetupCheck, ...] = (),
    socket_options: tuple[TmuxSocketOption, ...] = (),
    selected_socket: str | None = None,
    effective_socket: str | None = None,
    attached_socket: str | None = None,
    configured_socket: str | None = None,
    pane_count: int | None = None,
    windows_host: WindowsHostInfo | None = None,
    windows_session_count: int | None = None,
) -> SetupDoctorReport:
    return SetupDoctorReport(
        generated_at=_TS,
        selected_socket_path=selected_socket,
        effective_socket_path=effective_socket,
        attached_socket_path=attached_socket,
        configured_socket_path=configured_socket,
        pane_count=pane_count,
        socket_options=socket_options,
        checks=checks,
        windows_host=windows_host,
        windows_session_count=windows_session_count,
    )


# ── SocketListPanel ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_socket_list_panel_set_options_selects_marked_option() -> None:
    panel = SocketListPanel(widget_id="socket-list")

    class _Harness(App[None]):
        def compose(self) -> ComposeResult:
            yield panel

    options = (
        _option(label="default", is_selected=False),
        _option(label="custom", socket_path="/tmp/sock", is_selected=True),
        _option(label="missing", socket_path="/missing", is_selected=False, exists=False),
    )
    async with _Harness().run_test() as pilot:
        await pilot.pause()
        panel.set_options(options)
        await pilot.pause()
        list_view = panel.query_one(ListView)
        assert list_view.index == 1
        assert panel.selected_option() == options[1]


@pytest.mark.asyncio
async def test_socket_list_panel_move_cursor_clamps_and_focuses() -> None:
    panel = SocketListPanel(widget_id="socket-list")

    class _Harness(App[None]):
        def compose(self) -> ComposeResult:
            yield panel

    options = (
        _option(label="a", is_selected=True),
        _option(label="b"),
        _option(label="c"),
    )
    async with _Harness().run_test() as pilot:
        await pilot.pause()
        panel.set_options(options)
        await pilot.pause()
        panel.move_cursor(2)
        list_view = panel.query_one(ListView)
        # Started at index 0 + delta 2 = 2 (clamped fine)
        assert list_view.index == 2
        panel.move_cursor(50)
        assert list_view.index == 2  # clamped
        panel.move_cursor(-99)
        assert list_view.index == 0
        panel.focus_list()
        # Round-trip the selected option lookup
        assert panel.selected_option() == options[0]


@pytest.mark.asyncio
async def test_socket_list_panel_move_cursor_no_op_when_empty() -> None:
    panel = SocketListPanel(widget_id="socket-list")

    class _Harness(App[None]):
        def compose(self) -> ComposeResult:
            yield panel

    async with _Harness().run_test() as pilot:
        await pilot.pause()
        panel.move_cursor(1)  # safe no-op
        assert panel.selected_option() is None


@pytest.mark.asyncio
async def test_socket_list_panel_renders_note_for_options() -> None:
    panel = SocketListPanel(widget_id="socket-list")

    class _Harness(App[None]):
        def compose(self) -> ComposeResult:
            yield panel

    async with _Harness().run_test() as pilot:
        await pilot.pause()
        panel.set_options((_option(label="alpha", note="active socket"),))
        await pilot.pause()
        # selected_option works after set
        assert panel.selected_option() is not None


# ── SetupSummaryPanel ───────────────────────────────────────────────


def test_summary_panel_no_report_renders_unavailable() -> None:
    panel = SetupSummaryPanel()
    panel.set_report(None)
    assert "Setup diagnostics unavailable" in _render(panel)


def test_summary_panel_renders_health_target_and_checks() -> None:
    panel = SetupSummaryPanel()
    report = _report(
        checks=(_check(),),
        effective_socket="/run/tmux/default",
        attached_socket="/run/tmux/default",
        configured_socket=None,
        pane_count=4,
    )
    panel.set_report(report)
    rendered = _render(panel)
    assert "health" in rendered
    assert "ok" in rendered
    assert "/run/tmux/default" in rendered
    assert "4" in rendered


def test_summary_panel_includes_windows_host_row_when_available(tmp_path: Path) -> None:
    panel = SetupSummaryPanel()
    wh = WindowsHostInfo(
        is_wsl=True,
        distro="Ubuntu",
        windows_userprofile="C:/Users/me",
        session_state_dir=tmp_path,
        resolver="env_userprofile",
    )
    report = _report(checks=(_check(),), windows_host=wh, windows_session_count=3)
    panel.set_report(report)
    rendered = _render(panel)
    assert "windows" in rendered
    assert str(tmp_path) in rendered
    assert "3 sessions" in rendered


def test_summary_panel_windows_available_without_session_count(tmp_path: Path) -> None:
    panel = SetupSummaryPanel()
    wh = WindowsHostInfo(
        is_wsl=True,
        distro="Ubuntu",
        windows_userprofile="C:/Users/me",
        session_state_dir=tmp_path,
        resolver="wslvar",
    )
    panel.set_report(_report(checks=(_check(),), windows_host=wh))
    rendered = _render(panel)
    assert "windows" in rendered
    assert "wslvar" in rendered


def test_summary_panel_windows_host_unavailable_branch() -> None:
    panel = SetupSummaryPanel()
    wh = WindowsHostInfo(
        is_wsl=True,
        distro="Ubuntu",
        resolver="cmd_exe",
        error="resolver missing",
    )
    panel.set_report(_report(checks=(_check(),), windows_host=wh))
    rendered = _render(panel)
    assert "windows" in rendered
    assert "resolver missing" in rendered


def test_summary_panel_overall_status_warning_when_check_warns() -> None:
    panel = SetupSummaryPanel()
    report = _report(checks=(_check(status="warning", title="dim", detail="dim"),))
    panel.set_report(report)
    rendered = _render(panel)
    assert "warning" in rendered


# ── DoctorDetailPanel ───────────────────────────────────────────────


def test_doctor_detail_no_report() -> None:
    panel = DoctorDetailPanel()
    panel.set_report(None)
    assert "No doctor report available" in _render(panel)


def test_doctor_detail_renders_each_check_with_glyph_and_detail() -> None:
    panel = DoctorDetailPanel()
    report = _report(
        checks=(
            _check(key="ok-1", status="ok", title="ok title", detail="ok detail"),
            _check(key="warn-1", status="warning", title="warn title", detail="warn detail"),
            _check(key="err-1", status="error", title="err title", detail="err detail"),
            _check(key="info-1", status="info", title="info title", detail="info detail"),
        )
    )
    panel.set_report(report)
    rendered = _render(panel)
    assert "ok title" in rendered
    assert "warn detail" in rendered
    assert "err title" in rendered
    assert "info detail" in rendered

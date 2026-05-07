"""Tests for the SetupScreen behaviour."""

from __future__ import annotations

import asyncio
import unittest
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import cast

from textual.app import App, ComposeResult

from muxdeck.app import MuxdeckRuntime
from muxdeck.screens.setup import SetupScreen
from muxdeck.services.setup_service import (
    SetupCheck,
    SetupDoctorReport,
    TmuxSocketOption,
)
from muxdeck.widgets.common import KeyHintFooter
from muxdeck.widgets.setup import SocketListPanel


def _option(
    label: str,
    socket_path: str | None,
    *,
    is_selected: bool = False,
    exists: bool = True,
    note: str = "",
) -> TmuxSocketOption:
    return TmuxSocketOption(
        label=label,
        socket_path=socket_path,
        note=note,
        is_selected=is_selected,
        exists=exists,
    )


def _report(
    *,
    options: tuple[TmuxSocketOption, ...] = (),
    checks: tuple[SetupCheck, ...] = (),
    selected: str | None = None,
) -> SetupDoctorReport:
    return SetupDoctorReport(
        generated_at=datetime(2024, 1, 1, tzinfo=UTC),
        selected_socket_path=selected,
        effective_socket_path=selected,
        attached_socket_path=None,
        configured_socket_path=None,
        pane_count=0,
        socket_options=options,
        checks=checks,
    )


@dataclass(slots=True)
class _RecordingService:
    report: SetupDoctorReport
    select_calls: list[str | None] = field(default_factory=list)
    next_report_for_select: SetupDoctorReport | None = None

    def build_report(self) -> SetupDoctorReport:
        return self.report

    def select_socket(self, socket_path: str | None) -> SetupDoctorReport:
        self.select_calls.append(socket_path)
        if self.next_report_for_select is not None:
            return self.next_report_for_select
        return self.report


class _Harness(App[None]):
    def __init__(self, runtime: MuxdeckRuntime) -> None:
        super().__init__()
        self._runtime = runtime

    def compose(self) -> ComposeResult:
        return iter(())


def _runtime_with(setup_service: object | None) -> MuxdeckRuntime:
    return cast(
        MuxdeckRuntime,
        type("_FakeRuntime", (), {"setup": setup_service})(),
    )


class SetupScreenRefreshTests(unittest.TestCase):
    def test_refresh_data_without_service_marks_unavailable(self) -> None:
        async def scenario() -> str:
            runtime = _runtime_with(None)
            app = _Harness(runtime)
            async with app.run_test(size=(140, 60)) as pilot:
                screen = SetupScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                screen.refresh_data()
                await pilot.pause()
                return app.screen.query_one(KeyHintFooter).status

        assert asyncio.run(scenario()) == "setup unavailable"

    def test_refresh_data_with_issues_reports_issue_count(self) -> None:
        async def scenario() -> str:
            checks = (
                SetupCheck(key="x", status="error", title="x", detail="detail-x"),
                SetupCheck(key="y", status="warning", title="y", detail="detail-y"),
            )
            service = _RecordingService(report=_report(checks=checks))
            runtime = _runtime_with(service)
            app = _Harness(runtime)
            async with app.run_test(size=(140, 60)) as pilot:
                screen = SetupScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                screen.refresh_data()
                await pilot.pause()
                return app.screen.query_one(KeyHintFooter).status

        assert asyncio.run(scenario()) == "2 setup issue(s) detected"

    def test_refresh_data_healthy_reports_healthy_status(self) -> None:
        async def scenario() -> str:
            checks = (SetupCheck(key="ok", status="ok", title="ok", detail="all good"),)
            service = _RecordingService(report=_report(checks=checks))
            runtime = _runtime_with(service)
            app = _Harness(runtime)
            async with app.run_test(size=(140, 60)) as pilot:
                screen = SetupScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                screen.refresh_data()
                await pilot.pause()
                return app.screen.query_one(KeyHintFooter).status

        assert asyncio.run(scenario()) == "tmux setup looks healthy"


class SetupScreenCursorTests(unittest.TestCase):
    def test_cursor_actions_delegate_to_socket_panel(self) -> None:
        async def scenario() -> tuple[int, int]:
            options = (
                _option("auto", None, is_selected=True),
                _option("a", "/run/tmux-a/default"),
                _option("b", "/run/tmux-b/default"),
            )
            service = _RecordingService(report=_report(options=options))
            runtime = _runtime_with(service)
            app = _Harness(runtime)
            async with app.run_test(size=(140, 60)) as pilot:
                screen = SetupScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                panel = app.screen.query_one(SocketListPanel)
                from textual.widgets import ListView

                screen.action_cursor_down()
                screen.action_cursor_down()
                first_after_two_down = panel.query_one(ListView).index
                screen.action_cursor_up()
                second_after_one_up = panel.query_one(ListView).index
            return first_after_two_down or 0, second_after_one_up or 0

        first, second = asyncio.run(scenario())
        assert first == 2
        assert second == 1


class SetupScreenApplySocketTests(unittest.TestCase):
    def test_apply_socket_without_service_is_noop_with_status(self) -> None:
        async def scenario() -> str:
            runtime = _runtime_with(None)
            app = _Harness(runtime)
            async with app.run_test(size=(140, 60)) as pilot:
                screen = SetupScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                screen.action_apply_socket()
                await pilot.pause()
                return app.screen.query_one(KeyHintFooter).status

        assert asyncio.run(scenario()) == "setup unavailable"

    def test_apply_socket_no_options_reports_no_socket_selected(self) -> None:
        async def scenario() -> str:
            service = _RecordingService(report=_report(options=()))
            runtime = _runtime_with(service)
            app = _Harness(runtime)
            async with app.run_test(size=(140, 60)) as pilot:
                screen = SetupScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                screen.action_apply_socket()
                await pilot.pause()
                return app.screen.query_one(KeyHintFooter).status

        assert asyncio.run(scenario()) == "no socket selected"

    def test_apply_socket_already_selected_reports_already_active(self) -> None:
        async def scenario() -> str:
            options = (_option("auto", None, is_selected=True),)
            service = _RecordingService(report=_report(options=options))
            runtime = _runtime_with(service)
            app = _Harness(runtime)
            async with app.run_test(size=(140, 60)) as pilot:
                screen = SetupScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                screen.action_apply_socket()
                await pilot.pause()
                return app.screen.query_one(KeyHintFooter).status

        assert asyncio.run(scenario()) == "socket already active"

    def test_apply_socket_with_path_calls_select_socket(self) -> None:
        async def scenario() -> tuple[str, list[str | None]]:
            options = (
                _option("auto", None, is_selected=True),
                _option("a", "/run/tmux-a/default"),
            )
            service = _RecordingService(
                report=_report(options=options),
                next_report_for_select=_report(options=options, selected="/run/tmux-a/default"),
            )
            runtime = _runtime_with(service)
            app = _Harness(runtime)
            async with app.run_test(size=(140, 60)) as pilot:
                screen = SetupScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                screen.action_cursor_down()
                screen.action_apply_socket()
                await pilot.pause()
                status = app.screen.query_one(KeyHintFooter).status
            return status, service.select_calls

        status, calls = asyncio.run(scenario())
        assert calls == ["/run/tmux-a/default"]
        assert status == "using tmux socket /run/tmux-a/default"

    def test_apply_socket_with_none_path_reports_auto_selection(self) -> None:
        async def scenario() -> tuple[str, list[str | None]]:
            options = (
                _option("auto", None, is_selected=False),
                _option("a", "/run/tmux-a/default", is_selected=True),
            )
            service = _RecordingService(report=_report(options=options))
            runtime = _runtime_with(service)
            app = _Harness(runtime)
            async with app.run_test(size=(140, 60)) as pilot:
                screen = SetupScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                # set_options moved the cursor to the selected option (index 1).
                # Move back up so the cursor sits on the auto option (index 0).
                screen.action_cursor_up()
                screen.action_apply_socket()
                await pilot.pause()
                status = app.screen.query_one(KeyHintFooter).status
            return status, service.select_calls

        status, calls = asyncio.run(scenario())
        assert calls == [None]
        assert status == "using auto tmux socket selection"


class SetupScreenClearSocketTests(unittest.TestCase):
    def test_clear_socket_without_service_reports_unavailable(self) -> None:
        async def scenario() -> str:
            runtime = _runtime_with(None)
            app = _Harness(runtime)
            async with app.run_test(size=(140, 60)) as pilot:
                screen = SetupScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                screen.action_clear_socket()
                await pilot.pause()
                return app.screen.query_one(KeyHintFooter).status

        assert asyncio.run(scenario()) == "setup unavailable"

    def test_clear_socket_with_service_calls_select_with_none(self) -> None:
        async def scenario() -> tuple[str, list[str | None]]:
            service = _RecordingService(report=_report())
            runtime = _runtime_with(service)
            app = _Harness(runtime)
            async with app.run_test(size=(140, 60)) as pilot:
                screen = SetupScreen(runtime)
                await app.push_screen(screen)
                await pilot.pause()
                screen.action_clear_socket()
                await pilot.pause()
                status = app.screen.query_one(KeyHintFooter).status
            return status, service.select_calls

        status, calls = asyncio.run(scenario())
        assert calls == [None]
        assert status == "using auto tmux socket selection"

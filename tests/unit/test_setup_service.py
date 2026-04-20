# ruff: noqa: PT009

from __future__ import annotations

import shutil
import socket
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from muxdeck.exceptions import TmuxCommandError
from muxdeck.parsers.tmux_parser import TmuxListPanesParseResult, TmuxPaneRecord
from muxdeck.services.setup_service import SetupDoctorService


class _FakeTmux:
    def __init__(
        self,
        *,
        panes: tuple[TmuxPaneRecord, ...] = (),
        socket_path: Path | None = None,
        error: TmuxCommandError | None = None,
    ) -> None:
        self._panes = panes
        self.socket_path = socket_path
        self._error = error

    def list_panes(self) -> TmuxListPanesParseResult:
        if self._error is not None:
            raise self._error
        return TmuxListPanesParseResult(panes=self._panes)

    def set_socket_path(self, socket_path: str | Path | None) -> None:
        if socket_path is None:
            self.socket_path = None
            return
        self.socket_path = Path(socket_path).expanduser().resolve(strict=False)


class SetupDoctorServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_root = Path(tempfile.mkdtemp(prefix="setup-doctor-"))
        self.addCleanup(self._cleanup_temp_root)

    def _cleanup_temp_root(self) -> None:
        shutil.rmtree(self.temp_root, ignore_errors=True)

    def _create_socket(self, name: str) -> Path:
        socket_path = self.temp_root / name
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(socket_path))
        server.close()
        return socket_path.resolve(strict=False)

    def test_build_report_warns_when_selected_socket_differs_from_attached_server(self) -> None:
        attached_socket = self._create_socket("attached.sock")
        selected_socket = self._create_socket("selected.sock")
        tmux = _FakeTmux(
            panes=(
                TmuxPaneRecord(pane_id="%1"),
                TmuxPaneRecord(pane_id="%2"),
            ),
            socket_path=selected_socket,
        )
        service = SetupDoctorService(
            tmux,
            env={
                "TMUX": f"{attached_socket},401,0",
                "TMUX_PANE": "%9",
            },
            clock=lambda: datetime(2025, 1, 1, tzinfo=UTC),
            socket_search_roots=(self.temp_root,),
        )

        report = service.build_report()

        self.assertEqual(report.generated_at, datetime(2025, 1, 1, tzinfo=UTC))
        self.assertEqual(report.selected_socket_path, str(selected_socket))
        self.assertEqual(report.attached_socket_path, str(attached_socket))
        self.assertEqual(report.pane_count, 2)
        self.assertEqual(report.warning_count, 2)
        self.assertEqual(report.overall_status, "warning")
        self.assertTrue(
            any(
                check.key == "socket-selection"
                and check.status == "warning"
                and str(selected_socket) in check.detail
                for check in report.checks
            )
        )
        self.assertTrue(
            any(
                check.key == "current-pane" and check.status == "warning" and "%9" in check.detail
                for check in report.checks
            )
        )

    def test_select_socket_updates_adapter_and_marks_selected_option(self) -> None:
        selected_socket = self._create_socket("selected.sock")
        tmux = _FakeTmux(
            panes=(TmuxPaneRecord(pane_id="%1"),),
            socket_path=None,
        )
        service = SetupDoctorService(
            tmux,
            env={},
            socket_search_roots=(self.temp_root,),
        )

        report = service.select_socket(selected_socket)

        self.assertEqual(tmux.socket_path, selected_socket)
        self.assertEqual(report.selected_socket_path, str(selected_socket))
        self.assertTrue(
            any(
                option.socket_path == str(selected_socket) and option.is_selected
                for option in report.socket_options
            )
        )

    def test_build_report_surfaces_tmux_connection_errors(self) -> None:
        tmux = _FakeTmux(
            error=TmuxCommandError("tmux list-panes -a", stderr="no server running"),
        )
        service = SetupDoctorService(
            tmux,
            env={},
            socket_search_roots=(self.temp_root,),
        )

        report = service.build_report()

        self.assertIsNone(report.pane_count)
        self.assertEqual(report.error_count, 1)
        self.assertEqual(report.overall_status, "error")
        self.assertTrue(
            any(
                check.key == "tmux-connection"
                and check.status == "error"
                and "no server running" in check.detail
                for check in report.checks
            )
        )


if __name__ == "__main__":
    unittest.main()


class SetupDoctorWindowsHostTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_root = Path(tempfile.mkdtemp(prefix="setup-windows-"))
        self.addCleanup(lambda: shutil.rmtree(self.temp_root, ignore_errors=True))

    def _build_service(
        self,
        *,
        windows_host: object,
        windows_count: int | None = None,
    ) -> SetupDoctorService:
        tmux = _FakeTmux(
            panes=(
                TmuxPaneRecord(
                    session_name="main",
                    window_id="@1",
                    window_name="win",
                    pane_id="%1",
                    pane_current_command="bash",
                    pane_current_path="/home",
                    pane_pid=1,
                    pane_active=True,
                    window_active=True,
                ),
            ),
            socket_path=None,
        )
        return SetupDoctorService(
            tmux,  # type: ignore[arg-type]
            env={"TMUX": ""},
            clock=lambda: datetime(2026, 4, 16, tzinfo=UTC),
            socket_search_roots=(),
            windows_host_provider=lambda: windows_host,  # type: ignore[arg-type,return-value]
            windows_session_count_provider=lambda: windows_count,
        )

    def test_non_wsl_adds_no_windows_checks(self) -> None:
        from muxdeck.adapters.windows_host import WindowsHostInfo

        service = self._build_service(windows_host=WindowsHostInfo(is_wsl=False))
        report = service.build_report()

        keys = {c.key for c in report.checks}
        self.assertNotIn("windows-host", keys)
        self.assertNotIn("windows-sessions", keys)
        self.assertIsNone(report.windows_session_count)

    def test_wsl_with_available_host_reports_ok_and_count(self) -> None:
        from muxdeck.adapters.windows_host import WindowsHostInfo

        target = self.temp_root / "session-state"
        target.mkdir(parents=True)
        info = WindowsHostInfo(
            is_wsl=True,
            distro="Ubuntu",
            windows_userprofile="C:\\Users\\alice",
            session_state_dir=target,
            resolver="wslvar",
        )
        service = self._build_service(windows_host=info, windows_count=3)
        report = service.build_report()

        by_key = {c.key: c for c in report.checks}
        self.assertEqual(by_key["windows-host"].status, "ok")
        self.assertIn(str(target), by_key["windows-host"].detail)
        self.assertIn("wslvar", by_key["windows-host"].detail)
        self.assertEqual(by_key["windows-sessions"].status, "ok")
        self.assertIn("3 Copilot session", by_key["windows-sessions"].detail)
        self.assertEqual(report.windows_session_count, 3)

    def test_wsl_without_resolved_dir_surfaces_warning(self) -> None:
        from muxdeck.adapters.windows_host import WindowsHostInfo

        info = WindowsHostInfo(
            is_wsl=True,
            distro="Ubuntu",
            resolver="none",
            error="wslu not installed",
        )
        service = self._build_service(windows_host=info)
        report = service.build_report()

        by_key = {c.key: c for c in report.checks}
        self.assertEqual(by_key["windows-host"].status, "warning")
        self.assertIn("wslu", by_key["windows-host"].detail)
        # No count check when dir is unavailable.
        self.assertNotIn("windows-sessions", by_key)

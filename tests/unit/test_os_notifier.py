# ruff: noqa: ANN201

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import pytest

from copilot_commander.adapters.os_notifier import (
    NotifySendNotifier,
    NullNotifier,
    TerminalBellNotifier,
    WSLBurntToastNotifier,
    _escape_powershell_single_quoted,
    _is_wsl,
    detect_os_notifier,
)


class _RecordingRunner:
    def __init__(self, *, raises: BaseException | None = None) -> None:
        self.calls: list[tuple[str, ...]] = []
        self._raises = raises

    def __call__(self, argv: Sequence[str]) -> None:
        self.calls.append(tuple(argv))
        if self._raises is not None:
            raise self._raises


def test_notify_send_notifier_issues_expected_argv() -> None:
    runner = _RecordingRunner()
    notifier = NotifySendNotifier(runner=runner, app_name="commander-test")

    notifier.notify("Agent failed", "exit code 1", "critical")

    assert runner.calls == [
        (
            "notify-send",
            "--app-name",
            "commander-test",
            "--urgency",
            "critical",
            "Agent failed",
            "exit code 1",
        )
    ]


def test_wsl_burnttoast_escapes_single_quotes_and_uses_powershell() -> None:
    runner = _RecordingRunner()
    notifier = WSLBurntToastNotifier(runner=runner)

    notifier.notify("agent 'alpha' failed", "it's bad `now`", "critical")

    assert len(runner.calls) == 1
    argv = runner.calls[0]
    assert argv[0] == "powershell.exe"
    assert argv[1] == "-NoProfile"
    assert argv[2] == "-Command"
    script = argv[3]
    # Single quotes must be doubled inside the single-quoted literal.
    assert "'agent ''alpha'' failed'" in script
    assert "'it''s bad `now`'" in script
    assert "New-BurntToastNotification" in script
    assert "Get-Module" in script


def test_wsl_burnttoast_falls_back_when_runner_raises() -> None:
    runner = _RecordingRunner(raises=OSError("powershell.exe missing"))
    fallback_calls: list[tuple[str, str, str]] = []

    class _Fallback:
        def notify(self, title: str, body: str, urgency: str) -> None:
            fallback_calls.append((title, body, urgency))

    notifier = WSLBurntToastNotifier(runner=runner, fallback=_Fallback())
    notifier.notify("title", "body", "critical")

    assert fallback_calls == [("title", "body", "critical")]


def test_escape_powershell_single_quoted_strips_carriage_returns() -> None:
    assert _escape_powershell_single_quoted("a\r\nb") == "a\nb"
    assert _escape_powershell_single_quoted("it's") == "it''s"
    assert _escape_powershell_single_quoted("x\x00y") == "xy"


def test_terminal_bell_writes_bell_char() -> None:
    class _Stream:
        def __init__(self) -> None:
            self.buffer: list[str] = []
            self.flushed = 0

        def write(self, value: str) -> None:
            self.buffer.append(value)

        def flush(self) -> None:
            self.flushed += 1

    stream = _Stream()
    notifier = TerminalBellNotifier(stream=stream)
    notifier.notify("t", "b", "low")

    assert stream.buffer == ["\a"]
    assert stream.flushed == 1


def test_null_notifier_is_a_noop() -> None:
    NullNotifier().notify("t", "b", "critical")  # must not raise


def test_detect_os_notifier_prefers_burnttoast_on_wsl(tmp_path: Path) -> None:
    proc = tmp_path / "version"
    proc.write_text("Linux 5.15-microsoft-standard-WSL2\n", encoding="utf-8")
    which_calls: list[str] = []

    def which(name: str) -> str | None:
        which_calls.append(name)
        if name == "powershell.exe":
            return "/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"
        return None

    notifier = detect_os_notifier(
        which=which,
        proc_version=proc,
        platform="linux",
    )
    assert isinstance(notifier, WSLBurntToastNotifier)
    assert "powershell.exe" in which_calls


def test_detect_os_notifier_prefers_notify_send_on_linux(tmp_path: Path) -> None:
    proc = tmp_path / "version"
    proc.write_text("Linux 6.5 stock\n", encoding="utf-8")

    def which(name: str) -> str | None:
        if name == "notify-send":
            return "/usr/bin/notify-send"
        return None

    notifier = detect_os_notifier(
        which=which,
        proc_version=proc,
        platform="linux",
    )
    assert isinstance(notifier, NotifySendNotifier)


def test_detect_os_notifier_falls_back_to_terminal_bell(tmp_path: Path) -> None:
    proc = tmp_path / "missing"  # read failure → not WSL

    notifier = detect_os_notifier(
        which=lambda _name: None,
        proc_version=proc,
        platform="linux",
        env={},
    )
    assert isinstance(notifier, TerminalBellNotifier)


def test_detect_os_notifier_null_when_disabled_env(tmp_path: Path) -> None:
    proc = tmp_path / "version"
    proc.write_text("generic", encoding="utf-8")
    notifier = detect_os_notifier(
        which=lambda _name: None,
        proc_version=proc,
        platform="darwin",
        env={"COMMANDER_DISABLE_OS_NOTIFY": "1"},
    )
    assert isinstance(notifier, NullNotifier)


def test_is_wsl_reads_proc_version(tmp_path: Path) -> None:
    proc = tmp_path / "v"
    proc.write_text("Linux Microsoft WSL2 kernel", encoding="utf-8")
    assert _is_wsl(proc) is True

    proc.write_text("Linux generic", encoding="utf-8")
    assert _is_wsl(proc) is False


def test_is_wsl_handles_missing_file(tmp_path: Path) -> None:
    missing = tmp_path / "nope"
    assert _is_wsl(missing) is False


@pytest.mark.parametrize(
    ("title", "body"),
    [
        ("", ""),
        ("with\nnewline", "body"),
    ],
)
def test_notify_send_accepts_various_texts(title: str, body: str) -> None:
    runner = _RecordingRunner()
    NotifySendNotifier(runner=runner).notify(title, body, "normal")
    assert runner.calls[0][-2:] == (title, body)

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Literal, Protocol, runtime_checkable

_log = logging.getLogger(__name__)

NotifyUrgency = Literal["low", "normal", "critical"]

SubprocessRunner = Callable[[Sequence[str]], None]


def _default_runner(argv: Sequence[str]) -> None:
    """Default runner: swallow CalledProcessError/OSError, log and continue.

    OS notifications must never break the TUI; a missing helper or a
    stderr-chatty toast tool should be a no-op, not a crash.
    """
    try:
        subprocess.run(
            list(argv),
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        _log.debug("os notifier runner failed for %s: %s", argv[0] if argv else "?", exc)


@runtime_checkable
class OsNotifier(Protocol):
    def notify(self, title: str, body: str, urgency: NotifyUrgency) -> None: ...


class NullNotifier:
    """No-op notifier used in tests and when detection fails."""

    def notify(self, title: str, body: str, urgency: NotifyUrgency) -> None:
        return


class _BellStream(Protocol):
    def write(self, value: str, /) -> int | None: ...


class TerminalBellNotifier:
    """Fallback notifier that writes ``\\a`` to stdout."""

    def __init__(self, *, stream: _BellStream | None = None) -> None:
        self._stream: _BellStream = stream if stream is not None else sys.stdout

    def notify(self, title: str, body: str, urgency: NotifyUrgency) -> None:
        try:
            self._stream.write("\a")
            flush = getattr(self._stream, "flush", None)
            if callable(flush):
                flush()
        except OSError as exc:
            _log.debug("terminal bell write failed: %s", exc)


class NotifySendNotifier:
    """Linux ``notify-send`` adapter."""

    def __init__(
        self,
        *,
        runner: SubprocessRunner = _default_runner,
        binary: str = "notify-send",
        app_name: str = "copilot-commander",
    ) -> None:
        self._runner = runner
        self._binary = binary
        self._app_name = app_name

    def notify(self, title: str, body: str, urgency: NotifyUrgency) -> None:
        argv: tuple[str, ...] = (
            self._binary,
            "--app-name",
            self._app_name,
            "--urgency",
            urgency,
            title,
            body,
        )
        self._runner(argv)


def _escape_powershell_single_quoted(value: str) -> str:
    """Escape a string for a PowerShell single-quoted literal.

    PowerShell single-quoted strings are fully literal except for ``'`` which
    is escaped by doubling it. Backticks have no special meaning inside
    single-quoted strings, but we strip NULs and CRs to keep the command
    line clean.
    """
    cleaned = value.replace("\x00", "").replace("\r", "")
    return cleaned.replace("'", "''")


class WSLBurntToastNotifier:
    """WSL-hosted notifier that shells out to BurntToast on the Windows side.

    Failure modes (BurntToast not installed, ``powershell.exe`` missing)
    are swallowed by the runner and fall back to a terminal bell so the
    operator still gets an audible cue.
    """

    def __init__(
        self,
        *,
        runner: SubprocessRunner = _default_runner,
        fallback: OsNotifier | None = None,
        powershell: str = "powershell.exe",
    ) -> None:
        self._runner = runner
        self._fallback = fallback if fallback is not None else TerminalBellNotifier()
        self._powershell = powershell

    def notify(self, title: str, body: str, urgency: NotifyUrgency) -> None:
        quoted_title = _escape_powershell_single_quoted(title)
        quoted_body = _escape_powershell_single_quoted(body)
        script = (
            "if (Get-Module -ListAvailable -Name BurntToast) { "
            f"New-BurntToastNotification -Text '{quoted_title}', '{quoted_body}' "
            "} else { exit 2 }"
        )
        argv: tuple[str, ...] = (self._powershell, "-NoProfile", "-Command", script)
        try:
            self._runner(argv)
        except (OSError, subprocess.SubprocessError) as exc:
            _log.debug("BurntToast dispatch failed: %s", exc)
            self._fallback.notify(title, body, urgency)


def _is_wsl(proc_version: Path = Path("/proc/version")) -> bool:
    try:
        content = proc_version.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return "microsoft" in content.lower()


def detect_os_notifier(
    *,
    which: Callable[[str], str | None] = shutil.which,
    proc_version: Path = Path("/proc/version"),
    platform: str = sys.platform,
    runner: SubprocessRunner = _default_runner,
    env: dict[str, str] | None = None,
) -> OsNotifier:
    """Pick an appropriate :class:`OsNotifier` for this host.

    Order: WSL BurntToast → Linux notify-send → terminal bell.
    """
    environ = env if env is not None else dict(os.environ)
    if (
        platform.startswith("linux")
        and _is_wsl(proc_version)
        and which("powershell.exe") is not None
    ):
        return WSLBurntToastNotifier(runner=runner)
    if platform.startswith("linux") and which("notify-send") is not None:
        return NotifySendNotifier(runner=runner)
    # ``NO_OS_NOTIFY`` offers an escape hatch for headless CI environments
    # where even the terminal bell is undesirable.
    if environ.get("COMMANDER_DISABLE_OS_NOTIFY") == "1":
        return NullNotifier()
    return TerminalBellNotifier()


__all__ = [
    "NotifySendNotifier",
    "NotifyUrgency",
    "NullNotifier",
    "OsNotifier",
    "SubprocessRunner",
    "TerminalBellNotifier",
    "WSLBurntToastNotifier",
    "detect_os_notifier",
]

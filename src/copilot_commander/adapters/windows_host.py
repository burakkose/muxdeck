"""Detect whether muxdeck is running inside WSL and, if so, resolve the
Windows-side ``%USERPROFILE%\\.copilot\\session-state`` directory.

The resolver is a short, deterministic fallback chain:

1. ``$WSL_DISTRO_NAME`` / ``/proc/sys/kernel/osrelease`` decide whether we
   are in WSL at all.
2. ``wslvar USERPROFILE`` (from the ``wslu`` package) is the cheapest
   official way to read the Windows user-profile env var.
3. ``cmd.exe /c echo %USERPROFILE%`` is the universal fallback when
   ``wslvar`` isn't installed.
4. Scanning ``/mnt/c/Users/`` for directories that contain
   ``.copilot/session-state`` is the last-ditch option when neither
   helper works.

Detection runs at startup and whenever the Setup screen is refreshed.
It is fully injectable (env + command runner + filesystem probe) so the
tests never touch real ``cmd.exe``.
"""

from __future__ import annotations

import logging
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

_log = logging.getLogger(__name__)

WindowsResolver = Literal[
    "wslvar",
    "cmd_exe",
    "mnt_scan",
    "env_userprofile",
    "none",
]


@dataclass(frozen=True, slots=True)
class WindowsHostInfo:
    """Result of Windows-host detection.

    ``is_wsl`` reflects whether we are running inside WSL. The remaining
    fields are meaningful only when ``is_wsl`` is ``True``.
    """

    is_wsl: bool
    distro: str | None = None
    windows_userprofile: str | None = None
    session_state_dir: Path | None = None
    resolver: WindowsResolver = "none"
    error: str | None = None

    @property
    def is_available(self) -> bool:
        """True when we resolved a directory we can actually scan."""
        return (
            self.is_wsl and self.session_state_dir is not None and self.session_state_dir.is_dir()
        )


type CommandRunner = Callable[[list[str]], "CommandOutcome"]


@dataclass(frozen=True, slots=True)
class CommandOutcome:
    returncode: int
    stdout: str
    stderr: str = ""


def _default_runner(argv: list[str]) -> CommandOutcome:
    try:
        proc = subprocess.run(
            argv,
            check=False,
            text=True,
            capture_output=True,
            timeout=4.0,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return CommandOutcome(returncode=127, stdout="", stderr=str(exc))
    return CommandOutcome(
        returncode=proc.returncode,
        stdout=proc.stdout or "",
        stderr=proc.stderr or "",
    )


def _is_wsl(env: Mapping[str, str], osrelease_reader: Callable[[], str]) -> bool:
    if env.get("WSL_DISTRO_NAME"):
        return True
    try:
        osrelease = osrelease_reader().lower()
    except OSError:
        return False
    return "microsoft" in osrelease or "wsl" in osrelease


def _read_osrelease() -> str:
    return Path("/proc/sys/kernel/osrelease").read_text(encoding="utf-8", errors="replace")


def _winpath_to_wsl(winpath: str) -> Path | None:
    """Convert ``C:\\Users\\foo`` to ``/mnt/c/Users/foo`` without shelling out.

    Uses a manual translation so detection stays deterministic in tests
    that stub the command runner. ``wslpath`` would work too but we
    already have the Windows path verbatim.
    """
    trimmed = winpath.strip().strip("\"'")
    if not trimmed:
        return None
    # Only drive-letter absolute paths are supported. UNC, relative,
    # or "Z:\\" style paths that don't map to /mnt are left unresolved.
    if len(trimmed) < 3 or trimmed[1:3] not in (":\\", ":/"):
        return None
    drive = trimmed[0].lower()
    remainder = trimmed[3:].replace("\\", "/")
    return Path(f"/mnt/{drive}/{remainder}") if remainder else Path(f"/mnt/{drive}")


def _userprofile_via_wslvar(run: CommandRunner) -> str | None:
    outcome = run(["wslvar", "USERPROFILE"])
    if outcome.returncode != 0:
        return None
    value = outcome.stdout.strip()
    return value or None


def _userprofile_via_cmd(run: CommandRunner) -> str | None:
    # cmd.exe echoes a trailing CR because it writes CRLF; strip both.
    outcome = run(["cmd.exe", "/c", "echo %USERPROFILE%"])
    if outcome.returncode != 0:
        return None
    value = outcome.stdout.strip().strip("\r")
    if not value or value == "%USERPROFILE%":
        return None
    return value


def _userprofile_via_mnt_scan(mnt_root: Path) -> tuple[str, Path] | None:
    """Find a single ``/mnt/c/Users/<name>/.copilot/session-state`` dir."""
    users_dir = mnt_root / "Users"
    if not users_dir.is_dir():
        return None
    candidates: list[tuple[str, Path]] = []
    try:
        entries = list(users_dir.iterdir())
    except OSError:
        return None
    skip = {"public", "default", "default user", "all users"}
    for entry in entries:
        if not entry.is_dir() or entry.name.lower() in skip:
            continue
        target = entry / ".copilot" / "session-state"
        if target.is_dir():
            # The Windows-style USERPROFILE is C:\Users\<name>.
            winprofile = f"C:\\Users\\{entry.name}"
            candidates.append((winprofile, target))
    if len(candidates) != 1:
        # Zero or ambiguous — refuse to guess.
        return None
    return candidates[0]


def detect_windows_host(
    *,
    env: Mapping[str, str],
    runner: CommandRunner | None = None,
    osrelease_reader: Callable[[], str] | None = None,
    mnt_root: Path = Path("/mnt/c"),
) -> WindowsHostInfo:
    """Detect WSL + Windows-side Copilot session-state directory.

    Every external dependency is injected so tests exercise the real
    branching without touching ``/mnt``, ``cmd.exe``, or ``wslvar``.
    """
    run = runner or _default_runner
    read_osrelease = osrelease_reader or _read_osrelease

    if not _is_wsl(env, read_osrelease):
        return WindowsHostInfo(is_wsl=False)

    distro = env.get("WSL_DISTRO_NAME") or None

    # Try each resolver in order. A resolver that returns a USERPROFILE
    # mapping to an existing session-state dir wins. Otherwise we keep
    # trying — the whole point of the fallback chain is to paper over
    # installations where wslvar isn't present or cmd.exe prints the
    # path but the directory hasn't been created yet.
    last_miss: WindowsHostInfo | None = None

    explicit = env.get("USERPROFILE")
    if explicit:
        candidate = _try_resolver(distro, explicit, "env_userprofile")
        if candidate.is_available:
            return candidate
        last_miss = candidate

    wslvar_value = _userprofile_via_wslvar(run)
    if wslvar_value is not None:
        candidate = _try_resolver(distro, wslvar_value, "wslvar")
        if candidate.is_available:
            return candidate
        last_miss = last_miss or candidate

    cmd_value = _userprofile_via_cmd(run)
    if cmd_value is not None:
        candidate = _try_resolver(distro, cmd_value, "cmd_exe")
        if candidate.is_available:
            return candidate
        last_miss = last_miss or candidate

    scanned = _userprofile_via_mnt_scan(mnt_root)
    if scanned is not None:
        winprofile, session_state = scanned
        return WindowsHostInfo(
            is_wsl=True,
            distro=distro,
            windows_userprofile=winprofile,
            session_state_dir=session_state,
            resolver="mnt_scan",
        )

    if last_miss is not None:
        return last_miss

    return WindowsHostInfo(
        is_wsl=True,
        distro=distro,
        resolver="none",
        error=(
            "could not resolve Windows USERPROFILE. "
            "install wslu (`sudo apt install wslu`) or set USERPROFILE."
        ),
    )


def _try_resolver(
    distro: str | None,
    userprofile: str,
    resolver: WindowsResolver,
) -> WindowsHostInfo:
    resolved = _winpath_to_wsl(userprofile)
    if resolved is None:
        return WindowsHostInfo(
            is_wsl=True,
            distro=distro,
            windows_userprofile=userprofile,
            resolver=resolver,
            error=f"could not translate {userprofile} to a WSL path",
        )
    return _finalize(
        distro=distro,
        userprofile=userprofile,
        profile_path=resolved,
        resolver=resolver,
    )


def _finalize(
    *,
    distro: str | None,
    userprofile: str,
    profile_path: Path,
    resolver: WindowsResolver,
) -> WindowsHostInfo:
    session_state = profile_path / ".copilot" / "session-state"
    if not session_state.is_dir():
        return WindowsHostInfo(
            is_wsl=True,
            distro=distro,
            windows_userprofile=userprofile,
            session_state_dir=session_state,
            resolver=resolver,
            error=f"{session_state} does not exist",
        )
    return WindowsHostInfo(
        is_wsl=True,
        distro=distro,
        windows_userprofile=userprofile,
        session_state_dir=session_state,
        resolver=resolver,
    )


__all__ = [
    "CommandOutcome",
    "CommandRunner",
    "WindowsHostInfo",
    "WindowsResolver",
    "detect_windows_host",
]

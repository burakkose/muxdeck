from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from stat import S_ISSOCK
from typing import Literal

from copilot_commander.adapters.tmux_adapter import TmuxAdapter, parse_tmux_socket_path
from copilot_commander.adapters.windows_host import WindowsHostInfo
from copilot_commander.exceptions import TmuxCommandError
from copilot_commander.types import PathLike

SetupCheckStatus = Literal["ok", "warning", "error", "info"]


@dataclass(frozen=True, slots=True)
class SetupCheck:
    key: str
    status: SetupCheckStatus
    title: str
    detail: str


@dataclass(frozen=True, slots=True)
class TmuxSocketOption:
    label: str
    socket_path: str | None
    note: str
    is_selected: bool
    exists: bool


@dataclass(frozen=True, slots=True)
class SetupDoctorReport:
    generated_at: datetime
    selected_socket_path: str | None
    effective_socket_path: str | None
    attached_socket_path: str | None
    configured_socket_path: str | None
    pane_count: int | None
    socket_options: tuple[TmuxSocketOption, ...]
    checks: tuple[SetupCheck, ...]
    windows_host: WindowsHostInfo | None = None
    windows_session_count: int | None = None

    @property
    def error_count(self) -> int:
        return sum(1 for check in self.checks if check.status == "error")

    @property
    def warning_count(self) -> int:
        return sum(1 for check in self.checks if check.status == "warning")

    @property
    def overall_status(self) -> SetupCheckStatus:
        if self.error_count:
            return "error"
        if self.warning_count:
            return "warning"
        if any(check.status == "ok" for check in self.checks):
            return "ok"
        return "info"


class SetupDoctorService:
    def __init__(
        self,
        tmux: TmuxAdapter,
        *,
        configured_socket_path: Path | None = None,
        env: Mapping[str, str] | None = None,
        clock: Callable[[], datetime] | None = None,
        socket_search_roots: tuple[Path, ...] | None = None,
        windows_host_provider: Callable[[], WindowsHostInfo | None] | None = None,
        windows_session_count_provider: Callable[[], int | None] | None = None,
    ) -> None:
        self._tmux = tmux
        self._configured_socket_path = configured_socket_path
        self._env = os.environ if env is None else env
        self._clock = clock or (lambda: datetime.now(UTC))
        self._socket_search_roots = socket_search_roots
        self._windows_host_provider = windows_host_provider
        self._windows_session_count_provider = windows_session_count_provider

    def build_report(self) -> SetupDoctorReport:
        selected_socket_path = self._tmux.socket_path
        attached_socket_path = parse_tmux_socket_path(self._env.get("TMUX"))
        effective_socket_path = selected_socket_path or attached_socket_path
        current_pane_id = self._normalize_optional_text(self._env.get("TMUX_PANE"))
        options = self._build_socket_options(
            selected_socket_path=selected_socket_path,
            attached_socket_path=attached_socket_path,
        )
        checks: list[SetupCheck] = [
            self._attached_server_check(attached_socket_path),
            self._selection_check(
                selected_socket_path=selected_socket_path,
                effective_socket_path=effective_socket_path,
                attached_socket_path=attached_socket_path,
            ),
            self._socket_detection_check(options),
        ]

        pane_count: int | None = None
        try:
            panes = self._tmux.list_panes().panes
        except TmuxCommandError as exc:
            checks.append(
                SetupCheck(
                    key="tmux-connection",
                    status="error",
                    title="tmux connection",
                    detail=self._describe_tmux_error(exc),
                )
            )
        else:
            pane_count = len(panes)
            checks.append(
                SetupCheck(
                    key="tmux-connection",
                    status="ok",
                    title="tmux connection",
                    detail=f"connected to a tmux server with {pane_count} visible panes",
                )
            )
            if pane_count == 0:
                checks.append(
                    SetupCheck(
                        key="pane-visibility",
                        status="warning",
                        title="pane visibility",
                        detail="tmux responded, but list-panes -a returned no panes "
                        "on the selected server",
                    )
                )
            if current_pane_id is None:
                checks.append(
                    SetupCheck(
                        key="current-pane",
                        status="info",
                        title="current pane",
                        detail="TMUX_PANE is unset, so same-pane validation is unavailable",
                    )
                )
            elif any(pane.pane_id == current_pane_id for pane in panes):
                checks.append(
                    SetupCheck(
                        key="current-pane",
                        status="ok",
                        title="current pane",
                        detail=f"pane {current_pane_id} is visible on the selected tmux server",
                    )
                )
            else:
                checks.append(
                    SetupCheck(
                        key="current-pane",
                        status="warning",
                        title="current pane",
                        detail=f"pane {current_pane_id} is not visible on the selected tmux server",
                    )
                )

        windows_host: WindowsHostInfo | None = None
        windows_session_count: int | None = None
        if self._windows_host_provider is not None:
            windows_host = self._windows_host_provider()
            if windows_host is not None and windows_host.is_wsl:
                checks.append(self._windows_host_check(windows_host))
                if self._windows_session_count_provider is not None and windows_host.is_available:
                    windows_session_count = self._windows_session_count_provider()
                    checks.append(
                        self._windows_session_count_check(windows_host, windows_session_count)
                    )

        return SetupDoctorReport(
            generated_at=self._clock(),
            selected_socket_path=self._stringify_path(selected_socket_path),
            effective_socket_path=self._stringify_path(effective_socket_path),
            attached_socket_path=self._stringify_path(attached_socket_path),
            configured_socket_path=self._stringify_path(self._configured_socket_path),
            pane_count=pane_count,
            socket_options=options,
            checks=tuple(checks),
            windows_host=windows_host,
            windows_session_count=windows_session_count,
        )

    def select_socket(self, socket_path: PathLike | None) -> SetupDoctorReport:
        self._tmux.set_socket_path(socket_path)
        return self.build_report()

    def _windows_host_check(self, info: WindowsHostInfo) -> SetupCheck:
        """Describe how the Windows-side ``.copilot`` directory was found."""
        distro = f" ({info.distro})" if info.distro else ""
        if info.session_state_dir is not None and info.is_available:
            return SetupCheck(
                key="windows-host",
                status="ok",
                title=f"WSL bridge{distro}",
                detail=(
                    f"scanning Windows session-state at {info.session_state_dir} "
                    f"(resolved via {info.resolver})"
                ),
            )
        if info.session_state_dir is not None:
            return SetupCheck(
                key="windows-host",
                status="warning",
                title=f"WSL bridge{distro}",
                detail=(
                    f"resolved Windows USERPROFILE via {info.resolver} but "
                    f"{info.session_state_dir} is missing or not a directory"
                ),
            )
        return SetupCheck(
            key="windows-host",
            status="warning",
            title=f"WSL bridge{distro}",
            detail=info.error or "WSL detected but no Windows session-state directory was resolved",
        )

    def _windows_session_count_check(
        self,
        info: WindowsHostInfo,
        count: int | None,
    ) -> SetupCheck:
        if count is None:
            return SetupCheck(
                key="windows-sessions",
                status="info",
                title="Windows sessions",
                detail="session count unavailable",
            )
        if count == 0:
            return SetupCheck(
                key="windows-sessions",
                status="info",
                title="Windows sessions",
                detail=f"no Copilot sessions discovered under {info.session_state_dir}",
            )
        return SetupCheck(
            key="windows-sessions",
            status="ok",
            title="Windows sessions",
            detail=f"discovered {count} Copilot session(s) on the Windows side",
        )

    def _attached_server_check(self, attached_socket_path: Path | None) -> SetupCheck:
        if attached_socket_path is None:
            return SetupCheck(
                key="attached-server",
                status="warning",
                title="attached server",
                detail="run the app inside tmux or select an explicit socket "
                "to inspect another server",
            )
        return SetupCheck(
            key="attached-server",
            status="ok",
            title="attached server",
            detail=f"the UI is attached to {attached_socket_path}",
        )

    def _selection_check(
        self,
        *,
        selected_socket_path: Path | None,
        effective_socket_path: Path | None,
        attached_socket_path: Path | None,
    ) -> SetupCheck:
        if selected_socket_path is None:
            detail = "using tmux default socket resolution"
            if attached_socket_path is not None:
                detail = f"following the attached tmux server {attached_socket_path}"
            return SetupCheck(
                key="socket-selection",
                status="info",
                title="socket selection",
                detail=detail,
            )
        if attached_socket_path is not None and selected_socket_path != attached_socket_path:
            return SetupCheck(
                key="socket-selection",
                status="warning",
                title="socket selection",
                detail=f"targeting {selected_socket_path} instead of the attached "
                f"server {attached_socket_path}",
            )
        if effective_socket_path is None:
            return SetupCheck(
                key="socket-selection",
                status="warning",
                title="socket selection",
                detail="no tmux socket could be resolved for the current selection",
            )
        return SetupCheck(
            key="socket-selection",
            status="ok",
            title="socket selection",
            detail=f"targeting tmux socket {effective_socket_path}",
        )

    def _socket_detection_check(
        self,
        options: tuple[TmuxSocketOption, ...],
    ) -> SetupCheck:
        detected = tuple(option for option in options if option.socket_path is not None)
        if not detected:
            return SetupCheck(
                key="socket-detection",
                status="warning",
                title="socket detection",
                detail="no tmux socket files were discovered under /tmp for the current user",
            )
        return SetupCheck(
            key="socket-detection",
            status="ok",
            title="socket detection",
            detail=f"discovered {len(detected)} tmux socket candidates",
        )

    def _build_socket_options(
        self,
        *,
        selected_socket_path: Path | None,
        attached_socket_path: Path | None,
    ) -> tuple[TmuxSocketOption, ...]:
        options: list[TmuxSocketOption] = [
            TmuxSocketOption(
                label="Auto / attached server",
                socket_path=None,
                note=(
                    "follow the current TMUX attachment"
                    if attached_socket_path is not None
                    else "use tmux default socket lookup"
                ),
                is_selected=selected_socket_path is None,
                exists=True,
            )
        ]
        tagged_paths: dict[Path, list[str]] = {}
        for label, path in (
            ("attached", attached_socket_path),
            ("configured", self._configured_socket_path),
            ("selected", selected_socket_path),
        ):
            if path is None:
                continue
            tagged_paths.setdefault(path, []).append(label)
        for path in self._discover_tmux_sockets(attached_socket_path):
            tagged_paths.setdefault(path, []).append("detected")

        for path in sorted(tagged_paths, key=str):
            tags = self._unique(tagged_paths[path])
            exists = path.exists()
            note = ", ".join(tags)
            if not exists:
                note = f"{note}, missing"
            options.append(
                TmuxSocketOption(
                    label=str(path),
                    socket_path=str(path),
                    note=note,
                    is_selected=selected_socket_path == path,
                    exists=exists,
                )
            )
        return tuple(options)

    def _discover_tmux_sockets(self, attached_socket_path: Path | None) -> tuple[Path, ...]:
        directories: list[Path] = []
        if attached_socket_path is not None:
            directories.append(attached_socket_path.parent)
        if self._socket_search_roots is not None:
            directories.extend(self._socket_search_roots)
        elif hasattr(os, "getuid"):
            directories.append(Path("/tmp") / f"tmux-{os.getuid()}")

        discovered: list[Path] = []
        seen_directories: set[Path] = set()
        seen_paths: set[Path] = set()
        for directory in directories:
            resolved_directory = directory.expanduser().resolve(strict=False)
            if resolved_directory in seen_directories or not directory.is_dir():
                continue
            seen_directories.add(resolved_directory)
            try:
                children = sorted(directory.iterdir(), key=lambda child: child.name)
            except OSError:
                continue
            for child in children:
                candidate = child.expanduser().resolve(strict=False)
                if candidate in seen_paths or not self._is_socket_path(child):
                    continue
                seen_paths.add(candidate)
                discovered.append(candidate)
        return tuple(discovered)

    def _is_socket_path(self, path: Path) -> bool:
        try:
            return S_ISSOCK(path.stat().st_mode)
        except OSError:
            return False

    def _describe_tmux_error(self, exc: TmuxCommandError) -> str:
        stderr = self._normalize_optional_text(exc.stderr)
        if stderr is not None:
            return stderr
        stdout = self._normalize_optional_text(exc.stdout)
        if stdout is not None:
            return stdout
        return exc.command

    def _stringify_path(self, path: Path | None) -> str | None:
        return None if path is None else str(path)

    def _normalize_optional_text(self, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None

    def _unique(self, values: list[str]) -> tuple[str, ...]:
        ordered: list[str] = []
        seen: set[str] = set()
        for value in values:
            if value in seen:
                continue
            seen.add(value)
            ordered.append(value)
        return tuple(ordered)

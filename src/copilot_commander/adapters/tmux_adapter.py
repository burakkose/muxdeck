from __future__ import annotations

import shlex
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from copilot_commander.domain.value_objects import CommandResult
from copilot_commander.exceptions import CommandError, TmuxCommandError
from copilot_commander.parsers import (
    TmuxListPanesParseResult,
    TmuxPaneRecord,
    parse_tmux_list_panes_output,
)
from copilot_commander.types import CommandRunner, PathLike

_LIST_PANES_FIELDS: Final[tuple[tuple[str, str], ...]] = (
    ("session_name", "#{session_name}"),
    ("session_id", "#{session_id}"),
    ("window_id", "#{window_id}"),
    ("window_index", "#{window_index}"),
    ("window_name", "#{window_name}"),
    ("window_active", "#{window_active}"),
    ("pane_id", "#{pane_id}"),
    ("pane_index", "#{pane_index}"),
    ("pane_active", "#{pane_active}"),
    ("pane_pid", "#{pane_pid}"),
    ("pane_tty", "#{pane_tty}"),
    ("pane_current_path", "#{pane_current_path}"),
    ("pane_current_command", "#{pane_current_command}"),
)
_DISPLAY_MESSAGE_FIELDS: Final[tuple[tuple[str, str], ...]] = (
    *_LIST_PANES_FIELDS,
    ("pane_dead", "#{pane_dead}"),
)
LIST_PANES_FORMAT: Final[str] = "\t".join(
    f"{field_name}={field_format}" for field_name, field_format in _LIST_PANES_FIELDS
)
DISPLAY_MESSAGE_FORMAT: Final[str] = "\t".join(
    f"{field_name}={field_format}" for field_name, field_format in _DISPLAY_MESSAGE_FIELDS
)
_MISSING_TARGET_SNIPPETS: Final[tuple[str, ...]] = (
    "can't find pane",
    "can't find window",
    "can't find session",
)


def _render_command(command: tuple[str, ...]) -> str:
    return shlex.join(command)


def _normalize_optional_text(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    return normalized or None


def _parse_optional_bool(value: str | None) -> bool | None:
    normalized = _normalize_optional_text(value)
    if normalized is None:
        return None
    lowered = normalized.casefold()
    if lowered in {"1", "true", "yes", "on"}:
        return True
    if lowered in {"0", "false", "no", "off"}:
        return False
    return None


def parse_tmux_socket_path(value: str | None) -> Path | None:
    normalized = _normalize_optional_text(value)
    if normalized is None:
        return None
    socket_value = normalized.split(",", maxsplit=1)[0].strip()
    if not socket_value:
        return None
    return Path(socket_value).expanduser().resolve(strict=False)


@dataclass(frozen=True, slots=True)
class TmuxPaneMetadata:
    pane_id: str
    session_name: str | None = None
    session_id: str | None = None
    window_id: str | None = None
    window_index: int | None = None
    window_name: str | None = None
    window_active: bool | None = None
    pane_index: int | None = None
    pane_active: bool | None = None
    pane_pid: int | None = None
    pane_tty: str | None = None
    pane_current_path: str | None = None
    pane_current_command: str | None = None
    pane_dead: bool | None = None
    raw_fields: dict[str, str] | None = None

    @classmethod
    def from_record(cls, record: TmuxPaneRecord) -> TmuxPaneMetadata:
        raw_fields = {} if record.raw_fields is None else dict(record.raw_fields)
        return cls(
            pane_id=record.pane_id,
            session_name=record.session_name,
            session_id=record.session_id,
            window_id=record.window_id,
            window_index=record.window_index,
            window_name=record.window_name,
            window_active=record.window_active,
            pane_index=record.pane_index,
            pane_active=record.pane_active,
            pane_pid=record.pane_pid,
            pane_tty=record.pane_tty,
            pane_current_path=record.pane_current_path,
            pane_current_command=record.pane_current_command,
            pane_dead=_parse_optional_bool(raw_fields.get("pane_dead")),
            raw_fields=raw_fields,
        )


@dataclass(frozen=True, slots=True)
class TmuxWindowInfo:
    session_name: str
    window_id: str
    window_index: int | None = None
    window_name: str | None = None
    pane_ids: tuple[str, ...] = ()

    @property
    def pane_count(self) -> int:
        return len(self.pane_ids)


class TmuxAdapter:
    def __init__(
        self,
        command_runner: CommandRunner,
        *,
        binary: str = "tmux",
        timeout_sec: float = 10.0,
        socket_path: PathLike | None = None,
    ) -> None:
        if timeout_sec <= 0:
            msg = "timeout_sec must be greater than zero"
            raise ValueError(msg)
        self._command_runner = command_runner
        self._binary = binary
        self._timeout_sec = timeout_sec
        self._socket_path: Path | None = None
        self.set_socket_path(socket_path)

    @property
    def socket_path(self) -> Path | None:
        return self._socket_path

    def set_socket_path(self, socket_path: PathLike | None) -> None:
        if socket_path is None:
            self._socket_path = None
            return
        self._socket_path = Path(socket_path).expanduser().resolve(strict=False)

    def list_panes(self) -> TmuxListPanesParseResult:
        result = self._run_tmux("list-panes", "-a", "-F", LIST_PANES_FORMAT)
        return parse_tmux_list_panes_output(result.stdout)

    def list_windows(self) -> tuple[TmuxWindowInfo, ...]:
        parsed = self.list_panes()
        panes_by_window: dict[tuple[str, str], list[TmuxPaneRecord]] = defaultdict(list)
        for pane in parsed.panes:
            if pane.session_name is None or pane.window_id is None:
                continue
            panes_by_window[(pane.session_name, pane.window_id)].append(pane)
        windows: list[TmuxWindowInfo] = []
        for (session_name, window_id), panes in panes_by_window.items():
            first = panes[0]
            ordered_panes = tuple(
                pane.pane_id
                for pane in sorted(
                    panes,
                    key=lambda candidate: (
                        -1 if candidate.pane_index is None else candidate.pane_index,
                        candidate.pane_id,
                    ),
                )
            )
            windows.append(
                TmuxWindowInfo(
                    session_name=session_name,
                    window_id=window_id,
                    window_index=first.window_index,
                    window_name=first.window_name,
                    pane_ids=ordered_panes,
                )
            )
        return tuple(
            sorted(
                windows,
                key=lambda window: (
                    window.session_name,
                    -1 if window.window_index is None else window.window_index,
                    window.window_id,
                ),
            )
        )

    def display_pane_metadata(self, target_pane: str, /) -> TmuxPaneMetadata:
        metadata = self.get_pane_metadata(target_pane)
        if metadata is None:
            command = self._build_command(
                "display-message",
                "-p",
                "-t",
                target_pane,
                "-F",
                DISPLAY_MESSAGE_FORMAT,
            )
            raise TmuxCommandError(
                _render_command(command),
                stderr=f"pane not found: {target_pane}",
            )
        return metadata

    def get_pane_metadata(self, target_pane: str, /) -> TmuxPaneMetadata | None:
        try:
            result = self._run_tmux(
                "display-message",
                "-p",
                "-t",
                target_pane,
                "-F",
                DISPLAY_MESSAGE_FORMAT,
            )
        except TmuxCommandError as exc:
            if self._is_missing_target_error(exc):
                return None
            raise
        return self._parse_pane_metadata(result)

    def capture_pane(
        self,
        target_pane: str,
        /,
        *,
        start_line: str | int | None = None,
        end_line: str | int | None = None,
        join_wrapped_lines: bool = False,
        include_escape_sequences: bool = False,
    ) -> str:
        args = ["capture-pane", "-p", "-t", target_pane]
        if join_wrapped_lines:
            args.append("-J")
        if include_escape_sequences:
            # ``-e`` asks tmux to preserve ANSI SGR sequences so the
            # viewer can re-render colours instead of stripped plain
            # text. Harmless when the caller renders without styles.
            args.append("-e")
        if start_line is not None:
            args.extend(("-S", str(start_line)))
        if end_line is not None:
            args.extend(("-E", str(end_line)))
        return self._run_tmux(*args).stdout

    def pipe_pane_to_file(
        self,
        target_pane: str,
        /,
        *,
        target_path: Path,
        append: bool = True,
    ) -> None:
        """Attach ``pipe-pane`` so new bytes append to ``target_path``.

        Uses ``-o`` so the command toggles on (vs. toggling any
        existing pipe off). The shell command deliberately uses
        ``cat >>`` for append or ``cat >`` for truncate; tmux itself
        does not expose a native "write to file" knob.
        """
        redirect = ">>" if append else ">"
        # shlex.quote the path so embedded whitespace survives the
        # shell tmux spawns to run the pipe command.
        shell_path = shlex.quote(str(target_path))
        shell_command = f"cat {redirect} {shell_path}"
        self._run_tmux("pipe-pane", "-o", "-t", target_pane, shell_command)

    def stop_pipe_pane(self, target_pane: str, /) -> None:
        """Tear down any active ``pipe-pane`` on the target.

        Calling ``pipe-pane`` with no shell command is the documented
        way to stop piping. Best-effort: we swallow
        :class:`TmuxCommandError` so closing a viewer for a pane
        that already vanished doesn't crash the screen.
        """
        try:
            self._run_tmux("pipe-pane", "-t", target_pane)
        except TmuxCommandError:
            return

    def send_keys(
        self,
        target_pane: str,
        keys: Sequence[str],
        /,
        *,
        literal: bool = False,
        append_enter: bool = False,
    ) -> CommandResult:
        if not keys and not append_enter:
            msg = "keys must not be empty"
            raise ValueError(msg)
        if literal and append_enter:
            self._run_tmux("send-keys", "-t", target_pane, "-l", *keys)
            return self._run_tmux("send-keys", "-t", target_pane, "Enter")
        args = ["send-keys", "-t", target_pane]
        if literal:
            args.append("-l")
        args.extend(keys)
        if append_enter:
            args.append("Enter")
        return self._run_tmux(*args)

    def split_window(
        self,
        target_pane: str,
        /,
        *,
        vertical: bool = True,
        start_directory: Path | None = None,
        shell_command: Sequence[str] | None = None,
        size: int | None = None,
        detached: bool = False,
    ) -> TmuxPaneMetadata:
        args = ["split-window", "-P", "-F", DISPLAY_MESSAGE_FORMAT, "-t", target_pane]
        args.append("-v" if vertical else "-h")
        if start_directory is not None:
            args.extend(("-c", str(start_directory)))
        if size is not None:
            args.extend(("-l", str(size)))
        if detached:
            args.append("-d")
        if shell_command:
            args.extend(shell_command)
        return self._parse_pane_metadata(self._run_tmux(*args))

    def new_window(
        self,
        target_session: str | None = None,
        /,
        *,
        window_name: str | None = None,
        start_directory: Path | None = None,
        shell_command: Sequence[str] | None = None,
        detached: bool = False,
    ) -> TmuxPaneMetadata:
        args = ["new-window", "-P", "-F", DISPLAY_MESSAGE_FORMAT]
        if target_session is not None:
            args.extend(("-t", target_session))
        if window_name is not None:
            args.extend(("-n", window_name))
        if start_directory is not None:
            args.extend(("-c", str(start_directory)))
        if detached:
            args.append("-d")
        if shell_command:
            args.extend(shell_command)
        return self._parse_pane_metadata(self._run_tmux(*args))

    def break_pane(
        self,
        source_pane: str,
        /,
        *,
        window_name: str | None = None,
        target_window: str | None = None,
        detached: bool = True,
    ) -> TmuxPaneMetadata:
        normalized_source = source_pane.strip()
        if not normalized_source:
            msg = "source_pane must not be empty"
            raise ValueError(msg)
        args = ["break-pane", "-P", "-F", DISPLAY_MESSAGE_FORMAT, "-s", normalized_source]
        if detached:
            args.append("-d")
        if window_name is not None:
            normalized_name = window_name.strip()
            if not normalized_name:
                msg = "window_name must not be empty"
                raise ValueError(msg)
            args.extend(("-n", normalized_name))
        if target_window is not None:
            normalized_target = target_window.strip()
            if not normalized_target:
                msg = "target_window must not be empty"
                raise ValueError(msg)
            args.extend(("-t", normalized_target))
        return self._parse_pane_metadata(self._run_tmux(*args))

    def join_pane(
        self,
        source_pane: str,
        target_pane: str,
        /,
        *,
        detached: bool = True,
        vertical: bool = True,
    ) -> TmuxPaneMetadata:
        normalized_source = source_pane.strip()
        normalized_target = target_pane.strip()
        if not normalized_source:
            msg = "source_pane must not be empty"
            raise ValueError(msg)
        if not normalized_target:
            msg = "target_pane must not be empty"
            raise ValueError(msg)
        args = ["join-pane", "-s", normalized_source, "-t", normalized_target]
        if detached:
            args.append("-d")
        args.append("-v" if vertical else "-h")
        self._run_tmux(*args)
        return self.display_pane_metadata(normalized_source)

    def rename_window(self, target_window: str, new_name: str, /) -> CommandResult:
        normalized_target = target_window.strip()
        normalized_name = new_name.strip()
        if not normalized_target:
            msg = "target_window must not be empty"
            raise ValueError(msg)
        if not normalized_name:
            msg = "new_name must not be empty"
            raise ValueError(msg)
        return self._run_tmux("rename-window", "-t", normalized_target, normalized_name)

    def kill_pane(self, target_pane: str, /) -> CommandResult:
        normalized_target = target_pane.strip()
        if not normalized_target:
            msg = "target_pane must not be empty"
            raise ValueError(msg)
        return self._run_tmux("kill-pane", "-t", normalized_target)

    def select_pane(self, target_pane: str, /) -> CommandResult:
        return self._run_tmux("select-pane", "-t", target_pane)

    def select_window(self, target_window: str, /) -> CommandResult:
        return self._run_tmux("select-window", "-t", target_window)

    def switch_client(self, target: str, /) -> CommandResult:
        """Move the calling tmux client to ``target`` (session/window/pane).

        Useful when commander is attached inside tmux and needs to move the
        user's active client to the agent's pane rather than only flipping
        the window's active pane in the background.
        """
        return self._run_tmux("switch-client", "-t", target)

    def has_attached_client(self) -> bool:
        """Return True when at least one tmux client is attached.

        ``switch-client`` is a no-op when there is no attached client on
        the configured socket, which is the cross-server failure mode we
        need to distinguish from a real focus switch.
        """
        try:
            result = self._run_tmux("list-clients", "-F", "#{client_name}")
        except TmuxCommandError:
            return False
        return bool(result.stdout.strip())

    def pane_exists(self, target_pane: str, /) -> bool:
        return self.get_pane_metadata(target_pane) is not None

    def pane_is_dead(self, target_pane: str, /) -> bool:
        metadata = self.get_pane_metadata(target_pane)
        return metadata is None or metadata.pane_dead is True

    def _parse_pane_metadata(self, result: CommandResult) -> TmuxPaneMetadata:
        parsed = parse_tmux_list_panes_output(result.stdout)
        if len(parsed.panes) != 1:
            raise TmuxCommandError(
                _render_command(result.command),
                exit_code=result.exit_code,
                stderr="tmux metadata output did not contain exactly one pane",
                stdout=result.stdout,
            )
        return TmuxPaneMetadata.from_record(parsed.panes[0])

    def _run_tmux(self, *args: str) -> CommandResult:
        command = self._build_command(*args)
        try:
            result = self._command_runner.run(command, timeout_sec=self._timeout_sec)
        except CommandError as exc:
            raise TmuxCommandError(
                exc.command,
                exit_code=exc.exit_code,
                stderr=exc.stderr,
                stdout=exc.stdout,
            ) from exc
        if result.succeeded:
            return result
        raise TmuxCommandError(
            _render_command(result.command),
            exit_code=result.exit_code,
            stderr=result.stderr,
            stdout=result.stdout,
        )

    def _build_command(self, *args: str) -> tuple[str, ...]:
        command: list[str] = [self._binary]
        if self._socket_path is not None:
            command.extend(("-S", str(self._socket_path)))
        command.extend(args)
        return tuple(command)

    def _is_missing_target_error(self, exc: TmuxCommandError) -> bool:
        stderr = (exc.stderr or "").casefold()
        return any(snippet in stderr for snippet in _MISSING_TARGET_SNIPPETS)

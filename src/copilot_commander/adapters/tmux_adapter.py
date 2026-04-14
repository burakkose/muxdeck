from __future__ import annotations

import shlex
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
from copilot_commander.types import CommandRunner

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


class TmuxAdapter:
    def __init__(
        self,
        command_runner: CommandRunner,
        *,
        binary: str = "tmux",
        timeout_sec: float = 10.0,
    ) -> None:
        if timeout_sec <= 0:
            msg = "timeout_sec must be greater than zero"
            raise ValueError(msg)
        self._command_runner = command_runner
        self._binary = binary
        self._timeout_sec = timeout_sec

    def list_panes(self) -> TmuxListPanesParseResult:
        result = self._run_tmux("list-panes", "-a", "-F", LIST_PANES_FORMAT)
        return parse_tmux_list_panes_output(result.stdout)

    def display_pane_metadata(self, target_pane: str, /) -> TmuxPaneMetadata:
        metadata = self.get_pane_metadata(target_pane)
        if metadata is None:
            command = (
                self._binary,
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
    ) -> str:
        args = ["capture-pane", "-p", "-t", target_pane]
        if join_wrapped_lines:
            args.append("-J")
        if start_line is not None:
            args.extend(("-S", str(start_line)))
        if end_line is not None:
            args.extend(("-E", str(end_line)))
        return self._run_tmux(*args).stdout

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

    def select_pane(self, target_pane: str, /) -> CommandResult:
        return self._run_tmux("select-pane", "-t", target_pane)

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
        command = (self._binary, *args)
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

    def _is_missing_target_error(self, exc: TmuxCommandError) -> bool:
        stderr = (exc.stderr or "").casefold()
        return any(snippet in stderr for snippet in _MISSING_TARGET_SNIPPETS)

from __future__ import annotations

import logging
import os
import shlex
import subprocess
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path

from muxdeck.domain.value_objects import CommandResult
from muxdeck.exceptions import CommandError
from muxdeck.types import Clock

_log = logging.getLogger(__name__)

_DEFAULT_TIMEOUT_SEC = 30.0


def _utc_now() -> datetime:
    return datetime.now(tz=UTC)


def _normalize_command(command: Sequence[str]) -> tuple[str, ...]:
    normalized = tuple(command)
    if not normalized:
        msg = "command must not be empty"
        raise ValueError(msg)
    return normalized


def _render_command(command: Sequence[str]) -> str:
    return shlex.join(command)


def _normalize_timeout(
    timeout_sec: float | None,
    *,
    default_timeout_sec: float,
) -> float:
    effective_timeout = default_timeout_sec if timeout_sec is None else timeout_sec
    if effective_timeout <= 0:
        msg = "timeout_sec must be greater than zero"
        raise ValueError(msg)
    return effective_timeout


def _build_environment(env: Mapping[str, str] | None) -> dict[str, str] | None:
    if env is None:
        return None
    return {**os.environ, **env}


class ProcessAdapter:
    def __init__(
        self,
        *,
        clock: Clock = _utc_now,
        default_timeout_sec: float = _DEFAULT_TIMEOUT_SEC,
    ) -> None:
        self._clock = clock
        self._default_timeout_sec = _normalize_timeout(
            default_timeout_sec,
            default_timeout_sec=default_timeout_sec,
        )

    def run(
        self,
        command: Sequence[str],
        /,
        *,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        timeout_sec: float | None = None,
    ) -> CommandResult:
        normalized_command = _normalize_command(command)
        effective_timeout = _normalize_timeout(
            timeout_sec,
            default_timeout_sec=self._default_timeout_sec,
        )
        started_at = self._clock()
        command_text = _render_command(normalized_command)
        try:
            with subprocess.Popen(
                normalized_command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=None if cwd is None else str(cwd),
                env=_build_environment(env),
                text=True,
            ) as process:
                try:
                    stdout, stderr = process.communicate(timeout=effective_timeout)
                except subprocess.TimeoutExpired as exc:
                    process.kill()
                    timeout_stdout, timeout_stderr = process.communicate()
                    stdout = (
                        exc.stdout if isinstance(exc.stdout, str) else (exc.stdout or b"").decode()
                    ) or timeout_stdout
                    stderr = (
                        exc.stderr if isinstance(exc.stderr, str) else (exc.stderr or b"").decode()
                    ) or timeout_stderr
                    timeout_message = f"timed out after {effective_timeout:.3f}s"
                    raise CommandError(
                        command_text,
                        stderr=timeout_message if not stderr else f"{timeout_message}; {stderr}",
                        stdout=stdout,
                    ) from exc
                exit_code = process.returncode
        except (FileNotFoundError, PermissionError) as exc:
            raise CommandError(command_text, stderr=str(exc)) from exc
        finished_at = self._clock()
        return CommandResult(
            command=normalized_command,
            exit_code=exit_code,
            started_at=started_at,
            finished_at=finished_at,
            stdout=stdout,
            stderr=stderr,
            cwd=cwd,
        )

    def get_child_cmdlines(self, pid: int, /) -> tuple[str, ...]:
        """Read command lines of descendant processes from ``/proc``.

        Walks the process tree up to 4 levels deep and returns each
        descendant's full command line as a space-joined string.
        Silently ignores processes that have exited or are unreadable.
        """
        result: list[str] = []
        self._collect_child_cmdlines(pid, result, depth=0, max_depth=4)
        return tuple(result)

    def _collect_child_cmdlines(
        self,
        pid: int,
        result: list[str],
        depth: int,
        max_depth: int,
    ) -> None:
        if depth >= max_depth:
            return
        try:
            children_text = Path(f"/proc/{pid}/task/{pid}/children").read_text()
        except OSError:
            return
        for token in children_text.split():
            try:
                child_pid = int(token)
            except ValueError:
                continue
            try:
                raw = Path(f"/proc/{child_pid}/cmdline").read_text()
                cmdline = raw.replace("\0", " ").strip()
                if cmdline:
                    result.append(cmdline)
            except OSError:
                _log.debug("cannot read /proc/%d/cmdline", child_pid)
            self._collect_child_cmdlines(child_pid, result, depth + 1, max_depth)

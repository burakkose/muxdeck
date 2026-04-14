from __future__ import annotations

import os
import shlex
import subprocess
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path

from copilot_commander.domain.value_objects import CommandResult
from copilot_commander.exceptions import CommandError
from copilot_commander.types import Clock

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
            completed = subprocess.run(
                normalized_command,
                capture_output=True,
                check=False,
                cwd=None if cwd is None else str(cwd),
                env=_build_environment(env),
                text=True,
                timeout=effective_timeout,
            )
        except (FileNotFoundError, PermissionError) as exc:
            raise CommandError(command_text, stderr=str(exc)) from exc
        except subprocess.TimeoutExpired as exc:
            stdout = exc.stdout if isinstance(exc.stdout, str) else (exc.stdout or b"").decode()
            stderr = exc.stderr if isinstance(exc.stderr, str) else (exc.stderr or b"").decode()
            timeout_message = f"timed out after {effective_timeout:.3f}s"
            raise CommandError(
                command_text,
                stderr=timeout_message if not stderr else f"{timeout_message}; {stderr}",
                stdout=stdout,
            ) from exc
        finished_at = self._clock()
        return CommandResult(
            command=normalized_command,
            exit_code=completed.returncode,
            started_at=started_at,
            finished_at=finished_at,
            stdout=completed.stdout,
            stderr=completed.stderr,
            cwd=cwd,
        )

"""Narrow adapter producing unified diffs for replay file mutations.

The replay screen shows inline diffs alongside file-mutation entries.
We deliberately keep this adapter independent of :mod:`git_adapter`:

* Its surface is a single, focused ``diff_for_path`` method.
* Failures degrade to an empty string instead of raising — replay must
  never crash because ``git`` could not resolve a revision.
* A :class:`NullDiffAdapter` lets callers (sessions without a git
  context) opt out without conditional plumbing.

Both implementations satisfy the structural :class:`DiffPort`
``Protocol`` so the screen / service composition can depend on the
narrow type.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Protocol


class DiffPort(Protocol):
    """Structural type for diff resolution."""

    def diff_for_path(
        self,
        repo_path: Path,
        path: str,
        *,
        before: str | None,
        after: str | None,
    ) -> str: ...


class DiffAdapter:
    """Resolve unified diffs by shelling out to ``git diff``."""

    def __init__(self, *, binary: str = "git", timeout_sec: float = 5.0) -> None:
        if timeout_sec <= 0:
            msg = "timeout_sec must be greater than zero"
            raise ValueError(msg)
        self._binary = binary
        self._timeout_sec = timeout_sec

    def diff_for_path(
        self,
        repo_path: Path,
        path: str,
        *,
        before: str | None,
        after: str | None,
    ) -> str:
        """Return a unified diff for ``path`` in ``repo_path``.

        When both ``before`` and ``after`` are provided, runs
        ``git diff before..after -- path``. Otherwise falls back to
        ``git diff -- path`` against the working tree. On any failure
        (non-zero exit, timeout, missing binary) returns an empty
        string so the caller can render a graceful placeholder.
        """
        if not path:
            return ""
        command: list[str] = [self._binary, "--no-pager", "diff", "--no-color"]
        if before is not None and after is not None:
            command.append(f"{before}..{after}")
        command.extend(("--", path))
        try:
            completed = subprocess.run(
                command,
                cwd=repo_path,
                check=False,
                text=True,
                capture_output=True,
                timeout=self._timeout_sec,
            )
        except (OSError, subprocess.TimeoutExpired):
            return ""
        if completed.returncode != 0:
            return ""
        return completed.stdout


class NullDiffAdapter:
    """Diff resolver for sessions without a git context.

    Always returns an empty string; the widget renders a muted
    placeholder in that case.
    """

    def diff_for_path(
        self,
        repo_path: Path,
        path: str,
        *,
        before: str | None,
        after: str | None,
    ) -> str:
        del repo_path, path, before, after
        return ""


__all__ = ["DiffAdapter", "DiffPort", "NullDiffAdapter"]

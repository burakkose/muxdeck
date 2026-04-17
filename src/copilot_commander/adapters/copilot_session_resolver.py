"""Resolve Copilot session ids from OS state.

The Copilot CLI creates ``<session-root>/<session_id>/inuse.<pid>.lock``
while a session is attached. The ``<pid>`` belongs to the ``copilot``
Node process — always a descendant of the tmux pane's shell (or of a
``pwsh`` wrapper on WSL-to-Windows). muxdeck's agent row records the
pane's shell pid, not the inner copilot pid, so matching a pane to a
session requires a descendant-of check.

This adapter keeps the mechanism behind a narrow interface so the
dashboard and future session-association fixes share a single code
path, and so the reader still works in environments where ``/proc``
isn't usable (tests, non-Linux).
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

_log = logging.getLogger(__name__)


class _RootProvider(Protocol):
    @property
    def session_state_dir(self) -> Path: ...

    @property
    def extra_roots(self) -> tuple[object, ...]: ...


@dataclass(slots=True)
class InuseLockResolver:
    """Map a pane pid to a Copilot session id via ``inuse.<pid>.lock``.

    Only the Linux ``/proc`` layout is supported today; other kernels
    silently return ``None`` rather than raising — the dashboard treats
    that as "no sub-agents to show" which is the correct degraded
    behavior.
    """

    store: _RootProvider
    proc_dir: Path = field(default_factory=lambda: Path("/proc"))
    _max_ancestors: int = 16

    def resolve_for_pid(self, pane_pid: int | None) -> str | None:
        if pane_pid is None or pane_pid <= 0:
            return None
        have_proc = self.proc_dir.is_dir()
        for session_dir, lock_pid in self._iter_locks():
            if lock_pid == pane_pid:
                return session_dir.name
            if have_proc and self._is_descendant(lock_pid, pane_pid):
                return session_dir.name
        return None

    def _iter_locks(self) -> Iterable[tuple[Path, int]]:
        for root in self._roots():
            try:
                entries = list(root.iterdir())
            except OSError:
                continue
            for session_dir in entries:
                if not session_dir.is_dir():
                    continue
                try:
                    lock_files = list(session_dir.glob("inuse.*.lock"))
                except OSError:
                    continue
                for lock in lock_files:
                    pid = _parse_lock_pid(lock.name)
                    if pid is not None:
                        yield session_dir, pid

    def _roots(self) -> Iterable[Path]:
        yield self.store.session_state_dir
        for extra in self.store.extra_roots:
            path = getattr(extra, "path", None)
            if isinstance(path, Path):
                yield path

    def _is_descendant(self, candidate_pid: int, ancestor_pid: int) -> bool:
        """Return True if ``ancestor_pid`` appears in the ppid chain of

        ``candidate_pid`` within :attr:`_max_ancestors` hops.
        """
        current = candidate_pid
        for _ in range(self._max_ancestors):
            ppid = self._read_ppid(current)
            if ppid is None or ppid <= 1:
                return False
            if ppid == ancestor_pid:
                return True
            current = ppid
        return False

    def _read_ppid(self, pid: int) -> int | None:
        status = self.proc_dir / str(pid) / "status"
        try:
            with status.open("r", encoding="utf-8") as fh:
                for line in fh:
                    if line.startswith("PPid:"):
                        parts = line.split()
                        if len(parts) >= 2 and parts[1].isdigit():
                            return int(parts[1])
                        return None
        except OSError:
            return None
        return None


def _parse_lock_pid(name: str) -> int | None:
    if not name.startswith("inuse.") or not name.endswith(".lock"):
        return None
    middle = name[len("inuse.") : -len(".lock")]
    return int(middle) if middle.isdigit() else None

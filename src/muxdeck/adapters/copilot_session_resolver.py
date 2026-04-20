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
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Protocol

from muxdeck.adapters.tmux_adapter import parse_tmux_socket_path

_log = logging.getLogger(__name__)

# Matches ``--resume=<uuid>`` or ``--resume <uuid>`` inside a
# ``/proc/<pid>/cmdline`` read (args separated by NUL bytes, which we
# normalise to spaces before searching).
_RESUME_RE = re.compile(
    r"--resume[=\s]+"
    r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})"
)

# Process is considered a Copilot CLI host when any of these tokens
# appear in its cmdline. We match on basename + package path rather
# than the literal ``copilot`` so a shell named ``copilot-foo.sh``
# doesn't falsely qualify.
_COPILOT_CMDLINE_MARKERS = (
    "@github/copilot",
    "/bin/copilot",
    "copilot-linux-x64/copilot",
    "copilot-linux-arm64/copilot",
    "copilot-darwin-x64/copilot",
    "copilot-darwin-arm64/copilot",
    "copilot-win32-x64/copilot",
)


class _RootProvider(Protocol):
    @property
    def session_state_dir(self) -> Path: ...

    @property
    def extra_roots(self) -> tuple[object, ...]: ...


@dataclass(frozen=True, slots=True)
class CopilotSessionResolution:
    session_id: str | None = None
    state: Literal["resolved", "ambiguous", "missing"] = "missing"

    @property
    def is_ambiguous(self) -> bool:
        return self.state == "ambiguous"


@dataclass(frozen=True, slots=True)
class ResolvedCopilotTarget:
    session_id: str
    pane_id: str | None = None
    socket_path: Path | None = None


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

    def resolve(self, pane_pid: int | None) -> CopilotSessionResolution:
        """Return the live-session resolution for ``pane_pid``.

        Stale ``inuse.<pid>.lock`` files are the dominant failure mode
        here: Copilot does not always clean up its lock on exit, and
        the OS eventually reuses those pids for *other* Copilot runs
        belonging to completely different sessions. Trusting the
        lock's parent directory in that case points the sub-agent
        reader at an unrelated ``events.jsonl`` and the dashboard
        renders foreign sub-agents under the wrong agent.

        To avoid that we validate every live lock pid:

        * the pid must still exist in ``/proc``;
        * its cmdline must look like a Copilot CLI process (so a
          reused pid hosting an unrelated binary is rejected);
        * if the cmdline carries ``--resume=<uuid>`` we trust *that*
          uuid over the lock path — the lock path lies when Copilot
          re-used the pid without cleaning the old lock;
        * finally, the pid must genuinely be a descendant (or equal)
          of the pane pid.

        On kernels without ``/proc`` we degrade to the old lock-path
        behaviour — tests still exercise the old branch and
        non-Linux hosts don't have a better signal anyway.
        """
        exact_matches, descendant_matches = self._collect_matches(pane_pid)
        exact_resolution = _resolution_from_matches(exact_matches)
        if exact_resolution.state != "missing":
            return exact_resolution
        return _resolution_from_matches(descendant_matches)

    def resolve_for_pid(self, pane_pid: int | None) -> str | None:
        return self.resolve(pane_pid).session_id

    def resolve_target_for_pid(self, pane_pid: int | None) -> ResolvedCopilotTarget | None:
        exact_matches, descendant_matches = self._collect_matches(pane_pid)
        exact_target = _target_from_matches(exact_matches)
        if exact_target is not None:
            return exact_target
        return _target_from_matches(descendant_matches)

    def _collect_matches(
        self,
        pane_pid: int | None,
    ) -> tuple[list[ResolvedCopilotTarget], list[ResolvedCopilotTarget]]:
        if pane_pid is None or pane_pid <= 0:
            return [], []
        have_proc = self.proc_dir.is_dir()
        exact_matches: list[ResolvedCopilotTarget] = []
        descendant_matches: list[ResolvedCopilotTarget] = []
        for session_dir, lock_pid in self._iter_locks():
            if not have_proc:
                if lock_pid == pane_pid:
                    exact_matches.append(ResolvedCopilotTarget(session_id=session_dir.name))
                continue
            cmdline = self._read_cmdline(lock_pid)
            if cmdline is None:
                # Dead lock pid — the session this file points at is
                # almost certainly gone, and even if it's not, we
                # have no way to verify ownership without /proc.
                continue
            if not _looks_like_copilot(cmdline):
                # Pid was recycled for an unrelated process. The
                # lock file is a fossil; ignore it.
                continue
            effective_session = _extract_resume_session(cmdline) or session_dir.name
            target = self._build_target(effective_session, lock_pid)
            if lock_pid == pane_pid:
                exact_matches.append(target)
                continue
            if self._is_descendant(lock_pid, pane_pid):
                descendant_matches.append(target)
        return exact_matches, descendant_matches

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

    def _read_cmdline(self, pid: int) -> str | None:
        """Return ``/proc/<pid>/cmdline`` with NULs normalised to spaces.

        Returns ``None`` when the pid no longer exists — the caller
        uses that as the signal that a lock file is stale.
        """
        cmdline_path = self.proc_dir / str(pid) / "cmdline"
        try:
            raw = cmdline_path.read_bytes()
        except OSError:
            return None
        if not raw:
            # Kernel threads and zombie processes have empty cmdlines.
            # Treat them as "not copilot" — we can't verify identity.
            return ""
        return raw.replace(b"\x00", b" ").decode("utf-8", errors="replace").strip()

    def _build_target(self, session_id: str, pid: int) -> ResolvedCopilotTarget:
        environ = self._read_environ(pid)
        return ResolvedCopilotTarget(
            session_id=session_id,
            pane_id=environ.get("TMUX_PANE"),
            socket_path=parse_tmux_socket_path(environ.get("TMUX")),
        )

    def _read_environ(self, pid: int) -> dict[str, str]:
        environ_path = self.proc_dir / str(pid) / "environ"
        try:
            raw = environ_path.read_bytes()
        except OSError:
            return {}
        values: dict[str, str] = {}
        for chunk in raw.split(b"\x00"):
            if not chunk or b"=" not in chunk:
                continue
            key, value = chunk.split(b"=", 1)
            values[key.decode("utf-8", errors="replace")] = value.decode(
                "utf-8",
                errors="replace",
            )
        return values


def _parse_lock_pid(name: str) -> int | None:
    if not name.startswith("inuse.") or not name.endswith(".lock"):
        return None
    middle = name[len("inuse.") : -len(".lock")]
    return int(middle) if middle.isdigit() else None


def _looks_like_copilot(cmdline: str) -> bool:
    """Return True when ``cmdline`` appears to host the Copilot CLI.

    We check against a small allow-list of install-path substrings
    rather than matching the word ``copilot`` anywhere — otherwise a
    shell script or editor buffer path containing "copilot" would
    falsely validate a recycled pid.
    """
    if not cmdline:
        return False
    return any(marker in cmdline for marker in _COPILOT_CMDLINE_MARKERS)


def _extract_resume_session(cmdline: str) -> str | None:
    """Extract ``--resume=<uuid>`` from a Copilot cmdline, if present."""
    match = _RESUME_RE.search(cmdline)
    return match.group(1) if match else None


def _resolution_from_matches(
    matches: Iterable[ResolvedCopilotTarget],
) -> CopilotSessionResolution:
    unique: list[str] = []
    seen: set[str] = set()
    for match in matches:
        session_id = match.session_id
        if session_id in seen:
            continue
        seen.add(session_id)
        unique.append(session_id)
    if len(unique) == 1:
        return CopilotSessionResolution(session_id=unique[0], state="resolved")
    if unique:
        return CopilotSessionResolution(state="ambiguous")
    return CopilotSessionResolution()


def _target_from_matches(
    matches: Iterable[ResolvedCopilotTarget],
) -> ResolvedCopilotTarget | None:
    merged: dict[str, ResolvedCopilotTarget] = {}
    for match in matches:
        existing = merged.get(match.session_id)
        if existing is None:
            merged[match.session_id] = match
            continue
        merged[match.session_id] = ResolvedCopilotTarget(
            session_id=match.session_id,
            pane_id=existing.pane_id or match.pane_id,
            socket_path=existing.socket_path or match.socket_path,
        )
    if len(merged) == 1:
        return next(iter(merged.values()))
    return None

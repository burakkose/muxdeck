"""Read-only adapter for Copilot CLI local session storage.

Scans one or more ``.copilot/session-state/`` directories to discover
local sessions. When muxdeck runs inside WSL the store is pointed at
both the Linux home and the Windows-side ``%USERPROFILE%\\.copilot\\
session-state`` directory so sessions started from ``pwsh`` are
visible alongside WSL-native ones.
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

_log = logging.getLogger(__name__)

_DEFAULT_SESSION_DIR = Path.home() / ".copilot" / "session-state"

_CLEANLY_CLOSED_EVENTS = frozenset({"session.shutdown"})

SessionOrigin = Literal["local", "windows"]


@dataclass(frozen=True, slots=True)
class CopilotLocalSession:
    """Metadata about a Copilot CLI session discovered from disk."""

    session_id: str
    cwd: Path | None = None
    git_root: Path | None = None
    repository: str | None = None
    branch: str | None = None
    summary: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    last_event_type: str | None = None
    last_event_at: datetime | None = None
    checkpoint_count: int = 0
    is_cleanly_closed: bool = False
    origin: SessionOrigin = "local"
    # Verbatim Windows-style paths (``C:\Users\...``) preserved from
    # ``workspace.yaml`` so the resume command can hand them to pwsh
    # without re-translating from the WSL mount.
    windows_cwd: str | None = None
    windows_git_root: str | None = None


@dataclass(frozen=True, slots=True)
class SessionStoreRoot:
    """A directory to scan and the origin tag to stamp on its sessions."""

    path: Path
    origin: SessionOrigin = "local"


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        cleaned = value.strip()
        if cleaned.endswith("Z"):
            cleaned = cleaned[:-1] + "+00:00"
        return datetime.fromisoformat(cleaned)
    except (ValueError, TypeError):
        return None


def _parse_workspace_yaml(path: Path) -> dict[str, str]:
    """Minimal YAML parser for flat key: value files (no nesting)."""
    result: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return result
    for line in text.splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip()
        if key and value:
            result[key] = value
    return result


def _read_last_valid_event(events_path: Path, max_bytes: int = 8192) -> dict[str, object] | None:
    """Read the last valid JSON event from an events.jsonl file.

    Reads from the tail of the file to handle large files efficiently.
    Handles truncated/partial last lines from crashes.
    """
    try:
        file_size = events_path.stat().st_size
    except OSError:
        return None
    if file_size == 0:
        return None

    try:
        with events_path.open("rb") as fh:
            read_size = min(max_bytes, file_size)
            fh.seek(max(0, file_size - read_size))
            tail = fh.read(read_size).decode("utf-8", errors="replace")
    except OSError:
        return None

    # Scan lines in reverse to find last valid JSON
    lines = tail.splitlines()
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            return json.loads(line)  # type: ignore[no-any-return]
        except json.JSONDecodeError:
            continue
    return None


def _count_checkpoints(session_dir: Path) -> int:
    cp_dir = session_dir / "checkpoints"
    if not cp_dir.is_dir():
        return 0
    return sum(1 for f in cp_dir.iterdir() if f.suffix == ".md" and f.name != "index.md")


def _is_windows_style_path(value: str) -> bool:
    """Heuristic: drive-letter absolute paths like ``C:\\foo`` or ``C:/foo``."""
    return len(value) >= 3 and value[1:3] in (":\\", ":/")


def _parse_session_dir(
    session_dir: Path,
    *,
    origin: SessionOrigin = "local",
) -> CopilotLocalSession | None:
    """Parse a single session directory into a CopilotLocalSession."""
    workspace_path = session_dir / "workspace.yaml"
    if not workspace_path.exists():
        return None

    ws = _parse_workspace_yaml(workspace_path)
    session_id = ws.get("id", session_dir.name)

    cwd_str = ws.get("cwd")
    git_root_str = ws.get("git_root")

    # Windows sessions persist ``cwd`` in native form (``C:\Users\...``).
    # Preserve the raw strings so resume can feed them back to pwsh,
    # while still building a ``Path`` for POSIX-side consumers.
    windows_cwd: str | None = None
    windows_git_root: str | None = None
    if origin == "windows":
        if cwd_str and _is_windows_style_path(cwd_str):
            windows_cwd = cwd_str
        if git_root_str and _is_windows_style_path(git_root_str):
            windows_git_root = git_root_str

    # Parse last event
    events_path = session_dir / "events.jsonl"
    last_event = _read_last_valid_event(events_path)
    last_event_type: str | None = None
    last_event_at: datetime | None = None
    if last_event is not None:
        last_event_type = last_event.get("type")  # type: ignore[assignment]
        ts = last_event.get("timestamp")
        if isinstance(ts, str):
            last_event_at = _parse_iso(ts)

    is_closed = last_event_type in _CLEANLY_CLOSED_EVENTS

    return CopilotLocalSession(
        session_id=session_id,
        cwd=Path(cwd_str) if cwd_str else None,
        git_root=Path(git_root_str) if git_root_str else None,
        repository=ws.get("repository"),
        branch=ws.get("branch"),
        summary=ws.get("summary"),
        created_at=_parse_iso(ws.get("created_at")),
        updated_at=_parse_iso(ws.get("updated_at")),
        last_event_type=last_event_type,
        last_event_at=last_event_at,
        checkpoint_count=_count_checkpoints(session_dir),
        is_cleanly_closed=is_closed,
        origin=origin,
        windows_cwd=windows_cwd,
        windows_git_root=windows_git_root,
    )


@dataclass(slots=True)
class CopilotSessionStore:
    """Cached, read-only store for local Copilot CLI sessions.

    Scans one or more ``.copilot/session-state/`` roots and caches
    results with a TTL. In WSL the second root typically points at the
    Windows-side session-state directory so ``pwsh``-launched sessions
    are visible next to WSL-native ones.
    """

    session_state_dir: Path = field(default_factory=lambda: _DEFAULT_SESSION_DIR)
    max_age_days: int = 60
    cache_ttl_sec: float = 30.0
    extra_roots: tuple[SessionStoreRoot, ...] = ()

    _cache: list[CopilotLocalSession] = field(default_factory=list, init=False, repr=False)
    _cache_time: float = field(default=0.0, init=False, repr=False)

    def discover(self, *, force: bool = False) -> list[CopilotLocalSession]:
        """Return all local sessions, using cache if fresh."""
        now = time.monotonic()
        if not force and self._cache and (now - self._cache_time) < self.cache_ttl_sec:
            return list(self._cache)
        self._cache = self._scan()
        self._cache_time = now
        return list(self._cache)

    def set_extra_roots(self, roots: Sequence[SessionStoreRoot]) -> None:
        """Replace the secondary roots and invalidate the cache."""
        self.extra_roots = tuple(roots)
        self._cache = []
        self._cache_time = 0.0

    def get_session(self, session_id: str) -> CopilotLocalSession | None:
        """Look up a single session by ID."""
        for s in self.discover():
            if s.session_id == session_id:
                return s
        return None

    def count_by_origin(self, origin: SessionOrigin) -> int:
        """Count cached (or freshly-scanned) sessions for a given origin."""
        return sum(1 for s in self.discover() if s.origin == origin)

    def _iter_roots(self) -> list[SessionStoreRoot]:
        roots: list[SessionStoreRoot] = [SessionStoreRoot(self.session_state_dir, "local")]
        seen: set[Path] = set()
        for root in roots:
            seen.add(root.path)
        for extra in self.extra_roots:
            if extra.path in seen:
                continue
            seen.add(extra.path)
            roots.append(extra)
        return roots

    def _scan(self) -> list[CopilotLocalSession]:
        cutoff: datetime | None = None
        if self.max_age_days > 0:
            from datetime import timedelta

            cutoff = datetime.now(UTC) - timedelta(days=self.max_age_days)

        sessions: list[CopilotLocalSession] = []
        for root in self._iter_roots():
            sessions.extend(self._scan_root(root, cutoff=cutoff))

        # Deduplicate by session_id — the local root wins when the same
        # id shows up on both sides (shouldn't happen, but be defensive
        # against mounted paths overlapping).
        by_id: dict[str, CopilotLocalSession] = {}
        for session in sessions:
            existing = by_id.get(session.session_id)
            if existing is None or (existing.origin == "windows" and session.origin == "local"):
                by_id[session.session_id] = session
        deduped = list(by_id.values())

        deduped.sort(
            key=lambda s: s.updated_at or s.created_at or datetime.min.replace(tzinfo=UTC),
            reverse=True,
        )
        return deduped

    def _scan_root(
        self,
        root: SessionStoreRoot,
        *,
        cutoff: datetime | None,
    ) -> list[CopilotLocalSession]:
        if not root.path.is_dir():
            _log.debug("session state dir does not exist: %s", root.path)
            return []

        try:
            entries = [e for e in root.path.iterdir() if e.is_dir()]
        except OSError:
            _log.warning("failed to list session state dir: %s", root.path)
            return []

        if not entries:
            return []

        def _parse_one(entry: Path) -> CopilotLocalSession | None:
            try:
                return _parse_session_dir(entry, origin=root.origin)
            except Exception:
                _log.debug("failed to parse session dir: %s", entry.name, exc_info=True)
                return None

        # Parse entries in parallel. Each entry reads workspace.yaml and
        # tails events.jsonl — I/O-bound work that benefits sharply from
        # threading, especially on WSL's /mnt/c 9P mount where per-file
        # latency dominates. Keep the worker count bounded so we don't
        # swamp the filesystem or hold the GIL under contention.
        max_workers = min(16, max(2, (os.cpu_count() or 4) * 2), len(entries))
        sessions: list[CopilotLocalSession] = []
        with ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="copilot-session-scan",
        ) as pool:
            for session in pool.map(_parse_one, entries):
                if session is None:
                    continue
                if (
                    cutoff is not None
                    and session.updated_at is not None
                    and session.updated_at < cutoff
                ):
                    continue
                sessions.append(session)
        return sessions


__all__ = [
    "CopilotLocalSession",
    "CopilotSessionStore",
    "SessionOrigin",
    "SessionStoreRoot",
]

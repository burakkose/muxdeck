"""Read-only adapter for Copilot CLI local session storage.

Scans ``~/.copilot/session-state/`` to discover all local sessions and
parse their workspace metadata and last-event status.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

_log = logging.getLogger(__name__)

_DEFAULT_SESSION_DIR = Path.home() / ".copilot" / "session-state"

_CLEANLY_CLOSED_EVENTS = frozenset({"session.shutdown"})


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


def _parse_session_dir(session_dir: Path) -> CopilotLocalSession | None:
    """Parse a single session directory into a CopilotLocalSession."""
    workspace_path = session_dir / "workspace.yaml"
    if not workspace_path.exists():
        return None

    ws = _parse_workspace_yaml(workspace_path)
    session_id = ws.get("id", session_dir.name)

    cwd_str = ws.get("cwd")
    git_root_str = ws.get("git_root")

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
    )


@dataclass(slots=True)
class CopilotSessionStore:
    """Cached, read-only store for local Copilot CLI sessions.

    Scans ``~/.copilot/session-state/`` and caches results with a TTL.
    """

    session_state_dir: Path = field(default_factory=lambda: _DEFAULT_SESSION_DIR)
    max_age_days: int = 60
    cache_ttl_sec: float = 30.0

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

    def get_session(self, session_id: str) -> CopilotLocalSession | None:
        """Look up a single session by ID."""
        for s in self.discover():
            if s.session_id == session_id:
                return s
        return None

    def _scan(self) -> list[CopilotLocalSession]:
        if not self.session_state_dir.is_dir():
            _log.debug("session state dir does not exist: %s", self.session_state_dir)
            return []

        cutoff: datetime | None = None
        if self.max_age_days > 0:
            cutoff = datetime.now(UTC) - __import__("datetime").timedelta(days=self.max_age_days)

        sessions: list[CopilotLocalSession] = []
        try:
            entries = list(self.session_state_dir.iterdir())
        except OSError:
            _log.warning("failed to list session state dir: %s", self.session_state_dir)
            return []

        for entry in entries:
            if not entry.is_dir():
                continue
            try:
                session = _parse_session_dir(entry)
            except Exception:
                _log.debug("failed to parse session dir: %s", entry.name, exc_info=True)
                continue
            if session is None:
                continue
            # Filter by age
            if (
                cutoff is not None
                and session.updated_at is not None
                and session.updated_at < cutoff
            ):
                continue
            sessions.append(session)

        # Sort by updated_at desc (most recent first)
        sessions.sort(
            key=lambda s: s.updated_at or s.created_at or datetime.min.replace(tzinfo=UTC),
            reverse=True,
        )
        return sessions


__all__ = ["CopilotLocalSession", "CopilotSessionStore"]

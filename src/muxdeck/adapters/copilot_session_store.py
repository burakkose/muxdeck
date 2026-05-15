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
import threading
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
class CopilotSessionUsage:
    """Aggregated usage copied from a ``session.shutdown`` event."""

    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    premium_requests: int | None = None

    @property
    def total_tokens(self) -> int | None:
        parts = tuple(
            value
            for value in (
                self.input_tokens,
                self.output_tokens,
                self.cache_read_tokens,
                self.cache_write_tokens,
            )
            if value is not None
        )
        if not parts:
            return None
        return sum(parts)


@dataclass(frozen=True, slots=True)
class CopilotLocalSession:
    """Metadata about a Copilot CLI session discovered from disk."""

    session_id: str
    cwd: Path | None = None
    git_root: Path | None = None
    repository: str | None = None
    branch: str | None = None
    # ``name`` is the canonical Copilot CLI session title written to
    # ``workspace.yaml`` for newer sessions (set by the ``/name``
    # command or auto-generated). ``summary`` is the older field that
    # some sessions still carry, sometimes alongside an identical
    # ``name``. Prefer ``name`` when surfacing a label; fall back to
    # ``summary`` so historical sessions remain identifiable.
    name: str | None = None
    summary: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    last_event_type: str | None = None
    last_event_at: datetime | None = None
    checkpoint_count: int = 0
    usage: CopilotSessionUsage | None = None
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
    """Minimal YAML parser for flat key: value files.

    Copilot CLI writes multi-line fields — notably ``summary`` for
    agents launched through the ACP backend — using YAML block scalar
    syntax (``|``, ``|-``, ``|+``, ``>``, ``>-``, ``>+``). The previous
    partition-on-first-colon approach stored the scalar indicator
    literally (``summary: |-``), so those sessions surfaced the string
    ``|-`` as their summary in the UI.

    This parser stays dependency-free but correctly collects indented
    continuation lines after a block-scalar indicator. The parser is
    intentionally limited to the shape Copilot CLI emits — flat
    top-level keys, no nesting, no anchors, no aliases — so anything
    that looks like a YAML feature beyond block scalars is still
    passed through unchanged.
    """
    result: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return result

    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        # Top-level keys start at column 0; anything indented here
        # without a preceding block-scalar indicator is malformed for
        # our flat schema and is silently skipped.
        if not stripped or stripped.startswith("#") or line[:1].isspace() or ":" not in line:
            i += 1
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip()
        # Strip an inline trailing comment so e.g. ``key: value # note``
        # doesn't capture the comment as part of the value.
        if value and not value.startswith(("|", ">")):
            # Only strip comments when clearly separated; avoid cutting
            # inside unquoted URLs or timestamps.
            comment_at = value.find(" #")
            if comment_at != -1:
                value = value[:comment_at].rstrip()
        if not key:
            i += 1
            continue
        if value in {"|", "|-", "|+", ">", ">-", ">+"}:
            indicator = value
            block_lines, consumed = _read_block_scalar(lines, i + 1)
            result[key] = _apply_block_scalar(block_lines, indicator)
            i += 1 + consumed
            continue
        if value:
            # Drop surrounding quotes that the minimal parser would
            # otherwise leak into downstream UI.
            if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
                value = value[1:-1]
            result[key] = value
        i += 1
    return result


def _read_block_scalar(lines: list[str], start: int) -> tuple[list[str], int]:
    """Collect the indented continuation lines of a YAML block scalar.

    Returns the raw block lines (with their original leading whitespace)
    and the number of source lines consumed. The block ends at the first
    non-empty line that is not indented relative to column 0 — this
    matches Copilot CLI's flat schema where the next top-level key marks
    the end of the scalar.
    """
    collected: list[str] = []
    j = start
    while j < len(lines):
        line = lines[j]
        if line == "":
            collected.append("")
            j += 1
            continue
        if not line[:1].isspace():
            break
        collected.append(line)
        j += 1
    # Trim trailing empty lines so they don't get mistakenly included
    # when we strip or clip later.
    while collected and collected[-1] == "":
        collected.pop()
    return collected, j - start


def _apply_block_scalar(block_lines: list[str], indicator: str) -> str:
    """Apply chomping and folding rules for the supported indicators.

    * ``|`` / ``|-`` / ``|+`` — literal block: preserve line breaks.
    * ``>`` / ``>-`` / ``>+`` — folded block: collapse single newlines
      between non-empty content lines into single spaces; keep blank
      lines as hard breaks.

    Chomping:
      * ``-`` strips every trailing newline (default in our output).
      * ``+`` keeps them all.
      * bare indicator keeps a single trailing newline.

    The parser doesn't need to preserve trailing newlines for the UI's
    purposes, so we emit the scalar without any trailing ``\\n`` and
    let callers decide whether to keep blank-line structure.
    """
    if not block_lines:
        return ""
    indent = min(
        (len(line) - len(line.lstrip(" ")) for line in block_lines if line.strip()),
        default=0,
    )
    dedented = [line[indent:] if len(line) >= indent else line for line in block_lines]
    literal = indicator.startswith("|")
    if literal:
        return "\n".join(dedented).rstrip("\n")
    # Folded: collapse runs of non-empty lines into space-separated
    # paragraphs, preserve empty lines as paragraph breaks.
    paragraphs: list[str] = []
    buffer: list[str] = []
    for line in dedented:
        if line == "":
            if buffer:
                paragraphs.append(" ".join(buffer))
                buffer = []
            paragraphs.append("")
        else:
            buffer.append(line)
    if buffer:
        paragraphs.append(" ".join(buffer))
    return "\n".join(paragraphs).rstrip("\n")


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


def _as_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        cleaned = value.strip().replace(",", "")
        if cleaned.startswith("-"):
            cleaned = cleaned[1:]
        if cleaned.isdigit():
            return int(value.strip().replace(",", ""))
    return None


def _extract_session_usage(last_event: dict[str, object] | None) -> CopilotSessionUsage | None:
    if last_event is None or last_event.get("type") not in _CLEANLY_CLOSED_EVENTS:
        return None
    data = last_event.get("data")
    if not isinstance(data, dict):
        return None

    model_metrics = data.get("modelMetrics")
    input_tokens = 0
    output_tokens = 0
    cache_read_tokens = 0
    cache_write_tokens = 0
    saw_usage = False
    if isinstance(model_metrics, dict):
        for details in model_metrics.values():
            if not isinstance(details, dict):
                continue
            usage = details.get("usage")
            if not isinstance(usage, dict):
                continue
            input_tokens += _as_int(usage.get("inputTokens")) or 0
            output_tokens += _as_int(usage.get("outputTokens")) or 0
            cache_read_tokens += _as_int(usage.get("cacheReadTokens")) or 0
            cache_write_tokens += _as_int(usage.get("cacheWriteTokens")) or 0
            saw_usage = True

    premium_requests = _as_int(data.get("totalPremiumRequests"))
    if not saw_usage and premium_requests is None:
        return None
    return CopilotSessionUsage(
        input_tokens=input_tokens if saw_usage else None,
        output_tokens=output_tokens if saw_usage else None,
        cache_read_tokens=cache_read_tokens if saw_usage else None,
        cache_write_tokens=cache_write_tokens if saw_usage else None,
        premium_requests=premium_requests,
    )


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
    usage = _extract_session_usage(last_event)

    is_closed = last_event_type in _CLEANLY_CLOSED_EVENTS

    # Fall back to filesystem signals when workspace.yaml is missing
    # timestamps. Copilot CLI occasionally truncates workspace.yaml
    # mid-write on shutdown (observed in the wild as a 0-byte file),
    # which would otherwise leave both timestamps None and:
    #   * sort the row to the very bottom of the SESSIONS list
    #     (sort key collapses to ``datetime.min``),
    #   * render the ``updated`` column as ``—`` so the operator can't
    #     tell whether the session is recent or ancient.
    # last_event_at and the file mtimes are already cheap to read on
    # the cold-scan path, so use them as backstops rather than letting
    # a corrupt yaml file silently demote the row.
    updated_at = _parse_iso(ws.get("updated_at")) or last_event_at
    if updated_at is None:
        updated_at = _stat_mtime(events_path) or _stat_mtime(workspace_path)
    created_at = _parse_iso(ws.get("created_at"))
    if created_at is None:
        created_at = _stat_mtime(workspace_path)

    return CopilotLocalSession(
        session_id=session_id,
        cwd=Path(cwd_str) if cwd_str else None,
        git_root=Path(git_root_str) if git_root_str else None,
        repository=ws.get("repository"),
        branch=ws.get("branch"),
        name=ws.get("name"),
        summary=ws.get("summary"),
        created_at=created_at,
        updated_at=updated_at,
        last_event_type=last_event_type,
        last_event_at=last_event_at,
        checkpoint_count=_count_checkpoints(session_dir),
        usage=usage,
        is_cleanly_closed=is_closed,
        origin=origin,
        windows_cwd=windows_cwd,
        windows_git_root=windows_git_root,
    )


def _stat_mtime(path: Path) -> datetime | None:
    """Return the file mtime as a UTC ``datetime``, or None on error."""
    try:
        st = path.stat()
    except OSError:
        return None
    return datetime.fromtimestamp(st.st_mtime, tz=UTC)


@dataclass(slots=True)
class _CachedEntry:
    """Parsed session plus the mtimes that make it current.

    The Copilot CLI appends to ``events.jsonl`` on every exchange and
    rewrites ``workspace.yaml`` only on ``/name`` and resume -- both of
    which also emit events. So the warm-path validator only needs to
    track the events mtime; the workspace mtime is recorded for
    diagnostics and as an audit trail but is not consulted on cache
    hits. See :meth:`CopilotSessionStore._scan` for the lookup.
    """

    session: CopilotLocalSession
    events_mtime_ns: int
    workspace_mtime_ns: int


@dataclass(slots=True)
class CopilotSessionStore:
    """Cached, read-only store for local Copilot CLI sessions.

    Scans one or more ``.copilot/session-state/`` roots and caches
    results with a TTL. In WSL the second root typically points at the
    Windows-side session-state directory so ``pwsh``-launched sessions
    are visible next to WSL-native ones.

    Two caches work together:

    * ``_cache`` / ``_cache_time`` -- short-TTL gate that returns the
      most recent scan verbatim so rapid successive calls don't even
      hit the filesystem.
    * ``_entry_cache`` -- per-session-dir cache keyed by (events mtime,
      workspace mtime). Survives across scans and lets repeat
      discoveries skip re-reading files that haven't changed. This is
      what turns a slow 9P-mounted Windows root (~3 s cold) into a
      ~100 ms warm rescan.

    The store is shared by the runtime synchronizer worker and the
    SESSIONS screen worker, both of which can call :meth:`discover`
    concurrently, and the UI thread can call :meth:`invalidate`
    around action boundaries (e.g. after resume) to drop the TTL
    cache without touching the per-entry mtime cache. A single lock
    serialises every cache mutation so concurrent readers cannot
    observe a partially-updated index (e.g. a fresh ``_cache_time``
    paired with a stale ``_cache`` list).
    """

    session_state_dir: Path = field(default_factory=lambda: _DEFAULT_SESSION_DIR)
    max_age_days: int = 60
    # 10 seconds. The previous 300 s ceiling masked Copilot CLI state
    # changes -- newly resumed sessions, '/name' edits, and other
    # workspace.yaml mutations stayed invisible to the SESSIONS list
    # for up to five minutes after the change. The per-entry mtime
    # cache makes a "warm" rescan cheap (~100 ms on WSL with the
    # Windows mount), so paying that cost roughly once per sync cycle
    # is acceptable in exchange for fresher data.
    cache_ttl_sec: float = 10.0
    extra_roots: tuple[SessionStoreRoot, ...] = ()

    _cache: list[CopilotLocalSession] = field(default_factory=list, init=False, repr=False)
    _cache_time: float = field(default=0.0, init=False, repr=False)
    _entry_cache: dict[Path, _CachedEntry] = field(default_factory=dict, init=False, repr=False)
    _by_id: dict[str, CopilotLocalSession] = field(default_factory=dict, init=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def discover(self, *, force: bool = False) -> list[CopilotLocalSession]:
        """Return all local sessions, using cache if fresh."""
        with self._lock:
            now = time.monotonic()
            if not force and self._cache and (now - self._cache_time) < self.cache_ttl_sec:
                return list(self._cache)
            self._cache = self._scan()
            self._by_id = {s.session_id: s for s in self._cache}
            self._cache_time = now
            return list(self._cache)

    def invalidate(self) -> None:
        """Drop the TTL cache so the next :meth:`discover` re-scans.

        Call from action handlers that just changed disk state (e.g.
        after resuming a session, which causes Copilot CLI to write
        an ``inuse.<pid>.lock`` and may rename ``workspace.yaml`` via
        '/name') so the next sync-driven refresh paints fresh data
        without waiting out the TTL. The per-entry mtime cache is
        preserved -- workspace.yaml/events.jsonl files that have not
        changed will still hit the cache and skip a full re-parse.
        """
        with self._lock:
            self._cache = []
            self._by_id = {}
            self._cache_time = 0.0

    def set_extra_roots(self, roots: Sequence[SessionStoreRoot]) -> None:
        """Replace the secondary roots and invalidate the cache."""
        with self._lock:
            self.extra_roots = tuple(roots)
            self._cache = []
            self._by_id = {}
            self._cache_time = 0.0
            # Per-entry cache stays valid -- it's keyed by absolute
            # path, so entries under removed or added roots simply go
            # unused. This avoids paying the cold-scan cost again
            # when a root toggles.

    def get_session(
        self, session_id: str, *, warm_only: bool = False
    ) -> CopilotLocalSession | None:
        """Look up a single session by ID.

        Fast path: O(1) dict lookup against the last-scan index when
        the TTL cache is still warm. Falls back to ``discover()`` only
        if the cache is stale so the result is never a wrong answer
        from a deleted session.

        Set ``warm_only=True`` to skip the fallback entirely -- callers
        on the UI thread (e.g. cursor movement in the Sessions screen)
        use this so they never block on a multi-second rescan. Returns
        None if the id is not in the warm index; callers should surface
        stale data instead of freezing the UI.
        """
        with self._lock:
            now = time.monotonic()
            warm = bool(self._cache) and (now - self._cache_time) < self.cache_ttl_sec
            if warm:
                cached = self._by_id.get(session_id)
                if cached is not None:
                    return cached
                # Not in the warm index -- may be a freshly created
                # session that we haven't seen yet. Fall through to a
                # rescan when allowed.
            if warm_only:
                return self._by_id.get(session_id)
        # Rescan outside the lock so concurrent ``get_session`` calls
        # don't block on each other unnecessarily; ``discover`` takes
        # the lock again and is idempotent under it.
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
        live_paths: set[Path] = set()
        for root in self._iter_roots():
            root_sessions, root_paths = self._scan_root(root, cutoff=cutoff)
            sessions.extend(root_sessions)
            live_paths.update(root_paths)

        # Drop cached entries whose session directory disappeared so
        # the cache size tracks the real session-state dirs.
        for stale in set(self._entry_cache) - live_paths:
            self._entry_cache.pop(stale, None)

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
    ) -> tuple[list[CopilotLocalSession], set[Path]]:
        """Scan a single session-state root directory.

        Returns the discovered sessions and the set of live session-dir
        paths so the caller can prune the entry cache.
        """
        if not root.path.is_dir():
            _log.debug("session state dir does not exist: %s", root.path)
            return [], set()

        try:
            entries = [Path(e.path) for e in os.scandir(root.path) if e.is_dir()]
        except OSError:
            _log.warning("failed to list session state dir: %s", root.path)
            return [], set()

        if not entries:
            return [], set()

        origin = root.origin
        entry_cache = self._entry_cache

        def _resolve(entry: Path) -> CopilotLocalSession | None:
            """Return a session for one directory, using cache when possible.

            Fast path: stat just ``events.jsonl`` -- the file Copilot
            appends to on every exchange and the one whose mtime
            actually drives "did this session change?" detection. When
            its mtime matches the cached entry, return the cached
            session and skip the second stat on ``workspace.yaml``
            entirely. ``workspace.yaml`` only changes on ``/name`` and
            on resume, both of which also bump ``events.jsonl``, so
            tracking only the events mtime in the warm path keeps the
            cache correct in practice while halving stat traffic --
            material on 9P-mounted Windows roots where every stat is
            a network round trip.

            Slow path: stat ``workspace.yaml`` for the cache key (and
            as the "is this a session dir at all?" gate) and run a
            full :func:`_parse_session_dir`.
            """
            workspace_path = entry / "workspace.yaml"
            events_path = entry / "events.jsonl"

            try:
                events_mtime = events_path.stat().st_mtime_ns
            except OSError:
                events_mtime = 0

            cached = entry_cache.get(entry)
            if (
                cached is not None
                and cached.events_mtime_ns == events_mtime
                and cached.session.origin == origin
            ):
                # Warm hit. Trust the cache, do not stat
                # ``workspace.yaml`` -- on 9P mounts that's an extra
                # round trip per session per refresh.
                return cached.session

            try:
                workspace_mtime = workspace_path.stat().st_mtime_ns
            except OSError:
                # No workspace.yaml → not a session dir. Drop any stale
                # cache entry and skip.
                entry_cache.pop(entry, None)
                return None

            try:
                session = _parse_session_dir(entry, origin=origin)
            except Exception:
                _log.debug("failed to parse session dir: %s", entry.name, exc_info=True)
                return None
            if session is None:
                return None

            entry_cache[entry] = _CachedEntry(
                session=session,
                events_mtime_ns=events_mtime,
                workspace_mtime_ns=workspace_mtime,
            )
            return session

        # Parallelise both the mtime-check fast path and the full parse
        # slow path. Per-entry work is I/O-bound (stat + small file
        # reads), which is exactly where threading wins on 9P mounts.
        max_workers = min(16, max(2, (os.cpu_count() or 4) * 2), len(entries))
        sessions: list[CopilotLocalSession] = []
        with ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="copilot-session-scan",
        ) as pool:
            for session in pool.map(_resolve, entries):
                if session is None:
                    continue
                if (
                    cutoff is not None
                    and session.updated_at is not None
                    and session.updated_at < cutoff
                ):
                    continue
                sessions.append(session)
        return sessions, set(entries)


__all__ = [
    "CopilotLocalSession",
    "CopilotSessionStore",
    "CopilotSessionUsage",
    "SessionOrigin",
    "SessionStoreRoot",
]

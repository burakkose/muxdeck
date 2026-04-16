"""Parallel-scan behaviour for CopilotSessionStore.

The scan must stay correct when parsing many sessions in parallel —
order-independent results, robust against individual entries that fail
to parse, and still honouring the ``max_age_days`` cutoff.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from copilot_commander.adapters.copilot_session_store import (
    CopilotSessionStore,
    SessionStoreRoot,
)


def _write_session(
    root: Path,
    session_id: str,
    *,
    updated_at: str = "2026-04-16T12:00:00Z",
    include_workspace: bool = True,
) -> None:
    session_dir = root / session_id
    session_dir.mkdir(parents=True)
    if include_workspace:
        (session_dir / "workspace.yaml").write_text(
            f"id: {session_id}\n"
            f"cwd: /tmp/{session_id}\n"
            f"summary: session {session_id}\n"
            f"updated_at: {updated_at}\n",
            encoding="utf-8",
        )
    (session_dir / "events.jsonl").write_text(
        f'{{"type": "message.append", "timestamp": "{updated_at}"}}\n',
        encoding="utf-8",
    )


def test_parallel_scan_returns_all_valid_sessions(tmp_path: Path) -> None:
    root = tmp_path / "state"
    root.mkdir()
    ids = [f"sess-{i:04d}" for i in range(50)]
    for sid in ids:
        _write_session(root, sid)

    store = CopilotSessionStore(session_state_dir=root, cache_ttl_sec=0.0)
    discovered = {s.session_id for s in store.discover()}
    assert discovered == set(ids)


def test_parallel_scan_skips_unparseable_entries(tmp_path: Path) -> None:
    root = tmp_path / "state"
    root.mkdir()

    _write_session(root, "good-1")
    _write_session(root, "good-2")
    # Entry without workspace.yaml — should be silently skipped, not
    # crash the pool or take down sibling entries.
    (root / "missing-workspace").mkdir()
    # A regular file in the root should also be ignored.
    (root / "stray.txt").write_text("noise", encoding="utf-8")

    store = CopilotSessionStore(session_state_dir=root, cache_ttl_sec=0.0)
    ids = {s.session_id for s in store.discover()}
    assert ids == {"good-1", "good-2"}


def test_parallel_scan_applies_age_cutoff(tmp_path: Path) -> None:
    root = tmp_path / "state"
    root.mkdir()

    recent = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    stale = (datetime.now(UTC) - timedelta(days=365)).isoformat().replace("+00:00", "Z")

    _write_session(root, "recent-1", updated_at=recent)
    _write_session(root, "stale-1", updated_at=stale)

    store = CopilotSessionStore(
        session_state_dir=root,
        max_age_days=30,
        cache_ttl_sec=0.0,
    )
    ids = {s.session_id for s in store.discover()}
    assert ids == {"recent-1"}


def test_parallel_scan_across_multiple_roots(tmp_path: Path) -> None:
    local = tmp_path / "local"
    windows = tmp_path / "windows"
    local.mkdir()
    windows.mkdir()

    for i in range(10):
        _write_session(local, f"lin-{i}")
    for i in range(10):
        _write_session(windows, f"win-{i}")

    store = CopilotSessionStore(
        session_state_dir=local,
        extra_roots=(SessionStoreRoot(windows, "windows"),),
        cache_ttl_sec=0.0,
    )

    all_sessions = store.discover()
    assert len(all_sessions) == 20
    origins = {s.session_id: s.origin for s in all_sessions}
    assert all(origins[f"lin-{i}"] == "local" for i in range(10))
    assert all(origins[f"win-{i}"] == "windows" for i in range(10))


def test_incremental_cache_reuses_unchanged_sessions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Second scan should skip the full parse when nothing changed."""
    from copilot_commander.adapters import copilot_session_store as module

    root = tmp_path / "state"
    root.mkdir()
    for i in range(5):
        _write_session(root, f"sess-{i}")

    store = CopilotSessionStore(session_state_dir=root, cache_ttl_sec=0.0)
    store.discover()  # populate cache

    call_count = {"n": 0}
    real_parse = module._parse_session_dir

    def _counting_parse(*args: object, **kwargs: object) -> object:
        call_count["n"] += 1
        return real_parse(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(module, "_parse_session_dir", _counting_parse)

    # Second discover with nothing changed on disk.
    store.discover()
    assert call_count["n"] == 0, "cache hit should skip _parse_session_dir entirely"


def test_incremental_cache_invalidates_on_events_mtime_bump(tmp_path: Path) -> None:
    """Appending to events.jsonl must force a reparse of that entry."""
    import os

    root = tmp_path / "state"
    root.mkdir()
    _write_session(root, "sess-a")

    store = CopilotSessionStore(session_state_dir=root, cache_ttl_sec=0.0)
    first = store.discover()
    assert len(first) == 1

    events = root / "sess-a" / "events.jsonl"
    # Append a new event and bump mtime explicitly (file-system clock
    # resolution can otherwise collapse two writes onto the same ns).
    with events.open("a", encoding="utf-8") as fh:
        fh.write('{"type": "session.shutdown", "timestamp": "2099-01-01T00:00:00Z"}\n')
    st = events.stat()
    os.utime(events, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))

    second = store.discover()
    assert len(second) == 1
    # The shutdown event should now be reflected.
    assert second[0].last_event_type == "session.shutdown"
    assert second[0].is_cleanly_closed is True


def test_incremental_cache_drops_removed_sessions(tmp_path: Path) -> None:
    """Deleting a session dir should evict it from the cache."""
    import shutil

    root = tmp_path / "state"
    root.mkdir()
    _write_session(root, "keep")
    _write_session(root, "remove")

    store = CopilotSessionStore(session_state_dir=root, cache_ttl_sec=0.0)
    assert {s.session_id for s in store.discover()} == {"keep", "remove"}

    shutil.rmtree(root / "remove")

    assert {s.session_id for s in store.discover()} == {"keep"}
    # Cache should no longer reference the removed path.
    assert not any(p.name == "remove" for p in store._entry_cache)

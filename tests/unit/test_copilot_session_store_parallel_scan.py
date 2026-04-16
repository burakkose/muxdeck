"""Parallel-scan behaviour for CopilotSessionStore.

The scan must stay correct when parsing many sessions in parallel —
order-independent results, robust against individual entries that fail
to parse, and still honouring the ``max_age_days`` cutoff.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

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

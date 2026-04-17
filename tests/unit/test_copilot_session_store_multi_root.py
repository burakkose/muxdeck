"""Tests for multi-root Copilot session discovery (WSL + Windows)."""

from __future__ import annotations

from pathlib import Path

from copilot_commander.adapters.copilot_session_store import (
    CopilotSessionStore,
    SessionStoreRoot,
)


def _mk_session(root: Path, session_id: str, cwd: str, *, closed: bool = False) -> None:
    session_dir = root / session_id
    session_dir.mkdir(parents=True)
    (session_dir / "workspace.yaml").write_text(
        f"id: {session_id}\ncwd: {cwd}\nsummary: session-{session_id}\n",
        encoding="utf-8",
    )
    event = (
        '{"type": "session.shutdown", "timestamp": "2026-04-16T12:00:00Z"}\n'
        if closed
        else '{"type": "message.append", "timestamp": "2026-04-16T12:00:00Z"}\n'
    )
    (session_dir / "events.jsonl").write_text(event, encoding="utf-8")


def test_store_tags_windows_origin_and_preserves_winpath(tmp_path: Path) -> None:
    linux_root = tmp_path / "linux" / ".copilot" / "session-state"
    windows_root = tmp_path / "windows" / ".copilot" / "session-state"
    linux_root.mkdir(parents=True)
    windows_root.mkdir(parents=True)

    _mk_session(linux_root, "lin-1111", cwd="/home/alice/repo")
    _mk_session(windows_root, "win-2222", cwd="C:\\Users\\alice\\repo")

    store = CopilotSessionStore(
        session_state_dir=linux_root,
        extra_roots=(SessionStoreRoot(windows_root, "windows"),),
        cache_ttl_sec=0.0,
    )

    sessions = {s.session_id: s for s in store.discover()}
    assert set(sessions) == {"lin-1111", "win-2222"}
    assert sessions["lin-1111"].origin == "local"
    assert sessions["lin-1111"].windows_cwd is None
    win = sessions["win-2222"]
    assert win.origin == "windows"
    assert win.windows_cwd == "C:\\Users\\alice\\repo"


def test_store_counts_by_origin(tmp_path: Path) -> None:
    linux_root = tmp_path / "lin"
    windows_root = tmp_path / "win"
    linux_root.mkdir()
    windows_root.mkdir()
    _mk_session(linux_root, "lin-a", cwd="/a")
    _mk_session(windows_root, "win-a", cwd="C:\\a")
    _mk_session(windows_root, "win-b", cwd="C:\\b")

    store = CopilotSessionStore(
        session_state_dir=linux_root,
        extra_roots=(SessionStoreRoot(windows_root, "windows"),),
        cache_ttl_sec=0.0,
    )
    assert store.count_by_origin("local") == 1
    assert store.count_by_origin("windows") == 2


def test_set_extra_roots_invalidates_cache(tmp_path: Path) -> None:
    primary = tmp_path / "primary"
    secondary = tmp_path / "secondary"
    primary.mkdir()
    secondary.mkdir()
    _mk_session(primary, "p-1", cwd="/p")
    _mk_session(secondary, "s-1", cwd="C:\\s")

    store = CopilotSessionStore(session_state_dir=primary, cache_ttl_sec=60.0)
    assert len(store.discover()) == 1

    store.set_extra_roots([SessionStoreRoot(secondary, "windows")])
    discovered = store.discover()
    assert {s.session_id for s in discovered} == {"p-1", "s-1"}
    windows = [s for s in discovered if s.origin == "windows"]
    assert len(windows) == 1
    assert windows[0].windows_cwd == "C:\\s"


def test_store_ignores_duplicate_extra_root(tmp_path: Path) -> None:
    primary = tmp_path / "primary"
    primary.mkdir()
    _mk_session(primary, "p-1", cwd="/p")

    store = CopilotSessionStore(
        session_state_dir=primary,
        extra_roots=(SessionStoreRoot(primary, "windows"),),
        cache_ttl_sec=0.0,
    )
    # Same directory should only contribute one session, tagged local.
    sessions = store.discover()
    assert len(sessions) == 1
    assert sessions[0].origin == "local"

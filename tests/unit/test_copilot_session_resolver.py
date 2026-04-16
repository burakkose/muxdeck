"""Unit tests for :class:`InuseLockResolver`."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from copilot_commander.adapters.copilot_session_resolver import InuseLockResolver


@dataclass
class _FakeRoot:
    path: Path


@dataclass
class _FakeStore:
    session_state_dir: Path
    extra_roots: tuple[_FakeRoot, ...] = field(default_factory=tuple)


def _write_proc(proc_dir: Path, pid: int, ppid: int) -> None:
    d = proc_dir / str(pid)
    d.mkdir(parents=True, exist_ok=True)
    (d / "status").write_text(f"Name:\tsome-proc\nPPid:\t{ppid}\n", encoding="utf-8")


def _make_session(root: Path, session_id: str, *, lock_pid: int | None) -> Path:
    d = root / session_id
    d.mkdir(parents=True, exist_ok=True)
    if lock_pid is not None:
        (d / f"inuse.{lock_pid}.lock").write_text("", encoding="utf-8")
    return d


class TestInuseLockResolver:
    def test_direct_pid_match(self, tmp_path: Path) -> None:
        root = tmp_path / "sessions"
        _make_session(root, "sess-a", lock_pid=4242)
        store = _FakeStore(session_state_dir=root)
        resolver = InuseLockResolver(store=store, proc_dir=tmp_path / "proc")
        assert resolver.resolve_for_pid(4242) == "sess-a"

    def test_descendant_match_walks_ppid_chain(self, tmp_path: Path) -> None:
        root = tmp_path / "sessions"
        proc = tmp_path / "proc"
        proc.mkdir()
        # Copilot pid 9999, its parent is node 9000, which is a child
        # of the tmux pane shell (pid 1234).
        _make_session(root, "sess-b", lock_pid=9999)
        _write_proc(proc, pid=9999, ppid=9000)
        _write_proc(proc, pid=9000, ppid=1234)
        _write_proc(proc, pid=1234, ppid=1)
        store = _FakeStore(session_state_dir=root)
        resolver = InuseLockResolver(store=store, proc_dir=proc)
        assert resolver.resolve_for_pid(1234) == "sess-b"

    def test_no_match_returns_none(self, tmp_path: Path) -> None:
        root = tmp_path / "sessions"
        proc = tmp_path / "proc"
        proc.mkdir()
        _make_session(root, "sess-c", lock_pid=5555)
        _write_proc(proc, pid=5555, ppid=1)
        store = _FakeStore(session_state_dir=root)
        resolver = InuseLockResolver(store=store, proc_dir=proc)
        assert resolver.resolve_for_pid(42) is None

    def test_none_or_invalid_pid_short_circuits(self, tmp_path: Path) -> None:
        store = _FakeStore(session_state_dir=tmp_path)
        resolver = InuseLockResolver(store=store, proc_dir=tmp_path)
        assert resolver.resolve_for_pid(None) is None
        assert resolver.resolve_for_pid(0) is None
        assert resolver.resolve_for_pid(-1) is None

    def test_extra_roots_are_scanned(self, tmp_path: Path) -> None:
        primary = tmp_path / "wsl-sessions"
        windows = tmp_path / "windows-sessions"
        proc = tmp_path / "proc"
        proc.mkdir()
        _make_session(windows, "sess-win", lock_pid=7777)
        _write_proc(proc, pid=7777, ppid=1)
        store = _FakeStore(
            session_state_dir=primary,
            extra_roots=(_FakeRoot(path=windows),),
        )
        primary.mkdir()
        resolver = InuseLockResolver(store=store, proc_dir=proc)
        assert resolver.resolve_for_pid(7777) == "sess-win"

    def test_missing_proc_dir_returns_none(self, tmp_path: Path) -> None:
        store = _FakeStore(session_state_dir=tmp_path)
        resolver = InuseLockResolver(store=store, proc_dir=tmp_path / "nope")
        assert resolver.resolve_for_pid(123) is None

    def test_malformed_lock_names_are_skipped(self, tmp_path: Path) -> None:
        root = tmp_path / "sessions"
        d = root / "sess-d"
        d.mkdir(parents=True)
        (d / "inuse.notanint.lock").write_text("", encoding="utf-8")
        (d / "inuse.lock").write_text("", encoding="utf-8")
        (d / "inuse.4242.lock").write_text("", encoding="utf-8")
        store = _FakeStore(session_state_dir=root)
        resolver = InuseLockResolver(store=store, proc_dir=tmp_path / "proc")
        assert resolver.resolve_for_pid(4242) == "sess-d"

    def test_respects_ancestor_depth_limit(self, tmp_path: Path) -> None:
        root = tmp_path / "sessions"
        proc = tmp_path / "proc"
        proc.mkdir()
        _make_session(root, "sess-e", lock_pid=100)
        # Chain: 100 -> 101 -> 102 -> ... -> 199 -> 1. Ancestor 199 is
        # within the default limit (16 hops), so a shorter chain is
        # enough to prove the walk works; use 10 hops.
        for pid in range(100, 110):
            _write_proc(proc, pid=pid, ppid=pid + 1)
        _write_proc(proc, pid=110, ppid=1)
        store = _FakeStore(session_state_dir=root)
        resolver = InuseLockResolver(store=store, proc_dir=proc)
        assert resolver.resolve_for_pid(105) == "sess-e"

    def test_cycle_in_ppid_chain_does_not_hang(self, tmp_path: Path) -> None:
        """Pathological /proc state (should not happen, but be safe)."""
        root = tmp_path / "sessions"
        proc = tmp_path / "proc"
        proc.mkdir()
        _make_session(root, "sess-f", lock_pid=200)
        _write_proc(proc, pid=200, ppid=201)
        _write_proc(proc, pid=201, ppid=200)
        store = _FakeStore(session_state_dir=root)
        resolver = InuseLockResolver(store=store, proc_dir=proc)
        # Target pid is not in the (bounded) chain → returns None
        # rather than looping forever.
        assert resolver.resolve_for_pid(999) is None

"""Unit tests for :class:`InuseLockResolver`."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from muxdeck.adapters.copilot_session_resolver import (
    CopilotSessionResolution,
    InuseLockResolver,
    ResolvedCopilotTarget,
)


@dataclass
class _FakeRoot:
    path: Path


@dataclass
class _FakeStore:
    session_state_dir: Path
    extra_roots: tuple[_FakeRoot, ...] = field(default_factory=tuple)


def _write_proc(
    proc_dir: Path,
    pid: int,
    ppid: int,
    *,
    cmdline: str = "/usr/local/lib/node_modules/@github/copilot/copilot",
    environ: dict[str, str] | None = None,
) -> None:
    d = proc_dir / str(pid)
    d.mkdir(parents=True, exist_ok=True)
    (d / "status").write_text(f"Name:\tsome-proc\nPPid:\t{ppid}\n", encoding="utf-8")
    # cmdline is NUL-delimited in the kernel; split on spaces so
    # tests can write natural-looking strings.
    parts = cmdline.split(" ") if cmdline else []
    payload = "\x00".join(parts)
    if payload:
        payload += "\x00"
    (d / "cmdline").write_bytes(payload.encode("utf-8"))
    environ_payload = b""
    if environ:
        environ_payload = b"\x00".join(f"{key}={value}".encode() for key, value in environ.items())
        if environ_payload:
            environ_payload += b"\x00"
    (d / "environ").write_bytes(environ_payload)


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

    def test_stale_lock_with_dead_pid_is_skipped(self, tmp_path: Path) -> None:
        """A lock file whose pid no longer exists must not be trusted.

        Copilot occasionally crashes without cleaning ``inuse.<pid>.lock``,
        and those fossils routinely outlive the sessions that created
        them. Previously we'd happily return the stale session id;
        the dashboard would then render that session's sub-agents
        under whatever muxdeck agent happened to share the pane
        hierarchy.
        """
        root = tmp_path / "sessions"
        proc = tmp_path / "proc"
        proc.mkdir()
        _make_session(root, "stale-sess", lock_pid=12345)
        # Also publish a *live* lock in a different session so the
        # resolver has something else to pick — the bug was returning
        # "stale-sess" when a legitimate candidate existed.
        _make_session(root, "live-sess", lock_pid=2000)
        _write_proc(proc, pid=2000, ppid=1000)
        _write_proc(proc, pid=1000, ppid=1)
        store = _FakeStore(session_state_dir=root)
        resolver = InuseLockResolver(store=store, proc_dir=proc)
        assert resolver.resolve_for_pid(1000) == "live-sess"

    def test_recycled_non_copilot_pid_is_skipped(self, tmp_path: Path) -> None:
        """If the OS reused the lock's pid for an unrelated process
        (a user's editor, a background daemon…) we must not treat
        the lock as proof of session ownership.
        """
        root = tmp_path / "sessions"
        proc = tmp_path / "proc"
        proc.mkdir()
        _make_session(root, "recycled", lock_pid=5555)
        _write_proc(proc, pid=5555, ppid=1234, cmdline="/usr/bin/python /home/u/foo.py")
        _write_proc(proc, pid=1234, ppid=1)
        store = _FakeStore(session_state_dir=root)
        resolver = InuseLockResolver(store=store, proc_dir=proc)
        assert resolver.resolve_for_pid(1234) is None

    def test_resume_flag_overrides_stale_lock_path(self, tmp_path: Path) -> None:
        """When Copilot re-used a pid under ``--resume=<uuid>``, the
        lock path points at the *previous* session but the live
        process belongs to the uuid in its cmdline. Trust the
        cmdline.
        """
        root = tmp_path / "sessions"
        proc = tmp_path / "proc"
        proc.mkdir()
        # Stale session directory still carries a lock for a pid
        # the OS has since handed to a different copilot session.
        _make_session(root, "old-session", lock_pid=2395)
        resume_uuid = "73c19583-3363-499c-ac00-1ddb2c90c4ea"
        _write_proc(
            proc,
            pid=2395,
            ppid=2379,
            cmdline=(
                "/usr/local/lib/node_modules/@github/copilot/"
                f"node_modules/@github/copilot-linux-x64/copilot --resume={resume_uuid}"
            ),
        )
        _write_proc(proc, pid=2379, ppid=2324, cmdline="node /usr/local/bin/copilot")
        _write_proc(proc, pid=2324, ppid=1, cmdline="zsh")
        store = _FakeStore(session_state_dir=root)
        resolver = InuseLockResolver(store=store, proc_dir=proc)
        assert resolver.resolve_for_pid(2324) == resume_uuid

    def test_fresh_session_falls_back_to_lock_path(self, tmp_path: Path) -> None:
        """Without ``--resume`` the lock path *is* the session id."""
        root = tmp_path / "sessions"
        proc = tmp_path / "proc"
        proc.mkdir()
        _make_session(root, "fresh-session", lock_pid=8143)
        _write_proc(
            proc,
            pid=8143,
            ppid=8132,
            cmdline="/usr/local/lib/node_modules/@github/copilot/copilot",
        )
        _write_proc(proc, pid=8132, ppid=6854, cmdline="node /usr/local/bin/copilot")
        _write_proc(proc, pid=6854, ppid=1, cmdline="zsh")
        store = _FakeStore(session_state_dir=root)
        resolver = InuseLockResolver(store=store, proc_dir=proc)
        assert resolver.resolve_for_pid(6854) == "fresh-session"

    def test_exact_pid_match_wins_over_descendant_match(self, tmp_path: Path) -> None:
        """Prefer the lock whose pid literally equals the pane pid —
        matches are more reliable than ancestor walks.
        """
        root = tmp_path / "sessions"
        proc = tmp_path / "proc"
        proc.mkdir()
        _make_session(root, "ancestor-sess", lock_pid=111)
        _make_session(root, "exact-sess", lock_pid=500)
        _write_proc(proc, pid=111, ppid=500)
        _write_proc(proc, pid=500, ppid=1)
        store = _FakeStore(session_state_dir=root)
        resolver = InuseLockResolver(store=store, proc_dir=proc)
        assert resolver.resolve_for_pid(500) == "exact-sess"

    def test_multiple_descendant_sessions_return_none(self, tmp_path: Path) -> None:
        """Nested tmux can host multiple Copilot children under one outer pane.

        In that case there is no clean one-to-one mapping from the outer pane pid
        to a single Copilot session, so the resolver must refuse to guess.
        """
        root = tmp_path / "sessions"
        proc = tmp_path / "proc"
        proc.mkdir()
        _make_session(root, "sess-one", lock_pid=4101)
        _make_session(root, "sess-two", lock_pid=4102)
        _write_proc(proc, pid=4101, ppid=3100)
        _write_proc(proc, pid=4102, ppid=3100)
        _write_proc(proc, pid=3100, ppid=2100, cmdline="tmux")
        _write_proc(proc, pid=2100, ppid=1, cmdline="zsh")
        store = _FakeStore(session_state_dir=root)
        resolver = InuseLockResolver(store=store, proc_dir=proc)
        assert resolver.resolve_for_pid(2100) is None
        assert resolver.resolve(2100) == CopilotSessionResolution(state="ambiguous")

    def test_resolve_target_reads_nested_tmux_metadata(self, tmp_path: Path) -> None:
        root = tmp_path / "sessions"
        proc = tmp_path / "proc"
        proc.mkdir()
        _make_session(root, "sess-inner", lock_pid=7001)
        nested_socket = tmp_path / "nested" / "tmux.sock"
        _write_proc(
            proc,
            pid=7001,
            ppid=6100,
            environ={
                "TMUX": f"{nested_socket},123,0",
                "TMUX_PANE": "%42",
            },
        )
        _write_proc(proc, pid=6100, ppid=5100, cmdline="tmux")
        _write_proc(proc, pid=5100, ppid=1, cmdline="zsh")
        store = _FakeStore(session_state_dir=root)
        resolver = InuseLockResolver(store=store, proc_dir=proc)
        assert resolver.resolve_target_for_pid(5100) == ResolvedCopilotTarget(
            session_id="sess-inner",
            pane_id="%42",
            socket_path=nested_socket,
        )

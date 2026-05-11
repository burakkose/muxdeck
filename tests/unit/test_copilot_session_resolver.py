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
        # Extras are off by default — pane pids are always native to
        # the host the resolver runs on, so a Windows-side mount holds
        # no relevant locks. Callers must opt in explicitly.
        opted_in = InuseLockResolver(store=store, proc_dir=proc, include_extra_roots=True)
        assert opted_in.resolve_for_pid(7777) == "sess-win"

        # Default constructor must NOT walk the extra root, even when
        # the lock there would otherwise match.
        default = InuseLockResolver(store=store, proc_dir=proc)
        assert default.resolve_for_pid(7777) is None

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


class TestEnumerationCache:
    def test_iter_locks_caches_enumeration_within_ttl(self, tmp_path: Path) -> None:
        root = tmp_path / "sessions"
        proc = tmp_path / "proc"
        proc.mkdir()
        _make_session(root, "sess-cached", lock_pid=4242)
        _write_proc(proc, pid=4242, ppid=1)
        store = _FakeStore(session_state_dir=root)
        resolver = InuseLockResolver(store=store, proc_dir=proc, enumeration_ttl_seconds=60.0)
        first = list(resolver._iter_locks())
        # Add a second session after the cache populates — the TTL
        # window means the second walk must reuse the cache and not
        # see the new lock until invalidation.
        _make_session(root, "sess-new", lock_pid=5555)
        _write_proc(proc, pid=5555, ppid=1)
        second = list(resolver._iter_locks())
        assert first == second
        resolver.invalidate_lock_cache()
        third = list(resolver._iter_locks())
        assert {pid for _, pid in third} == {4242, 5555}

    def test_iter_locks_disabled_cache_always_walks(self, tmp_path: Path) -> None:
        root = tmp_path / "sessions"
        proc = tmp_path / "proc"
        proc.mkdir()
        _make_session(root, "sess-a", lock_pid=4242)
        _write_proc(proc, pid=4242, ppid=1)
        store = _FakeStore(session_state_dir=root)
        resolver = InuseLockResolver(store=store, proc_dir=proc, enumeration_ttl_seconds=0.0)
        first = list(resolver._iter_locks())
        _make_session(root, "sess-b", lock_pid=5555)
        _write_proc(proc, pid=5555, ppid=1)
        second = list(resolver._iter_locks())
        assert {pid for _, pid in first} == {4242}
        assert {pid for _, pid in second} == {4242, 5555}

    def test_iter_locks_cache_expires_after_ttl(self, tmp_path: Path) -> None:
        import time as time_mod

        root = tmp_path / "sessions"
        proc = tmp_path / "proc"
        proc.mkdir()
        _make_session(root, "sess-a", lock_pid=4242)
        _write_proc(proc, pid=4242, ppid=1)
        store = _FakeStore(session_state_dir=root)
        # Use a tiny TTL to keep the test cheap and deterministic.
        resolver = InuseLockResolver(store=store, proc_dir=proc, enumeration_ttl_seconds=0.01)
        list(resolver._iter_locks())
        _make_session(root, "sess-b", lock_pid=5555)
        _write_proc(proc, pid=5555, ppid=1)
        time_mod.sleep(0.02)
        refreshed = list(resolver._iter_locks())
        assert {pid for _, pid in refreshed} == {4242, 5555}

    def test_resolve_calls_share_cached_enumeration(self, tmp_path: Path) -> None:
        from unittest.mock import patch

        root = tmp_path / "sessions"
        proc = tmp_path / "proc"
        proc.mkdir()
        _make_session(root, "sess-a", lock_pid=4242)
        _write_proc(proc, pid=4242, ppid=1)
        store = _FakeStore(session_state_dir=root)
        resolver = InuseLockResolver(store=store, proc_dir=proc, enumeration_ttl_seconds=60.0)
        # ``slots=True`` blocks attribute monkeypatching, so spy at
        # the class level via patch.object.
        with patch.object(
            InuseLockResolver,
            "_enumerate_locks",
            autospec=True,
            side_effect=InuseLockResolver._enumerate_locks,
        ) as spy:
            for _ in range(10):
                resolver.resolve(4242)
            assert spy.call_count == 1


# ── _read_ppid / _read_cmdline / _read_environ edge cases ────────────


class TestReadProcFiles:
    def test_read_ppid_returns_none_when_status_missing(self, tmp_path: Path) -> None:
        store = _FakeStore(session_state_dir=tmp_path / "sess")
        resolver = InuseLockResolver(store=store, proc_dir=tmp_path / "proc")
        # /proc/9999/status does not exist → OSError → None.
        assert resolver._read_ppid(9999) is None

    def test_read_ppid_returns_none_when_no_ppid_line(self, tmp_path: Path) -> None:
        proc = tmp_path / "proc"
        (proc / "100").mkdir(parents=True)
        # No PPid: line at all — loop completes without finding it.
        (proc / "100" / "status").write_text("Name:\tfoo\nState:\tR\n", encoding="utf-8")
        store = _FakeStore(session_state_dir=tmp_path / "sess")
        resolver = InuseLockResolver(store=store, proc_dir=proc)
        assert resolver._read_ppid(100) is None

    def test_read_ppid_returns_none_when_value_not_digit(self, tmp_path: Path) -> None:
        proc = tmp_path / "proc"
        (proc / "200").mkdir(parents=True)
        # Malformed PPid value — must return None, not raise.
        (proc / "200" / "status").write_text("PPid:\tnot-a-number\n", encoding="utf-8")
        store = _FakeStore(session_state_dir=tmp_path / "sess")
        resolver = InuseLockResolver(store=store, proc_dir=proc)
        assert resolver._read_ppid(200) is None

    def test_read_cmdline_returns_empty_string_for_kernel_thread(self, tmp_path: Path) -> None:
        proc = tmp_path / "proc"
        (proc / "300").mkdir(parents=True)
        # Kernel threads / zombies have empty cmdline files.
        (proc / "300" / "cmdline").write_bytes(b"")
        store = _FakeStore(session_state_dir=tmp_path / "sess")
        resolver = InuseLockResolver(store=store, proc_dir=proc)
        assert resolver._read_cmdline(300) == ""

    def test_read_cmdline_returns_none_when_missing(self, tmp_path: Path) -> None:
        store = _FakeStore(session_state_dir=tmp_path / "sess")
        resolver = InuseLockResolver(store=store, proc_dir=tmp_path / "proc")
        assert resolver._read_cmdline(9999) is None

    def test_read_environ_returns_empty_when_missing(self, tmp_path: Path) -> None:
        store = _FakeStore(session_state_dir=tmp_path / "sess")
        resolver = InuseLockResolver(store=store, proc_dir=tmp_path / "proc")
        assert resolver._read_environ(9999) == {}

    def test_read_environ_skips_chunks_without_equals(self, tmp_path: Path) -> None:
        proc = tmp_path / "proc"
        (proc / "400").mkdir(parents=True)
        # "BARE" lacks '=' and must be skipped without raising.
        (proc / "400" / "environ").write_bytes(b"FOO=bar\x00BARE\x00BAZ=qux\x00")
        store = _FakeStore(session_state_dir=tmp_path / "sess")
        resolver = InuseLockResolver(store=store, proc_dir=proc)
        env = resolver._read_environ(400)
        assert env == {"FOO": "bar", "BAZ": "qux"}


# ── _enumerate_locks OSError paths ───────────────────────────────────


class TestEnumerateLocksErrors:
    def test_iterdir_oserror_skips_root(self, tmp_path: Path) -> None:
        # Provide a session_state_dir that *exists as a file* — iterdir
        # raises NotADirectoryError (an OSError) which the loop swallows
        # without surfacing.
        not_a_dir = tmp_path / "not-a-dir"
        not_a_dir.write_text("garbage")
        store = _FakeStore(session_state_dir=not_a_dir)
        resolver = InuseLockResolver(store=store, proc_dir=tmp_path / "proc")
        assert list(resolver._enumerate_locks()) == []

    def test_glob_oserror_inside_session_dir_is_swallowed(self, tmp_path: Path) -> None:
        from unittest.mock import patch

        root = tmp_path / "sessions"
        _make_session(root, "sess-a", lock_pid=4242)
        store = _FakeStore(session_state_dir=root)
        resolver = InuseLockResolver(store=store, proc_dir=tmp_path / "proc")

        original_glob = Path.glob

        def boom(self: Path, pattern: str, *args: object, **kwargs: object) -> object:
            if pattern == "inuse.*.lock":
                raise OSError("permission denied")
            return original_glob(self, pattern, *args, **kwargs)  # type: ignore[arg-type]

        with patch.object(Path, "glob", boom):
            assert list(resolver._enumerate_locks()) == []


# ── _parse_lock_pid edge cases ───────────────────────────────────────


class TestParseLockPid:
    def test_returns_none_for_unrelated_filenames(self) -> None:
        from muxdeck.adapters.copilot_session_resolver import _parse_lock_pid

        assert _parse_lock_pid("not-a-lock") is None
        assert _parse_lock_pid("inuse.lock") is None
        assert _parse_lock_pid("inuse.notdigit.lock") is None
        assert _parse_lock_pid("inuse.123.lock") == 123


# ── _looks_like_copilot edge cases ───────────────────────────────────


class TestLooksLikeCopilot:
    def test_empty_string_returns_false(self) -> None:
        from muxdeck.adapters.copilot_session_resolver import _looks_like_copilot

        assert _looks_like_copilot("") is False

    def test_returns_true_for_install_path_substrings(self) -> None:
        from muxdeck.adapters.copilot_session_resolver import _looks_like_copilot

        assert (
            _looks_like_copilot("/usr/local/lib/node_modules/@github/copilot/copilot --resume X")
            is True
        )

    def test_returns_false_for_unrelated_command(self) -> None:
        from muxdeck.adapters.copilot_session_resolver import _looks_like_copilot

        assert _looks_like_copilot("/usr/bin/python my_copilot_helper.py") is False


# ── _resolution_from_matches & _target_from_matches ──────────────────


class TestResolutionAndTargetFromMatches:
    def test_resolution_returns_missing_for_empty(self) -> None:
        from muxdeck.adapters.copilot_session_resolver import _resolution_from_matches

        assert _resolution_from_matches([]) == CopilotSessionResolution()

    def test_resolution_dedups_same_session(self) -> None:
        from muxdeck.adapters.copilot_session_resolver import _resolution_from_matches

        result = _resolution_from_matches(
            [
                ResolvedCopilotTarget(session_id="s1"),
                ResolvedCopilotTarget(session_id="s1", pane_id="%2"),
            ]
        )
        assert result.state == "resolved"
        assert result.session_id == "s1"

    def test_target_from_matches_returns_none_for_empty(self) -> None:
        from muxdeck.adapters.copilot_session_resolver import _target_from_matches

        assert _target_from_matches([]) is None

    def test_target_from_matches_returns_none_for_multiple_sessions(self) -> None:
        from muxdeck.adapters.copilot_session_resolver import _target_from_matches

        result = _target_from_matches(
            [
                ResolvedCopilotTarget(session_id="s1"),
                ResolvedCopilotTarget(session_id="s2"),
            ]
        )
        assert result is None

    def test_target_from_matches_merges_pane_and_socket_for_same_session(self) -> None:
        from muxdeck.adapters.copilot_session_resolver import _target_from_matches

        socket = Path("/var/run/tmux-1000/default")
        merged = _target_from_matches(
            [
                ResolvedCopilotTarget(session_id="s1", pane_id="%2", socket_path=None),
                ResolvedCopilotTarget(session_id="s1", pane_id=None, socket_path=socket),
            ]
        )
        assert merged is not None
        assert merged.session_id == "s1"
        assert merged.pane_id == "%2"
        assert merged.socket_path == socket


# ── resolve_target_for_pid edge cases ────────────────────────────────


class TestResolveTargetForPid:
    def test_returns_none_for_pane_pid_none_or_zero(self, tmp_path: Path) -> None:
        store = _FakeStore(session_state_dir=tmp_path / "sess")
        resolver = InuseLockResolver(store=store, proc_dir=tmp_path / "proc")
        assert resolver.resolve_target_for_pid(None) is None
        assert resolver.resolve_target_for_pid(0) is None
        assert resolver.resolve_target_for_pid(-5) is None

    def test_returns_none_when_two_distinct_sessions_match(self, tmp_path: Path) -> None:
        # Two pids both descend from the pane and host different
        # sessions → ambiguous → resolve_target_for_pid returns None
        # (covers _target_from_matches "len(merged) != 1" branch).
        root = tmp_path / "sessions"
        proc = tmp_path / "proc"
        proc.mkdir()
        _make_session(root, "sess-a", lock_pid=2001)
        _make_session(root, "sess-b", lock_pid=2002)
        _write_proc(proc, pid=2001, ppid=1234)
        _write_proc(proc, pid=2002, ppid=1234)
        _write_proc(proc, pid=1234, ppid=1)
        store = _FakeStore(session_state_dir=root)
        resolver = InuseLockResolver(store=store, proc_dir=proc)
        assert resolver.resolve_target_for_pid(1234) is None


# ── _roots: extra_roots filtering ────────────────────────────────────


class TestRoots:
    def test_extra_roots_with_non_path_attributes_are_skipped(self, tmp_path: Path) -> None:
        # Build an _enumerate_locks call that surfaces a non-Path
        # ``path`` attribute on an extra root — the loop must skip it
        # silently rather than yield an unusable entry.
        @dataclass
        class _BadRoot:
            path: object  # not a Path

        extras = (_BadRoot(path="not a path"),)
        store = _FakeStore(session_state_dir=tmp_path / "sess", extra_roots=extras)  # type: ignore[arg-type]
        resolver = InuseLockResolver(
            store=store, proc_dir=tmp_path / "proc", include_extra_roots=True
        )
        assert list(resolver._enumerate_locks()) == []

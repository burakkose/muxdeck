"""Delete-path behaviour for :class:`CopilotSessionStore`.

These tests cover both the single-session and bulk-delete entry points,
including the boundary conditions where caches must stay consistent
after the on-disk directory is removed.
"""

from __future__ import annotations

from pathlib import Path

import pytest  # noqa: F401  -- imported for future fixture wiring

from muxdeck.adapters.copilot_session_store import (
    CopilotSessionStore,
    SessionStoreRoot,
)


def _write_session(
    root: Path,
    session_id: str,
    *,
    updated_at: str = "2026-04-16T12:00:00Z",
) -> Path:
    session_dir = root / session_id
    session_dir.mkdir(parents=True)
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
    return session_dir


def test_resolve_session_dir_finds_directory_under_primary_root(tmp_path: Path) -> None:
    root = tmp_path / "state"
    root.mkdir()
    expected = _write_session(root, "sess-a")
    store = CopilotSessionStore(session_state_dir=root, cache_ttl_sec=0.0)

    assert store.resolve_session_dir("sess-a") == expected


def test_resolve_session_dir_checks_extra_roots(tmp_path: Path) -> None:
    primary = tmp_path / "primary"
    secondary = tmp_path / "secondary"
    primary.mkdir()
    secondary.mkdir()
    _write_session(primary, "sess-a")
    expected = _write_session(secondary, "sess-b")
    store = CopilotSessionStore(
        session_state_dir=primary,
        extra_roots=(SessionStoreRoot(secondary, "windows"),),
        cache_ttl_sec=0.0,
    )

    assert store.resolve_session_dir("sess-b") == expected


def test_resolve_session_dir_returns_none_for_unknown_id(tmp_path: Path) -> None:
    root = tmp_path / "state"
    root.mkdir()
    store = CopilotSessionStore(session_state_dir=root, cache_ttl_sec=0.0)

    assert store.resolve_session_dir("missing") is None


def test_resolve_session_dir_skips_dir_without_workspace_yaml(tmp_path: Path) -> None:
    # A stray directory that doesn't actually carry a session should
    # not be treated as a delete target -- otherwise a typo'd id would
    # rmtree the wrong thing.
    root = tmp_path / "state"
    root.mkdir()
    (root / "not-a-session").mkdir()

    store = CopilotSessionStore(session_state_dir=root, cache_ttl_sec=0.0)

    assert store.resolve_session_dir("not-a-session") is None


def test_delete_session_removes_directory_and_invalidates_caches(tmp_path: Path) -> None:
    root = tmp_path / "state"
    root.mkdir()
    target = _write_session(root, "sess-a")
    _write_session(root, "sess-b")
    store = CopilotSessionStore(session_state_dir=root, cache_ttl_sec=60.0)
    # Warm the caches so we can verify they're cleaned up.
    discovered_before = {s.session_id for s in store.discover()}
    assert discovered_before == {"sess-a", "sess-b"}

    removed_path = store.delete_session("sess-a")

    assert removed_path == target
    assert not target.exists()
    # Cache must no longer surface the deleted session even when the
    # TTL is long, so the next discover doesn't yield a phantom row.
    cached_after = {s.session_id for s in store.discover()}
    assert cached_after == {"sess-b"}


def test_delete_session_returns_none_when_already_gone(tmp_path: Path) -> None:
    root = tmp_path / "state"
    root.mkdir()
    _write_session(root, "sess-a")
    store = CopilotSessionStore(session_state_dir=root, cache_ttl_sec=60.0)
    # Prime the index so the missing-id branch is exercised even
    # though the on-disk dir was never present for "ghost".
    store.discover()

    assert store.delete_session("ghost") is None
    # The legitimate session must remain untouched.
    assert {s.session_id for s in store.discover()} == {"sess-a"}


def test_delete_session_with_empty_id_returns_none(tmp_path: Path) -> None:
    root = tmp_path / "state"
    root.mkdir()
    store = CopilotSessionStore(session_state_dir=root, cache_ttl_sec=0.0)

    assert store.delete_session("") is None


def test_delete_sessions_returns_partial_success_on_failure(tmp_path: Path) -> None:
    root = tmp_path / "state"
    root.mkdir()
    _write_session(root, "sess-a")
    _write_session(root, "sess-b")
    _write_session(root, "sess-c")

    # Subclass the store so we can inject a controlled failure for one
    # session id without touching slot-based attributes on the dataclass.
    class _FlakyStore(CopilotSessionStore):
        def delete_session(self, session_id: str) -> Path | None:
            if session_id == "sess-b":
                msg = "simulated permission denied"
                raise OSError(msg)
            return super().delete_session(session_id)

    store = _FlakyStore(session_state_dir=root, cache_ttl_sec=0.0)

    deleted, failures = store.delete_sessions(["sess-a", "sess-b", "sess-c"])

    # The good ids are removed and the failing one is reported -- the
    # method must not abort mid-batch on the first error.
    assert sorted(deleted) == ["sess-a", "sess-c"]
    assert failures == [("sess-b", "simulated permission denied")]
    remaining = {s.session_id for s in store.discover()}
    assert remaining == {"sess-b"}


def test_delete_sessions_with_empty_list_is_noop(tmp_path: Path) -> None:
    root = tmp_path / "state"
    root.mkdir()
    _write_session(root, "sess-a")
    store = CopilotSessionStore(session_state_dir=root, cache_ttl_sec=0.0)

    deleted, failures = store.delete_sessions([])

    assert deleted == []
    assert failures == []
    assert {s.session_id for s in store.discover()} == {"sess-a"}


def test_delete_sessions_invokes_progress_callback_for_each_attempt(
    tmp_path: Path,
) -> None:
    """The UI's "deleting N/M…" indicator depends on per-id progress.

    Without per-attempt callbacks the worker would silently churn and
    the status bar would lie about the operation being stuck at 0/M.
    """
    root = tmp_path / "state"
    root.mkdir()
    _write_session(root, "sess-a")
    _write_session(root, "sess-b")
    _write_session(root, "sess-c")
    store = CopilotSessionStore(session_state_dir=root, cache_ttl_sec=0.0)

    events: list[tuple[int, int, int]] = []

    def _record(deleted: int, failed: int, total: int) -> None:
        events.append((deleted, failed, total))

    deleted, failures = store.delete_sessions(
        ["sess-a", "sess-b", "sess-c"],
        progress_callback=_record,
    )

    assert events == [(1, 0, 3), (2, 0, 3), (3, 0, 3)]
    assert sorted(deleted) == ["sess-a", "sess-b", "sess-c"]
    assert failures == []


def test_delete_sessions_swallows_progress_callback_errors(tmp_path: Path) -> None:
    """A buggy UI callback must not abort the bulk delete mid-flight.

    The store sees the callback as user-supplied code; if it raises
    (e.g. the screen is being torn down concurrently), the deletes
    that already completed have to stick and the remaining ids still
    have to be attempted -- otherwise a transient UI fault leaves
    the on-disk state half-deleted.
    """
    root = tmp_path / "state"
    root.mkdir()
    _write_session(root, "sess-a")
    _write_session(root, "sess-b")
    store = CopilotSessionStore(session_state_dir=root, cache_ttl_sec=0.0)

    def _explode(_deleted: int, _failed: int, _total: int) -> None:
        msg = "ui torn down"
        raise RuntimeError(msg)

    deleted, failures = store.delete_sessions(
        ["sess-a", "sess-b"],
        progress_callback=_explode,
    )

    assert sorted(deleted) == ["sess-a", "sess-b"]
    assert failures == []
    assert {s.session_id for s in store.discover()} == set()


def test_delete_session_under_extra_root(tmp_path: Path) -> None:
    primary = tmp_path / "primary"
    secondary = tmp_path / "secondary"
    primary.mkdir()
    secondary.mkdir()
    _write_session(primary, "sess-a")
    target = _write_session(secondary, "sess-b")
    store = CopilotSessionStore(
        session_state_dir=primary,
        extra_roots=(SessionStoreRoot(secondary, "windows"),),
        cache_ttl_sec=0.0,
    )

    removed = store.delete_session("sess-b")

    assert removed == target
    assert not target.exists()
    # The primary-root session must remain untouched.
    assert (primary / "sess-a").exists()

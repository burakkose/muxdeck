"""Tests for CopilotSessionStore — local session discovery from disk."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from copilot_commander.adapters.copilot_session_store import (
    CopilotSessionStore,
    _parse_session_dir,
    _parse_workspace_yaml,
    _read_last_valid_event,
)

# ── workspace.yaml parsing ──────────────────────────────────────


def test_parse_workspace_yaml_basic(tmp_path: Path) -> None:
    ws = tmp_path / "workspace.yaml"
    ws.write_text(
        "id: abc-123\n"
        "cwd: /home/user/projects/foo\n"
        "repository: user/foo\n"
        "branch: main\n"
        "summary: Fix the bug\n"
        "created_at: 2026-01-15T10:00:00.000Z\n"
        "updated_at: 2026-01-15T12:00:00.000Z\n"
    )
    result = _parse_workspace_yaml(ws)
    assert result["id"] == "abc-123"
    assert result["repository"] == "user/foo"
    assert result["branch"] == "main"
    assert result["summary"] == "Fix the bug"


def test_parse_workspace_yaml_missing_file(tmp_path: Path) -> None:
    result = _parse_workspace_yaml(tmp_path / "nonexistent.yaml")
    assert result == {}


def test_parse_workspace_yaml_empty_values(tmp_path: Path) -> None:
    ws = tmp_path / "workspace.yaml"
    ws.write_text("id: abc\nbranch:\nsummary: \n")
    result = _parse_workspace_yaml(ws)
    assert result["id"] == "abc"
    assert "branch" not in result  # empty value stripped
    assert "summary" not in result


# ── events.jsonl reading ────────────────────────────────────────


def test_read_last_valid_event_normal(tmp_path: Path) -> None:
    events = tmp_path / "events.jsonl"
    events.write_text(
        '{"type":"session.start","timestamp":"2026-01-15T10:00:00Z"}\n'
        '{"type":"tool.execution_start","timestamp":"2026-01-15T10:01:00Z"}\n'
        '{"type":"session.shutdown","timestamp":"2026-01-15T12:00:00Z"}\n'
    )
    result = _read_last_valid_event(events)
    assert result is not None
    assert result["type"] == "session.shutdown"


def test_read_last_valid_event_truncated_last_line(tmp_path: Path) -> None:
    events = tmp_path / "events.jsonl"
    events.write_text(
        '{"type":"session.start","timestamp":"2026-01-15T10:00:00Z"}\n'
        '{"type":"tool.execution_start","timestamp":"2026-01-15T10:01:00Z"}\n'
        '{"type":"session.shutdow'  # truncated — crash
    )
    result = _read_last_valid_event(events)
    assert result is not None
    assert result["type"] == "tool.execution_start"


def test_read_last_valid_event_empty_file(tmp_path: Path) -> None:
    events = tmp_path / "events.jsonl"
    events.write_text("")
    assert _read_last_valid_event(events) is None


def test_read_last_valid_event_missing_file(tmp_path: Path) -> None:
    assert _read_last_valid_event(tmp_path / "nonexistent.jsonl") is None


def test_read_last_valid_event_blank_lines_at_end(tmp_path: Path) -> None:
    events = tmp_path / "events.jsonl"
    events.write_text('{"type":"session.start","timestamp":"2026-01-15T10:00:00Z"}\n\n\n')
    result = _read_last_valid_event(events)
    assert result is not None
    assert result["type"] == "session.start"


# ── session dir parsing ─────────────────────────────────────────


def _make_session(
    tmp_path: Path,
    session_id: str,
    *,
    repository: str = "user/repo",
    branch: str = "main",
    summary: str = "Test session",
    events: list[dict[str, str]] | None = None,
    checkpoints: int = 0,
) -> Path:
    session_dir = tmp_path / session_id
    session_dir.mkdir()
    ws = session_dir / "workspace.yaml"
    now = datetime.now(UTC)
    ws.write_text(
        f"id: {session_id}\n"
        f"cwd: /home/user/projects/test\n"
        f"git_root: /home/user/projects/test\n"
        f"repository: {repository}\n"
        f"branch: {branch}\n"
        f"summary: {summary}\n"
        f"created_at: {now.isoformat()}\n"
        f"updated_at: {now.isoformat()}\n"
    )
    if events:
        ef = session_dir / "events.jsonl"
        ef.write_text("\n".join(json.dumps(e) for e in events) + "\n")
    if checkpoints > 0:
        cp_dir = session_dir / "checkpoints"
        cp_dir.mkdir()
        (cp_dir / "index.md").write_text("# Checkpoints\n")
        for i in range(1, checkpoints + 1):
            (cp_dir / f"{i:03d}-checkpoint.md").write_text(f"# CP {i}\n")
    return session_dir


def test_parse_session_dir_complete(tmp_path: Path) -> None:
    events = [
        {"type": "session.start", "timestamp": "2026-01-15T10:00:00Z"},
        {"type": "session.shutdown", "timestamp": "2026-01-15T12:00:00Z"},
    ]
    sd = _make_session(tmp_path, "abc-123", events=events, checkpoints=3)
    session = _parse_session_dir(sd)
    assert session is not None
    assert session.session_id == "abc-123"
    assert session.repository == "user/repo"
    assert session.is_cleanly_closed is True
    assert session.checkpoint_count == 3
    assert session.last_event_type == "session.shutdown"


def test_parse_session_dir_unclosed(tmp_path: Path) -> None:
    events = [
        {"type": "session.start", "timestamp": "2026-01-15T10:00:00Z"},
        {"type": "tool.execution_start", "timestamp": "2026-01-15T10:30:00Z"},
    ]
    sd = _make_session(tmp_path, "def-456", events=events)
    session = _parse_session_dir(sd)
    assert session is not None
    assert session.is_cleanly_closed is False
    assert session.last_event_type == "tool.execution_start"


def test_parse_session_dir_no_workspace(tmp_path: Path) -> None:
    sd = tmp_path / "empty-session"
    sd.mkdir()
    assert _parse_session_dir(sd) is None


def test_parse_session_dir_no_events(tmp_path: Path) -> None:
    sd = _make_session(tmp_path, "no-events")
    session = _parse_session_dir(sd)
    assert session is not None
    assert session.last_event_type is None
    assert session.is_cleanly_closed is False


# ── CopilotSessionStore ────────────────────────────────────────


def test_store_discover_multiple(tmp_path: Path) -> None:
    _make_session(
        tmp_path,
        "s1",
        summary="First",
        events=[{"type": "session.shutdown", "timestamp": "2026-01-15T12:00:00Z"}],
    )
    _make_session(
        tmp_path,
        "s2",
        summary="Second",
        events=[{"type": "tool.execution_start", "timestamp": "2026-01-15T11:00:00Z"}],
    )
    store = CopilotSessionStore(session_state_dir=tmp_path, cache_ttl_sec=0)
    sessions = store.discover()
    assert len(sessions) == 2
    # Both found
    ids = {s.session_id for s in sessions}
    assert ids == {"s1", "s2"}


def test_store_caching(tmp_path: Path) -> None:
    _make_session(tmp_path, "cached-1", summary="Cached")
    store = CopilotSessionStore(session_state_dir=tmp_path, cache_ttl_sec=60)
    first = store.discover()
    assert len(first) == 1
    # Add another session — should NOT appear due to cache
    _make_session(tmp_path, "cached-2", summary="New")
    second = store.discover()
    assert len(second) == 1
    # Force refresh should find it
    third = store.discover(force=True)
    assert len(third) == 2


def test_store_get_session(tmp_path: Path) -> None:
    _make_session(tmp_path, "lookup-1", summary="Find me")
    store = CopilotSessionStore(session_state_dir=tmp_path, cache_ttl_sec=0)
    found = store.get_session("lookup-1")
    assert found is not None
    assert found.summary == "Find me"
    assert store.get_session("nonexistent") is None


def test_store_get_session_warm_only_skips_rescan(tmp_path: Path) -> None:
    """Warm-only lookups must never trigger a disk scan.

    Cursor movement in the Sessions screen calls this on the UI
    thread, so a rescan would freeze the UI for multi-second 9P walks.
    We verify this by deleting the session dir after the initial warm
    scan: a warm-only lookup must still return the cached entry (no
    rescan observes the deletion), while the default lookup re-scans
    and returns None.
    """
    _make_session(tmp_path, "warm-1", summary="First")
    store = CopilotSessionStore(session_state_dir=tmp_path, cache_ttl_sec=60)
    store.discover()
    # Expire the TTL and delete the backing session dir.
    store._cache_time = 0.0
    import shutil

    shutil.rmtree(tmp_path / "warm-1")

    warm = store.get_session("warm-1", warm_only=True)
    assert warm is not None
    assert warm.summary == "First"
    # Default lookup triggers a rescan, which now finds nothing.
    store._cache_time = 0.0
    assert store.get_session("warm-1") is None


def test_store_empty_dir(tmp_path: Path) -> None:
    store = CopilotSessionStore(session_state_dir=tmp_path, cache_ttl_sec=0)
    assert store.discover() == []


def test_store_nonexistent_dir() -> None:
    store = CopilotSessionStore(
        session_state_dir=Path("/nonexistent/path"),
        cache_ttl_sec=0,
    )
    assert store.discover() == []


def test_store_age_filtering(tmp_path: Path) -> None:
    # Create a session with old updated_at
    sd = tmp_path / "old-session"
    sd.mkdir()
    old_time = (datetime.now(UTC) - timedelta(days=100)).isoformat()
    (sd / "workspace.yaml").write_text(
        f"id: old-session\nsummary: Old\ncreated_at: {old_time}\nupdated_at: {old_time}\n"
    )
    store = CopilotSessionStore(
        session_state_dir=tmp_path,
        max_age_days=30,
        cache_ttl_sec=0,
    )
    sessions = store.discover()
    assert len(sessions) == 0  # filtered by age

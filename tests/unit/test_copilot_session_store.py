"""Tests for CopilotSessionStore — local session discovery from disk."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from muxdeck.adapters.copilot_session_store import (
    CopilotSessionStore,
    _CachedEntry,
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


def test_parse_workspace_yaml_literal_block_scalar_strip(tmp_path: Path) -> None:
    """``summary: |-`` must capture the indented continuation text.

    Copilot CLI emits multi-line summaries for ACP-backed sessions
    using YAML literal block scalars. The previous minimal parser
    stored the literal string ``|-`` as the summary, which then showed
    up verbatim in the sessions list. The parser must now dedent the
    continuation lines, preserve internal newlines, and strip the
    trailing newline implied by the ``-`` chomping indicator.
    """
    ws = tmp_path / "workspace.yaml"
    ws.write_text(
        "id: abc-123\n"
        "summary: |-\n"
        "  You are being used as the active ACP agent backend.\n"
        "\n"
        "  Use ACP capabilities to complete tasks.\n"
        "repository: user/foo\n"
    )
    result = _parse_workspace_yaml(ws)
    assert result["id"] == "abc-123"
    assert result["repository"] == "user/foo"
    assert result["summary"] == (
        "You are being used as the active ACP agent backend.\n"
        "\n"
        "Use ACP capabilities to complete tasks."
    )


def test_parse_workspace_yaml_literal_block_keeps_trailing_newlines(tmp_path: Path) -> None:
    """``|`` (no chomping) keeps trailing blank lines removed, ``|+`` keeps them.

    The UI doesn't rely on terminal newlines, but the chomping
    distinction matters for callers that join multiple fields. This
    test locks in both variants emitting sensible content.
    """
    ws = tmp_path / "workspace.yaml"
    ws.write_text("summary: |\n  line one\n  line two\nnext: value\n")
    result = _parse_workspace_yaml(ws)
    assert result["summary"] == "line one\nline two"
    assert result["next"] == "value"


def test_parse_workspace_yaml_folded_block_scalar(tmp_path: Path) -> None:
    """``>`` folds consecutive non-empty lines into space-separated text."""
    ws = tmp_path / "workspace.yaml"
    ws.write_text(
        "summary: >-\n  fix the\n  broken parser\n\n  and add tests\nrepository: user/foo\n"
    )
    result = _parse_workspace_yaml(ws)
    assert result["summary"] == "fix the broken parser\n\nand add tests"
    assert result["repository"] == "user/foo"


def test_parse_workspace_yaml_block_scalar_at_eof(tmp_path: Path) -> None:
    """A block scalar may be the final key in the file — no next key to terminate it.

    Trailing empty lines must be trimmed so the scalar doesn't carry
    dangling newlines that would later render as blank rows.
    """
    ws = tmp_path / "workspace.yaml"
    ws.write_text("id: abc\nsummary: |-\n  only summary\n  more summary\n\n")
    result = _parse_workspace_yaml(ws)
    assert result["summary"] == "only summary\nmore summary"


def test_parse_workspace_yaml_strips_surrounding_quotes(tmp_path: Path) -> None:
    ws = tmp_path / "workspace.yaml"
    ws.write_text("summary: \"Quoted summary with : colons\"\nbranch: 'single-quoted'\n")
    result = _parse_workspace_yaml(ws)
    assert result["summary"] == "Quoted summary with : colons"
    assert result["branch"] == "single-quoted"


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
    events: Sequence[Mapping[str, object]] | None = None,
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


def test_parse_session_dir_reads_name_field(tmp_path: Path) -> None:
    """Newer Copilot CLI sessions only carry the canonical ``name``.

    Operators reported sessions appearing as nameless rows in the
    UI even though ``copilot --resume`` showed a clear title -- the
    title lives in ``workspace.yaml`` under the ``name:`` key (set
    by the ``/name`` command or auto-generated). The parser must
    surface it on ``CopilotLocalSession.name`` so the controller
    can prefer it over the legacy ``summary`` field.
    """
    session_dir = tmp_path / "name-session"
    session_dir.mkdir()
    (session_dir / "workspace.yaml").write_text(
        "id: name-session\ncwd: /tmp/work\nname: Build Configuration Subscriber\nuser_named: true\n"
    )

    session = _parse_session_dir(session_dir)
    assert session is not None
    assert session.name == "Build Configuration Subscriber"
    assert session.summary is None


def test_parse_session_dir_reads_both_name_and_summary(tmp_path: Path) -> None:
    """Older sessions carry both fields -- both must round-trip.

    For sessions in the transition window Copilot CLI writes
    ``name`` and ``summary`` side by side, often with identical
    values. Surfacing both lets the controller still treat ``name``
    as canonical without losing the legacy ``summary`` for any
    consumer that wants it.
    """
    session_dir = tmp_path / "dual-session"
    session_dir.mkdir()
    (session_dir / "workspace.yaml").write_text(
        "id: dual-session\n"
        "cwd: /tmp/work\n"
        "name: Limit Agent Model Choices\n"
        "summary: Limit Agent Model Choices\n"
    )

    session = _parse_session_dir(session_dir)
    assert session is not None
    assert session.name == "Limit Agent Model Choices"
    assert session.summary == "Limit Agent Model Choices"


def test_parse_session_dir_extracts_shutdown_usage(tmp_path: Path) -> None:
    events: list[dict[str, object]] = [
        {"type": "session.start", "timestamp": "2026-01-15T10:00:00Z"},
        {
            "type": "session.shutdown",
            "timestamp": "2026-01-15T12:00:00Z",
            "data": {
                "totalPremiumRequests": 4,
                "modelMetrics": {
                    "gpt-5.4": {
                        "usage": {
                            "inputTokens": 1200,
                            "outputTokens": 45,
                            "cacheReadTokens": 400,
                            "cacheWriteTokens": 0,
                        }
                    },
                    "claude-opus-4.6": {
                        "usage": {
                            "inputTokens": 300,
                            "outputTokens": 30,
                            "cacheReadTokens": 50,
                            "cacheWriteTokens": 10,
                        }
                    },
                },
            },
        },
    ]
    sd = _make_session(tmp_path, "usage-123", events=events)
    session = _parse_session_dir(sd)
    assert session is not None
    assert session.usage is not None
    assert session.usage.input_tokens == 1500
    assert session.usage.output_tokens == 75
    assert session.usage.cache_read_tokens == 450
    assert session.usage.cache_write_tokens == 10
    assert session.usage.total_tokens == 2035
    assert session.usage.premium_requests == 4


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
    assert session.usage is None


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


def test_parse_session_dir_empty_workspace_falls_back_to_event_timestamp(
    tmp_path: Path,
) -> None:
    """A 0-byte ``workspace.yaml`` (Copilot CLI truncated it on shutdown)
    must not strand the session at the bottom of the SESSIONS list with
    a blank ``updated`` column. ``_parse_session_dir`` should fall back
    to the last event timestamp from ``events.jsonl`` so the session
    sorts by recency and stays findable.
    """
    sd = tmp_path / "truncated-yaml"
    sd.mkdir()
    (sd / "workspace.yaml").write_text("")
    last_event_iso = "2026-05-14T22:54:26.021Z"
    events = [
        {"type": "session.start", "timestamp": "2026-05-14T19:25:11.190Z"},
        {"type": "session.shutdown", "timestamp": last_event_iso},
    ]
    (sd / "events.jsonl").write_text("\n".join(json.dumps(e) for e in events) + "\n")

    session = _parse_session_dir(sd)

    assert session is not None
    assert session.session_id == "truncated-yaml"
    assert session.is_cleanly_closed is True
    assert session.last_event_at is not None
    assert session.last_event_at == datetime(2026, 5, 14, 22, 54, 26, 21000, tzinfo=UTC)
    # ``updated_at`` falls back to the events.jsonl tail timestamp so
    # the row sorts by recency rather than to ``datetime.min``.
    assert session.updated_at == session.last_event_at
    # ``created_at`` falls back to the workspace.yaml mtime — there's
    # no cheap "first event" timestamp to read on the cold scan path.
    assert session.created_at is not None


def test_parse_session_dir_empty_workspace_and_no_events_uses_file_mtimes(
    tmp_path: Path,
) -> None:
    """If both ``workspace.yaml`` is empty *and* ``events.jsonl`` is
    missing or empty, fall back to the on-disk file mtime so the row
    still has a timestamp anchor instead of collapsing to ``None``.
    """
    sd = tmp_path / "really-broken"
    sd.mkdir()
    workspace_path = sd / "workspace.yaml"
    workspace_path.write_text("")

    session = _parse_session_dir(sd)

    assert session is not None
    assert session.last_event_at is None
    assert session.updated_at is not None
    assert session.created_at is not None
    # Both timestamps anchored to the workspace.yaml mtime.
    assert session.updated_at == session.created_at


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


# ── parser helper edge cases ────────────────────────────────────────


def test_parse_iso_returns_none_for_empty_or_malformed() -> None:
    from muxdeck.adapters.copilot_session_store import _parse_iso

    assert _parse_iso("") is None
    assert _parse_iso(None) is None
    assert _parse_iso("not-a-date") is None
    # Trailing 'Z' is normalized to +00:00 — sanity check the happy path.
    parsed = _parse_iso("2026-01-15T10:00:00Z")
    assert parsed is not None
    assert parsed.tzinfo is not None


def test_parse_workspace_yaml_skips_indented_lines_and_comments(tmp_path: Path) -> None:
    ws = tmp_path / "workspace.yaml"
    ws.write_text(
        "# leading comment\n"
        "  indented_at_top: ignored\n"  # leading whitespace → skipped
        "no_colon_line\n"  # no colon → skipped
        ": empty_key\n"  # blank key → skipped
        "\n"
        "id: real-id\n"
        "url: http://example.com/path # inline comment\n"
    )
    result = _parse_workspace_yaml(ws)
    assert result == {"id": "real-id", "url": "http://example.com/path"}


def test_apply_block_scalar_returns_empty_when_no_lines() -> None:
    from muxdeck.adapters.copilot_session_store import _apply_block_scalar

    assert _apply_block_scalar([], "|-") == ""


def test_apply_block_scalar_handles_blank_paragraphs_in_folded_mode() -> None:
    from muxdeck.adapters.copilot_session_store import _apply_block_scalar

    # Folded with one paragraph followed by a blank line and EOF — the
    # blank line is preserved, but no trailing buffer flush happens.
    text = _apply_block_scalar(["one", "two", "", "three"], ">")
    assert text == "one two\n\nthree"


def test_count_checkpoints_returns_zero_when_dir_missing(tmp_path: Path) -> None:
    from muxdeck.adapters.copilot_session_store import _count_checkpoints

    sd = tmp_path / "no-cp-session"
    sd.mkdir()
    assert _count_checkpoints(sd) == 0


def test_count_checkpoints_ignores_index_md_and_non_md(tmp_path: Path) -> None:
    from muxdeck.adapters.copilot_session_store import _count_checkpoints

    sd = tmp_path / "session"
    cp = sd / "checkpoints"
    cp.mkdir(parents=True)
    (cp / "index.md").write_text("# index")
    (cp / "001-good.md").write_text("# good")
    (cp / "002-also-good.md").write_text("# good")
    (cp / "junk.txt").write_text("not md")
    assert _count_checkpoints(sd) == 2


def test_as_int_handles_various_inputs() -> None:
    from muxdeck.adapters.copilot_session_store import _as_int

    assert _as_int(42) == 42
    assert _as_int("42") == 42
    assert _as_int("1,234") == 1234
    assert _as_int("-99") == -99
    # Bool must NOT be treated as int.
    assert _as_int(True) is None
    assert _as_int(False) is None
    # Non-digit strings, lists, dicts → None.
    assert _as_int("abc") is None
    assert _as_int(None) is None
    assert _as_int([1]) is None


def test_extract_session_usage_returns_none_for_non_shutdown_event() -> None:
    from muxdeck.adapters.copilot_session_store import _extract_session_usage

    # Wrong event type → None.
    assert (
        _extract_session_usage(
            {"type": "tool.execution_start", "data": {"totalPremiumRequests": 5}}
        )
        is None
    )
    # None input → None.
    assert _extract_session_usage(None) is None


def test_extract_session_usage_returns_none_when_data_not_dict() -> None:
    from muxdeck.adapters.copilot_session_store import _extract_session_usage

    assert _extract_session_usage({"type": "session.shutdown", "data": "not-a-dict"}) is None


def test_extract_session_usage_returns_none_when_no_usage_or_premium() -> None:
    from muxdeck.adapters.copilot_session_store import _extract_session_usage

    # No modelMetrics, no totalPremiumRequests → None.
    result = _extract_session_usage({"type": "session.shutdown", "data": {}})
    assert result is None


def test_extract_session_usage_skips_non_dict_entries() -> None:
    from muxdeck.adapters.copilot_session_store import _extract_session_usage

    # modelMetrics with non-dict details and details with non-dict usage.
    payload: dict[str, object] = {
        "type": "session.shutdown",
        "data": {
            "totalPremiumRequests": 1,
            "modelMetrics": {
                "broken-1": "not-a-dict",
                "broken-2": {"usage": "still-not-a-dict"},
            },
        },
    }
    result = _extract_session_usage(payload)
    assert result is not None
    # No usage entries seen → token fields stay None, premium captured.
    assert result.input_tokens is None
    assert result.premium_requests == 1


def test_extract_session_usage_total_tokens_sums_when_some_none() -> None:
    from muxdeck.adapters.copilot_session_store import CopilotSessionUsage

    # If every component is None, total_tokens is None.
    usage = CopilotSessionUsage()
    assert usage.total_tokens is None

    # If at least one component is present, total_tokens sums it.
    usage = CopilotSessionUsage(input_tokens=10, output_tokens=5)
    assert usage.total_tokens == 15


# ── _iter_roots / _scan_root edge cases ──────────────────────────────


def test_iter_roots_dedups_extra_root_matching_primary(tmp_path: Path) -> None:
    from muxdeck.adapters.copilot_session_store import SessionStoreRoot

    store = CopilotSessionStore(
        session_state_dir=tmp_path,
        cache_ttl_sec=0,
        extra_roots=(SessionStoreRoot(tmp_path, "windows"),),
    )
    roots = store._iter_roots()
    # Duplicate path is dropped — only the local root remains.
    assert len(roots) == 1
    assert roots[0].origin == "local"


def test_scan_root_returns_empty_for_missing_dir(tmp_path: Path) -> None:
    from muxdeck.adapters.copilot_session_store import SessionStoreRoot

    store = CopilotSessionStore(session_state_dir=tmp_path / "absent", cache_ttl_sec=0)
    sessions, paths = store._scan_root(
        SessionStoreRoot(tmp_path / "absent", "local"),
        cutoff=None,
    )
    assert sessions == []
    assert paths == set()


def test_scan_root_returns_empty_when_root_is_empty(tmp_path: Path) -> None:
    from muxdeck.adapters.copilot_session_store import SessionStoreRoot

    store = CopilotSessionStore(session_state_dir=tmp_path, cache_ttl_sec=0)
    sessions, paths = store._scan_root(
        SessionStoreRoot(tmp_path, "local"),
        cutoff=None,
    )
    assert sessions == []
    assert paths == set()


def test_scan_root_swallows_scandir_oserror(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import muxdeck.adapters.copilot_session_store as mod
    from muxdeck.adapters.copilot_session_store import SessionStoreRoot

    real_scandir = mod.os.scandir  # type: ignore[attr-defined]

    def fake_scandir(path: object, /) -> object:
        raise OSError("permission denied")

    monkeypatch.setattr(mod.os, "scandir", fake_scandir)  # type: ignore[attr-defined]
    try:
        store = CopilotSessionStore(session_state_dir=tmp_path, cache_ttl_sec=0)
        sessions, paths = store._scan_root(
            SessionStoreRoot(tmp_path, "local"),
            cutoff=None,
        )
        assert sessions == []
        assert paths == set()
    finally:
        monkeypatch.setattr(mod.os, "scandir", real_scandir)  # type: ignore[attr-defined]


def test_scan_drops_session_dir_when_directory_disappears(tmp_path: Path) -> None:
    """Full deletion of a session dir must drop it from the cache.

    The per-entry cache holds parsed sessions keyed by absolute path,
    so the scan also has to prune entries whose directory has gone
    away. We pin this via the live-paths cleanup pass in
    :meth:`CopilotSessionStore._scan`: after the scan, any cached
    entry whose path was not seen by ``os.scandir`` is dropped.

    Note: this test specifically exercises *full* directory deletion.
    Surgical removal of just ``workspace.yaml`` (with the directory and
    ``events.jsonl`` left intact) is intentionally NOT detected on the
    warm path -- the cache key is the events.jsonl mtime and workspace
    deletes that don't bump it are accepted as the cost of skipping
    the second stat per session per scan. ``/name`` and resume always
    bump events.jsonl, and Copilot CLI removes the whole directory
    when a session is deleted.
    """
    _make_session(tmp_path, "to-be-removed", summary="Now you see me")
    store = CopilotSessionStore(session_state_dir=tmp_path, cache_ttl_sec=0)
    first = store.discover()
    assert any(s.session_id == "to-be-removed" for s in first)
    # Remove the entire session directory -- the realistic teardown.
    import shutil

    shutil.rmtree(tmp_path / "to-be-removed")
    second = store.discover(force=True)
    assert all(s.session_id != "to-be-removed" for s in second)
    # And the per-entry cache entry must be gone too.
    assert not any(path.name == "to-be-removed" for path in store._entry_cache), (
        "stale per-entry cache entry should be evicted by the live-paths sweep"
    )


def test_scan_dedups_when_same_id_appears_in_local_and_windows(tmp_path: Path) -> None:
    from muxdeck.adapters.copilot_session_store import SessionStoreRoot

    # Same session_id in both local and windows roots — local must win.
    local_root = tmp_path / "local"
    windows_root = tmp_path / "windows"
    local_root.mkdir()
    windows_root.mkdir()
    _make_session(local_root, "shared-id", summary="local-side")
    _make_session(windows_root, "shared-id", summary="windows-side")

    store = CopilotSessionStore(
        session_state_dir=local_root,
        cache_ttl_sec=0,
        extra_roots=(SessionStoreRoot(windows_root, "windows"),),
    )
    sessions = store.discover()
    matches = [s for s in sessions if s.session_id == "shared-id"]
    assert len(matches) == 1
    assert matches[0].origin == "local"
    assert matches[0].summary == "local-side"


def test_set_extra_roots_invalidates_top_level_cache(tmp_path: Path) -> None:
    _make_session(tmp_path, "warm-1", summary="Warm")
    store = CopilotSessionStore(session_state_dir=tmp_path, cache_ttl_sec=60)
    store.discover()
    assert store._cache_time > 0.0
    # Replacing extras must blow away the top-level cache so the next
    # discover() re-walks. Per-entry cache stays intact (verified via
    # the fact that discover still returns the original entry).
    store.set_extra_roots(())
    assert store._cache_time == 0.0
    assert store._cache == []
    refreshed = store.discover()
    assert any(s.session_id == "warm-1" for s in refreshed)


def test_warm_path_uses_events_mtime_only_for_cache_validation(tmp_path: Path) -> None:
    """The cache-hit path keys only on ``events.jsonl`` mtime.

    Bumping ``workspace.yaml`` without touching ``events.jsonl`` must
    NOT bust the cache: ``/name`` and resume both also emit events,
    so the events mtime is the right invalidation signal. Skipping
    the second stat halves the per-session syscall cost on 9P-mounted
    Windows session-state roots, where every stat is a network round
    trip.
    """
    import os

    sd = _make_session(
        tmp_path,
        "warm-1",
        summary="initial summary",
        events=[{"type": "session.start", "timestamp": "2026-01-15T10:00:00Z"}],
    )
    store = CopilotSessionStore(session_state_dir=tmp_path, cache_ttl_sec=0)

    first = store.discover()
    assert len(first) == 1
    assert first[0].summary == "initial summary"

    # Edit workspace.yaml only -- bump its mtime well past the cached
    # value but leave events.jsonl untouched.
    ws = sd / "workspace.yaml"
    text = ws.read_text().replace("initial summary", "edited summary")
    ws.write_text(text)
    new_ws_ts = ws.stat().st_mtime + 5
    os.utime(ws, (new_ws_ts, new_ws_ts))

    # Cache must hit -- the workspace mtime change is intentionally
    # ignored on the warm path, so the parser is NOT re-invoked and
    # we still see the original summary.
    second = store.discover(force=True)
    assert len(second) == 1
    assert second[0].summary == "initial summary"


def test_warm_path_revalidates_when_events_mtime_changes(tmp_path: Path) -> None:
    """Touching ``events.jsonl`` must trigger a re-parse.

    Companion to the workspace-only test above: this is the change
    signal we DO trust. Once events.jsonl moves, the cached
    ``CopilotLocalSession`` is dropped and the next discover() reads
    the updated workspace.yaml.
    """
    import os

    sd = _make_session(
        tmp_path,
        "warm-2",
        summary="initial summary",
        events=[{"type": "session.start", "timestamp": "2026-01-15T10:00:00Z"}],
    )
    store = CopilotSessionStore(session_state_dir=tmp_path, cache_ttl_sec=0)

    first = store.discover()
    assert first[0].summary == "initial summary"

    # Update workspace.yaml AND bump events.jsonl mtime explicitly --
    # mtime granularity on some filesystems is too coarse to trust an
    # in-test append within the same second.
    ws = sd / "workspace.yaml"
    ws.write_text(ws.read_text().replace("initial summary", "edited summary"))

    ev = sd / "events.jsonl"
    with ev.open("a") as fh:
        fh.write(json.dumps({"type": "session.note", "timestamp": "2026-01-15T11:00:00Z"}))
        fh.write("\n")
    new_ev_ts = ev.stat().st_mtime + 5
    os.utime(ev, (new_ev_ts, new_ev_ts))

    second = store.discover(force=True)
    assert len(second) == 1
    assert second[0].summary == "edited summary"


def test_invalidate_drops_top_level_cache_keeps_entry_cache(tmp_path: Path) -> None:
    """``invalidate`` flushes the TTL gate but keeps the mtime cache.

    Action handlers (e.g. resume) call this so the next disk-read
    sees fresh state without paying the cost of a full re-parse for
    files that didn't change. We pin both halves: the TTL cache
    drops to zero, and the per-entry cache preserves the entries
    keyed by absolute path so a follow-up ``discover()`` only pays
    for new/changed dirs.
    """
    _make_session(tmp_path, "preserve-1", summary="A")
    store = CopilotSessionStore(session_state_dir=tmp_path, cache_ttl_sec=60)
    first = store.discover()
    assert len(first) == 1
    assert store._cache_time > 0.0
    assert store._entry_cache, "discover should have populated the entry cache"
    entry_cache_snapshot = dict(store._entry_cache)

    store.invalidate()

    assert store._cache == []
    assert store._cache_time == 0.0
    assert store._by_id == {}
    # Per-entry cache must survive so the next scan can hit the warm
    # mtime path for files that didn't change.
    assert store._entry_cache == entry_cache_snapshot


def test_invalidate_makes_subsequent_discover_see_new_session(tmp_path: Path) -> None:
    """The post-invalidate discover must observe disk changes.

    This is the Sessions-screen contract: after the operator
    presses ``R``, the resume action invalidates the cache so the
    next sync-driven refresh paints the freshly active session
    rather than the stale "completed" state.
    """
    _make_session(tmp_path, "before-1", summary="A")
    # Long TTL so we know discover() would have returned the cache
    # without invalidation -- the only way the new session can
    # appear is via the explicit invalidate() call.
    store = CopilotSessionStore(session_state_dir=tmp_path, cache_ttl_sec=600)
    initial = store.discover()
    assert len(initial) == 1

    _make_session(tmp_path, "after-1", summary="B")

    # Without invalidate(), the long TTL hides the new session.
    cached = store.discover()
    assert len(cached) == 1

    store.invalidate()
    refreshed = store.discover()
    ids = {s.session_id for s in refreshed}
    assert ids == {"before-1", "after-1"}


def test_default_cache_ttl_is_short_enough_for_renames() -> None:
    """The TTL must keep ``/name`` edits visible within ~10 s.

    The previous 300 s ceiling masked Copilot CLI state changes for
    five minutes after the operator renamed a session inside the
    resumed shell. Pin the contract so a future tweak doesn't
    silently regress operator UX.
    """
    store = CopilotSessionStore()
    assert store.cache_ttl_sec <= 10.0


def test_scan_local_only_returns_local_sessions_without_touching_extras(
    tmp_path: Path,
) -> None:
    """The fast-pass path skips secondary roots entirely.

    The SESSIONS screen calls this to paint Linux sessions in the
    first ~50 ms even when the slow Windows-mounted root would take
    seconds to walk -- so the fast-pass must NOT scan the extras at
    all. Pin both halves of that contract: only the primary root's
    sessions come back, AND the entry cache for windows entries is
    untouched (no opportunistic warming, no eviction).
    """
    from muxdeck.adapters.copilot_session_store import SessionStoreRoot

    local_root = tmp_path / "local"
    windows_root = tmp_path / "windows"
    local_root.mkdir()
    windows_root.mkdir()
    _make_session(local_root, "linux-1", summary="from linux")
    _make_session(windows_root, "windows-1", summary="from windows")

    store = CopilotSessionStore(
        session_state_dir=local_root,
        cache_ttl_sec=60,
        extra_roots=(SessionStoreRoot(windows_root, "windows"),),
    )

    fast = store.scan_local_only()
    assert [s.session_id for s in fast] == ["linux-1"]
    assert all(s.origin == "local" for s in fast)
    # The TTL cache must NOT be primed from the partial pass -- a
    # subsequent discover() needs to see the windows root too. If
    # scan_local_only updated _by_id, the next discover() inside the
    # TTL window would mistakenly return only the partial set.
    assert store._cache == []
    assert store._by_id == {}


def test_scan_local_only_sorts_newest_first(tmp_path: Path) -> None:
    """Fast-pass must keep the same newest-first ordering as discover().

    The SESSIONS list relies on stable ordering between the partial
    and the full paint -- the rows shouldn't reshuffle when the
    Windows sessions land. Pin newest-first by ``updated_at``.
    """
    from datetime import timedelta

    older = datetime.now(UTC) - timedelta(hours=2)
    newer = datetime.now(UTC) - timedelta(minutes=5)

    sd_older = tmp_path / "older"
    sd_older.mkdir()
    (sd_older / "workspace.yaml").write_text(
        f"id: older\nsummary: o\ncreated_at: {older.isoformat()}\nupdated_at: {older.isoformat()}\n"
    )
    sd_newer = tmp_path / "newer"
    sd_newer.mkdir()
    (sd_newer / "workspace.yaml").write_text(
        f"id: newer\nsummary: n\ncreated_at: {newer.isoformat()}\nupdated_at: {newer.isoformat()}\n"
    )

    store = CopilotSessionStore(session_state_dir=tmp_path, cache_ttl_sec=0)
    result = store.scan_local_only()
    assert [s.session_id for s in result] == ["newer", "older"]


def test_scan_local_only_handles_missing_root() -> None:
    """An absent local root yields an empty list, not an exception."""
    store = CopilotSessionStore(
        session_state_dir=Path("/nonexistent/muxdeck/scan-local-only"),
        cache_ttl_sec=0,
    )
    assert store.scan_local_only() == []


def test_count_by_origin_categorises_sessions(tmp_path: Path) -> None:
    _make_session(tmp_path, "local-1", summary="L")
    store = CopilotSessionStore(session_state_dir=tmp_path, cache_ttl_sec=0)
    assert store.count_by_origin("local") == 1
    assert store.count_by_origin("windows") == 0


# ── Persistent cache ────────────────────────────────────────────────


def test_persistent_cache_round_trip_seeds_new_store(tmp_path: Path) -> None:
    """A fresh store seeded from disk skips re-parsing unchanged sessions.

    First store does a cold scan and writes the cache file. Second
    store points at the same file and the same session dir; its
    discover() must reuse the persisted ``_entry_cache`` and skip the
    full parse path. Pin both halves: the second store's ``discover``
    returns the same session AND the parser is NOT invoked (we patch
    ``_parse_session_dir`` to detect any call).
    """
    cache_path = tmp_path / "cache" / "sessions.json"
    sessions_root = tmp_path / "sessions"
    sessions_root.mkdir()
    _make_session(sessions_root, "persisted-1", summary="Persisted")

    store1 = CopilotSessionStore(
        session_state_dir=sessions_root,
        cache_ttl_sec=0,
        persistent_cache_path=cache_path,
    )
    first = store1.discover()
    assert len(first) == 1
    assert first[0].summary == "Persisted"
    assert cache_path.exists()

    # Second store, fresh in-memory state, but same cache file.
    store2 = CopilotSessionStore(
        session_state_dir=sessions_root,
        cache_ttl_sec=0,
        persistent_cache_path=cache_path,
    )

    from muxdeck.adapters import copilot_session_store as mod

    parse_calls = 0
    real_parse = mod._parse_session_dir

    def _counting_parse(*args: object, **kwargs: object) -> object:
        nonlocal parse_calls
        parse_calls += 1
        return real_parse(*args, **kwargs)  # type: ignore[arg-type]

    original = mod._parse_session_dir
    try:
        mod._parse_session_dir = _counting_parse  # type: ignore[assignment]
        second = store2.discover()
    finally:
        mod._parse_session_dir = original  # type: ignore[assignment]

    assert len(second) == 1
    assert second[0].summary == "Persisted"
    assert parse_calls == 0, "persistent cache should bypass _parse_session_dir"


def test_persistent_cache_corrupt_file_falls_back_to_cold_scan(tmp_path: Path) -> None:
    """A corrupt cache file must not crash the store."""
    cache_path = tmp_path / "cache" / "sessions.json"
    cache_path.parent.mkdir(parents=True)
    cache_path.write_text("this is not json {{{")

    sessions_root = tmp_path / "sessions"
    sessions_root.mkdir()
    _make_session(sessions_root, "fallback-1", summary="Fresh")

    store = CopilotSessionStore(
        session_state_dir=sessions_root,
        cache_ttl_sec=0,
        persistent_cache_path=cache_path,
    )
    # Must not raise; cold scan succeeds and overwrites the bad file.
    result = store.discover()
    assert len(result) == 1
    assert result[0].summary == "Fresh"
    # The save path should also recover and produce a valid file.
    import json as _json

    payload = _json.loads(cache_path.read_text())
    assert isinstance(payload, dict)
    assert payload.get("version") == 1


def test_persistent_cache_version_mismatch_drops_cache(tmp_path: Path) -> None:
    """Older/newer cache schema versions are ignored."""
    import json as _json

    cache_path = tmp_path / "cache" / "sessions.json"
    cache_path.parent.mkdir(parents=True)
    cache_path.write_text(_json.dumps({"version": 999, "entries": []}))

    sessions_root = tmp_path / "sessions"
    sessions_root.mkdir()
    _make_session(sessions_root, "v-mismatch", summary="Should be re-parsed")

    store = CopilotSessionStore(
        session_state_dir=sessions_root,
        cache_ttl_sec=0,
        persistent_cache_path=cache_path,
    )
    result = store.discover()
    # The mismatched cache was dropped; result comes from a fresh
    # parse of the on-disk session.
    assert len(result) == 1
    assert result[0].summary == "Should be re-parsed"


def test_persistent_cache_filters_entries_outside_configured_roots(tmp_path: Path) -> None:
    """Loaded entries outside the current root set must be ignored.

    A cache file written by a previous muxdeck run with different
    roots may contain entries the current store can never observe.
    Loading them into the in-memory cache would risk drift between
    what's surfaced and what's actually configured. Pin that they're
    silently filtered out.
    """
    import json as _json

    foreign_dir = tmp_path / "foreign-root"
    foreign_dir.mkdir()
    foreign_session_dir = foreign_dir / "ghost-session"
    foreign_session_dir.mkdir()

    cache_path = tmp_path / "cache" / "sessions.json"
    cache_path.parent.mkdir(parents=True)
    cache_path.write_text(
        _json.dumps(
            {
                "version": 1,
                "entries": [
                    {
                        "path": str(foreign_session_dir),
                        "events_mtime_ns": 0,
                        "workspace_mtime_ns": 0,
                        "session": {
                            "session_id": "ghost-session",
                            "summary": "should not appear",
                            "origin": "local",
                            "checkpoint_count": 0,
                            "is_cleanly_closed": False,
                        },
                    }
                ],
            }
        )
    )

    sessions_root = tmp_path / "sessions"
    sessions_root.mkdir()
    # Note: no real session in sessions_root; result must be empty.

    store = CopilotSessionStore(
        session_state_dir=sessions_root,
        cache_ttl_sec=0,
        persistent_cache_path=cache_path,
    )
    result = store.discover()
    assert result == []
    assert all(not str(p).startswith(str(foreign_dir)) for p in store._entry_cache), (
        "foreign-root entries should be filtered out on load"
    )


def test_persistent_cache_disabled_when_path_is_none(tmp_path: Path) -> None:
    """No path = no persistence (default for tests + direct construction)."""
    sessions_root = tmp_path / "sessions"
    sessions_root.mkdir()
    _make_session(sessions_root, "no-persist", summary="x")

    store = CopilotSessionStore(
        session_state_dir=sessions_root,
        cache_ttl_sec=0,
        persistent_cache_path=None,
    )
    store.discover()
    # No file should appear anywhere under tmp_path.
    cache_files = list(tmp_path.rglob("*.json"))
    assert cache_files == []


def test_persistent_cache_writes_outside_lock(tmp_path: Path) -> None:
    """The disk write must NOT happen while the scan lock is held.

    Otherwise concurrent ``discover``/``get_session`` callers would
    queue behind a slow 9P rename. Pin this by replacing the save
    helper with one that asserts the lock is currently free.
    """
    cache_path = tmp_path / "cache" / "sessions.json"
    sessions_root = tmp_path / "sessions"
    sessions_root.mkdir()
    _make_session(sessions_root, "lock-check", summary="x")

    store = CopilotSessionStore(
        session_state_dir=sessions_root,
        cache_ttl_sec=0,
        persistent_cache_path=cache_path,
    )

    saw_unlocked = False

    def _spy_save(path: Path, entries: dict[Path, _CachedEntry]) -> None:
        nonlocal saw_unlocked
        # Try to acquire ``_lock`` non-blocking. If it succeeds we
        # know the scan released it before calling us.
        acquired = store._lock.acquire(blocking=False)
        if acquired:
            saw_unlocked = True
            store._lock.release()
        # Simulate a real save so the round-trip stays valid.
        _real_save(path, entries)

    from muxdeck.adapters import copilot_session_store as mod

    _real_save = mod._save_persistent_cache_file
    try:
        mod._save_persistent_cache_file = _spy_save  # type: ignore[assignment]
        store.discover()
    finally:
        mod._save_persistent_cache_file = _real_save  # type: ignore[assignment]

    assert saw_unlocked, "persistent-cache write must run after _lock is released"


# ── _parse_session_dir for windows-style cwd ─────────────────────────


def test_parse_session_dir_preserves_windows_paths(tmp_path: Path) -> None:
    sd = tmp_path / "winsess"
    sd.mkdir()
    (sd / "workspace.yaml").write_text(
        "id: winsess\n"
        "cwd: 'C:\\Users\\alice\\projects\\foo'\n"
        "git_root: 'C:\\Users\\alice\\projects\\foo'\n"
        "summary: Windows session\n"
    )
    session = _parse_session_dir(sd, origin="windows")
    assert session is not None
    assert session.windows_cwd == "C:\\Users\\alice\\projects\\foo"
    assert session.windows_git_root == "C:\\Users\\alice\\projects\\foo"


def test_parse_session_dir_omits_windows_paths_for_non_windows_style_cwd(tmp_path: Path) -> None:
    sd = tmp_path / "winsess-posix"
    sd.mkdir()
    (sd / "workspace.yaml").write_text(
        "id: winsess-posix\ncwd: /usr/local/projects/foo\ngit_root: /usr/local/projects/foo\n"
    )
    session = _parse_session_dir(sd, origin="windows")
    assert session is not None
    assert session.windows_cwd is None
    assert session.windows_git_root is None

"""CLI smoke tests."""

from __future__ import annotations

import io
from pathlib import Path

import pytest

from muxdeck.__main__ import main
from muxdeck.cli import _parse_args, _write_perf_table, run_perf
from muxdeck.perf import SpanSummary, record, summarize


def test_main_runs_shell_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    observed: list[object] = []

    def fake_run_app(config: object = None) -> int:
        observed.append(("ran", config))
        return 0

    monkeypatch.setattr("muxdeck.cli.run_app", fake_run_app)

    assert main([]) == 0
    assert observed == [("ran", None)]


def test_main_forwards_config_path(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[object] = []

    def fake_run_app(config: object = None) -> int:
        captured.append(config)
        return 0

    monkeypatch.setattr("muxdeck.cli.run_app", fake_run_app)

    assert main(["--config", "/tmp/m.toml"]) == 0
    assert captured == [Path("/tmp/m.toml")]


def test_main_dispatches_perf_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, object]] = []

    def fake_run_perf(
        *,
        cycles: int,
        config_path: object = None,
        output: object = None,
    ) -> int:
        calls.append({"cycles": cycles, "config_path": config_path})
        return 0

    monkeypatch.setattr("muxdeck.cli.run_perf", fake_run_perf)

    assert main(["--perf", "--perf-cycles", "3"]) == 0
    assert calls == [{"cycles": 3, "config_path": None}]


def test_parse_args_defaults() -> None:
    namespace = _parse_args([])
    assert namespace.perf is False
    assert namespace.perf_cycles == 10
    assert namespace.config is None


def test_write_perf_table_renders_rows() -> None:
    stream = io.StringIO()
    stats = (
        SpanSummary(
            name="sqlite.list sessions",
            count=4,
            total_ms=12.5,
            avg_ms=3.125,
            max_ms=6.0,
            min_ms=1.0,
            p95_ms=5.5,
        ),
    )
    _write_perf_table(stream, stats, cycles=2)
    out = stream.getvalue()
    assert "PERF SUMMARY (2 cycles)" in out
    assert "sqlite.list sessions" in out


def test_write_perf_table_handles_empty_stats() -> None:
    stream = io.StringIO()
    _write_perf_table(stream, (), cycles=1)
    out = stream.getvalue()
    assert "PERF SUMMARY (1 cycle)" in out
    assert "no spans recorded" in out


def test_run_perf_rejects_non_positive_cycles() -> None:
    with pytest.raises(ValueError, match="must be positive"):
        run_perf(cycles=0)


def test_run_perf_executes_cycles_and_emits_summary(monkeypatch: pytest.MonkeyPatch) -> None:
    summarize(reset=True)

    refresh_count = 0
    items_count = 0
    state_count = 0

    class _FakeSynchronizer:
        def refresh(self) -> object:
            nonlocal refresh_count
            refresh_count += 1
            record("sync.fake", 1.0)
            return object()

    class _FakeDashboard:
        def build_agent_items(self) -> tuple[object, ...]:
            nonlocal items_count
            items_count += 1
            record("dashboard.fake_items", 1.0)
            return ()

        def build_state(self, *, precomputed_items: object) -> object:
            nonlocal state_count
            state_count += 1
            record("dashboard.fake_state", 1.0)
            return object()

    closed: list[str] = []

    class _FakeStore:
        def close(self) -> None:
            closed.append("store")

    class _FakeSyncStore:
        def close(self) -> None:
            closed.append("sync_store")

    class _FakeRuntime:
        def __init__(self) -> None:
            self.synchronizer = _FakeSynchronizer()
            self.sync_dashboard = _FakeDashboard()
            self.store = _FakeStore()
            self.sync_store = _FakeSyncStore()

    monkeypatch.setattr("muxdeck.cli.load_config", lambda _path: object())
    monkeypatch.setattr("muxdeck.cli.build_runtime", lambda _config: _FakeRuntime())

    stream = io.StringIO()
    exit_code = run_perf(cycles=3, output=stream)

    assert exit_code == 0
    assert refresh_count == 3
    assert items_count == 3
    assert state_count == 3
    out = stream.getvalue()
    assert "PERF SUMMARY (3 cycles)" in out
    assert "sync.fake" in out
    assert "dashboard.fake_items" in out
    assert "dashboard.fake_state" in out
    # build_runtime spans were reset before the loop, so the summary
    # has exactly 3 samples per fake span.
    summarize(reset=True)
    assert closed == ["store", "sync_store"]


def test_run_perf_returns_error_when_synchronizer_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    class _FakeStore:
        def close(self) -> None:
            return None

    class _BrokenRuntime:
        def __init__(self) -> None:
            self.synchronizer = None
            self.sync_dashboard = None
            self.store = _FakeStore()
            self.sync_store = None

    monkeypatch.setattr("muxdeck.cli.load_config", lambda _path: object())
    monkeypatch.setattr("muxdeck.cli.build_runtime", lambda _config: _BrokenRuntime())

    stream = io.StringIO()
    assert run_perf(cycles=1, output=stream) == 1
    assert "runtime synchronizer" in stream.getvalue()

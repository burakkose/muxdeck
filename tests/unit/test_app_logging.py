from __future__ import annotations

import logging

import pytest

from muxdeck.app import run_app


class _FakeClosable:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _FakeRuntime:
    def __init__(self) -> None:
        self.store = _FakeClosable()
        self.sync_store = _FakeClosable()


class _FakeMuxdeckApp:
    def __init__(self, runtime: _FakeRuntime) -> None:
        self.runtime = runtime
        self.ran = False

    def run(self) -> None:
        self.ran = True


def _install_run_app_fakes(
    monkeypatch: pytest.MonkeyPatch,
    runtime: _FakeRuntime,
    basic_config_calls: list[dict[str, object]],
) -> None:
    def fake_load_config(config_path: str | None = None) -> object:
        del config_path
        return object()

    def fake_build_runtime(config: object) -> _FakeRuntime:
        del config
        return runtime

    def fake_perf_log_summary(*, reset: bool = True) -> None:
        del reset

    def fake_basic_config(**kwargs: object) -> None:
        basic_config_calls.append(dict(kwargs))

    monkeypatch.setattr("muxdeck.app.load_config", fake_load_config)
    monkeypatch.setattr("muxdeck.app.build_runtime", fake_build_runtime)
    monkeypatch.setattr("muxdeck.app.MuxdeckApp", _FakeMuxdeckApp)
    monkeypatch.setattr("muxdeck.app.perf_log_summary", fake_perf_log_summary)
    monkeypatch.setattr("muxdeck.app.logging.basicConfig", fake_basic_config)


def test_run_app_skips_console_logging_without_muxdeck_log(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _FakeRuntime()
    basic_config_calls: list[dict[str, object]] = []
    monkeypatch.delenv("MUXDECK_LOG", raising=False)
    _install_run_app_fakes(monkeypatch, runtime, basic_config_calls)

    assert run_app() == 0
    assert basic_config_calls == []
    assert runtime.store.closed is True
    assert runtime.sync_store.closed is True


def test_run_app_configures_console_logging_when_muxdeck_log_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _FakeRuntime()
    basic_config_calls: list[dict[str, object]] = []
    monkeypatch.setenv("MUXDECK_LOG", "1")
    _install_run_app_fakes(monkeypatch, runtime, basic_config_calls)

    assert run_app() == 0
    assert basic_config_calls == [
        {
            "level": logging.WARNING,
            "format": "%(asctime)s %(name)s %(levelname)s %(message)s",
            "datefmt": "%H:%M:%S",
        }
    ]

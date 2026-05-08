"""Tests for :mod:`muxdeck.screens.compose_mirror`."""

# ruff: noqa: SLF001

from __future__ import annotations

import asyncio
import tempfile
import unittest
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

from textual.app import App, ComposeResult
from textual.containers import Vertical
from textual.widgets import Label, TextArea

from muxdeck.adapters.pane_stream import PaneStreamAdapter
from muxdeck.app import MuxdeckRuntime
from muxdeck.exceptions import TmuxCommandError
from muxdeck.screens.compose_mirror import ComposeWithMirrorScreen
from muxdeck.ui_preferences import UiPreferences
from muxdeck.widgets.live_pane_viewer import LivePaneViewer


@dataclass
class _FakeTmuxStream:
    """Stand-in for the tmux surface used by :class:`PaneStreamAdapter`.

    Records every call so tests can assert seed / pipe / send-keys
    lifecycle without touching a real tmux.
    """

    captures: list[str] = field(default_factory=list)
    capture_flags: list[tuple[str, bool, bool]] = field(default_factory=list)
    pipe_started: list[str] = field(default_factory=list)
    pipe_paths: list[Path] = field(default_factory=list)
    pipe_stopped: list[str] = field(default_factory=list)
    sent: list[tuple[str, tuple[str, ...], bool]] = field(default_factory=list)
    seed_text: str = "seeded pane output\n"

    def capture_pane(
        self,
        target_pane: str,
        /,
        *,
        start_line: str | int | None = None,
        end_line: str | int | None = None,
        join_wrapped_lines: bool = False,
        include_escape_sequences: bool = False,
    ) -> str:
        del start_line, end_line
        self.captures.append(target_pane)
        self.capture_flags.append((target_pane, join_wrapped_lines, include_escape_sequences))
        return self.seed_text

    def pipe_pane_to_file(
        self,
        target_pane: str,
        /,
        *,
        target_path: Path,
        append: bool = True,
    ) -> None:
        del append
        self.pipe_started.append(target_pane)
        self.pipe_paths.append(target_path)

    def stop_pipe_pane(self, target_pane: str, /) -> None:
        self.pipe_stopped.append(target_pane)

    def send_keys(
        self,
        target_pane: str,
        keys: Sequence[str],
        /,
        *,
        literal: bool = False,
        append_enter: bool = False,
    ) -> object:
        del append_enter
        self.sent.append((target_pane, tuple(keys), literal))
        return None

    def pane_exists(self, pane_id: str, /) -> bool:
        del pane_id
        return True


@dataclass
class _FakeActionService:
    """Minimal action service recording `send_message` calls."""

    calls: list[tuple[str, str]] = field(default_factory=list)

    def send_message(self, pane_id: str, text: str) -> _FakeActionService._Result:
        self.calls.append((pane_id, text))
        return _FakeActionService._Result(success=True, message=f"sent to {pane_id}")

    @dataclass
    class _Result:
        success: bool
        message: str


def _fake_runtime(actions: _FakeActionService, stream: PaneStreamAdapter) -> MuxdeckRuntime:
    return cast(
        MuxdeckRuntime,
        type(
            "FakeRuntime",
            (),
            {
                "actions": actions,
                "pane_stream": stream,
            },
        )(),
    )


class _Harness(App[None]):
    def compose(self) -> ComposeResult:
        return iter(())


class _MuxdeckHarness(_Harness):
    def __init__(self) -> None:
        super().__init__()
        self.ui_preferences = UiPreferences()

    def action_toggle_log_wrap(self) -> None:
        self.ui_preferences = UiPreferences(
            density=self.ui_preferences.density,
            glyphs=self.ui_preferences.glyphs,
            contrast=self.ui_preferences.contrast,
            decorations=self.ui_preferences.decorations,
            wrap_logs=not self.ui_preferences.wrap_logs,
        )
        if isinstance(self.screen, ComposeWithMirrorScreen):
            self.screen.apply_ui_preferences()


class ComposeWithMirrorScreenTests(unittest.TestCase):
    """End-to-end behavioural tests for the compose + mirror screen."""

    def test_custom_stream_adapter_overrides_runtime_default(self) -> None:
        async def scenario(tmp: Path) -> tuple[list[str], list[str]]:
            default_tmux = _FakeTmuxStream(seed_text="outer pane\n")
            nested_tmux = _FakeTmuxStream(seed_text="inner pane\n")
            actions = _FakeActionService()
            runtime = _fake_runtime(actions, PaneStreamAdapter(tmux=default_tmux))

            app = _Harness()
            async with app.run_test() as pilot:
                screen = ComposeWithMirrorScreen(
                    runtime,
                    pane_id="%42",
                    display_name="demo",
                    ring_dir=tmp,
                    stream_adapter=PaneStreamAdapter(tmux=nested_tmux),
                )
                await app.push_screen(screen)
                await pilot.pause()
                return default_tmux.captures, nested_tmux.captures

        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            default_captures, nested_captures = asyncio.run(scenario(Path(tmp)))

        assert default_captures == []
        assert nested_captures == ["%42"]

    def test_seeds_mirror_and_sends_composed_text(self) -> None:
        async def scenario(tmp: Path) -> tuple[list[str], list[tuple[str, str]]]:
            tmux = _FakeTmuxStream(seed_text="hello pane\nmore context\n")
            stream = PaneStreamAdapter(tmux=tmux)
            actions = _FakeActionService()
            runtime = _fake_runtime(actions, stream)

            app = _Harness()
            async with app.run_test() as pilot:
                screen = ComposeWithMirrorScreen(
                    runtime,
                    pane_id="%7",
                    display_name="demo",
                    ring_dir=tmp,
                )
                await app.push_screen(screen)
                await pilot.pause()

                mirror = app.screen.query_one(LivePaneViewer)
                editor = app.screen.query_one("#compose-editor", TextArea)

                assert mirror.has_content
                assert tmux.captures == ["%7"]
                assert tmux.pipe_started == ["%7"]

                editor.text = "please keep going\n"
                await pilot.press("ctrl+s")
                await pilot.pause()

                # Editor is cleared on successful send so the user
                # can immediately compose another message without
                # manual cleanup.
                assert editor.text == ""
                return tmux.pipe_started, actions.calls

        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            pipe_started, send_calls = asyncio.run(scenario(Path(tmp)))

        assert pipe_started == ["%7"]
        assert send_calls == [("%7", "please keep going")]

    def test_blank_compose_does_not_send_or_close(self) -> None:
        async def scenario(tmp: Path) -> tuple[bool, list[tuple[str, str]]]:
            tmux = _FakeTmuxStream()
            stream = PaneStreamAdapter(tmux=tmux)
            actions = _FakeActionService()
            runtime = _fake_runtime(actions, stream)

            app = _Harness()
            async with app.run_test() as pilot:
                screen = ComposeWithMirrorScreen(
                    runtime,
                    pane_id="%9",
                    display_name="demo",
                    ring_dir=tmp,
                )
                await app.push_screen(screen)
                await pilot.pause()

                await pilot.press("ctrl+s")
                await pilot.pause()

                still_mounted = isinstance(app.screen, ComposeWithMirrorScreen)
                return still_mounted, actions.calls

        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            still_mounted, send_calls = asyncio.run(scenario(Path(tmp)))

        assert still_mounted is True
        assert send_calls == []

    def test_escape_closes_and_tears_down_pipe(self) -> None:
        async def scenario(tmp: Path) -> list[str]:
            tmux = _FakeTmuxStream()
            stream = PaneStreamAdapter(tmux=tmux)
            actions = _FakeActionService()
            runtime = _fake_runtime(actions, stream)

            app = _Harness()
            async with app.run_test() as pilot:
                screen = ComposeWithMirrorScreen(
                    runtime,
                    pane_id="%11",
                    display_name="demo",
                    ring_dir=tmp,
                )
                await app.push_screen(screen)
                await pilot.pause()
                await pilot.press("escape")
                await pilot.pause()
                return tmux.pipe_stopped

        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            stopped = asyncio.run(scenario(Path(tmp)))

        assert stopped == ["%11"]

    def test_tab_switches_focus_between_editor_and_mirror(self) -> None:
        async def scenario(tmp: Path) -> tuple[bool, bool, bool]:
            tmux = _FakeTmuxStream()
            stream = PaneStreamAdapter(tmux=tmux)
            actions = _FakeActionService()
            runtime = _fake_runtime(actions, stream)

            app = _Harness()
            async with app.run_test() as pilot:
                screen = ComposeWithMirrorScreen(
                    runtime,
                    pane_id="%3",
                    display_name="demo",
                    ring_dir=tmp,
                )
                await app.push_screen(screen)
                await pilot.pause()

                editor = app.screen.query_one("#compose-editor", TextArea)
                mirror = app.screen.query_one(LivePaneViewer)

                editor_focus_on_mount = editor.has_focus
                await pilot.press("tab")
                await pilot.pause()
                mirror_focus_after_tab = mirror.has_focus
                await pilot.press("tab")
                await pilot.pause()
                editor_focus_after_second_tab = editor.has_focus
                return (
                    editor_focus_on_mount,
                    mirror_focus_after_tab,
                    editor_focus_after_second_tab,
                )

        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            initial_editor, mirror_after_tab, editor_after_two = asyncio.run(scenario(Path(tmp)))

        assert initial_editor is True
        assert mirror_after_tab is True
        assert editor_after_two is True

    def test_live_input_mode_forwards_keys_before_escape_closes_screen(self) -> None:
        async def scenario(
            tmp: Path,
        ) -> tuple[list[tuple[str, tuple[str, ...], bool]], bool, list[str], bool]:
            tmux = _FakeTmuxStream()
            stream = PaneStreamAdapter(tmux=tmux)
            actions = _FakeActionService()
            runtime = _fake_runtime(actions, stream)

            app = _Harness()
            async with app.run_test() as pilot:
                screen = ComposeWithMirrorScreen(
                    runtime,
                    pane_id="%13",
                    display_name="demo",
                    ring_dir=tmp,
                )
                await app.push_screen(screen)
                await pilot.pause()

                mirror = app.screen.query_one(LivePaneViewer)
                await pilot.press("tab")
                await pilot.pause()
                await pilot.press("i")
                await pilot.pause()
                input_on = screen.mirror_input_active and mirror.has_class("-input-on")

                await pilot.press("a")
                await pilot.press("enter")
                await pilot.pause()
                await pilot.press("escape")
                await pilot.pause()
                still_open_after_first_escape = isinstance(app.screen, ComposeWithMirrorScreen)
                input_off = not screen.mirror_input_active and not mirror.has_class("-input-on")

                await pilot.press("escape")
                await pilot.pause()
                return (
                    tmux.sent,
                    still_open_after_first_escape,
                    tmux.pipe_stopped,
                    input_on and input_off,
                )

        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            sent, still_open, stopped, input_toggled = asyncio.run(scenario(Path(tmp)))

        assert input_toggled is True
        assert still_open is True
        assert sent == [
            ("%13", ("a",), True),
            ("%13", ("Enter",), False),
        ]
        assert stopped == ["%13"]

    def test_resize_bindings_adjust_editor_height(self) -> None:
        async def scenario(tmp: Path) -> tuple[int, float, int, float]:
            tmux = _FakeTmuxStream()
            stream = PaneStreamAdapter(tmux=tmux)
            actions = _FakeActionService()
            runtime = _fake_runtime(actions, stream)

            app = _Harness()
            async with app.run_test() as pilot:
                screen = ComposeWithMirrorScreen(
                    runtime,
                    pane_id="%15",
                    display_name="demo",
                    ring_dir=tmp,
                )
                await app.push_screen(screen)
                await pilot.pause()

                editor_wrap = app.screen.query_one("#compose-editor-wrap", Vertical)
                await pilot.press("alt+down")
                await pilot.pause()
                grown_height = screen.editor_height
                grown_scalar = editor_wrap.styles.height
                assert grown_scalar is not None
                grown_style = grown_scalar.value
                await pilot.press("alt+up")
                await pilot.pause()
                restored_scalar = editor_wrap.styles.height
                assert restored_scalar is not None
                return (
                    grown_height,
                    grown_style,
                    screen.editor_height,
                    restored_scalar.value,
                )

        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            grown_height, grown_style, restored_height, restored_style = asyncio.run(
                scenario(Path(tmp))
            )

        assert grown_height == 12
        assert grown_style == 12.0
        assert restored_height == 10
        assert restored_style == 10.0

    def test_viewer_only_mode_hides_editor_and_keeps_send_as_noop(self) -> None:
        async def scenario(tmp: Path) -> tuple[bool, bool, int, list[tuple[str, str]]]:
            tmux = _FakeTmuxStream(seed_text="hello pane\n")
            stream = PaneStreamAdapter(tmux=tmux)
            actions = _FakeActionService()
            runtime = _fake_runtime(actions, stream)

            app = _Harness()
            async with app.run_test() as pilot:
                screen = ComposeWithMirrorScreen(
                    runtime,
                    pane_id="%19",
                    display_name="demo",
                    ring_dir=tmp,
                    show_editor=False,
                )
                await app.push_screen(screen)
                await pilot.pause()

                mirror = app.screen.query_one(LivePaneViewer)
                editor_count = len(list(app.screen.query("#compose-editor")))
                focus_on_mount = mirror.has_focus
                await pilot.press("tab")
                await pilot.pause()
                focus_after_tab = mirror.has_focus
                await pilot.press("ctrl+s")
                await pilot.pause()
                return focus_on_mount, focus_after_tab, editor_count, actions.calls

        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            focus_on_mount, focus_after_tab, editor_count, send_calls = asyncio.run(
                scenario(Path(tmp))
            )

        assert focus_on_mount is True
        assert focus_after_tab is True
        assert editor_count == 0
        assert send_calls == []

    def test_viewer_only_mode_footer_hints_drop_compose_shortcuts(self) -> None:
        runtime = _fake_runtime(_FakeActionService(), PaneStreamAdapter(tmux=_FakeTmuxStream()))

        screen = ComposeWithMirrorScreen(
            runtime,
            pane_id="%19",
            display_name="demo",
            ring_dir=Path.cwd(),
            show_editor=False,
        )

        hints = tuple((hint.key, hint.label) for hint in screen.footer_hints())

        assert hints == (
            ("w", "wrap"),
            ("f", "follow"),
            ("i", "interact"),
            ("r", "resync"),
            ("esc", "back"),
            ("q", "quit"),
        )

    def test_follow_binding_toggles_live_follow_mode(self) -> None:
        async def scenario(tmp: Path) -> tuple[bool, bool, str]:
            tmux = _FakeTmuxStream(seed_text="hello pane\n")
            stream = PaneStreamAdapter(tmux=tmux)
            actions = _FakeActionService()
            runtime = _fake_runtime(actions, stream)

            app = _Harness()
            async with app.run_test() as pilot:
                screen = ComposeWithMirrorScreen(
                    runtime,
                    pane_id="%27",
                    display_name="demo",
                    ring_dir=tmp,
                    show_editor=False,
                )
                await app.push_screen(screen)
                await pilot.pause()

                mirror = app.screen.query_one(LivePaneViewer)
                await pilot.press("f")
                await pilot.pause()
                follow_after_first_toggle = mirror.follow_enabled
                await pilot.press("f")
                await pilot.pause()
                return follow_after_first_toggle, mirror.follow_enabled, screen._status

        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            first_toggle, second_toggle, status = asyncio.run(scenario(Path(tmp)))

        assert first_toggle is False
        assert second_toggle is True
        assert status == "live follow on"

    def test_wrap_binding_tracks_app_log_wrap_preference(self) -> None:
        async def scenario(tmp: Path) -> tuple[bool, bool, bool, bool]:
            tmux = _FakeTmuxStream(seed_text="hello pane\n")
            stream = PaneStreamAdapter(tmux=tmux)
            actions = _FakeActionService()
            runtime = _fake_runtime(actions, stream)

            app = _MuxdeckHarness()
            async with app.run_test() as pilot:
                screen = ComposeWithMirrorScreen(
                    runtime,
                    pane_id="%29",
                    display_name="demo",
                    ring_dir=tmp,
                    show_editor=False,
                )
                await app.push_screen(screen)
                await pilot.pause()

                mirror = app.screen.query_one(LivePaneViewer)
                await pilot.press("w")
                await pilot.pause()
                first_wrap = mirror.wrap_enabled
                first_pref = app.ui_preferences.wrap_logs
                await pilot.press("w")
                await pilot.pause()
                return first_wrap, first_pref, mirror.wrap_enabled, app.ui_preferences.wrap_logs

        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            first_wrap, first_pref, second_wrap, second_pref = asyncio.run(scenario(Path(tmp)))

        assert first_wrap is True
        assert first_pref is True
        assert second_wrap is False
        assert second_pref is False

    def test_manual_refresh_reconciles_snapshot_tail_without_losing_history(self) -> None:
        async def scenario(tmp: Path) -> tuple[tuple[str, ...], list[tuple[str, bool, bool]]]:
            tmux = _FakeTmuxStream(seed_text="one\ntwo\nthree\n")
            stream = PaneStreamAdapter(tmux=tmux)
            actions = _FakeActionService()
            runtime = _fake_runtime(actions, stream)

            app = _Harness()
            async with app.run_test() as pilot:
                screen = ComposeWithMirrorScreen(
                    runtime,
                    pane_id="%17",
                    display_name="demo",
                    ring_dir=tmp,
                )
                await app.push_screen(screen)
                await pilot.pause()

                ring_path = tmux.pipe_paths[0]
                with ring_path.open("a", encoding="utf-8") as fh:
                    fh.write("four\nfive\n")
                screen._drain_ring()
                mirror = app.screen.query_one(LivePaneViewer)
                tmux.seed_text = "three\nFOUR\nFIVE\n"

                screen.refresh_data()
                await pilot.pause()
                return mirror.buffer_lines, tmux.capture_flags

        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            buffer_lines, capture_flags = asyncio.run(scenario(Path(tmp)))

        assert buffer_lines == ("one", "two", "three", "FOUR", "FIVE")
        assert capture_flags[0] == ("%17", False, True)

    def test_manual_refresh_skips_tail_replay_when_stream_already_matches_snapshot(
        self,
    ) -> None:
        async def scenario(tmp: Path) -> tuple[tuple[str, ...], int, str]:
            tmux = _FakeTmuxStream(seed_text="one\ntwo\n")
            stream = PaneStreamAdapter(tmux=tmux)
            actions = _FakeActionService()
            runtime = _fake_runtime(actions, stream)

            app = _Harness()
            async with app.run_test() as pilot:
                screen = ComposeWithMirrorScreen(
                    runtime,
                    pane_id="%23",
                    display_name="demo",
                    ring_dir=tmp,
                )
                await app.push_screen(screen)
                await pilot.pause()

                ring_path = tmux.pipe_paths[0]
                with ring_path.open("a", encoding="utf-8") as fh:
                    fh.write("three\nfour\n")
                screen._drain_ring()

                mirror = app.screen.query_one(LivePaneViewer)
                tmux.seed_text = "one\ntwo\nthree\nfour\n"

                with patch.object(
                    mirror,
                    "replace_tail",
                    wraps=mirror.replace_tail,
                ) as replace_tail:
                    screen.refresh_data()
                    await pilot.pause()
                    return mirror.buffer_lines, replace_tail.call_count, screen._last_snapshot

        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            buffer_lines, replace_tail_calls, last_snapshot = asyncio.run(scenario(Path(tmp)))

        assert buffer_lines == ("one", "two", "three", "four")
        assert replace_tail_calls == 0
        assert last_snapshot == "one\ntwo\nthree\nfour\n"

    def test_manual_refresh_clears_mirror_when_snapshot_becomes_empty(self) -> None:
        async def scenario(tmp: Path) -> tuple[tuple[str, ...], str]:
            tmux = _FakeTmuxStream(seed_text="one\ntwo\n")
            stream = PaneStreamAdapter(tmux=tmux)
            actions = _FakeActionService()
            runtime = _fake_runtime(actions, stream)

            app = _Harness()
            async with app.run_test() as pilot:
                screen = ComposeWithMirrorScreen(
                    runtime,
                    pane_id="%24",
                    display_name="demo",
                    ring_dir=tmp,
                )
                await app.push_screen(screen)
                await pilot.pause()

                tmux.seed_text = ""
                screen.refresh_data()
                await pilot.pause()

                mirror = app.screen.query_one(LivePaneViewer)
                return mirror.buffer_lines, screen._last_snapshot

        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            buffer_lines, last_snapshot = asyncio.run(scenario(Path(tmp)))

        assert buffer_lines == ()
        assert last_snapshot == ""


@dataclass
class _RaisingTmuxStream:
    """Tmux stream that raises configurable errors at each lifecycle hook.

    Used to drive the error branches in
    :meth:`ComposeWithMirrorScreen._seed_and_stream`,
    :meth:`_sync_snapshot`, and :meth:`_teardown_pipe`.
    """

    capture_error: BaseException | None = None
    pipe_error: BaseException | None = None
    stop_error: BaseException | None = None
    seed_text: str = "seeded\n"
    captures: list[str] = field(default_factory=list)
    pipe_started: list[str] = field(default_factory=list)
    pipe_paths: list[Path] = field(default_factory=list)
    pipe_stopped: list[str] = field(default_factory=list)

    def capture_pane(
        self,
        target_pane: str,
        /,
        *,
        start_line: str | int | None = None,
        end_line: str | int | None = None,
        join_wrapped_lines: bool = False,
        include_escape_sequences: bool = False,
    ) -> str:
        del start_line, end_line, join_wrapped_lines, include_escape_sequences
        self.captures.append(target_pane)
        if self.capture_error is not None:
            raise self.capture_error
        return self.seed_text

    def pipe_pane_to_file(
        self,
        target_pane: str,
        /,
        *,
        target_path: Path,
        append: bool = True,
    ) -> None:
        del append
        self.pipe_started.append(target_pane)
        self.pipe_paths.append(target_path)
        if self.pipe_error is not None:
            raise self.pipe_error

    def stop_pipe_pane(self, target_pane: str, /) -> None:
        self.pipe_stopped.append(target_pane)
        if self.stop_error is not None:
            raise self.stop_error

    def send_keys(
        self,
        target_pane: str,
        keys: Sequence[str],
        /,
        *,
        literal: bool = False,
        append_enter: bool = False,
    ) -> object:
        del target_pane, keys, literal, append_enter
        return None

    def pane_exists(self, pane_id: str, /) -> bool:
        del pane_id
        return True


def _runtime_without_pane_stream(actions: _FakeActionService) -> MuxdeckRuntime:
    return cast(
        MuxdeckRuntime,
        type(
            "FakeRuntime",
            (),
            {
                "actions": actions,
                "pane_stream": None,
            },
        )(),
    )


def _runtime_without_actions(stream: PaneStreamAdapter) -> MuxdeckRuntime:
    return cast(
        MuxdeckRuntime,
        type(
            "FakeRuntime",
            (),
            {
                "actions": None,
                "pane_stream": stream,
            },
        )(),
    )


class ComposeMirrorMountErrorTests(unittest.TestCase):
    """Branches taken when the screen mounts without a stream / errors."""

    def test_mount_with_no_adapter_marks_capture_error_and_clears_loading(self) -> None:
        async def scenario(tmp: Path) -> tuple[str | None, bool, str]:
            actions = _FakeActionService()
            runtime = _runtime_without_pane_stream(actions)

            app = _Harness()
            async with app.run_test() as pilot:
                screen = ComposeWithMirrorScreen(
                    runtime,
                    pane_id="%101",
                    display_name="demo",
                    ring_dir=tmp,
                    show_editor=False,
                )
                await app.push_screen(screen)
                await pilot.pause()
                viewer = app.screen.query_one(LivePaneViewer)
                return (
                    screen._capture_error,
                    screen._loading_cleared,
                    str(viewer.border_subtitle or ""),
                )

        with tempfile.TemporaryDirectory() as tmp:
            err, cleared, subtitle = asyncio.run(scenario(Path(tmp)))

        # The on_mount adapter-None branch sets a capture-error message
        # and immediately clears the loading state so the user isn't
        # stuck on a spinner.
        assert err == "✗ pane streaming unavailable"
        assert cleared is True
        assert subtitle == "capture failed"

    def test_capture_pane_tmux_error_marks_capture_error(self) -> None:
        async def scenario(tmp: Path) -> tuple[str | None, bool]:
            tmux = _RaisingTmuxStream(
                capture_error=TmuxCommandError("capture-pane", stderr="boom"),
            )
            stream = PaneStreamAdapter(tmux=cast(Any, tmux))
            actions = _FakeActionService()
            runtime = _fake_runtime(actions, stream)

            app = _Harness()
            async with app.run_test() as pilot:
                screen = ComposeWithMirrorScreen(
                    runtime,
                    pane_id="%102",
                    display_name="demo",
                    ring_dir=tmp,
                    show_editor=False,
                )
                await app.push_screen(screen)
                await pilot.pause()
                return screen._capture_error, screen._loading_cleared

        with tempfile.TemporaryDirectory() as tmp:
            err, cleared = asyncio.run(scenario(Path(tmp)))

        assert err is not None
        assert err.startswith("✗ capture failed:")
        assert cleared is True

    def test_capture_pane_oserror_marks_capture_error(self) -> None:
        async def scenario(tmp: Path) -> tuple[str | None, bool]:
            tmux = _RaisingTmuxStream(capture_error=OSError("disk gone"))
            stream = PaneStreamAdapter(tmux=cast(Any, tmux))
            actions = _FakeActionService()
            runtime = _fake_runtime(actions, stream)

            app = _Harness()
            async with app.run_test() as pilot:
                screen = ComposeWithMirrorScreen(
                    runtime,
                    pane_id="%103",
                    display_name="demo",
                    ring_dir=tmp,
                    show_editor=False,
                )
                await app.push_screen(screen)
                await pilot.pause()
                return screen._capture_error, screen._loading_cleared

        with tempfile.TemporaryDirectory() as tmp:
            err, cleared = asyncio.run(scenario(Path(tmp)))

        assert err is not None
        assert "disk gone" in err
        assert cleared is True

    def test_pipe_pane_tmux_error_records_stream_warning(self) -> None:
        async def scenario(tmp: Path) -> tuple[str | None, str | None]:
            tmux = _RaisingTmuxStream(
                pipe_error=TmuxCommandError("pipe-pane", stderr="no pipe"),
            )
            stream = PaneStreamAdapter(tmux=cast(Any, tmux))
            actions = _FakeActionService()
            runtime = _fake_runtime(actions, stream)

            app = _Harness()
            async with app.run_test() as pilot:
                screen = ComposeWithMirrorScreen(
                    runtime,
                    pane_id="%104",
                    display_name="demo",
                    ring_dir=tmp,
                    show_editor=False,
                )
                await app.push_screen(screen)
                await pilot.pause()
                return screen._capture_error, screen._stream_warning

        with tempfile.TemporaryDirectory() as tmp:
            err, warning = asyncio.run(scenario(Path(tmp)))

        # Pipe failures degrade to "snapshot sync only" without
        # poisoning the capture-error state.
        assert err is None
        assert warning is not None
        assert "live stream unavailable" in warning

    def test_pipe_pane_oserror_records_stream_warning(self) -> None:
        async def scenario(tmp: Path) -> str | None:
            tmux = _RaisingTmuxStream(pipe_error=OSError("no fd"))
            stream = PaneStreamAdapter(tmux=cast(Any, tmux))
            actions = _FakeActionService()
            runtime = _fake_runtime(actions, stream)

            app = _Harness()
            async with app.run_test() as pilot:
                screen = ComposeWithMirrorScreen(
                    runtime,
                    pane_id="%105",
                    display_name="demo",
                    ring_dir=tmp,
                    show_editor=False,
                )
                await app.push_screen(screen)
                await pilot.pause()
                return screen._stream_warning

        with tempfile.TemporaryDirectory() as tmp:
            warning = asyncio.run(scenario(Path(tmp)))

        assert warning is not None
        assert "no fd" in warning


class ComposeMirrorSyncSnapshotErrorTests(unittest.TestCase):
    """Error branches inside ``_sync_snapshot``."""

    def test_sync_snapshot_tmux_error_records_warning(self) -> None:
        async def scenario(tmp: Path) -> tuple[str | None, str | None]:
            tmux = _RaisingTmuxStream(seed_text="hello\n")
            stream = PaneStreamAdapter(tmux=cast(Any, tmux))
            actions = _FakeActionService()
            runtime = _fake_runtime(actions, stream)

            app = _Harness()
            async with app.run_test() as pilot:
                screen = ComposeWithMirrorScreen(
                    runtime,
                    pane_id="%106",
                    display_name="demo",
                    ring_dir=tmp,
                    show_editor=False,
                )
                await app.push_screen(screen)
                await pilot.pause()
                # Now flip the stream so the next capture raises.
                tmux.capture_error = TmuxCommandError("capture-pane", stderr="snap fail")
                screen._sync_snapshot(force=False)
                return screen._sync_warning, screen._capture_error

        with tempfile.TemporaryDirectory() as tmp:
            warning, capture_err = asyncio.run(scenario(Path(tmp)))

        assert warning is not None
        assert "snapshot sync failed" in warning
        # The non-forced path keeps the existing content so it must NOT
        # raise the warning to a hard capture-error.
        assert capture_err is None

    def test_sync_snapshot_force_with_no_content_promotes_to_capture_error(self) -> None:
        async def scenario(tmp: Path) -> str | None:
            tmux = _RaisingTmuxStream(seed_text="hello\n")
            stream = PaneStreamAdapter(tmux=cast(Any, tmux))
            actions = _FakeActionService()
            runtime = _fake_runtime(actions, stream)

            app = _Harness()
            async with app.run_test() as pilot:
                screen = ComposeWithMirrorScreen(
                    runtime,
                    pane_id="%107",
                    display_name="demo",
                    ring_dir=tmp,
                    show_editor=False,
                )
                await app.push_screen(screen)
                await pilot.pause()
                # Drop everything we have so the "no content" branch
                # fires: clear the viewer and switch the stream to error.
                viewer = app.screen.query_one(LivePaneViewer)
                viewer.set_snapshot("")
                tmux.capture_error = TmuxCommandError("capture-pane", stderr="hard fail")
                screen._sync_snapshot(force=True)
                return screen._capture_error

        with tempfile.TemporaryDirectory() as tmp:
            err = asyncio.run(scenario(Path(tmp)))

        assert err is not None
        assert "snapshot sync failed" in err

    def test_sync_snapshot_oserror_records_warning(self) -> None:
        async def scenario(tmp: Path) -> str | None:
            tmux = _RaisingTmuxStream(seed_text="hello\n")
            stream = PaneStreamAdapter(tmux=cast(Any, tmux))
            actions = _FakeActionService()
            runtime = _fake_runtime(actions, stream)

            app = _Harness()
            async with app.run_test() as pilot:
                screen = ComposeWithMirrorScreen(
                    runtime,
                    pane_id="%108",
                    display_name="demo",
                    ring_dir=tmp,
                    show_editor=False,
                )
                await app.push_screen(screen)
                await pilot.pause()
                tmux.capture_error = OSError("io")
                screen._sync_snapshot(force=False)
                return screen._sync_warning

        with tempfile.TemporaryDirectory() as tmp:
            warning = asyncio.run(scenario(Path(tmp)))

        assert warning is not None
        assert "io" in warning

    def test_sync_snapshot_force_oserror_with_no_content_promotes_to_capture_error(
        self,
    ) -> None:
        async def scenario(tmp: Path) -> str | None:
            tmux = _RaisingTmuxStream(seed_text="hello\n")
            stream = PaneStreamAdapter(tmux=cast(Any, tmux))
            actions = _FakeActionService()
            runtime = _fake_runtime(actions, stream)

            app = _Harness()
            async with app.run_test() as pilot:
                screen = ComposeWithMirrorScreen(
                    runtime,
                    pane_id="%109",
                    display_name="demo",
                    ring_dir=tmp,
                    show_editor=False,
                )
                await app.push_screen(screen)
                await pilot.pause()
                viewer = app.screen.query_one(LivePaneViewer)
                viewer.set_snapshot("")
                tmux.capture_error = OSError("dead")
                screen._sync_snapshot(force=True)
                return screen._capture_error

        with tempfile.TemporaryDirectory() as tmp:
            err = asyncio.run(scenario(Path(tmp)))

        assert err is not None
        assert "dead" in err


class ComposeMirrorActionEdgeTests(unittest.TestCase):
    """Action handlers exercising guard-clause / failure branches."""

    def test_action_send_no_action_service_sets_status(self) -> None:
        async def scenario(tmp: Path) -> str:
            tmux = _FakeTmuxStream()
            stream = PaneStreamAdapter(tmux=tmux)
            runtime = _runtime_without_actions(stream)

            app = _Harness()
            async with app.run_test() as pilot:
                screen = ComposeWithMirrorScreen(
                    runtime,
                    pane_id="%201",
                    display_name="demo",
                    ring_dir=tmp,
                )
                await app.push_screen(screen)
                await pilot.pause()
                # Need text in the editor or the no-text branch wins.
                editor = app.screen.query_one("#compose-editor", TextArea)
                editor.text = "anything"
                screen.action_send()
                return screen._status

        with tempfile.TemporaryDirectory() as tmp:
            status = asyncio.run(scenario(Path(tmp)))

        assert status == "✗ action service unavailable"

    def test_action_send_failure_result_sets_failure_status(self) -> None:
        @dataclass
        class _FailingActions:
            calls: list[tuple[str, str]] = field(default_factory=list)

            def send_message(self, pane_id: str, text: str) -> _FakeActionService._Result:
                self.calls.append((pane_id, text))
                return _FakeActionService._Result(success=False, message="quota exceeded")

        async def scenario(tmp: Path) -> str:
            tmux = _FakeTmuxStream()
            stream = PaneStreamAdapter(tmux=tmux)
            actions = _FailingActions()
            runtime = _fake_runtime(cast(Any, actions), stream)

            app = _Harness()
            async with app.run_test() as pilot:
                screen = ComposeWithMirrorScreen(
                    runtime,
                    pane_id="%202",
                    display_name="demo",
                    ring_dir=tmp,
                )
                await app.push_screen(screen)
                await pilot.pause()
                editor = app.screen.query_one("#compose-editor", TextArea)
                editor.text = "msg"
                screen.action_send()
                return screen._status

        with tempfile.TemporaryDirectory() as tmp:
            status = asyncio.run(scenario(Path(tmp)))

        assert status == "✗ quota exceeded"

    def test_action_send_in_viewer_only_mode_emits_status(self) -> None:
        async def scenario(tmp: Path) -> str:
            tmux = _FakeTmuxStream()
            stream = PaneStreamAdapter(tmux=tmux)
            actions = _FakeActionService()
            runtime = _fake_runtime(actions, stream)

            app = _Harness()
            async with app.run_test() as pilot:
                screen = ComposeWithMirrorScreen(
                    runtime,
                    pane_id="%203",
                    display_name="demo",
                    ring_dir=tmp,
                    show_editor=False,
                )
                await app.push_screen(screen)
                await pilot.pause()
                screen.action_send()
                return screen._status

        with tempfile.TemporaryDirectory() as tmp:
            status = asyncio.run(scenario(Path(tmp)))

        assert "live viewer only" in status

    def test_grow_and_shrink_no_op_when_editor_hidden(self) -> None:
        """Verify the ``if not self._show_editor: return`` guard.

        The original test called grow then shrink (cancelling out) and
        asserted height stayed at 10 — that assertion held whether or
        not the guard existed. Spy on ``_set_editor_height`` so the
        guard's absence becomes a recorded call.
        """

        async def scenario(tmp: Path) -> tuple[int, int]:
            tmux = _FakeTmuxStream()
            stream = PaneStreamAdapter(tmux=tmux)
            actions = _FakeActionService()
            runtime = _fake_runtime(actions, stream)

            app = _Harness()
            async with app.run_test() as pilot:
                screen = ComposeWithMirrorScreen(
                    runtime,
                    pane_id="%204",
                    display_name="demo",
                    ring_dir=tmp,
                    show_editor=False,
                )
                await app.push_screen(screen)
                await pilot.pause()
                set_calls: list[int] = []
                screen._set_editor_height = lambda v: set_calls.append(v)  # type: ignore[method-assign,assignment]
                screen.action_grow_editor()
                screen.action_shrink_editor()
                return screen._editor_height, len(set_calls)

        with tempfile.TemporaryDirectory() as tmp:
            height, set_count = asyncio.run(scenario(Path(tmp)))

        # Height untouched AND, more importantly, the underlying
        # setter was never invoked because the guard short-circuited.
        # Without the guard, set_count would be 2.
        assert height == 10
        assert set_count == 0, (
            f"_show_editor guard removed: _set_editor_height was called {set_count} time(s)"
        )

    def test_set_editor_height_no_op_for_same_value(self) -> None:
        """Verify the ``if clamped == self._editor_height: return``
        early-return short-circuits the side effects.

        The earlier test asserted that height stayed at the same value,
        which is true with or without the guard (since assigning a
        clamped value back to itself is a no-op). Spy on the side
        effects ``_apply_editor_height``/``_refresh_guidance`` that
        the guard skips.
        """

        async def scenario(tmp: Path) -> tuple[int, int, int]:
            tmux = _FakeTmuxStream()
            stream = PaneStreamAdapter(tmux=tmux)
            actions = _FakeActionService()
            runtime = _fake_runtime(actions, stream)

            app = _Harness()
            async with app.run_test() as pilot:
                screen = ComposeWithMirrorScreen(
                    runtime,
                    pane_id="%205",
                    display_name="demo",
                    ring_dir=tmp,
                )
                await app.push_screen(screen)
                await pilot.pause()
                apply_calls: list[bool] = []
                refresh_calls: list[bool] = []
                screen._apply_editor_height = lambda: apply_calls.append(True)  # type: ignore[method-assign]
                screen._refresh_guidance = lambda *a, **kw: refresh_calls.append(True)  # type: ignore[method-assign]
                screen._set_editor_height(screen._editor_height)
                return screen._editor_height, len(apply_calls), len(refresh_calls)

        with tempfile.TemporaryDirectory() as tmp:
            height, apply_count, refresh_count = asyncio.run(scenario(Path(tmp)))

        assert height == 10
        assert apply_count == 0, (
            f"early-return removed: _apply_editor_height fired {apply_count} time(s)"
        )
        assert refresh_count == 0, (
            f"early-return removed: _refresh_guidance fired {refresh_count} time(s)"
        )

    def test_set_mirror_input_mode_idempotent(self) -> None:
        """Verify the ``if self._mirror_input_active == enabled: return``
        guard skips ``_refresh_guidance`` when called with the
        already-active value. Asserting the flag stayed False isn't
        enough — assigning False to False yields the same observable
        flag whether or not the guard exists.
        """

        async def scenario(tmp: Path) -> tuple[bool, int]:
            tmux = _FakeTmuxStream()
            stream = PaneStreamAdapter(tmux=tmux)
            actions = _FakeActionService()
            runtime = _fake_runtime(actions, stream)

            app = _Harness()
            async with app.run_test() as pilot:
                screen = ComposeWithMirrorScreen(
                    runtime,
                    pane_id="%206",
                    display_name="demo",
                    ring_dir=tmp,
                )
                await app.push_screen(screen)
                await pilot.pause()
                refresh_calls: list[bool] = []
                screen._refresh_guidance = lambda *a, **kw: refresh_calls.append(True)  # type: ignore[method-assign]
                # Already-False; calling False again should early-return.
                screen._set_mirror_input_mode(False)
                return screen._mirror_input_active, len(refresh_calls)

        with tempfile.TemporaryDirectory() as tmp:
            active, refresh_count = asyncio.run(scenario(Path(tmp)))

        assert active is False
        assert refresh_count == 0, (
            f"idempotency guard removed: _refresh_guidance fired {refresh_count} time(s)"
        )


class ComposeMirrorKeyHandlingTests(unittest.TestCase):
    """Cross-cutting key handling edges."""

    def test_shift_tab_in_compose_mode_swaps_focus(self) -> None:
        async def scenario(tmp: Path) -> tuple[bool, bool]:
            tmux = _FakeTmuxStream()
            stream = PaneStreamAdapter(tmux=tmux)
            actions = _FakeActionService()
            runtime = _fake_runtime(actions, stream)

            app = _Harness()
            async with app.run_test() as pilot:
                screen = ComposeWithMirrorScreen(
                    runtime,
                    pane_id="%301",
                    display_name="demo",
                    ring_dir=tmp,
                )
                await app.push_screen(screen)
                await pilot.pause()
                editor = app.screen.query_one("#compose-editor", TextArea)
                mirror = app.screen.query_one(LivePaneViewer)
                # Editor focused on mount → shift+tab moves to mirror.
                await pilot.press("shift+tab")
                await pilot.pause()
                mirror_focused = mirror.has_focus
                # And shift+tab again returns to the editor.
                await pilot.press("shift+tab")
                await pilot.pause()
                editor_focused = editor.has_focus
                return mirror_focused, editor_focused

        with tempfile.TemporaryDirectory() as tmp:
            mirror_focused, editor_focused = asyncio.run(scenario(Path(tmp)))

        assert mirror_focused is True
        assert editor_focused is True

    def test_live_input_unhandled_key_emits_status_and_does_not_send(self) -> None:
        async def scenario(tmp: Path) -> tuple[str, list[tuple[str, tuple[str, ...], bool]]]:
            tmux = _FakeTmuxStream()
            stream = PaneStreamAdapter(tmux=tmux)
            actions = _FakeActionService()
            runtime = _fake_runtime(actions, stream)

            app = _Harness()
            async with app.run_test() as pilot:
                screen = ComposeWithMirrorScreen(
                    runtime,
                    pane_id="%302",
                    display_name="demo",
                    ring_dir=tmp,
                    show_editor=False,
                )
                await app.push_screen(screen)
                await pilot.pause()
                # Mirror gets focus on mount in viewer-only mode.
                await pilot.press("i")
                await pilot.pause()
                # ``f1`` is not in the textual→tmux translation map.
                await pilot.press("f1")
                await pilot.pause()
                return screen._status, tmux.sent

        with tempfile.TemporaryDirectory() as tmp:
            status, sent = asyncio.run(scenario(Path(tmp)))

        assert "live input ignores" in status
        assert sent == []


class ComposeMirrorGuidanceTests(unittest.TestCase):
    """Status / subtitle / label rendering helpers."""

    def test_viewer_subtitle_reports_capture_error_when_set(self) -> None:
        async def scenario(tmp: Path) -> str:
            tmux = _FakeTmuxStream()
            stream = PaneStreamAdapter(tmux=tmux)
            actions = _FakeActionService()
            runtime = _fake_runtime(actions, stream)

            app = _Harness()
            async with app.run_test() as pilot:
                screen = ComposeWithMirrorScreen(
                    runtime,
                    pane_id="%401",
                    display_name="demo",
                    ring_dir=tmp,
                    show_editor=False,
                )
                await app.push_screen(screen)
                await pilot.pause()
                screen._capture_error = "✗ broken"
                screen._refresh_guidance(update_status=False)
                viewer = app.screen.query_one(LivePaneViewer)
                return str(viewer.border_subtitle or "")

        with tempfile.TemporaryDirectory() as tmp:
            subtitle = asyncio.run(scenario(Path(tmp)))

        assert subtitle == "capture failed"

    def test_viewer_subtitle_reports_sync_warning_when_set(self) -> None:
        async def scenario(tmp: Path) -> str:
            tmux = _FakeTmuxStream()
            stream = PaneStreamAdapter(tmux=tmux)
            actions = _FakeActionService()
            runtime = _fake_runtime(actions, stream)

            app = _Harness()
            async with app.run_test() as pilot:
                screen = ComposeWithMirrorScreen(
                    runtime,
                    pane_id="%402",
                    display_name="demo",
                    ring_dir=tmp,
                    show_editor=False,
                )
                await app.push_screen(screen)
                await pilot.pause()
                screen._sync_warning = "⚠ snap"
                screen._refresh_guidance(update_status=True)
                viewer = app.screen.query_one(LivePaneViewer)
                return str(viewer.border_subtitle or "")

        with tempfile.TemporaryDirectory() as tmp:
            subtitle = asyncio.run(scenario(Path(tmp)))

        assert "snapshot sync warning" in subtitle

    def test_viewer_subtitle_reports_stream_warning_when_set(self) -> None:
        async def scenario(tmp: Path) -> str:
            tmux = _FakeTmuxStream()
            stream = PaneStreamAdapter(tmux=tmux)
            actions = _FakeActionService()
            runtime = _fake_runtime(actions, stream)

            app = _Harness()
            async with app.run_test() as pilot:
                screen = ComposeWithMirrorScreen(
                    runtime,
                    pane_id="%403",
                    display_name="demo",
                    ring_dir=tmp,
                    show_editor=False,
                )
                await app.push_screen(screen)
                await pilot.pause()
                screen._stream_warning = "⚠ stream"
                screen._refresh_guidance(update_status=False)
                viewer = app.screen.query_one(LivePaneViewer)
                return str(viewer.border_subtitle or "")

        with tempfile.TemporaryDirectory() as tmp:
            subtitle = asyncio.run(scenario(Path(tmp)))

        assert "snapshot sync only" in subtitle

    def test_status_message_returns_capture_error_directly(self) -> None:
        async def scenario(tmp: Path) -> str:
            tmux = _FakeTmuxStream()
            stream = PaneStreamAdapter(tmux=tmux)
            actions = _FakeActionService()
            runtime = _fake_runtime(actions, stream)

            app = _Harness()
            async with app.run_test() as pilot:
                screen = ComposeWithMirrorScreen(
                    runtime,
                    pane_id="%404",
                    display_name="demo",
                    ring_dir=tmp,
                    show_editor=False,
                )
                await app.push_screen(screen)
                await pilot.pause()
                screen._capture_error = "✗ explicit-error"
                screen._refresh_guidance(update_status=True)
                return screen._status

        with tempfile.TemporaryDirectory() as tmp:
            status = asyncio.run(scenario(Path(tmp)))

        assert status == "✗ explicit-error"

    def test_status_message_prefixes_warning_to_guidance(self) -> None:
        async def scenario(tmp: Path) -> str:
            tmux = _FakeTmuxStream()
            stream = PaneStreamAdapter(tmux=tmux)
            actions = _FakeActionService()
            runtime = _fake_runtime(actions, stream)

            app = _Harness()
            async with app.run_test() as pilot:
                screen = ComposeWithMirrorScreen(
                    runtime,
                    pane_id="%405",
                    display_name="demo",
                    ring_dir=tmp,
                    show_editor=False,
                )
                await app.push_screen(screen)
                await pilot.pause()
                screen._sync_warning = "⚠ snap-warn"
                screen._capture_error = None
                screen._refresh_guidance(update_status=True)
                return screen._status

        with tempfile.TemporaryDirectory() as tmp:
            status = asyncio.run(scenario(Path(tmp)))

        assert status.startswith("⚠ snap-warn ·")

    def test_editor_label_in_viewer_only_mode_returns_live_viewer(self) -> None:
        async def scenario(tmp: Path) -> str:
            tmux = _FakeTmuxStream()
            stream = PaneStreamAdapter(tmux=tmux)
            actions = _FakeActionService()
            runtime = _fake_runtime(actions, stream)

            app = _Harness()
            async with app.run_test() as pilot:
                screen = ComposeWithMirrorScreen(
                    runtime,
                    pane_id="%406",
                    display_name="demo",
                    ring_dir=tmp,
                    show_editor=False,
                )
                await app.push_screen(screen)
                await pilot.pause()
                viewer = app.screen.query_one(LivePaneViewer)
                # Direct call: the helper short-circuits before any
                # editor query when ``_show_editor`` is False.
                return screen._editor_label(viewer)

        with tempfile.TemporaryDirectory() as tmp:
            label_text = asyncio.run(scenario(Path(tmp)))

        assert label_text == "live pane viewer"


class ComposeMirrorTeardownTests(unittest.TestCase):
    """``_teardown_pipe`` + ``_drain_ring`` adapter-None branches."""

    def test_teardown_swallows_tmux_command_error(self) -> None:
        async def scenario(tmp: Path) -> list[str]:
            tmux = _RaisingTmuxStream(
                seed_text="seed\n",
                stop_error=TmuxCommandError("kill-pipe", stderr="x"),
            )
            stream = PaneStreamAdapter(tmux=cast(Any, tmux))
            actions = _FakeActionService()
            runtime = _fake_runtime(actions, stream)

            app = _Harness()
            async with app.run_test() as pilot:
                screen = ComposeWithMirrorScreen(
                    runtime,
                    pane_id="%501",
                    display_name="demo",
                    ring_dir=tmp,
                    show_editor=False,
                )
                await app.push_screen(screen)
                await pilot.pause()
                # Trigger teardown explicitly. Should not raise even
                # though stop_pipe_pane raises ``TmuxCommandError``.
                screen._teardown_pipe()
                return tmux.pipe_stopped

        with tempfile.TemporaryDirectory() as tmp:
            stopped = asyncio.run(scenario(Path(tmp)))

        assert stopped == ["%501"]

    def test_teardown_swallows_oserror(self) -> None:
        async def scenario(tmp: Path) -> list[str]:
            tmux = _RaisingTmuxStream(
                seed_text="seed\n",
                stop_error=OSError("io"),
            )
            stream = PaneStreamAdapter(tmux=cast(Any, tmux))
            actions = _FakeActionService()
            runtime = _fake_runtime(actions, stream)

            app = _Harness()
            async with app.run_test() as pilot:
                screen = ComposeWithMirrorScreen(
                    runtime,
                    pane_id="%502",
                    display_name="demo",
                    ring_dir=tmp,
                    show_editor=False,
                )
                await app.push_screen(screen)
                await pilot.pause()
                screen._teardown_pipe()
                return tmux.pipe_stopped

        with tempfile.TemporaryDirectory() as tmp:
            stopped = asyncio.run(scenario(Path(tmp)))

        assert stopped == ["%502"]

    def test_drain_ring_no_op_when_adapter_is_none(self) -> None:
        async def scenario(tmp: Path) -> tuple[str | None, bool]:
            actions = _FakeActionService()
            runtime = _runtime_without_pane_stream(actions)

            app = _Harness()
            async with app.run_test() as pilot:
                screen = ComposeWithMirrorScreen(
                    runtime,
                    pane_id="%503",
                    display_name="demo",
                    ring_dir=tmp,
                    show_editor=False,
                )
                await app.push_screen(screen)
                await pilot.pause()
                # Adapter is None due to no pane_stream; _drain_ring
                # must short-circuit without raising.
                screen._drain_ring()
                screen._sync_snapshot(force=True)
                return screen._capture_error, screen._loading_cleared

        with tempfile.TemporaryDirectory() as tmp:
            err, cleared = asyncio.run(scenario(Path(tmp)))

        # The on_mount adapter-None branch already ran; both helpers
        # leave that state alone.
        assert err == "✗ pane streaming unavailable"
        assert cleared is True

    def test_drain_ring_clears_loading_when_first_chunk_arrives_late(self) -> None:
        async def scenario(tmp: Path) -> tuple[bool, bool]:
            tmux = _FakeTmuxStream(seed_text="hello\n")
            stream = PaneStreamAdapter(tmux=tmux)
            actions = _FakeActionService()
            runtime = _fake_runtime(actions, stream)

            app = _Harness()
            async with app.run_test() as pilot:
                screen = ComposeWithMirrorScreen(
                    runtime,
                    pane_id="%504",
                    display_name="demo",
                    ring_dir=tmp,
                    show_editor=False,
                )
                await app.push_screen(screen)
                await pilot.pause()
                # Force the not-yet-cleared branch by resetting the
                # flag: the next ring chunk will then re-clear loading.
                screen._loading_cleared = False
                ring_path = tmux.pipe_paths[0]
                with ring_path.open("a", encoding="utf-8") as fh:
                    fh.write("late\n")
                screen._drain_ring()
                return screen._loading_cleared, True

        with tempfile.TemporaryDirectory() as tmp:
            cleared, ok = asyncio.run(scenario(Path(tmp)))

        assert cleared is True
        assert ok is True


class ComposeMirrorAsyncPerfTests(unittest.TestCase):
    """Regression tests for the lazy/off-thread compose-mirror paths."""

    def test_live_input_keystroke_returns_before_tmux_send(self) -> None:
        """``_handle_live_input_key`` must not block on the tmux subprocess.

        Before the perf fix, every keystroke ran ``send_keys``
        synchronously on the UI thread. A slow tmux call would freeze
        the event loop until it returned. The fix dispatches the send
        through an asyncio task; the handler returns immediately and
        the lock-protected task drives the actual subprocess.
        """

        async def scenario(tmp: Path) -> tuple[bool, list[tuple[str, tuple[str, ...], bool]]]:
            recorded: list[tuple[str, tuple[str, ...], bool]] = []

            class _SlowTmux(_FakeTmuxStream):
                def send_keys(  # type: ignore[override]
                    self,
                    target_pane: str,
                    keys: Sequence[str],
                    /,
                    *,
                    literal: bool = False,
                    append_enter: bool = False,
                ) -> None:
                    del append_enter
                    # Simulate a slow tmux subprocess. If
                    # ``_handle_live_input_key`` were still called
                    # synchronously this delay would block the
                    # handler's return; with the fix the handler
                    # returns first and only the worker thread waits.
                    import threading

                    threading.Event().wait(0.05)
                    recorded.append((target_pane, tuple(keys), literal))

            tmux = _SlowTmux()
            stream = PaneStreamAdapter(tmux=tmux)
            actions = _FakeActionService()
            runtime = _fake_runtime(actions, stream)
            app = _MuxdeckHarness()
            async with app.run_test() as pilot:
                screen = ComposeWithMirrorScreen(
                    runtime,
                    pane_id="%99",
                    display_name="demo",
                    ring_dir=tmp,
                    show_editor=False,
                )
                await app.push_screen(screen)
                await pilot.pause()
                # Enter live-input mode.
                mirror = app.screen.query_one(LivePaneViewer)
                mirror.focus()
                await pilot.pause()
                screen._set_mirror_input_mode(True)
                from textual import events as textual_events

                event = textual_events.Key(key="x", character="x")
                # The handler must return before the (slow) tmux send
                # completes. We assert this by observing that the
                # ``recorded`` list is empty immediately after the
                # synchronous handler call returns.
                screen._handle_live_input_key(event)
                handler_returned_before_send = not recorded
                # Now drain the pending sends and observe the result.
                await screen._wait_for_pending_sends()
                return handler_returned_before_send, recorded

        with tempfile.TemporaryDirectory() as tmp:
            returned_early, sent = asyncio.run(scenario(Path(tmp)))

        assert returned_early is True
        assert sent == [("%99", ("x",), True)]

    def test_rapid_keystrokes_arrive_at_tmux_in_typed_order(self) -> None:
        """Asyncio.Lock guarantees keystrokes hit tmux in submission order."""

        async def scenario(tmp: Path) -> list[tuple[str, tuple[str, ...], bool]]:
            tmux = _FakeTmuxStream()
            stream = PaneStreamAdapter(tmux=tmux)
            actions = _FakeActionService()
            runtime = _fake_runtime(actions, stream)
            app = _MuxdeckHarness()
            async with app.run_test() as pilot:
                screen = ComposeWithMirrorScreen(
                    runtime,
                    pane_id="%17",
                    display_name="demo",
                    ring_dir=tmp,
                    show_editor=False,
                )
                await app.push_screen(screen)
                await pilot.pause()
                mirror = app.screen.query_one(LivePaneViewer)
                mirror.focus()
                screen._set_mirror_input_mode(True)
                from textual import events as textual_events

                # Submit a flurry of keystrokes synchronously, mimicking
                # a fast typist. Each submission appends a task; the
                # FIFO asyncio.Lock must serialise execution.
                for ch in "abcdef":
                    screen._handle_live_input_key(textual_events.Key(key=ch, character=ch))
                await screen._wait_for_pending_sends()
                return tmux.sent

        with tempfile.TemporaryDirectory() as tmp:
            sent = asyncio.run(scenario(Path(tmp)))

        assert sent == [
            ("%17", ("a",), True),
            ("%17", ("b",), True),
            ("%17", ("c",), True),
            ("%17", ("d",), True),
            ("%17", ("e",), True),
            ("%17", ("f",), True),
        ]

    def test_background_snapshot_tick_skips_when_already_in_flight(self) -> None:
        """Don't queue overlapping snapshot workers while one is running."""

        async def scenario(tmp: Path) -> tuple[int, int]:
            tmux = _FakeTmuxStream()
            stream = PaneStreamAdapter(tmux=tmux)
            actions = _FakeActionService()
            runtime = _fake_runtime(actions, stream)
            app = _MuxdeckHarness()
            async with app.run_test() as pilot:
                screen = ComposeWithMirrorScreen(
                    runtime,
                    pane_id="%41",
                    display_name="demo",
                    ring_dir=tmp,
                    show_editor=False,
                )
                await app.push_screen(screen)
                await pilot.pause()
                # Pretend a snapshot worker is already running. A
                # second tick must drop instead of dispatching a new
                # worker — the next periodic tick will catch up once
                # the in-flight one returns.
                screen._snapshot_in_flight = True
                snapshot_calls_before = len(tmux.captures)
                screen._tick_snapshot_in_background()
                await pilot.pause()
                snapshot_calls_after = len(tmux.captures)
                return snapshot_calls_before, snapshot_calls_after

        with tempfile.TemporaryDirectory() as tmp:
            before, after = asyncio.run(scenario(Path(tmp)))

        # Mount produced exactly one capture (seed); the skipped tick
        # must not have added another.
        assert before == after

    def test_background_snapshot_apply_records_tmux_error(self) -> None:
        """Errors from the worker capture surface as a sync warning."""

        async def scenario(tmp: Path) -> str | None:
            tmux = _FakeTmuxStream()
            stream = PaneStreamAdapter(tmux=tmux)
            actions = _FakeActionService()
            runtime = _fake_runtime(actions, stream)
            app = _MuxdeckHarness()
            async with app.run_test() as pilot:
                screen = ComposeWithMirrorScreen(
                    runtime,
                    pane_id="%42",
                    display_name="demo",
                    ring_dir=tmp,
                    show_editor=False,
                )
                await app.push_screen(screen)
                await pilot.pause()
                screen._apply_background_snapshot(
                    None,
                    TmuxCommandError(
                        "tmux capture-pane",
                        exit_code=1,
                        stderr="no such pane",
                        stdout="",
                    ),
                )
                return screen._sync_warning

        with tempfile.TemporaryDirectory() as tmp:
            warning = asyncio.run(scenario(Path(tmp)))

        assert warning is not None
        assert "no such pane" in warning


# Mark intentionally retained references to avoid unused-import errors
# from ruff in case future tests don't reach them.
_ = (Label, patch)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

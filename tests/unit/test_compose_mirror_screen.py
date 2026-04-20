"""Tests for :mod:`muxdeck.screens.compose_mirror`."""

from __future__ import annotations

import asyncio
import unittest
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast
from unittest.mock import patch

from textual.app import App, ComposeResult
from textual.containers import Vertical
from textual.widgets import TextArea

from muxdeck.adapters.pane_stream import PaneStreamAdapter
from muxdeck.app import MuxdeckRuntime
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


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

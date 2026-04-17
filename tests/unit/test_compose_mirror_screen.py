"""Tests for :mod:`copilot_commander.screens.compose_mirror`."""

from __future__ import annotations

import asyncio
import unittest
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

from textual.app import App, ComposeResult
from textual.widgets import TextArea

from copilot_commander.adapters.pane_stream import PaneStreamAdapter
from copilot_commander.app import CommanderRuntime
from copilot_commander.screens.compose_mirror import ComposeWithMirrorScreen
from copilot_commander.widgets.live_pane_viewer import LivePaneViewer


@dataclass
class _FakeTmuxStream:
    """Stand-in for the tmux surface used by :class:`PaneStreamAdapter`.

    Records every call so tests can assert seed / pipe / send-keys
    lifecycle without touching a real tmux.
    """

    captures: list[str] = field(default_factory=list)
    pipe_started: list[str] = field(default_factory=list)
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
        del start_line, end_line, join_wrapped_lines, include_escape_sequences
        self.captures.append(target_pane)
        return self.seed_text

    def pipe_pane_to_file(
        self,
        target_pane: str,
        /,
        *,
        target_path: Path,
        append: bool = True,
    ) -> None:
        del target_path, append
        self.pipe_started.append(target_pane)

    def stop_pipe_pane(self, target_pane: str, /) -> None:
        self.pipe_stopped.append(target_pane)

    def send_keys(
        self,
        target_pane: str,
        keys: Iterable[str],
        /,
        *,
        literal: bool = False,
    ) -> None:
        self.sent.append((target_pane, tuple(keys), literal))

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


def _fake_runtime(actions: _FakeActionService, stream: PaneStreamAdapter) -> CommanderRuntime:
    return cast(
        CommanderRuntime,
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


class ComposeWithMirrorScreenTests(unittest.TestCase):
    """End-to-end behavioural tests for the compose + mirror screen."""

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


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

"""Full-screen compose-with-live-mirror screen.

Opened from the Dashboard via ``v``.  The layout:

* **Top (most of the screen)** — a live mirror of the agent's tmux
  pane.  We seed it with the current scrollback, then wire
  ``tmux pipe-pane`` so every new byte written to the real pane is
  streamed in.  The widget is a scrollable RichLog, so the user can
  scroll up through history, follow the tail, and select text using
  Textual's built-in selection.

* **Bottom** — a multi-line :class:`TextArea` composer backed by
  Textual's full editor bindings (cursor motion, selection, copy,
  cut, paste, word-wise motion, delete, undo).  This is where the
  user types a message to send to the agent.

Key bindings:

* ``tab`` / ``shift+tab`` — move focus between the editor and the
  mirror (so the user can scroll the mirror without leaving the
  screen).
* ``ctrl+s`` / ``ctrl+enter`` / ``ctrl+j`` — send the composed text
  to the pane (via ``tmux send-keys``, appending Enter).
* ``escape`` — close the screen.  Also exits the compose editor when
  it currently has focus; pressing ``escape`` a second time (while
  the mirror has focus) closes the screen.

Closing the screen tears the ``pipe-pane`` down so tmux doesn't keep
a dangling writer open against a vanished sink.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.widgets import Label, TextArea

from copilot_commander.adapters.pane_stream import (
    PaneRingReader,
    PaneStreamAdapter,
    ring_file_for_pane,
)
from copilot_commander.bindings import BindingSpec, KeyHint
from copilot_commander.exceptions import TmuxCommandError
from copilot_commander.screens.base import ShellScreen
from copilot_commander.widgets.live_pane_viewer import LivePaneViewer

if TYPE_CHECKING:
    from copilot_commander.app import CommanderRuntime


# Poll the ring file often enough that the mirror feels live (100ms)
# without pummelling the filesystem.  The reader short-circuits on an
# unchanged ``st_size`` so idle panes are effectively free.
_POLL_INTERVAL_SEC = 0.1


COMPOSE_MIRROR_BINDINGS: list[BindingSpec] = [
    Binding("ctrl+s", "send", "Send", show=False),
    Binding("ctrl+enter", "send", "Send", show=False),
    Binding("ctrl+j", "send", "Send", show=False),
]

COMPOSE_MIRROR_HINTS: tuple[KeyHint, ...] = (
    KeyHint("ctrl+s", "send"),
    KeyHint("tab", "focus mirror/editor"),
    KeyHint("pgup/pgdn", "scroll mirror"),
    KeyHint("esc", "close"),
)


class ComposeWithMirrorScreen(ShellScreen):
    """Pane mirror on top, compose editor on bottom, single screen."""

    SCREEN_TITLE = "COMPOSE"
    BINDINGS = COMPOSE_MIRROR_BINDINGS
    FOOTER_HINTS = COMPOSE_MIRROR_HINTS

    DEFAULT_CSS = """
    ComposeWithMirrorScreen #compose-container {
        height: 1fr;
        width: 1fr;
    }

    ComposeWithMirrorScreen #compose-mirror-wrap {
        height: 1fr;
        width: 1fr;
    }

    ComposeWithMirrorScreen LivePaneViewer {
        height: 1fr;
        width: 1fr;
    }

    ComposeWithMirrorScreen #compose-editor-wrap {
        height: 14;
        width: 1fr;
        margin-top: 1;
    }

    ComposeWithMirrorScreen #compose-editor-label {
        height: 1;
        width: 1fr;
        color: #a89984;
    }

    ComposeWithMirrorScreen #compose-editor {
        height: 1fr;
        width: 1fr;
        background: #1d2021;
        border: solid #504945;
    }

    ComposeWithMirrorScreen #compose-editor:focus {
        border: solid #b8bb26;
    }
    """

    def __init__(
        self,
        runtime: CommanderRuntime,
        *,
        pane_id: str,
        display_name: str,
        ring_dir: Path | None = None,
    ) -> None:
        super().__init__(runtime)
        self._pane_id = pane_id
        self._display_name = display_name
        resolved_root = ring_dir if ring_dir is not None else Path(tempfile.gettempdir())
        self._ring_path = ring_file_for_pane(resolved_root, pane_id)
        self._adapter: PaneStreamAdapter | None = runtime.pane_stream
        self._reader = PaneRingReader(self._ring_path)
        self._pipe_started = False
        self._loading_cleared = False

    # ── composition & mount ──────────────────────────────────────────

    def compose_body(self) -> ComposeResult:
        with Vertical(id="compose-container"):
            with Vertical(id="compose-mirror-wrap"):
                viewer = LivePaneViewer(widget_id="compose-mirror")
                viewer.border_title = self._format_mirror_title()
                yield viewer
            with Vertical(id="compose-editor-wrap"):
                yield Label(
                    "compose · ctrl+s send · tab switch focus · esc close",
                    id="compose-editor-label",
                )
                editor = TextArea(
                    id="compose-editor",
                    show_line_numbers=False,
                    soft_wrap=True,
                )
                editor.border_title = f"message → {self._display_name}"
                yield editor

    def on_mount(self) -> None:
        viewer = self.query_one(LivePaneViewer)
        self.begin_loading(viewer)
        if self._adapter is None:
            self.end_loading(viewer)
            self.set_status("✗ pane streaming unavailable")
        else:
            self._seed_and_stream(viewer)
        # Land focus in the editor so typing just works.  The mirror
        # can be reached with tab.
        self.query_one("#compose-editor", TextArea).focus()
        self.set_status(self._default_status())

    def on_unmount(self) -> None:
        self._teardown_pipe()

    def _seed_and_stream(self, viewer: LivePaneViewer) -> None:
        assert self._adapter is not None
        try:
            seed = self._adapter.seed(self._pane_id)
        except TmuxCommandError as exc:
            self.end_loading(viewer)
            self.set_status(f"✗ capture failed: {exc.stderr or 'tmux error'}")
            return
        except OSError as exc:
            self.end_loading(viewer)
            self.set_status(f"✗ capture failed: {exc}")
            return
        if seed:
            viewer.append(seed)
            self.end_loading(viewer)
            self._loading_cleared = True
        try:
            self._adapter.start_pipe(self._pane_id, self._ring_path)
            self._pipe_started = True
        except TmuxCommandError as exc:
            self.set_status(f"⚠ live stream unavailable: {exc.stderr or 'tmux error'}")
        except OSError as exc:
            self.set_status(f"⚠ live stream unavailable: {exc}")
        self.set_interval(_POLL_INTERVAL_SEC, self._drain_ring)

    # ── polling ──────────────────────────────────────────────────────

    def _drain_ring(self) -> None:
        if self._adapter is None:
            return
        try:
            chunk = self._reader.read_new()
        except OSError as exc:
            self.set_status(f"⚠ ring read failed: {exc}")
            return
        if not chunk:
            return
        viewer = self.query_one(LivePaneViewer)
        viewer.append(chunk)
        if not self._loading_cleared:
            self.end_loading(viewer)
            self._loading_cleared = True

    # ── key handling ─────────────────────────────────────────────────

    async def on_key(self, event: events.Key) -> None:
        # Tab/Shift+Tab cycles focus between editor and mirror.  The
        # editor consumes tab for indentation by default, so we handle
        # the swap at the screen level *before* it gets there.
        if event.key == "tab":
            self._swap_focus(forward=True)
            event.stop()
            event.prevent_default()
            return
        if event.key == "shift+tab":
            self._swap_focus(forward=False)
            event.stop()
            event.prevent_default()
            return
        # Escape closes the screen regardless of which child has focus.
        # TextArea doesn't consume escape, and RichLog doesn't either,
        # so we never steal the key from a legitimate handler.
        if event.key == "escape":
            event.stop()
            event.prevent_default()
            self.action_close()
            return

    def _swap_focus(self, *, forward: bool) -> None:
        del forward  # two-widget cycle — direction doesn't matter
        editor = self.query_one("#compose-editor", TextArea)
        mirror = self.query_one(LivePaneViewer)
        if editor.has_focus:
            mirror.focus()
        else:
            editor.focus()

    # ── actions ──────────────────────────────────────────────────────

    def action_send(self) -> None:
        if self.runtime.actions is None:
            self.set_status("✗ action service unavailable")
            return
        editor = self.query_one("#compose-editor", TextArea)
        text = editor.text.rstrip()
        if not text:
            self.set_status("nothing to send — type a message first")
            editor.focus()
            return
        result = self.runtime.actions.send_message(self._pane_id, text)
        if result.success:
            editor.text = ""
            editor.focus()
            self.set_status(f"✓ sent to {self._display_name} ({self._pane_id})")
        else:
            self.set_status(f"✗ {result.message}")

    def action_close(self) -> None:
        self._teardown_pipe()
        self.app.pop_screen()

    # ── helpers ──────────────────────────────────────────────────────

    def _teardown_pipe(self) -> None:
        if self._adapter is None or not self._pipe_started:
            return
        self._pipe_started = False
        try:
            self._adapter.stop_pipe(self._pane_id)
        except TmuxCommandError:
            return
        except OSError:
            return

    def _format_mirror_title(self) -> str:
        return f"mirror · pane {self._pane_id} — {self._display_name}"

    def _default_status(self) -> str:
        return (
            f"compose → {self._display_name} ({self._pane_id}) · "
            "ctrl+s send · tab switch focus · esc close"
        )


__all__ = [
    "COMPOSE_MIRROR_BINDINGS",
    "COMPOSE_MIRROR_HINTS",
    "ComposeWithMirrorScreen",
]

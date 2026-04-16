"""Full-screen live tmux pane viewer.

Opened from the Dashboard via ``v``. The screen:

* Seeds the viewer with the current pane scrollback (coloured, with
  wrapped lines joined) so the user sees context immediately.
* Wires ``tmux pipe-pane`` so every byte written to the real pane is
  appended to a session-scoped ring file and streamed into the viewer.
* On unmount / Escape, stops piping so tmux doesn't keep a dangling
  shell open against a vanished screen.
* Optionally (``x``) forwards typed keys to the pane via ``send-keys``.
  Write-through is off by default so a broken key path can't break
  viewing — read-only observation always works.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical

from copilot_commander.adapters.pane_stream import (
    PaneRingReader,
    PaneStreamAdapter,
    ring_file_for_pane,
    translate_textual_key,
)
from copilot_commander.bindings import BindingSpec, KeyHint
from copilot_commander.exceptions import TmuxCommandError
from copilot_commander.screens.base import ShellScreen
from copilot_commander.widgets.live_pane_viewer import LivePaneViewer

if TYPE_CHECKING:
    from copilot_commander.app import CommanderRuntime


# Poll the ring file often enough that the viewer feels live (100ms)
# without pummelling the filesystem. The reader short-circuits on an
# unchanged ``st_size`` so idle panes are nearly free.
_POLL_INTERVAL_SEC = 0.1


PANE_VIEWER_BINDINGS: list[BindingSpec] = [
    Binding("escape", "close_viewer", "Close", show=False, priority=True),
    Binding("x", "toggle_write_through", "Toggle input", show=False),
]

PANE_VIEWER_HINTS: tuple[KeyHint, ...] = (
    KeyHint("esc", "close"),
    KeyHint("x", "toggle input"),
)


class PaneViewerScreen(ShellScreen):
    """Live, streaming view of one tmux pane."""

    SCREEN_TITLE = "PANE"
    BINDINGS = PANE_VIEWER_BINDINGS
    FOOTER_HINTS = PANE_VIEWER_HINTS

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
        self._write_through = False
        self._loading_cleared = False
        self._first_chunk_seen = False

    # ── composition & mount ──────────────────────────────────────────

    def compose_body(self) -> ComposeResult:
        with Vertical(id="pane-viewer-root"):
            viewer = LivePaneViewer(widget_id="pane-viewer")
            viewer.border_title = f"Pane {self._pane_id} — {self._display_name}"
            yield viewer

    def on_mount(self) -> None:
        viewer = self.query_one(LivePaneViewer)
        self.begin_loading(viewer)
        if self._adapter is None:
            self.end_loading(viewer)
            self.set_status("✗ pane streaming unavailable")
            return
        # Seed with the current scrollback so the viewer isn't empty
        # until the first streamed byte arrives.
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
            self._first_chunk_seen = True
            self.end_loading(viewer)
            self._loading_cleared = True
        # Start streaming; if this fails we keep the seeded view
        # rather than tear the whole screen down.
        try:
            self._adapter.start_pipe(self._pane_id, self._ring_path)
            self._pipe_started = True
        except TmuxCommandError as exc:
            self.set_status(f"⚠ pipe-pane failed: {exc.stderr or 'tmux error'} — read-only")
        except OSError as exc:
            self.set_status(f"⚠ pipe-pane failed: {exc} — read-only")
        self.set_interval(_POLL_INTERVAL_SEC, self._drain_ring)
        self._update_status()

    def on_unmount(self) -> None:
        self._teardown_pipe()

    # ── polling ──────────────────────────────────────────────────────

    def _drain_ring(self) -> None:
        if self._adapter is None:
            return
        try:
            chunk = self._reader.read_new()
        except OSError as exc:
            # Ring reader already swallows OSError internally, but be
            # defensive — a corrupted file shouldn't kill the timer.
            self.set_status(f"⚠ ring read failed: {exc}")
            return
        if not chunk:
            return
        viewer = self.query_one(LivePaneViewer)
        viewer.append(chunk)
        if not self._first_chunk_seen:
            self._first_chunk_seen = True
        if not self._loading_cleared:
            self.end_loading(viewer)
            self._loading_cleared = True

    # ── actions ──────────────────────────────────────────────────────

    def action_close_viewer(self) -> None:
        # ``switch_mode`` to dashboard handles both push_screen and
        # mode-based navigation callers; we were added via push_screen
        # from the dashboard so ``pop_screen`` is the right tear-down.
        self._teardown_pipe()
        self.app.pop_screen()

    def action_toggle_write_through(self) -> None:
        if self._adapter is None:
            self.set_status("✗ pane streaming unavailable")
            return
        self._write_through = not self._write_through
        self._update_status()

    # ── key forwarding ───────────────────────────────────────────────

    async def on_key(self, event: events.Key) -> None:
        """Forward keystrokes to the pane when write-through is on.

        Only handled when the viewer has focus *and* the user has
        explicitly enabled write-through via ``x``. Otherwise we let
        Textual dispatch the key normally (so Escape / x / tab nav
        still work). The bound actions take precedence over this
        handler because they're declared in ``BINDINGS`` with
        ``priority=True`` where needed.
        """
        if not self._write_through or self._adapter is None:
            return
        translation = translate_textual_key(event.key)
        if translation is None:
            return
        # Stop Textual dispatching this key to its own bindings while
        # write-through is active — otherwise printable characters
        # would still try to trigger ``x`` / ``escape`` at the screen.
        event.stop()
        event.prevent_default()
        try:
            self._adapter.send_keys(self._pane_id, translation)
        except TmuxCommandError as exc:
            self.set_status(f"✗ send-keys failed: {exc.stderr or 'tmux error'}")
        except OSError as exc:
            self.set_status(f"✗ send-keys failed: {exc}")

    # ── helpers ──────────────────────────────────────────────────────

    def _teardown_pipe(self) -> None:
        if self._adapter is None or not self._pipe_started:
            return
        self._pipe_started = False
        try:
            self._adapter.stop_pipe(self._pane_id)
        except TmuxCommandError:
            # Pane may already be dead — nothing to unpipe.
            return
        except OSError:
            return

    def _update_status(self) -> None:
        mode = "input: ON" if self._write_through else "input: off (press x)"
        pipe = "streaming" if self._pipe_started else "static"
        self.set_status(f"pane {self._pane_id} · {pipe} · {mode}")


__all__ = ["PANE_VIEWER_BINDINGS", "PANE_VIEWER_HINTS", "PaneViewerScreen"]

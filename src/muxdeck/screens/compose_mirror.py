"""Full-screen live tmux-pane viewer with optional compose editor.

The screen always renders a live mirror of the agent's tmux pane. Callers
may additionally enable a bottom-half compose editor when they want
message drafting in the same view.

Key bindings:

* ``tab`` / ``shift+tab`` — move focus between the editor and mirror
  when compose mode is enabled.
* ``ctrl+s`` / ``ctrl+enter`` / ``ctrl+j`` — send the composed text in
  compose mode.
* ``i`` (while the mirror is focused) — enter live-input mode; keys go
  directly to the tmux pane until ``escape``.
* ``alt+up`` / ``alt+down`` — shrink / grow the compose editor in
  compose mode.
* ``escape`` — leave live-input mode, otherwise close the screen.

Closing the screen tears the ``pipe-pane`` down and removes the per-pane
ring file.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from pathlib import Path
from typing import TYPE_CHECKING, cast

from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.widgets import Label, TextArea

from muxdeck import theme
from muxdeck.adapters.pane_stream import (
    KeyTranslation,
    PaneRingReader,
    PaneStreamAdapter,
    ring_file_for_pane,
    translate_textual_key,
)
from muxdeck.bindings import BindingSpec, KeyHint
from muxdeck.exceptions import TmuxCommandError
from muxdeck.screens.base import ShellScreen
from muxdeck.widgets.live_pane_viewer import LivePaneViewer

if TYPE_CHECKING:
    from muxdeck.app import MuxdeckApp, MuxdeckRuntime


_log = logging.getLogger(__name__)


# Ring-file polling stays fast for fresh line-oriented output; the
# snapshot poll runs on a worker thread and corrects dynamic redraws
# (vim, less, status lines) that bypass the line-oriented pipe-pane
# stream. We poll at 1s rather than 250ms because:
#
# * the subprocess fan-out at 4 Hz starves the UI thread on slower
#   shells (especially WSL ↔ Windows tmux), and
# * the ring-file drain already reflects fresh line output within
#   100ms, so the snapshot is purely a correction layer.
_POLL_INTERVAL_SEC = 0.1
_SNAPSHOT_SYNC_INTERVAL_SEC = 1.0
_SNAPSHOT_WORKER_GROUP = "compose-snapshot"
_DEFAULT_EDITOR_HEIGHT = 10
_MIN_EDITOR_HEIGHT = 7
_MAX_EDITOR_HEIGHT = 24
_EDITOR_HEIGHT_STEP = 2
_RING_DIR_NAME = "pane-mirror"


COMPOSE_MIRROR_BINDINGS: list[BindingSpec] = [
    Binding("ctrl+s", "send", "Send", show=False),
    Binding("ctrl+enter", "send", "Send", show=False),
    Binding("ctrl+j", "send", "Send", show=False),
    Binding("w", "toggle_wrap", "Wrap logs", show=False),
    Binding("f", "toggle_follow", "Follow output", show=False),
    Binding("alt+up", "shrink_editor", "More mirror", show=False),
    Binding("alt+down", "grow_editor", "More editor", show=False),
]

COMPOSE_MIRROR_HINTS: tuple[KeyHint, ...] = (
    KeyHint("ctrl+s", "send"),
    KeyHint("tab", "focus"),
    KeyHint("w", "wrap"),
    KeyHint("f", "follow"),
    KeyHint("i", "interact"),
    KeyHint("alt+up/down", "resize"),
    KeyHint("r", "resync"),
    KeyHint("esc", "back"),
)

LIVE_MIRROR_HINTS: tuple[KeyHint, ...] = (
    KeyHint("w", "wrap"),
    KeyHint("f", "follow"),
    KeyHint("i", "interact"),
    KeyHint("r", "resync"),
    KeyHint("esc", "back"),
)


class ComposeWithMirrorScreen(ShellScreen):
    """Pane mirror on top, compose editor on bottom, single screen."""

    SCREEN_TITLE = "COMPOSE"
    BINDINGS = COMPOSE_MIRROR_BINDINGS
    FOOTER_HINTS = COMPOSE_MIRROR_HINTS

    DEFAULT_CSS = f"""
    ComposeWithMirrorScreen #compose-container {{
        height: 1fr;
        width: 1fr;
    }}

    ComposeWithMirrorScreen #compose-mirror-wrap {{
        height: 1fr;
        width: 1fr;
    }}

    ComposeWithMirrorScreen LivePaneViewer {{
        height: 1fr;
        width: 1fr;
    }}

    ComposeWithMirrorScreen #compose-editor-wrap {{
        height: 10;
        width: 1fr;
        margin-top: 1;
    }}

    ComposeWithMirrorScreen #compose-editor-label {{
        height: 1;
        width: 1fr;
        color: {theme.FG2};
    }}

    ComposeWithMirrorScreen #compose-editor {{
        height: 1fr;
        width: 1fr;
        background: {theme.BG_HARD};
        color: {theme.FG};
        border: solid {theme.BORDER};
    }}

    ComposeWithMirrorScreen #compose-editor:focus {{
        border: solid {theme.BORDER_FOCUS};
    }}
    """

    def __init__(
        self,
        runtime: MuxdeckRuntime,
        *,
        pane_id: str,
        display_name: str,
        ring_dir: Path | None = None,
        show_editor: bool = True,
        stream_adapter: PaneStreamAdapter | None = None,
    ) -> None:
        super().__init__(runtime)
        self._pane_id = pane_id
        self._display_name = display_name
        self._show_editor = show_editor
        resolved_root = (
            ring_dir if ring_dir is not None else runtime.config.paths.state_dir / _RING_DIR_NAME
        )
        self._ring_path = ring_file_for_pane(resolved_root, pane_id)
        self._adapter: PaneStreamAdapter | None = stream_adapter or runtime.pane_stream
        self._reader = PaneRingReader(self._ring_path)
        self._pipe_started = False
        self._loading_cleared = False
        self._mirror_input_active = False
        self._editor_height = _DEFAULT_EDITOR_HEIGHT
        self._last_snapshot = ""
        self._capture_error: str | None = None
        self._stream_warning: str | None = None
        self._sync_warning: str | None = None
        # In-flight asyncio tasks for forwarded keystrokes. We keep
        # references so the GC doesn't drop them mid-flight and so
        # tests can deterministically wait for the full keystroke
        # round-trip via :meth:`_wait_for_pending_sends`.
        self._send_tasks: set[asyncio.Task[None]] = set()
        # FIFO lock guarantees keystrokes hit tmux in the order the
        # operator typed them, even though each ``send_keys`` call
        # runs on a worker thread off the UI loop.
        self._send_lock = asyncio.Lock()
        # ``True`` while a periodic snapshot worker is in flight; we
        # skip subsequent ticks rather than queueing them so a slow
        # tmux subprocess can never accumulate a backlog of pending
        # captures and starve the worker pool.
        self._snapshot_in_flight = False

    @property
    def editor_height(self) -> int:
        return self._editor_height

    @property
    def mirror_input_active(self) -> bool:
        return self._mirror_input_active

    @property
    def muxdeck_app(self) -> MuxdeckApp:
        return cast("MuxdeckApp", self.app)

    def footer_hints(self) -> tuple[KeyHint, ...]:
        hints = COMPOSE_MIRROR_HINTS if self._show_editor else LIVE_MIRROR_HINTS
        return (*hints, KeyHint("q", "quit"))

    def apply_ui_preferences(self) -> bool:
        if not self.is_mounted:
            return True
        viewer = self.query_one(LivePaneViewer)
        viewer.set_wrap_mode(self.muxdeck_app.ui_preferences.wrap_logs)
        self._refresh_guidance(update_status=False)
        return True

    # ── composition & mount ──────────────────────────────────────────

    def compose_body(self) -> ComposeResult:
        with Vertical(id="compose-container"):
            with Vertical(id="compose-mirror-wrap"):
                viewer = LivePaneViewer(widget_id="compose-mirror")
                viewer.border_title = self._format_mirror_title()
                yield viewer
            if self._show_editor:
                with Vertical(id="compose-editor-wrap"):
                    yield Label("loading pane mirror…", id="compose-editor-label")
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
        viewer.border_subtitle = "loading tmux snapshot…"
        self.set_status("loading pane snapshot… live mirror will follow tmux output")
        self._apply_editor_height()
        self.apply_ui_preferences()
        if self._adapter is None:
            self._capture_error = "✗ pane streaming unavailable"
            self.end_loading(viewer)
            self._loading_cleared = True
        else:
            self._seed_and_stream(viewer)
        if self._show_editor:
            # Land focus in the editor so typing just works. The mirror can
            # be reached with tab.
            self.query_one("#compose-editor", TextArea).focus()
        else:
            viewer.focus()
        self._refresh_guidance()

    def on_unmount(self) -> None:
        self._teardown_pipe()

    def on_descendant_focus(self, event: events.DescendantFocus) -> None:
        super().on_descendant_focus(event)
        del event
        mirror = self.query_one(LivePaneViewer)
        if self._mirror_input_active and not mirror.has_focus:
            self._mirror_input_active = False
        self._refresh_guidance()

    def _seed_and_stream(self, viewer: LivePaneViewer) -> None:
        assert self._adapter is not None
        try:
            snapshot = self._adapter.capture_snapshot(self._pane_id)
        except TmuxCommandError as exc:
            self._capture_error = f"✗ capture failed: {exc.stderr or 'tmux error'}"
            self.end_loading(viewer)
            self._loading_cleared = True
            return
        except OSError as exc:
            self._capture_error = f"✗ capture failed: {exc}"
            self.end_loading(viewer)
            self._loading_cleared = True
            return
        self._capture_error = None
        self._sync_warning = None
        viewer.set_snapshot(snapshot)
        self._last_snapshot = snapshot
        self.end_loading(viewer)
        self._loading_cleared = True
        try:
            self._adapter.start_pipe(self._pane_id, self._ring_path)
            self._pipe_started = True
            self._stream_warning = None
        except TmuxCommandError as exc:
            detail = exc.stderr or "tmux error"
            self._stream_warning = f"⚠ live stream unavailable ({detail}); snapshot sync only"
        except OSError as exc:
            self._stream_warning = f"⚠ live stream unavailable ({exc}); snapshot sync only"
        self.set_interval(_POLL_INTERVAL_SEC, self._drain_ring)
        # Snapshot resyncs are corrective ─ they fix dynamic redraws
        # (vim/less/status lines) that the line-oriented pipe-pane
        # stream can't represent. The capture itself is a tmux
        # subprocess that takes 30-100 ms on slower shells; running
        # it on the UI thread froze the event loop several times a
        # second. We dispatch each tick to a worker thread instead so
        # keystrokes, scrolling, and other paints stay snappy.
        self.set_interval(_SNAPSHOT_SYNC_INTERVAL_SEC, self._tick_snapshot_in_background)

    # ── polling ──────────────────────────────────────────────────────

    def _drain_ring(self) -> None:
        if self._adapter is None:
            return
        chunk = self._reader.read_new()
        if not chunk:
            return
        viewer = self.query_one(LivePaneViewer)
        viewer.append(chunk)
        self._refresh_guidance(update_status=False)
        if not self._loading_cleared:
            self.end_loading(viewer)
            self._loading_cleared = True
            self._refresh_guidance()

    def _sync_snapshot(self, *, force: bool = False) -> None:
        if self._adapter is None:
            return
        viewer = self.query_one(LivePaneViewer)
        try:
            snapshot = self._adapter.capture_snapshot(self._pane_id)
        except TmuxCommandError as exc:
            self._sync_warning = f"⚠ snapshot sync failed: {exc.stderr or 'tmux error'}"
            if force and not viewer.has_content:
                self._capture_error = self._sync_warning
            if force or not self._loading_cleared:
                self.end_loading(viewer)
                self._loading_cleared = True
            self._refresh_guidance(update_status=True)
            return
        except OSError as exc:
            self._sync_warning = f"⚠ snapshot sync failed: {exc}"
            if force and not viewer.has_content:
                self._capture_error = self._sync_warning
            if force or not self._loading_cleared:
                self.end_loading(viewer)
                self._loading_cleared = True
            self._refresh_guidance(update_status=True)
            return
        had_warning = self._capture_error is not None or self._sync_warning is not None
        self._capture_error = None
        self._sync_warning = None
        if snapshot != self._last_snapshot:
            if viewer.matches_snapshot_tail(snapshot):
                self._last_snapshot = snapshot
            elif viewer.has_content and snapshot:
                viewer.replace_tail(snapshot)
                self._last_snapshot = snapshot
            else:
                viewer.set_snapshot(snapshot)
                self._last_snapshot = snapshot
        if force or not self._loading_cleared:
            self.end_loading(viewer)
            self._loading_cleared = True
        self._refresh_guidance(update_status=force or had_warning)

    def refresh_data(self) -> None:
        viewer = self.query_one(LivePaneViewer)
        self.begin_loading(viewer)
        self._sync_snapshot(force=True)

    def _tick_snapshot_in_background(self) -> None:
        """Periodic snapshot resync — runs the tmux capture on a worker.

        Replaces the original "call ``_sync_snapshot`` on the UI thread
        every 250 ms" loop, which forked a tmux subprocess on every
        tick and froze the event loop for the duration of the capture.
        We now fan the capture out to a worker thread and only the
        cheap ``viewer.set_snapshot`` / ``viewer.replace_tail`` paths
        run on the UI thread when the result lands.
        """
        if self._adapter is None:
            return
        if self._mirror_input_active:
            # While the operator is typing into the live pane the
            # pipe-pane stream is the source of truth — every keystroke
            # echoes back through ``_drain_ring`` and lands in the
            # viewer within ~100ms. A snapshot resync at this point
            # almost always disagrees with the streamed tail (the
            # cursor moved between snapshots) and the apply path then
            # invalidates the viewer's decoded cache and re-renders the
            # full ~2000-line buffer on the UI thread, hitching every
            # keystroke. Skip the tick entirely; the next periodic
            # capture after the operator presses ``esc`` will resync
            # any genuine drift.
            return
        if self._snapshot_in_flight:
            # Another tick is still mid-capture. Don't queue another
            # — let it complete; the next periodic tick will pick up
            # any drift. Queueing would let a slow capture starve
            # the worker pool indefinitely on a busy system.
            return
        self._snapshot_in_flight = True
        adapter = self._adapter
        pane_id = self._pane_id

        def _capture() -> tuple[str | None, BaseException | None]:
            try:
                return adapter.capture_snapshot(pane_id), None
            except (TmuxCommandError, OSError) as exc:
                return None, exc

        def _worker() -> None:
            snapshot, error = _capture()
            self.app.call_from_thread(self._apply_background_snapshot, snapshot, error)

        self.run_worker(
            _worker,
            thread=True,
            exclusive=True,
            group=_SNAPSHOT_WORKER_GROUP,
        )

    def _apply_background_snapshot(
        self,
        snapshot: str | None,
        error: BaseException | None,
    ) -> None:
        """Apply a worker-fetched snapshot on the UI thread.

        Mirrors the body of :meth:`_sync_snapshot` minus the capture
        call, so the manual ``r`` resync (which still calls
        ``_sync_snapshot(force=True)`` directly) keeps its
        synchronous semantics for tests and the existing wait-for-r
        UX guarantee.
        """
        self._snapshot_in_flight = False
        if not self.is_mounted:
            return
        try:
            viewer = self.query_one(LivePaneViewer)
        except Exception:
            return
        if error is not None:
            if isinstance(error, TmuxCommandError):
                detail = error.stderr or "tmux error"
            else:
                detail = str(error)
            self._sync_warning = f"⚠ snapshot sync failed: {detail}"
            self._refresh_guidance(update_status=True)
            return
        if snapshot is None:
            return
        had_warning = self._capture_error is not None or self._sync_warning is not None
        self._capture_error = None
        self._sync_warning = None
        if snapshot != self._last_snapshot:
            if viewer.matches_snapshot_tail(snapshot):
                self._last_snapshot = snapshot
            elif viewer.has_content and snapshot:
                viewer.replace_tail(snapshot)
                self._last_snapshot = snapshot
            else:
                viewer.set_snapshot(snapshot)
                self._last_snapshot = snapshot
        if not self._loading_cleared:
            self.end_loading(viewer)
            self._loading_cleared = True
        self._refresh_guidance(update_status=had_warning)

    # ── key handling ─────────────────────────────────────────────────

    async def on_key(self, event: events.Key) -> None:
        if self._handle_live_input_key(event):
            return
        mirror = self.query_one(LivePaneViewer)
        if event.key == "i" and mirror.has_focus and self._adapter is not None:
            self._set_mirror_input_mode(True)
            event.stop()
            event.prevent_default()
            return
        # Tab/Shift+Tab cycles focus between editor and mirror. The
        # editor consumes tab for indentation by default, so we handle
        # the swap at the screen level *before* it gets there.
        if self._show_editor and event.key == "tab":
            self._swap_focus(forward=True)
            event.stop()
            event.prevent_default()
            return
        if self._show_editor and event.key == "shift+tab":
            self._swap_focus(forward=False)
            event.stop()
            event.prevent_default()
            return
        if not self._show_editor and event.key in {"tab", "shift+tab"}:
            self.query_one(LivePaneViewer).focus()
            event.stop()
            event.prevent_default()
            return
        if event.key == "escape":
            event.stop()
            event.prevent_default()
            self.action_close()
            return

    def _handle_live_input_key(self, event: events.Key) -> bool:
        if not self._mirror_input_active or self._adapter is None:
            return False
        mirror = self.query_one(LivePaneViewer)
        if not mirror.has_focus:
            return False
        event.stop()
        event.prevent_default()
        if event.key == "escape":
            self._set_mirror_input_mode(False)
            return True
        translation = translate_textual_key(event.key)
        if translation is None:
            self.set_status(f"live input ignores {event.key!r} · esc stops live input")
            return True
        # Forward to tmux without blocking the UI thread. ``send_keys``
        # is a synchronous tmux subprocess call (~30-100 ms on slower
        # shells); waiting for it inline made every keystroke feel
        # laggy. The asyncio.Lock guarantees keystrokes still arrive
        # at tmux in the order the operator typed them.
        task = asyncio.create_task(self._send_translation_async(translation))
        self._send_tasks.add(task)
        task.add_done_callback(self._send_tasks.discard)
        return True

    async def _send_translation_async(self, translation: KeyTranslation) -> None:
        adapter = self._adapter
        if adapter is None:
            return
        async with self._send_lock:
            try:
                await asyncio.to_thread(adapter.send_keys, self._pane_id, translation)
            except (TmuxCommandError, OSError):
                _log.exception("compose: send_keys failed for pane %s", self._pane_id)

    async def _wait_for_pending_sends(self) -> None:
        """Block until all queued keystroke forwards have completed.

        Test helper. Production code never needs to wait — fire-and-
        forget is the whole point of moving the call off the event
        loop.
        """
        pending = tuple(self._send_tasks)
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    def _swap_focus(self, *, forward: bool) -> None:
        del forward  # two-widget cycle — direction doesn't matter
        if not self._show_editor:
            self.query_one(LivePaneViewer).focus()
            return
        editor = self.query_one("#compose-editor", TextArea)
        mirror = self.query_one(LivePaneViewer)
        if editor.has_focus:
            mirror.focus()
        else:
            editor.focus()

    # ── actions ──────────────────────────────────────────────────────

    def action_send(self) -> None:
        if not self._show_editor:
            self.set_status("live viewer only · this screen does not compose messages")
            return
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

    def action_grow_editor(self) -> None:
        if not self._show_editor:
            return
        self._set_editor_height(self._editor_height + _EDITOR_HEIGHT_STEP)

    def action_shrink_editor(self) -> None:
        if not self._show_editor:
            return
        self._set_editor_height(self._editor_height - _EDITOR_HEIGHT_STEP)

    def action_toggle_wrap(self) -> None:
        self.muxdeck_app.action_toggle_log_wrap()

    def action_toggle_follow(self) -> None:
        viewer = self.query_one(LivePaneViewer)
        viewer.set_follow_mode(not viewer.follow_enabled)
        self._refresh_guidance(update_status=False)
        self.set_status(f"live follow {viewer.follow_state}")

    def action_close(self) -> None:
        self._teardown_pipe()
        self.app.pop_screen()

    # ── helpers ──────────────────────────────────────────────────────

    def _set_editor_height(self, next_height: int) -> None:
        clamped = max(_MIN_EDITOR_HEIGHT, min(_MAX_EDITOR_HEIGHT, next_height))
        if clamped == self._editor_height:
            return
        self._editor_height = clamped
        self._apply_editor_height()
        self._refresh_guidance()

    def _apply_editor_height(self) -> None:
        if not self._show_editor:
            return
        editor_wrap = self.query_one("#compose-editor-wrap", Vertical)
        editor_wrap.styles.height = self._editor_height

    def _set_mirror_input_mode(self, enabled: bool) -> None:
        if self._mirror_input_active == enabled:
            return
        self._mirror_input_active = enabled
        if enabled:
            self.query_one(LivePaneViewer).focus()
        self._refresh_guidance()

    def _refresh_guidance(self, *, update_status: bool = True) -> None:
        if not self.is_mounted:
            return
        viewer = self.query_one(LivePaneViewer)
        if self._show_editor:
            label = self.query_one("#compose-editor-label", Label)
            label.update(self._editor_label(viewer))
        if self._mirror_input_active and viewer.has_focus:
            viewer.add_class("-input-on")
        else:
            viewer.remove_class("-input-on")
        viewer.border_subtitle = self._viewer_subtitle(viewer)
        if update_status:
            self.set_status(self._status_message(viewer))

    def _editor_label(self, viewer: LivePaneViewer) -> str:
        if not self._show_editor:
            return "live pane viewer"
        if self._mirror_input_active and viewer.has_focus:
            return (
                f"live input on · keys go to tmux · esc stop · editor {self._editor_height} lines"
            )
        editor = self.query_one("#compose-editor", TextArea)
        if editor.has_focus:
            return (
                f"compose ({self._editor_height} lines) · ctrl+s send · "
                "tab mirror · i on mirror to interact"
            )
        return (
            f"mirror focus · i interact · tab editor · alt+up/down resize · "
            f"editor {self._editor_height} lines"
        )

    def _viewer_subtitle(self, viewer: LivePaneViewer) -> str:
        wrap_state = "wrap on" if viewer.wrap_enabled else "wrap off"
        follow_state = f"follow {viewer.follow_state}"
        if self._mirror_input_active and viewer.has_focus:
            return f"live input · {follow_state} · {wrap_state} · esc stops"
        if self._capture_error is not None:
            return "capture failed"
        if self._sync_warning is not None:
            return f"snapshot sync warning · {follow_state} · {wrap_state} · r retry"
        if self._stream_warning is not None:
            return f"snapshot sync only · {follow_state} · {wrap_state} · r resync"
        if not viewer.has_content:
            return f"waiting for pane output · {follow_state} · {wrap_state}"
        if not self._show_editor:
            return f"live mirror · {follow_state} · {wrap_state} · i interact · r resync"
        return f"live mirror + snapshot sync · {follow_state} · {wrap_state} · r resync"

    def _status_message(self, viewer: LivePaneViewer) -> str:
        wrap_state = "wrap on" if viewer.wrap_enabled else "wrap off"
        follow_state = f"follow {viewer.follow_state}"
        if self._capture_error is not None:
            return self._capture_error
        if self._mirror_input_active and viewer.has_focus:
            guidance = (
                f"live input → {self._display_name} ({self._pane_id}) · "
                f"{follow_state} · {wrap_state} · keys go to tmux · esc stops"
            )
        elif not self._show_editor:
            guidance = (
                f"live pane → {self._display_name} ({self._pane_id}) · "
                f"{follow_state} · {wrap_state} · scroll freely · i interact"
            )
        elif viewer.has_focus:
            guidance = (
                f"mirror → {self._display_name} ({self._pane_id}) · "
                f"{follow_state} · {wrap_state} · scroll freely · i interact · tab editor"
            )
        else:
            guidance = (
                f"compose → {self._display_name} ({self._pane_id}) · "
                f"{follow_state} · {wrap_state} · ctrl+s send · tab focus · "
                "alt+up/down resize · i interact"
            )
        warning = self._sync_warning or self._stream_warning
        if warning is not None:
            return f"{warning} · {guidance}"
        if not viewer.has_content:
            return f"{guidance} · waiting for pane output"
        return guidance

    def _teardown_pipe(self) -> None:
        if self._adapter is not None and self._pipe_started:
            self._pipe_started = False
            try:
                self._adapter.stop_pipe(self._pane_id)
            except TmuxCommandError:
                pass
            except OSError:
                pass
        with contextlib.suppress(FileNotFoundError, OSError):
            self._ring_path.unlink()

    def _format_mirror_title(self) -> str:
        prefix = "mirror" if self._show_editor else "live pane"
        return f"{prefix} · pane {self._pane_id} — {self._display_name}"


__all__ = [
    "COMPOSE_MIRROR_BINDINGS",
    "COMPOSE_MIRROR_HINTS",
    "ComposeWithMirrorScreen",
]

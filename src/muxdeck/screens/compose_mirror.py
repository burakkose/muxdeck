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
_POLL_INTERVAL_SEC = 0.05
# Snapshot resync runs at two cadences: ``_SNAPSHOT_SYNC_INTERVAL_SEC``
# is the steady-state interval when the operator is just watching the
# pane, and ``_SNAPSHOT_INTERACT_INTERVAL_SEC`` is the faster cadence
# during interact mode where the snapshot is the only source of
# truth for TUI programs (Copilot CLI, vim, less, …) that redraw
# their input area via ANSI cursor positioning rather than line-
# oriented stdout. Without the faster tick the operator would see
# nothing in the mirror until they pressed ``esc`` and triggered the
# steady-state snapshot — which is exactly the regression operators
# reported after the previous "skip snapshot during interact" fix.
_SNAPSHOT_SYNC_INTERVAL_SEC = 1.0
_SNAPSHOT_INTERACT_INTERVAL_SEC = 0.4
_SNAPSHOT_WORKER_GROUP = "compose-snapshot"
# Maximum time to wait for additional keystrokes after the first
# arrives before flushing them to tmux. Coalescing keystrokes into a
# single ``send-keys`` call (which itself can take 30-100 ms on slower
# shells) cuts subprocess fan-out by 10x or more during a typing
# burst, which is the difference between "instant" and "type, wait
# two seconds, watch every char arrive late" in interact mode.
_KEYSTROKE_COALESCE_SEC = 0.02
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
        # Inbound keystroke queue + a single long-lived dispatcher
        # task that drains it. Per-keystroke ``asyncio.create_task``
        # +``asyncio.to_thread`` was forking one tmux subprocess per
        # character (~30-100 ms each, serialized via a lock); typing
        # 20 chars in a second built up 1-2 seconds of backlog and
        # made the live mirror feel "type, wait, see chars trickle
        # in". The dispatcher coalesces every keystroke that arrives
        # within ``_KEYSTROKE_COALESCE_SEC`` of the previous one into
        # a single ``send-keys`` invocation, cutting subprocess
        # overhead by ~10x for a typing burst.
        self._send_queue: asyncio.Queue[KeyTranslation] = asyncio.Queue()
        self._dispatcher_task: asyncio.Task[None] | None = None
        # ``True`` while a coalesced batch is mid-flush in the worker
        # thread. The test helper waits on both the queue and this
        # flag so it doesn't return between "queue drained" and
        # "subprocess actually finished".
        self._flush_in_flight = False
        # ``True`` while a periodic snapshot worker is in flight; we
        # skip subsequent ticks rather than queueing them so a slow
        # tmux subprocess can never accumulate a backlog of pending
        # captures and starve the worker pool.
        self._snapshot_in_flight = False
        # Monotonic timestamp of the last snapshot dispatch. The
        # snapshot tick is wall-clock-throttled rather than
        # ``set_interval``-throttled so we can change the cadence
        # dynamically when the operator enters/exits interact mode
        # without tearing down a Textual interval.
        self._last_snapshot_tick = 0.0

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
            # Start the keystroke dispatcher after seeding so the
            # adapter and pane id are guaranteed to be live.
            self._dispatcher_task = asyncio.create_task(self._run_send_dispatcher())
        if self._show_editor:
            # Land focus in the editor so typing just works. The mirror can
            # be reached with tab.
            self.query_one("#compose-editor", TextArea).focus()
        else:
            viewer.focus()
        self._refresh_guidance()

    def on_unmount(self) -> None:
        self._teardown_pipe()
        if self._dispatcher_task is not None:
            self._dispatcher_task.cancel()
            self._dispatcher_task = None

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
        #
        # The Textual interval fires at the *interact* cadence so the
        # tick has a chance to dispatch a snapshot whenever the
        # operator is typing. The wall-clock throttle inside
        # ``_tick_snapshot_in_background`` falls back to the slower
        # steady-state cadence when the operator is just watching, so
        # subprocess overhead stays at ~1 capture/sec outside interact.
        self.set_interval(
            _SNAPSHOT_INTERACT_INTERVAL_SEC,
            self._tick_snapshot_in_background,
        )

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

        During interact mode we throttle to the faster interval so the
        operator sees their typed characters within ~400 ms even when
        the underlying program (Copilot CLI, vim, …) re-renders its
        input via cursor positioning rather than line-oriented stdout
        the line-buffered pipe-pane stream can decode.
        """
        if self._adapter is None:
            return
        if self._snapshot_in_flight:
            # Another tick is still mid-capture. Don't queue another
            # — let it complete; the next periodic tick will pick up
            # any drift. Queueing would let a slow capture starve
            # the worker pool indefinitely on a busy system.
            return
        loop = asyncio.get_event_loop()
        now = loop.time()
        # Steady-state cadence stays at 1 s to keep tmux subprocess
        # overhead low when the operator is just watching the pane.
        # Interact mode tightens the cadence for fresh feedback.
        target_interval = (
            _SNAPSHOT_INTERACT_INTERVAL_SEC
            if self._mirror_input_active
            else _SNAPSHOT_SYNC_INTERVAL_SEC
        )
        if (now - self._last_snapshot_tick) < target_interval:
            return
        self._last_snapshot_tick = now
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

        During interact mode we deliberately use ``set_snapshot``
        (full replace, ~50 lines) rather than ``replace_tail`` (which
        forces a 2000-line ``_rerender_from_buffer`` on the UI
        thread). The operator is typing, not scrolling history; they
        need a fresh, hitch-free view of the pane every ~400 ms. The
        steady-state path keeps ``replace_tail`` so non-interact
        viewers preserve their pipe-pane scrollback.
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
            elif self._mirror_input_active and snapshot:
                # Cheap full-replace during interact: avoids the
                # ~2000-line decode + write rerender that
                # ``replace_tail`` triggers when the streamed tail
                # disagrees with the snapshot (which happens almost
                # every tick while the operator types).
                viewer.set_snapshot(snapshot)
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
        # Hand off to the dispatcher coroutine. ``put_nowait`` on an
        # unbounded asyncio.Queue is a sub-microsecond memory append,
        # so the keystroke handler returns immediately and Textual
        # never blocks waiting for tmux. The dispatcher coalesces
        # bursts into a single ``send-keys`` subprocess call.
        self._send_queue.put_nowait(translation)
        return True

    async def _run_send_dispatcher(self) -> None:
        """Drain the keystroke queue, batching bursts into one send-keys call.

        Per-keystroke ``asyncio.to_thread(send_keys, …)`` was forking a
        tmux subprocess (~30-100 ms) per character and serializing
        them through an ``asyncio.Lock``. A typing burst of 20 chars
        could pile up 1-2 seconds of subprocess work, which the
        operator perceived as "type, wait, watch chars trickle in
        late". We instead pull from the queue, wait up to
        ``_KEYSTROKE_COALESCE_SEC`` for adjacent keystrokes, and
        flush the run to tmux as a single batched call when the
        translation kind (literal vs symbolic) is compatible.
        """
        loop = asyncio.get_running_loop()
        try:
            while True:
                first = await self._send_queue.get()
                # Mark a flush as pending the moment the dispatcher
                # commits to a batch. This keeps the test helper
                # ``_wait_for_pending_sends`` from racing past the
                # coalesce window where the queue is empty but the
                # batch hasn't been handed to tmux yet.
                self._flush_in_flight = True
                batch: list[KeyTranslation] = [first]
                deadline = loop.time() + _KEYSTROKE_COALESCE_SEC
                while True:
                    timeout = deadline - loop.time()
                    if timeout <= 0:
                        break
                    try:
                        more = await asyncio.wait_for(self._send_queue.get(), timeout)
                    except TimeoutError:
                        break
                    batch.append(more)
                await self._flush_batch(batch)
        except asyncio.CancelledError:
            # Drain remaining keystrokes on screen close so the agent
            # doesn't lose what the operator typed during teardown.
            remaining: list[KeyTranslation] = []
            while not self._send_queue.empty():
                try:
                    remaining.append(self._send_queue.get_nowait())
                except asyncio.QueueEmpty:
                    break
            if remaining:
                with contextlib.suppress(Exception):
                    await self._flush_batch(remaining)
            raise

    async def _flush_batch(self, batch: list[KeyTranslation]) -> None:
        """Coalesce a contiguous run into the fewest possible send_keys calls."""
        adapter = self._adapter
        if adapter is None or not batch:
            self._flush_in_flight = False
            return
        # Group consecutive entries that share the same ``literal``
        # flag: tmux ``send-keys -l`` is mode-locked per invocation,
        # so a literal run and a symbolic run can't ride the same
        # subprocess. Within a group, every key tuple is concatenated
        # into one positional arg list.
        groups = self._group_by_literal(batch)
        try:
            for literal, keys in groups:
                try:
                    await asyncio.to_thread(adapter.send_keys_raw, self._pane_id, keys, literal)
                except (TmuxCommandError, OSError):
                    _log.exception("compose: send_keys failed for pane %s", self._pane_id)
        finally:
            self._flush_in_flight = False

    @staticmethod
    def _group_by_literal(
        batch: list[KeyTranslation],
    ) -> list[tuple[bool, tuple[str, ...]]]:
        groups: list[tuple[bool, tuple[str, ...]]] = []
        current_literal: bool | None = None
        current_keys: list[str] = []
        for translation in batch:
            if not translation.keys:
                continue
            if current_literal is None:
                current_literal = translation.literal
            if translation.literal != current_literal:
                groups.append((current_literal, tuple(current_keys)))
                current_literal = translation.literal
                current_keys = []
            current_keys.extend(translation.keys)
        if current_literal is not None and current_keys:
            groups.append((current_literal, tuple(current_keys)))
        return groups

    async def _wait_for_pending_sends(self) -> None:
        """Block until the keystroke queue is fully drained and flushed.

        Test helper. Production code never needs to wait — the
        fire-and-forget dispatcher is the whole point of moving the
        call off the event loop.
        """
        # Poll until both the queue is empty *and* no flush is mid-
        # subprocess. Bound at ~5 seconds to keep a wedged dispatcher
        # from hanging the test runner forever.
        for _ in range(1000):
            if self._send_queue.empty() and not self._flush_in_flight:
                # One more cooperative yield so any final ``to_thread``
                # callback can land on the test event loop.
                await asyncio.sleep(0)
                if self._send_queue.empty() and not self._flush_in_flight:
                    return
            await asyncio.sleep(0.005)

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
            # Reset the snapshot throttle so the operator gets a
            # fresh capture on the very next interval tick rather
            # than waiting up to a full ``_SNAPSHOT_SYNC_INTERVAL_SEC``
            # window if the last snapshot just fired.
            self._last_snapshot_tick = 0.0
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

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, cast

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.css.query import NoMatches
from textual.timer import Timer
from textual.worker import Worker, WorkerState

from muxdeck.bindings import WORKTREE_BINDINGS, WORKTREE_HINTS
from muxdeck.controllers import (
    WorktreeActionView,
    WorktreeDetailView,
    WorktreeStartAgentIntent,
    WorktreeSummaryView,
)
from muxdeck.exceptions import DomainValidationError, PersistenceError
from muxdeck.screens.base import ShellScreen
from muxdeck.screens.confirm_dialog import ConfirmScreen
from muxdeck.screens.worktree_input import (
    AttachWorktreeResult,
    AttachWorktreeScreen,
    CreateWorktreeResult,
    CreateWorktreeScreen,
    LaunchAgentResult,
    LaunchAgentScreen,
)
from muxdeck.services.action_service import ActionModelHint
from muxdeck.widgets.worktrees import (
    ConflictPanel,
    StartIntentPanel,
    WorktreeDetailPanel,
    WorktreeListPanel,
)

if TYPE_CHECKING:
    from muxdeck.app import MuxdeckApp, MuxdeckRuntime


_log = logging.getLogger(__name__)

_WORKER_NAME = "worktrees_load"
_DETAIL_WORKER_NAME = "worktrees_detail"


def _build_delete_worktree_message(
    summary: WorktreeSummaryView,
    selected_worktree_id: str | None,
) -> str:
    """Compose the delete-confirm dialog message.

    Long worktree paths used to wrap mid-segment in the narrow dialog,
    making the visible truncation look like the real path. Lead with the
    recognizable basename and branch so the user can identify the target
    at a glance, then surface the full path on a labeled second line.
    """

    raw_path = summary.path or ""
    basename = Path(raw_path).name if raw_path else ""
    label = basename or raw_path or selected_worktree_id or "unknown"
    branch_label = summary.branch or "unknown"
    full_path = raw_path or selected_worktree_id or label
    return f"Delete worktree '{label}' (branch: {branch_label})?\nFull path: {full_path}"


@dataclass(frozen=True, slots=True)
class _LoadedWorktreesState:
    worktrees: tuple[WorktreeSummaryView, ...]
    detail: WorktreeDetailView | None
    start_intent: WorktreeStartAgentIntent | None
    selected_worktree_id: str | None
    # When detail/intent enrichment fails for the selected worktree we
    # still want to render the list. The fatal error is surfaced via
    # this field so the user sees a status hint instead of an empty
    # screen and a swallowed exception.
    warning_message: str | None = None


class WorktreesScreen(ShellScreen):
    SCREEN_TITLE = "WORKTREES"
    BINDINGS = WORKTREE_BINDINGS
    FOOTER_HINTS = WORKTREE_HINTS

    def __init__(self, runtime: MuxdeckRuntime) -> None:
        super().__init__(runtime)
        self._worktrees: tuple[WorktreeSummaryView, ...] = ()
        self._selected_worktree_id: str | None = None
        self._detail: WorktreeDetailView | None = None
        self._start_intent: WorktreeStartAgentIntent | None = None
        self._detail_timer: Timer | None = None
        self._loading: bool = False
        self._refresh_pending: bool = False
        # Suppress the redundant ``on_show`` that fires immediately
        # after ``on_mount`` on first activation.
        self._skip_next_show_refresh: bool = True
        self._loaded_once: bool = False
        # Status message to display once the next refresh worker
        # finishes — used by post-action callbacks (create/prune/etc.)
        # so the user-visible "✓ created …" survives the async reload
        # that would otherwise overwrite it with "X worktrees loaded".
        self._pending_status_after_refresh: str | None = None

    def compose_body(self) -> ComposeResult:
        with Vertical(id="worktrees-root"):  # noqa: SIM117
            with Horizontal(id="worktrees-main", classes="frame"):
                yield WorktreeListPanel(widget_id="worktrees-list", classes="divider-right")
                with Vertical(id="worktrees-sidebar"):
                    yield WorktreeDetailPanel(id="worktrees-detail", classes="section")
                    yield ConflictPanel(id="worktrees-conflicts", classes="section-top")
                    yield StartIntentPanel(id="worktrees-intent", classes="section-top")

    def on_mount(self) -> None:
        self.refresh_data()
        self.call_after_refresh(self.query_one(WorktreeListPanel).focus_list)

    def on_show(self) -> None:
        if self._skip_next_show_refresh:
            self._skip_next_show_refresh = False
            return
        self.refresh_data()

    def refresh_data(self) -> None:
        """Kick off a background worker; UI thread is never blocked.

        ``WorktreeService.list_worktrees`` and ``get_worktree_detail``
        each shell out to ``git`` and read on-disk worktree metadata.
        On WSL with worktrees on ``/mnt/c`` that's seconds per call,
        which used to freeze the tab on every switch. The work now
        happens in a worker thread; the UI keeps showing the previous
        snapshot (or a Textual ``loading`` overlay on first load) until
        the worker returns.
        """
        if self._loading:
            # Coalesce overlapping refresh requests — thread workers
            # can't be force-cancelled mid-blocking-IO.
            self._refresh_pending = True
            return

        # Snapshot inputs so the worker doesn't read mutable state.
        # Use the thread-safe sync controller when available — the
        # foreground SQLite connection is bound to the UI thread.
        worktree_service = getattr(self.runtime, "sync_worktrees", None) or self.runtime.worktrees
        selected_id = self._selected_worktree_id
        model_hint: ActionModelHint = self._launch_model_hint()
        configured_model = model_hint.configured_model

        first_load = not self._loaded_once
        if first_load:
            self.set_status("loading worktrees…")
            self.begin_loading(*self._loading_widgets())

        def _load() -> _LoadedWorktreesState:
            worktrees = tuple(worktree_service.list_worktrees())
            effective_selected = selected_id
            ids = {w.worktree_id for w in worktrees}
            if effective_selected is None or effective_selected not in ids:
                effective_selected = worktrees[0].worktree_id if worktrees else None
            detail: WorktreeDetailView | None = None
            start_intent: WorktreeStartAgentIntent | None = None
            warning: str | None = None
            if effective_selected is not None:
                # Detail and intent enrichment shell out to git and read
                # on-disk worktree metadata. They can fail for many
                # reasons that should NOT take down the whole screen:
                # the row was deleted mid-flight, the repo path is
                # offline (WSL/Windows mount), git refuses to operate,
                # and so on. We catch broadly here precisely because
                # this is the worker boundary: if we let the exception
                # escape, the worker errors out and the user sees an
                # empty list with no recourse. The full exception is
                # logged so the swallowed error is debuggable.
                try:
                    detail = worktree_service.get_worktree_detail(effective_selected)
                except Exception as exc:
                    _log.exception(
                        "worktree detail load failed for %s",
                        effective_selected,
                    )
                    warning = f"worktree detail unavailable: {str(exc).splitlines()[0]}"
                if detail is not None:
                    try:
                        start_intent = worktree_service.start_agent_intent(
                            effective_selected,
                            model=configured_model,
                        )
                    except Exception as exc:
                        _log.exception(
                            "worktree start_agent_intent failed for %s",
                            effective_selected,
                        )
                        if warning is None:
                            warning = f"start intent unavailable: {str(exc).splitlines()[0]}"
            return _LoadedWorktreesState(
                worktrees=worktrees,
                detail=detail,
                start_intent=start_intent,
                selected_worktree_id=effective_selected,
                warning_message=warning,
            )

        self._loading = True
        self.run_worker(_load, thread=True, exclusive=True, name=_WORKER_NAME)

    def _schedule_pending_refresh(self) -> None:
        if not self._refresh_pending:
            return
        self._refresh_pending = False
        if self.is_mounted:
            self.call_after_refresh(self.refresh_data)

    def _loading_widgets(self) -> tuple[object, ...]:
        return (
            self.query_one(WorktreeListPanel),
            self.query_one(WorktreeDetailPanel),
            self.query_one(ConflictPanel),
            self.query_one(StartIntentPanel),
        )

    def _apply_loaded_state(self, state: _LoadedWorktreesState) -> None:
        self._worktrees = state.worktrees
        self._selected_worktree_id = state.selected_worktree_id
        self._detail = state.detail
        self._start_intent = state.start_intent
        if self._selected_worktree_id is not None:
            self.muxdeck_app.remember_worktree_selection(self._selected_worktree_id)
        self.query_one(WorktreeListPanel).set_worktrees(
            self._worktrees,
            selected_worktree_id=self._selected_worktree_id,
            notify=False,
        )
        self.query_one(WorktreeDetailPanel).set_detail(self._detail)
        self.query_one(ConflictPanel).set_conflicts(
            () if self._detail is None else self._detail.conflicts
        )
        self.query_one(StartIntentPanel).set_intent(self._start_intent)
        if self._pending_status_after_refresh is not None:
            self.set_status(self._pending_status_after_refresh)
            self._pending_status_after_refresh = None
            return
        if state.warning_message is not None:
            self.set_status(state.warning_message)
            return
        self.set_status(
            "no worktrees discovered"
            if not self._worktrees
            else f"{len(self._worktrees)} worktrees loaded"
        )

    def set_status(self, message: str) -> None:
        # Action callbacks frequently set a status right before/after a
        # background refresh. Without this guard the refresh worker's
        # default "X worktrees loaded" message would clobber the
        # action result. Stash the message so ``_apply_loaded_state``
        # can reapply it after the worker finishes.
        if self._loading and not message.startswith("loading worktrees"):
            self._pending_status_after_refresh = message
        super().set_status(message)

    @property
    def muxdeck_app(self) -> MuxdeckApp:
        return cast("MuxdeckApp", self.app)

    def on_worktree_list_panel_worktree_selected(
        self,
        message: WorktreeListPanel.WorktreeSelected,
    ) -> None:
        if message.worktree_id == self._selected_worktree_id:
            return
        self._selected_worktree_id = message.worktree_id
        # Debounce: cancel pending detail load so rapid j/k doesn't
        # stack blocking calls while the user holds arrow keys.
        if self._detail_timer is not None:
            self._detail_timer.stop()
        self._detail_timer = self.set_timer(0.05, self._update_selected_detail)

    def _update_selected_detail(self) -> None:
        """Refresh the detail/conflict/intent panels for the current selection.

        Runs in a worker because ``get_worktree_detail`` and
        ``start_agent_intent`` shell out to ``git`` and can take
        seconds on slow filesystems. Holding arrow keys would
        otherwise queue blocking calls behind every keypress.
        """
        if self._selected_worktree_id is None:
            self._detail = None
            self._start_intent = None
            self.query_one(WorktreeDetailPanel).set_detail(None)
            self.query_one(ConflictPanel).set_conflicts(())
            self.query_one(StartIntentPanel).set_intent(None)
            return
        worktree_service = getattr(self.runtime, "sync_worktrees", None) or self.runtime.worktrees
        target_id = self._selected_worktree_id
        configured_model = self._launch_model_hint().configured_model
        self.muxdeck_app.remember_worktree_selection(target_id)

        def _load() -> tuple[str, WorktreeDetailView | None, WorktreeStartAgentIntent | None]:
            # Detail and intent calls shell out to git and may fail for
            # transient reasons unrelated to the screen (deleted row,
            # offline mount, git refusing). We MUST keep the worker
            # alive so the screen doesn't crash on selection change;
            # the stale-result guard below will discard the empty
            # payload if the user has already moved on. Exceptions
            # are logged so the swallow is debuggable.
            try:
                detail = worktree_service.get_worktree_detail(target_id)
            except PersistenceError:
                # Most common race: worktree was deleted mid-flight.
                # Quiet path; not worth a stack trace.
                return (target_id, None, None)
            except Exception:
                _log.exception("worktree detail load failed for %s", target_id)
                return (target_id, None, None)
            try:
                start_intent = worktree_service.start_agent_intent(
                    target_id, model=configured_model
                )
            except PersistenceError:
                start_intent = None
            except Exception:
                _log.exception("worktree start_agent_intent failed for %s", target_id)
                start_intent = None
            return (target_id, detail, start_intent)

        self.run_worker(_load, thread=True, exclusive=True, name=_DETAIL_WORKER_NAME)

    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        super().on_worker_state_changed(event)
        name = event.worker.name
        if name == _DETAIL_WORKER_NAME:
            if event.state != WorkerState.SUCCESS:
                return
            payload = event.worker.result
            if payload is None:
                return
            target_id, detail, start_intent = payload
            # Drop stale results: the user may have moved on to a
            # different selection while this worker was in flight.
            if target_id != self._selected_worktree_id:
                return
            self._detail = detail
            self._start_intent = start_intent
            self.query_one(WorktreeDetailPanel).set_detail(detail)
            self.query_one(ConflictPanel).set_conflicts(() if detail is None else detail.conflicts)
            self.query_one(StartIntentPanel).set_intent(start_intent)
            return
        if name != _WORKER_NAME:
            return
        if event.state == WorkerState.ERROR:
            self._loading = False
            self.end_loading(*self._loading_widgets())
            self.set_status("worktree load failed")
            self._schedule_pending_refresh()
            return
        if event.state == WorkerState.CANCELLED:
            self._loading = False
            self.end_loading(*self._loading_widgets())
            self._schedule_pending_refresh()
            return
        if event.state != WorkerState.SUCCESS:
            return
        self._loading = False
        result = event.worker.result
        self.end_loading(*self._loading_widgets())
        if result is None:
            self._schedule_pending_refresh()
            return
        self._apply_loaded_state(result)
        self._loaded_once = True
        self._schedule_pending_refresh()

    def action_cursor_down(self) -> None:
        self.query_one(WorktreeListPanel).move_cursor(1)

    def action_cursor_up(self) -> None:
        self.query_one(WorktreeListPanel).move_cursor(-1)

    def action_launch_agent(self) -> None:
        if self._selected_worktree_id is None:
            self.set_status("no worktree selected")
            return
        model_hint = self._launch_model_hint()
        intent = self._start_intent or self.runtime.worktrees.start_agent_intent(
            self._selected_worktree_id,
            model=model_hint.configured_model,
        )
        self.app.push_screen(
            LaunchAgentScreen(
                self.runtime.worktrees,
                intent=intent,
                model_hint=model_hint,
            ),
            callback=self._on_launch_agent_result,
        )

    def action_execute_start(self) -> None:
        """Backward-compatible alias for the launch modal."""
        self.action_launch_agent()

    def action_preview_start_agent(self) -> None:
        """Backward-compatible alias for older launch bindings."""
        self.action_launch_agent()

    def _on_launch_agent_result(self, result: LaunchAgentResult | None) -> None:
        if result is None:
            return
        self._selected_worktree_id = result.selected_worktree_id
        self.refresh_data()
        if not result.confirmed:
            self.set_status("launch cancelled")
            return
        if self.runtime.actions is None:
            self.set_status("✗ action service unavailable")
            return
        intent = self.runtime.worktrees.start_agent_intent(
            result.selected_worktree_id,
            prompt=result.prompt or None,
            model=result.model,
            target_session_name=result.target_session_name,
            window_name=result.window_name,
        )
        action_result = self.runtime.actions.start_agent(
            cwd=Path(intent.worktree_path),
            model=intent.model,
            window_name=intent.suggested_window_name,
            target_session=intent.suggested_session_name,
            prompt=intent.prompt or None,
        )
        prefix = "✓" if action_result.success else "✗"
        self.set_status(f"{prefix} {action_result.message}")

    def _launch_model_hint(self) -> ActionModelHint:
        actions = self.runtime.actions
        if actions is None:
            return ActionModelHint()
        loader = getattr(actions, "launch_model_hint", None)
        if not callable(loader):
            return ActionModelHint()
        hint = loader()
        configured_model = getattr(hint, "configured_model", None)
        message = getattr(hint, "message", ActionModelHint().message)
        if configured_model is not None and not isinstance(configured_model, str):
            return ActionModelHint()
        if not isinstance(message, str):
            return ActionModelHint()
        return ActionModelHint(configured_model=configured_model, message=message)

    def action_create_worktree(self) -> None:
        if self._detail is None:
            self.set_status("no repo selected for create")
            return
        self.app.push_screen(
            CreateWorktreeScreen(repo_root=self._detail.summary.repo_root),
            callback=self._on_create_worktree_result,
        )

    def _on_create_worktree_result(self, result: CreateWorktreeResult | None) -> None:
        if result is None:
            self.set_status("create cancelled")
            return
        try:
            action_view = self.runtime.worktrees.create_worktree(
                result.repo_root,
                task_title=result.task_title,
            )
        except (DomainValidationError, PersistenceError) as exc:
            self.set_status(f"✗ create failed: {exc}")
            return
        self._refresh_after_worktree_action(action_view)

    def action_attach_worktree(self) -> None:
        self.app.push_screen(
            AttachWorktreeScreen(),
            callback=self._on_attach_worktree_result,
        )

    def _on_attach_worktree_result(self, result: AttachWorktreeResult | None) -> None:
        if result is None:
            self.set_status("select existing cancelled")
            return
        try:
            action_view = self.runtime.worktrees.attach_worktree(result.path)
        except (DomainValidationError, PersistenceError) as exc:
            self.set_status(f"✗ attach failed: {exc}")
            return
        self._refresh_after_worktree_action(action_view)

    def action_open_git_terminal(self) -> None:
        if self._detail is None:
            self.set_status("no worktree selected")
            return
        if self.runtime.actions is None:
            self.set_status("✗ action service unavailable")
            return
        branch_tail = self._detail.summary.branch.rsplit("/", 1)[-1]
        result = self.runtime.actions.open_terminal(
            cwd=Path(self._detail.summary.path),
            window_name=f"git-{branch_tail}",
        )
        prefix = "✓" if result.success else "✗"
        self.set_status(f"{prefix} {result.message}")

    def action_copy_details(self) -> None:
        selected_worktree_id = self.query_one(WorktreeListPanel).selected_worktree_id
        if selected_worktree_id is None:
            self.set_status("no worktree selected")
            return
        self._selected_worktree_id = selected_worktree_id
        self.muxdeck_app.remember_worktree_selection(selected_worktree_id)
        self._update_selected_detail()
        if self._detail is None:
            self.set_status("no worktree detail loaded")
            return
        self.copy_rendered_text(
            "worktree details",
            self.query_one(WorktreeDetailPanel),
            self.query_one(ConflictPanel),
            self.query_one(StartIntentPanel),
        )

    def action_delete_worktree(self) -> None:
        """Delete the selected worktree after confirmation."""
        if self._selected_worktree_id is None:
            self.set_status("no worktree selected")
            return
        if self._detail is None:
            self.set_status("no worktree detail loaded")
            return
        if self._detail.summary.is_main_worktree:
            self.set_status("✗ cannot delete the main worktree")
            return
        message = _build_delete_worktree_message(
            self._detail.summary,
            self._selected_worktree_id,
        )
        self.app.push_screen(
            ConfirmScreen(
                message=message,
                title="Delete Worktree",
            ),
            callback=self._on_delete_confirmed,
        )

    def _on_delete_confirmed(self, confirmed: bool | None) -> None:
        if not confirmed:
            self.set_status("delete cancelled")
            return
        if self._selected_worktree_id is None:
            return
        deleted_id = self._selected_worktree_id
        try:
            action_view = self.runtime.worktrees.remove_worktree(
                deleted_id,
                force=False,
            )
        except Exception as exc:
            self.set_status(f"✗ delete failed: {exc}")
            return
        self._drop_worktree_from_local_state(deleted_id)
        self._refresh_after_worktree_action(action_view)

    def _drop_worktree_from_local_state(self, worktree_id: str) -> None:
        """Repaint the list/detail without the deleted row before the worker reloads."""
        self._worktrees = tuple(item for item in self._worktrees if item.worktree_id != worktree_id)
        self._selected_worktree_id = None
        self._detail = None
        self._start_intent = None
        try:
            list_panel = self.query_one(WorktreeListPanel)
            detail_panel = self.query_one(WorktreeDetailPanel)
            conflicts_panel = self.query_one(ConflictPanel)
            intent_panel = self.query_one(StartIntentPanel)
        except NoMatches:
            return
        list_panel.set_worktrees(
            self._worktrees,
            selected_worktree_id=None,
            notify=False,
        )
        detail_panel.set_detail(None)
        conflicts_panel.set_conflicts(())
        intent_panel.set_intent(None)

    def action_prune_worktrees(self) -> None:
        """Prune stale worktrees after confirmation."""
        if not self._worktrees:
            self.set_status("no worktrees to prune")
            return
        # Use first worktree's repo root as the prune target
        first_detail = self._detail
        if first_detail is None:
            self.set_status("no worktree detail loaded")
            return
        self.app.push_screen(
            ConfirmScreen(
                message=(
                    "Prune all stale worktrees?"
                    " This removes worktrees whose directories no longer exist."
                ),
                title="Prune Worktrees",
            ),
            callback=self._on_prune_confirmed,
        )

    def _on_prune_confirmed(self, confirmed: bool | None) -> None:
        if not confirmed:
            self.set_status("prune cancelled")
            return
        if self._detail is None:
            return
        try:
            action_view = self.runtime.worktrees.prune_worktrees(
                self._detail.summary.repo_root,
                dry_run=False,
            )
        except Exception as exc:
            self.set_status(f"✗ prune failed: {exc}")
            return
        self._refresh_after_worktree_action(action_view)

    def _refresh_after_worktree_action(self, action_view: WorktreeActionView) -> None:
        self._start_intent = None
        if action_view.worktree is not None:
            self._selected_worktree_id = action_view.worktree.summary.worktree_id
        # Surface the action result both immediately (in case the
        # refresh worker is queued behind another) and after the
        # worker completes (so it isn't overwritten by the default
        # "X worktrees loaded" status the worker would otherwise
        # set on completion).
        message = f"✓ {action_view.message}"
        self._pending_status_after_refresh = message
        self.set_status(message)
        self.refresh_data()

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, cast

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical

from copilot_commander.bindings import WORKTREE_BINDINGS, WORKTREE_HINTS
from copilot_commander.controllers import (
    WorktreeActionView,
    WorktreeDetailView,
    WorktreeStartAgentIntent,
    WorktreeSummaryView,
)
from copilot_commander.exceptions import DomainValidationError, PersistenceError
from copilot_commander.screens.base import ShellScreen
from copilot_commander.screens.confirm_dialog import ConfirmScreen
from copilot_commander.screens.worktree_input import (
    AttachWorktreeResult,
    AttachWorktreeScreen,
    CreateWorktreeResult,
    CreateWorktreeScreen,
)
from copilot_commander.widgets.worktrees import (
    ConflictPanel,
    StartIntentPanel,
    WorktreeDetailPanel,
    WorktreeListPanel,
)

if TYPE_CHECKING:
    from copilot_commander.app import CommanderApp, CommanderRuntime


class WorktreesScreen(ShellScreen):
    SCREEN_TITLE = "WORKTREES"
    BINDINGS = WORKTREE_BINDINGS
    FOOTER_HINTS = WORKTREE_HINTS

    def __init__(self, runtime: CommanderRuntime) -> None:
        super().__init__(runtime)
        self._worktrees: tuple[WorktreeSummaryView, ...] = ()
        self._selected_worktree_id: str | None = None
        self._detail: WorktreeDetailView | None = None
        self._start_intent: WorktreeStartAgentIntent | None = None
        self._detail_timer: object | None = None

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
        self.refresh_data()

    def refresh_data(self) -> None:
        # The expensive bit used to be `WorktreeController._conflict_map`,
        # which ran `git worktree list --porcelain` per repo_root on every
        # refresh. The controller now skips conflict detection for the
        # list view (only the selected worktree computes conflicts in its
        # detail panel), so this call is back to a handful of cached
        # SQLite queries and safe to run on the UI thread.
        self._worktrees = self.runtime.worktrees.list_worktrees()
        if self._selected_worktree_id is None and self._worktrees:
            self._selected_worktree_id = self._worktrees[0].worktree_id
        if self._selected_worktree_id is not None and not any(
            worktree.worktree_id == self._selected_worktree_id for worktree in self._worktrees
        ):
            self._selected_worktree_id = self._worktrees[0].worktree_id if self._worktrees else None
        self._detail = None
        if self._selected_worktree_id is not None:
            self._detail = self.runtime.worktrees.get_worktree_detail(self._selected_worktree_id)
            self.commander_app.remember_worktree_selection(self._selected_worktree_id)
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
        self.set_status(
            "no worktrees discovered"
            if not self._worktrees
            else f"{len(self._worktrees)} worktrees loaded"
        )

    @property
    def commander_app(self) -> CommanderApp:
        return cast("CommanderApp", self.app)

    def on_worktree_list_panel_worktree_selected(
        self,
        message: WorktreeListPanel.WorktreeSelected,
    ) -> None:
        if message.worktree_id == self._selected_worktree_id:
            return
        self._selected_worktree_id = message.worktree_id
        self._start_intent = None
        # Debounce: cancel pending detail load so rapid j/k doesn't
        # stack blocking calls while the user holds arrow keys.
        if self._detail_timer is not None:
            self._detail_timer.stop()  # type: ignore[union-attr]
        self._detail_timer = self.set_timer(0.05, self._update_selected_detail)

    def _update_selected_detail(self) -> None:
        """Update only the detail/conflict/intent panels for the current selection."""
        self._detail = None
        if self._selected_worktree_id is not None:
            self._detail = self.runtime.worktrees.get_worktree_detail(self._selected_worktree_id)
            self.commander_app.remember_worktree_selection(self._selected_worktree_id)
        self.query_one(WorktreeDetailPanel).set_detail(self._detail)
        self.query_one(ConflictPanel).set_conflicts(
            () if self._detail is None else self._detail.conflicts
        )
        self.query_one(StartIntentPanel).set_intent(self._start_intent)

    def action_cursor_down(self) -> None:
        self.query_one(WorktreeListPanel).move_cursor(1)

    def action_cursor_up(self) -> None:
        self.query_one(WorktreeListPanel).move_cursor(-1)

    def action_preview_start_agent(self) -> None:
        if self._selected_worktree_id is None:
            self.set_status("no worktree selected")
            return
        self._start_intent = self.runtime.worktrees.start_agent_intent(
            self._selected_worktree_id,
        )
        self.query_one(StartIntentPanel).set_intent(self._start_intent)
        self.set_status(
            "start intent "
            f"{self._start_intent.suggested_session_name}/"
            f"{self._start_intent.suggested_window_name}"
        )

    def action_execute_start(self) -> None:
        """Execute the previewed start agent intent via tmux."""
        if self._start_intent is None:
            self.set_status("no start intent — press s first")
            return
        if self.runtime.actions is None:
            self.set_status("✗ action service unavailable")
            return
        intent = self._start_intent

        result = self.runtime.actions.start_agent(
            cwd=Path(intent.worktree_path),
            model=intent.model,
            window_name=intent.suggested_window_name,
        )
        if result.success:
            self.set_status(f"✓ {result.message}")
            self._start_intent = None
        else:
            self.set_status(f"✗ {result.message}")

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

    def action_delete_worktree(self) -> None:
        """Delete the selected worktree after confirmation."""
        if self._selected_worktree_id is None:
            self.set_status("no worktree selected")
            return
        if self._detail is None:
            self.set_status("no worktree detail loaded")
            return
        name = self._detail.summary.path or self._selected_worktree_id
        if self._detail.summary.is_main_worktree:
            self.set_status("✗ cannot delete the main worktree")
            return
        self.app.push_screen(
            ConfirmScreen(
                message=f"Delete worktree {name}?",
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
        try:
            action_view = self.runtime.worktrees.remove_worktree(
                self._selected_worktree_id,
                force=False,
            )
            self.set_status(f"✓ {action_view.message}")
        except Exception as exc:
            self.set_status(f"✗ delete failed: {exc}")
            return
        self._selected_worktree_id = None
        self.refresh_data()

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
            count = len(action_view.pruned_paths)
            self.set_status(f"✓ pruned {count} stale worktree(s)")
        except Exception as exc:
            self.set_status(f"✗ prune failed: {exc}")
            return
        self.refresh_data()

    def _refresh_after_worktree_action(self, action_view: WorktreeActionView) -> None:
        self._start_intent = None
        if action_view.worktree is not None:
            self._selected_worktree_id = action_view.worktree.summary.worktree_id
        self.refresh_data()
        self.set_status(f"✓ {action_view.message}")

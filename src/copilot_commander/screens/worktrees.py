from __future__ import annotations

from typing import TYPE_CHECKING, cast

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical

from copilot_commander.bindings import WORKTREE_BINDINGS, WORKTREE_HINTS
from copilot_commander.controllers import (
    WorktreeDetailView,
    WorktreeStartAgentIntent,
    WorktreeSummaryView,
)
from copilot_commander.screens.base import ShellScreen
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
        self.refresh_data()

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
            model="gpt-5.4",
        )
        self.query_one(StartIntentPanel).set_intent(self._start_intent)
        self.set_status(
            "start intent "
            f"{self._start_intent.suggested_session_name}/"
            f"{self._start_intent.suggested_window_name}"
        )

    def action_execute_start(self) -> None:
        """Execute the previewed start agent intent."""
        if self._start_intent is None:
            self.set_status("no start intent — press s first")
            return
        if self.runtime.actions is None:
            self.set_status("✗ action service unavailable")
            return
        intent = self._start_intent
        session = intent.suggested_session_name
        window = intent.suggested_window_name
        cwd = intent.worktree_path
        model_flag = f" --model {intent.model}" if intent.model else ""
        cmd = f"copilot{model_flag}"
        self.set_status(
            f"✓ launch intent ready: {cmd} in {cwd} (session={session} window={window})"
        )
        self._start_intent = None

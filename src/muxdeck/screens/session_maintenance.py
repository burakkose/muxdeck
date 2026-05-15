"""Bulk-delete cohort picker for stale Copilot CLI sessions."""

from __future__ import annotations

from typing import ClassVar

from textual import on
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Label, OptionList
from textual.widgets.option_list import Option

from muxdeck import theme
from muxdeck.bindings import BindingSpec
from muxdeck.controllers.sessions_controller import MaintenanceCohortsView


class SessionMaintenanceScreen(ModalScreen[int | None]):
    """Show age-bucket cohorts and dismiss with the chosen threshold.

    Dismisses with the selected ``older_than_days`` integer when the
    operator confirms a cohort, or ``None`` on cancel / when there is
    nothing to delete in any cohort.
    """

    DEFAULT_CSS = f"""
    SessionMaintenanceScreen {{
        align: center middle;
    }}

    #maintenance-dialog {{
        width: 70;
        height: auto;
        max-height: 22;
        background: {theme.BG1};
        border: thick {theme.BORDER};
        border-title-color: {theme.BORDER_FOCUS};
        padding: 1 2;
    }}

    #maintenance-header {{
        height: auto;
        margin-bottom: 1;
        color: {theme.FG2};
    }}

    #maintenance-options {{
        height: auto;
        max-height: 10;
        margin-bottom: 1;
        background: {theme.BG_HARD};
        border: tall {theme.BG3};
    }}

    #maintenance-options:focus {{
        border: tall {theme.BORDER_FOCUS};
    }}

    #maintenance-footer {{
        height: auto;
        color: {theme.FG3};
        margin-bottom: 1;
    }}

    #maintenance-buttons {{
        height: auto;
        align: right middle;
    }}

    #maintenance-buttons Button {{
        margin-left: 1;
        min-width: 12;
    }}
    """

    BINDINGS: ClassVar[list[BindingSpec]] = [
        ("escape", "cancel", "Cancel"),
        ("q", "cancel", "Cancel"),
        ("enter", "confirm_selected", "Select"),
    ]

    def __init__(self, view: MaintenanceCohortsView) -> None:
        super().__init__()
        self._view = view

    def compose(self) -> ComposeResult:
        with Vertical(id="maintenance-dialog") as dialog:
            dialog.border_title = "Session maintenance"
            yield Label(
                f"{self._view.total_eligible} session(s) eligible — pick a cohort to delete",
                id="maintenance-header",
            )
            yield OptionList(
                *self._build_options(),
                id="maintenance-options",
            )
            yield Label(self._footer_text(), id="maintenance-footer")
            with Horizontal(id="maintenance-buttons"):
                yield Button("Cancel", id="btn-cancel", variant="default")
                yield Button("Delete", id="btn-confirm", variant="error")

    def _build_options(self) -> list[Option]:
        if not self._view.cohorts:
            # Nothing to render but the OptionList still needs at least
            # one row so it can take focus / show context.
            return [Option("Nothing to clean up", id="none", disabled=True)]
        options: list[Option] = []
        for cohort in self._view.cohorts:
            label = f"{cohort.label}  ({cohort.count} session(s))"
            options.append(
                Option(
                    label,
                    id=str(cohort.older_than_days),
                    disabled=cohort.count == 0,
                )
            )
        return options

    def _footer_text(self) -> str:
        if self._view.skipped_live:
            return (
                f"{self._view.skipped_live} live session(s) are excluded automatically. "
                "Live sessions cannot be deleted while their pane is running."
            )
        return "Live sessions are automatically excluded."

    def on_mount(self) -> None:
        options = self.query_one(OptionList)
        # Pre-select the first cohort with content (counts are
        # monotonically non-increasing as the threshold grows, so the
        # broadest non-empty bucket is the first eligible row).
        for idx, cohort in enumerate(self._view.cohorts):
            if cohort.count > 0:
                options.highlighted = idx
                break
        options.focus()

    @on(Button.Pressed, "#btn-confirm")
    def _on_confirm(self) -> None:
        self._dismiss_selected()

    @on(Button.Pressed, "#btn-cancel")
    def _on_cancel(self) -> None:
        self.dismiss(None)

    @on(OptionList.OptionSelected, "#maintenance-options")
    def _on_option_selected(self, event: OptionList.OptionSelected) -> None:
        self._dismiss_with_option(event.option)

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_confirm_selected(self) -> None:
        self._dismiss_selected()

    def _dismiss_selected(self) -> None:
        options = self.query_one(OptionList)
        highlighted = options.highlighted
        if highlighted is None:
            self.dismiss(None)
            return
        try:
            option = options.get_option_at_index(highlighted)
        except IndexError:
            self.dismiss(None)
            return
        self._dismiss_with_option(option)

    def _dismiss_with_option(self, option: Option) -> None:
        option_id = option.id
        if option_id is None or option.disabled:
            self.dismiss(None)
            return
        try:
            days = int(option_id)
        except ValueError:
            self.dismiss(None)
            return
        self.dismiss(days)

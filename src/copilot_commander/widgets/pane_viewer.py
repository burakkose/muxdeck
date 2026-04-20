"""Widget for displaying captured tmux pane output."""

from __future__ import annotations

from rich.text import Text
from textual.widgets import Static

from copilot_commander import theme


class PaneOutputPanel(Static):
    """Displays captured tmux pane output with terminal-like styling.

    Call ``set_output()`` to update the displayed text and
    ``clear_output()`` to reset.  The widget is designed to sit in the
    dashboard sidebar and show a live tail of the selected agent's pane.
    """

    DEFAULT_CSS = f"""
    PaneOutputPanel {{
        height: 1fr;
        overflow-y: auto;
        padding: 0 1;
        background: {theme.BG_HARD};
        border: solid {theme.BORDER};
        border-title-color: {theme.PANEL_TITLE};
        border-title-style: bold;
    }}

    PaneOutputPanel:focus-within {{
        border: solid {theme.BORDER_FOCUS};
    }}
    """

    def __init__(
        self,
        *,
        name: str | None = None,
        widget_id: str | None = None,
        classes: str | None = None,
        disabled: bool = False,
    ) -> None:
        super().__init__(name=name, id=widget_id, classes=classes, disabled=disabled)
        self.border_title = "Agent Output"
        self._has_content: bool = False

    def set_output(self, text: str, *, pane_id: str = "") -> None:
        """Update the displayed pane output.

        Args:
            text: Raw captured pane output text.
            pane_id: Optional pane ID to show in the border title.
        """
        if pane_id:
            self.border_title = f"Agent Output [{pane_id}]"

        if not text.strip():
            self._show_placeholder("no output captured")
            return

        self._has_content = True
        styled = self._style_output(text)
        self.update(styled)

    def clear_output(self) -> None:
        """Reset to empty state."""
        self._has_content = False
        self.border_title = "Agent Output"
        self._show_placeholder("select an agent to view output")

    def _show_placeholder(self, message: str) -> None:
        """Show a dimmed placeholder message."""
        self._has_content = False
        placeholder = Text(message, style=f"italic {theme.FG4}")
        self.update(placeholder)

    def _style_output(self, raw: str) -> Text:
        """Apply terminal-like styling to captured output.

        Highlights:
        - Lines starting with ``$``, ``>`` or prompt markers in green (commands)
        - Lines containing ERROR/error keywords in red
        - Lines containing WARNING/warning keywords in yellow
        - Box-drawing characters in border color
        - Copilot markers in accent color
        - Everything else in default foreground
        """
        result = Text()
        lines = raw.splitlines()

        for i, line in enumerate(lines):
            if i > 0:
                result.append("\n")

            stripped = line.strip()

            if not stripped:
                result.append(line, style=theme.FG4)
            elif stripped.startswith(("$", ">", "\u276f")):
                result.append(line, style=f"bold {theme.GREEN}")
            elif any(kw in stripped.lower() for kw in ("error", "failed", "traceback")):
                result.append(line, style=theme.RED)
            elif any(kw in stripped.lower() for kw in ("warning", "warn")):
                result.append(line, style=theme.YELLOW)
            elif stripped.startswith(("─", "━", "╭", "╰", "│", "┃")):
                result.append(line, style=theme.BORDER)
            elif any(kw in stripped for kw in ("⏺", "✓", "✗", "●")):
                result.append(line, style=theme.BLUE)
            else:
                result.append(line, style=theme.FG)

        return result

    def on_mount(self) -> None:
        """Show placeholder on initial mount."""
        self._show_placeholder("select an agent to view output")


__all__ = ["PaneOutputPanel"]

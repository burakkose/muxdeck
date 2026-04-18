"""Gruvbox Dark palette for Copilot Commander TUI.

Centralized color constants so every widget and the TCSS stylesheet
reference a single source of truth.  Palette uses the Gruvbox Dark
color scheme — warm, high-contrast tones designed for readability.
"""

from __future__ import annotations

from typing import Final

# ── backgrounds (Gruvbox dark) ──────────────────────────────────────
BG_HARD: Final[str] = "#1d2021"
BG: Final[str] = "#282828"
BG1: Final[str] = "#3c3836"
BG2: Final[str] = "#504945"
BG3: Final[str] = "#665c54"
BG4: Final[str] = "#7c6f64"

# ── foregrounds (Gruvbox light tones) ───────────────────────────────
FG: Final[str] = "#ebdbb2"
FG1: Final[str] = "#d5c4a1"
FG2: Final[str] = "#bdae93"
FG3: Final[str] = "#a89984"
FG4: Final[str] = "#928374"

# ── bright accents (Gruvbox bright) ─────────────────────────────────
RED: Final[str] = "#fb4934"
GREEN: Final[str] = "#b8bb26"
YELLOW: Final[str] = "#fabd2f"
BLUE: Final[str] = "#83a598"
PURPLE: Final[str] = "#d3869b"
AQUA: Final[str] = "#8ec07c"
ORANGE: Final[str] = "#fe8019"

# ── neutral accents (Gruvbox neutral / dimmed) ──────────────────────
RED_DIM: Final[str] = "#cc241d"
GREEN_DIM: Final[str] = "#98971a"
YELLOW_DIM: Final[str] = "#d79921"
BLUE_DIM: Final[str] = "#458588"
PURPLE_DIM: Final[str] = "#b16286"
AQUA_DIM: Final[str] = "#689d6a"
ORANGE_DIM: Final[str] = "#d65d0e"

# ── semantic: status badges ──────────────────────────────────────────
STATUS_RUNNING: Final[str] = GREEN
STATUS_IDLE: Final[str] = YELLOW
STATUS_WAITING_INPUT: Final[str] = ORANGE
STATUS_BLOCKED: Final[str] = ORANGE_DIM
STATUS_ERROR: Final[str] = RED
STATUS_DEAD: Final[str] = YELLOW
STATUS_COMPLETED: Final[str] = FG4
STATUS_DISCOVERED: Final[str] = BLUE
STATUS_STARTING: Final[str] = AQUA
STATUS_UNKNOWN: Final[str] = FG3

# ── semantic: severity badges ────────────────────────────────────────
SEVERITY_INFO: Final[str] = BLUE
SEVERITY_WARNING: Final[str] = YELLOW
SEVERITY_ERROR: Final[str] = RED

# ── semantic: health tones ───────────────────────────────────────────
TONE_HEALTHY_BG: Final[str] = "#1e3522"
TONE_HEALTHY_FG: Final[str] = GREEN
TONE_WARNING_BG: Final[str] = "#3c2e10"
TONE_WARNING_FG: Final[str] = YELLOW
TONE_CRITICAL_BG: Final[str] = "#3c1f1f"
TONE_CRITICAL_FG: Final[str] = RED

# ── semantic: UI chrome ──────────────────────────────────────────────
BORDER: Final[str] = BG3
BORDER_FOCUS: Final[str] = AQUA
PANEL_BG: Final[str] = BG1
PANEL_TITLE: Final[str] = FG2
HEADER_BG: Final[str] = BG_HARD
FOOTER_BG: Final[str] = BG_HARD
BADGE_BG: Final[str] = BLUE_DIM
BADGE_FG: Final[str] = "#ebdbb2"
SELECTED_ROW_BG: Final[str] = "#3c3836"
ATTENTION_ROW_BG: Final[str] = "#3c2e10"
SCROLLBAR_BG: Final[str] = BG1
SCROLLBAR_FG: Final[str] = BG3

__all__ = [
    "AQUA",
    "AQUA_DIM",
    "ATTENTION_ROW_BG",
    "BADGE_BG",
    "BADGE_FG",
    "BG",
    "BG1",
    "BG2",
    "BG3",
    "BG4",
    "BG_HARD",
    "BLUE",
    "BLUE_DIM",
    "BORDER",
    "BORDER_FOCUS",
    "FG",
    "FG1",
    "FG2",
    "FG3",
    "FG4",
    "FOOTER_BG",
    "GREEN",
    "GREEN_DIM",
    "HEADER_BG",
    "ORANGE",
    "ORANGE_DIM",
    "PANEL_BG",
    "PANEL_TITLE",
    "PURPLE",
    "PURPLE_DIM",
    "RED",
    "RED_DIM",
    "SCROLLBAR_BG",
    "SCROLLBAR_FG",
    "SELECTED_ROW_BG",
    "SEVERITY_ERROR",
    "SEVERITY_INFO",
    "SEVERITY_WARNING",
    "STATUS_BLOCKED",
    "STATUS_COMPLETED",
    "STATUS_DEAD",
    "STATUS_DISCOVERED",
    "STATUS_ERROR",
    "STATUS_IDLE",
    "STATUS_RUNNING",
    "STATUS_STARTING",
    "STATUS_UNKNOWN",
    "STATUS_WAITING_INPUT",
    "TONE_CRITICAL_BG",
    "TONE_CRITICAL_FG",
    "TONE_HEALTHY_BG",
    "TONE_HEALTHY_FG",
    "TONE_WARNING_BG",
    "TONE_WARNING_FG",
    "YELLOW",
    "YELLOW_DIM",
]

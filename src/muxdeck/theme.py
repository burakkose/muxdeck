"""Ayu Mirage palette for Muxdeck TUI.

Centralized color constants so every widget and the TCSS stylesheet
reference a single source of truth. Core surfaces and accents come
from the official Ayu Mirage palette; a few mid-tone overlays are
kept as opaque approximations so the terminal UI stays readable and
deterministic without depending on alpha blending support.
"""

from __future__ import annotations

from typing import Final

# ── backgrounds (Ayu Mirage surfaces) ───────────────────────────────
BG_HARD: Final[str] = "#181C26"
BG: Final[str] = "#1F2430"
BG1: Final[str] = "#242936"
BG2: Final[str] = "#282E3B"
BG3: Final[str] = "#2F3744"
BG4: Final[str] = "#48515F"

# ── foregrounds (Ayu editor + UI tones) ─────────────────────────────
FG: Final[str] = "#CCCAC2"
FG1: Final[str] = "#C7C7C7"
FG2: Final[str] = "#8A9199"
FG3: Final[str] = "#707A8C"
FG4: Final[str] = "#5C6773"

# ── bright accents (Ayu Mirage) ─────────────────────────────────────
RED: Final[str] = "#FF6666"
GREEN: Final[str] = "#D5FF80"
YELLOW: Final[str] = "#FFCD66"
BLUE: Final[str] = "#73D0FF"
PURPLE: Final[str] = "#DFBFFF"
AQUA: Final[str] = "#95E6CB"
ORANGE: Final[str] = "#FFA659"

# ── neutral accents (Ayu Mirage terminal tones) ─────────────────────
RED_DIM: Final[str] = "#F28779"
GREEN_DIM: Final[str] = "#87D96C"
YELLOW_DIM: Final[str] = "#FCCA60"
BLUE_DIM: Final[str] = "#5CCFE6"
PURPLE_DIM: Final[str] = "#DDBBFF"
AQUA_DIM: Final[str] = "#93E2C8"
ORANGE_DIM: Final[str] = "#F29E74"

# ── semantic: status badges ──────────────────────────────────────────
STATUS_RUNNING: Final[str] = GREEN_DIM
STATUS_IDLE: Final[str] = YELLOW
STATUS_WAITING_INPUT: Final[str] = ORANGE
STATUS_BLOCKED: Final[str] = ORANGE_DIM
STATUS_ERROR: Final[str] = RED
STATUS_DEAD: Final[str] = YELLOW_DIM
STATUS_COMPLETED: Final[str] = FG4
STATUS_DISCOVERED: Final[str] = BLUE_DIM
STATUS_STARTING: Final[str] = AQUA
STATUS_UNKNOWN: Final[str] = FG3

# ── semantic: severity badges ────────────────────────────────────────
SEVERITY_INFO: Final[str] = BLUE
SEVERITY_WARNING: Final[str] = YELLOW
SEVERITY_ERROR: Final[str] = RED

# ── semantic: health tones ───────────────────────────────────────────
TONE_HEALTHY_BG: Final[str] = "#30413A"
TONE_HEALTHY_FG: Final[str] = GREEN
TONE_WARNING_BG: Final[str] = "#463B2A"
TONE_WARNING_FG: Final[str] = YELLOW
TONE_CRITICAL_BG: Final[str] = "#402B34"
TONE_CRITICAL_FG: Final[str] = RED

# ── semantic: UI chrome ──────────────────────────────────────────────
BORDER: Final[str] = "#171B24"
BORDER_FOCUS: Final[str] = "#FFCC66"
PANEL_BG: Final[str] = BG1
PANEL_TITLE: Final[str] = FG3
HEADER_BG: Final[str] = BG_HARD
FOOTER_BG: Final[str] = BG_HARD
BADGE_BG: Final[str] = "#FFCC66"
BADGE_FG: Final[str] = "#735923"
SELECTED_ROW_BG: Final[str] = BG3
ATTENTION_ROW_BG: Final[str] = TONE_WARNING_BG
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

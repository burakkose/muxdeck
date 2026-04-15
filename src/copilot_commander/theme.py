"""Modern dark palette for Copilot Commander TUI.

Centralized color constants so every widget and the TCSS stylesheet
reference a single source of truth.  Palette uses cool blue-gray tones
inspired by modern developer tooling (Tailwind Slate, GitHub Dark).
"""

from __future__ import annotations

from typing import Final

# ── backgrounds (deep blue-gray) ────────────────────────────────────
BG_HARD: Final[str] = "#0d1017"
BG: Final[str] = "#151921"
BG1: Final[str] = "#1c2029"
BG2: Final[str] = "#272c36"
BG3: Final[str] = "#353b47"
BG4: Final[str] = "#4a5167"

# ── foregrounds (cool whites) ───────────────────────────────────────
FG: Final[str] = "#e2e8f0"
FG1: Final[str] = "#cbd5e1"
FG2: Final[str] = "#94a3b8"
FG3: Final[str] = "#64748b"
FG4: Final[str] = "#475569"

# ── bright accents ───────────────────────────────────────────────────
RED: Final[str] = "#f87171"
GREEN: Final[str] = "#4ade80"
YELLOW: Final[str] = "#fbbf24"
BLUE: Final[str] = "#60a5fa"
PURPLE: Final[str] = "#a78bfa"
AQUA: Final[str] = "#22d3ee"
ORANGE: Final[str] = "#fb923c"

# ── neutral accents (dimmed variants) ────────────────────────────────
RED_DIM: Final[str] = "#ef4444"
GREEN_DIM: Final[str] = "#22c55e"
YELLOW_DIM: Final[str] = "#f59e0b"
BLUE_DIM: Final[str] = "#3b82f6"
PURPLE_DIM: Final[str] = "#8b5cf6"
AQUA_DIM: Final[str] = "#06b6d4"
ORANGE_DIM: Final[str] = "#f97316"

# ── semantic: status badges ──────────────────────────────────────────
STATUS_RUNNING: Final[str] = GREEN
STATUS_IDLE: Final[str] = YELLOW
STATUS_WAITING_INPUT: Final[str] = ORANGE
STATUS_BLOCKED: Final[str] = ORANGE_DIM
STATUS_ERROR: Final[str] = RED
STATUS_DEAD: Final[str] = RED_DIM
STATUS_COMPLETED: Final[str] = FG4
STATUS_DISCOVERED: Final[str] = BLUE
STATUS_STARTING: Final[str] = AQUA
STATUS_UNKNOWN: Final[str] = FG3

# ── semantic: severity badges ────────────────────────────────────────
SEVERITY_INFO: Final[str] = BLUE
SEVERITY_WARNING: Final[str] = YELLOW
SEVERITY_ERROR: Final[str] = RED

# ── semantic: health tones ───────────────────────────────────────────
TONE_HEALTHY_BG: Final[str] = "#0f291a"
TONE_HEALTHY_FG: Final[str] = GREEN
TONE_WARNING_BG: Final[str] = "#291f0f"
TONE_WARNING_FG: Final[str] = YELLOW
TONE_CRITICAL_BG: Final[str] = "#290f0f"
TONE_CRITICAL_FG: Final[str] = RED

# ── semantic: UI chrome ──────────────────────────────────────────────
BORDER: Final[str] = BG3
BORDER_FOCUS: Final[str] = BLUE
PANEL_BG: Final[str] = BG2
PANEL_TITLE: Final[str] = FG2
HEADER_BG: Final[str] = BG_HARD
FOOTER_BG: Final[str] = BG_HARD
BADGE_BG: Final[str] = BLUE_DIM
BADGE_FG: Final[str] = "#e2e8f0"
SELECTED_ROW_BG: Final[str] = "#1e293b"
ATTENTION_ROW_BG: Final[str] = "#291f0f"
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

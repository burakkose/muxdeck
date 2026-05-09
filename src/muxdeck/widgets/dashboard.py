from __future__ import annotations

import contextlib
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from rich.table import Table
from rich.text import Text
from textual import events
from textual._context import NoActiveAppError
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.message import Message
from textual.widgets import Input, Static

from muxdeck.controllers import (
    DashboardAgentListItemView,
    DashboardAlertView,
    DashboardHealthSummary,
    DashboardLogLineView,
    DashboardMetricView,
    DashboardSelectedAgentView,
    DashboardSubAgentTreeView,
    DashboardSubAgentView,
    DashboardSubTaskView,
)
from muxdeck.domain.enums import AgentStatus
from muxdeck.services.operator_status_service import (
    OperatorStatus,
    OperatorStatusKind,
    describe_operator_status,
)
from muxdeck.theme import (
    AQUA,
    ATTENTION_ROW_BG,
    BLUE,
    FG,
    FG1,
    FG2,
    FG3,
    FG4,
    GREEN,
    ORANGE,
    SELECTED_ROW_BG,
    SEVERITY_ERROR,
    SEVERITY_INFO,
    SEVERITY_WARNING,
    TONE_CRITICAL_FG,
    TONE_HEALTHY_FG,
    TONE_WARNING_FG,
    YELLOW,
)
from muxdeck.ui_preferences import (
    UiDensity,
    UiGlyphs,
    UiPreferences,
    resolve_ui_preferences,
)
from muxdeck.widgets.common import (
    format_short_timestamp,
    item_separator,
    pipe_separator,
    status_glyph_parts,
    ui_symbol,
)

# ── ANSI / control-byte stripper ────────────────────────────────────
#
# Pane output captured from real shells — particularly PowerShell on
# WSL with PSReadLine — contains far more than the basic SGR colour
# sequences the previous implementation handled. PSReadLine emits DEC
# private-mode toggles like ``\x1b[?25h`` and ``\x1b[?2004h``
# (bracketed paste) constantly, OSC sequences terminated with ``ST``
# (``\x1b\\``) instead of ``BEL`` (``\x07``) for window titles and
# OSC 8 hyperlinks, charset designators (``\x1b(B``), and naked
# 2-character escapes (``\x1b=``, ``\x1bM``). Windows-originated
# streams also occasionally leak BOMs, stray ``\r`` cursor returns,
# and backspaces. The earlier ``\x1b\[[0-9;]*[a-zA-Z]`` pattern
# matched none of these, so the raw bytes leaked through into the
# dashboard "output" panel as visible garbage.
#
# The patterns below follow ECMA-48 / xterm conventions:
#   * CSI: ``ESC [`` then param bytes (0x30-0x3F: digits, ``:;<=>?``),
#     intermediate bytes (0x20-0x2F), and a final byte (0x40-0x7E).
#   * OSC / DCS / SOS / PM / APC: introducer then a body terminated
#     by ``BEL``, ``ST`` (``ESC \``), or single-byte ``ST`` (``\x9c``).
#   * Charset designators: ``ESC`` + intermediate (0x20-0x2F) + final.
#   * Other Fe escapes: ``ESC`` + 0x40-0x5F (excluding the introducers
#     handled above by ordering the OSC/DCS patterns first).
#
# After ANSI stripping we also drop control-byte noise that survives
# (``\x00-\x08``, ``\x0b-\x1f``, ``\x7f``, BOM, and stray ``\r`` not
# followed by ``\n``). TAB (``\x09``) and LF (``\x0a``) are preserved.
_ANSI_RE = re.compile(
    "|".join(
        (
            # OSC ... (BEL | ST | 0x9c)
            r"\x1b\][^\x07\x1b\x9c]*?(?:\x07|\x1b\\|\x9c)",
            # DCS / SOS / PM / APC ... (ST | 0x9c)
            r"\x1b[PX^_][^\x1b\x9c]*?(?:\x1b\\|\x9c)",
            # CSI
            r"\x1b\[[\x30-\x3f]*[\x20-\x2f]*[\x40-\x7e]",
            # Charset designators (e.g. ``ESC ( B``, ``ESC ) 0``)
            r"\x1b[\x20-\x2f]+[\x30-\x7e]",
            # Other two-character escapes spanning the Fp (0x30-0x3F:
            # ``ESC =``, ``ESC >``, ``ESC 7``, ``ESC 8``...), Fe
            # (0x40-0x5F: ``ESC M``, ``ESC D``, ``ESC E``...) and
            # Fs (0x60-0x7E: ``ESC c``...) ranges. The introducers
            # ``P`` (0x50), ``X`` (0x58), ``[`` (0x5B), ``]`` (0x5D),
            # ``^`` (0x5E), ``_`` (0x5F) are excluded so the more
            # specific patterns above own them.
            r"\x1b[\x30-\x4f\x51-\x57\x59-\x5a\x5c\x60-\x7e]",
        )
    )
)

# Control-byte cleanup. We deliberately preserve TAB (\x09), LF
# (\x0a), and CR-when-followed-by-LF — splitlines() and downstream
# rendering rely on those. Everything else in the C0 range, plus
# DEL, BOM, and stray CRs, is noise that produces visible artefacts
# (Windows stdouts in particular leak BOMs and bare CRs).
_CONTROL_NOISE_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\ufeff]|\r(?!\n)")


def _strip_ansi(text: str) -> str:
    return _CONTROL_NOISE_RE.sub("", _ANSI_RE.sub("", text))


_SEVERITY_STYLES: dict[str, str] = {
    "info": f"bold {SEVERITY_INFO}",
    "warning": f"bold {SEVERITY_WARNING}",
    "error": f"bold {SEVERITY_ERROR}",
}

_HEALTH_TONE_STYLES: dict[str, tuple[str, str]] = {
    "healthy": ("▲ healthy", TONE_HEALTHY_FG),
    "warning": ("▬ review", TONE_WARNING_FG),
    "critical": ("▼ critical", TONE_CRITICAL_FG),
}

_LIST_STATUS_LABELS: dict[OperatorStatusKind, str] = {
    OperatorStatusKind.STARTING: "STARTING",
    OperatorStatusKind.WORKING: "RUNNING",
    OperatorStatusKind.WAITING_INPUT: "WAITING",
    OperatorStatusKind.BLOCKED: "BLOCKED",
    OperatorStatusKind.REVIEW_READY: "NEEDS REVIEW",
    OperatorStatusKind.FAILED: "FAILED",
    OperatorStatusKind.TERMINATED: "DONE",
    OperatorStatusKind.STALE: "STALE",
    OperatorStatusKind.COMPLETED: "DONE",
}

_LOG_SOURCE_STYLES: dict[str, str] = {
    "stdout": FG3,
    "stderr": f"bold {SEVERITY_ERROR}",
    "tmux": FG4,
    "tmux_capture": FG4,
    # Agent speech is the signal; user prompts are context. The
    # graphite redesign puts the loudest log line in primary text and
    # demotes user prompts to FG2 so colour stays reserved for
    # warnings/errors below.
    "assistant": FG,
    "user": f"bold {FG2}",
    "system": FG4,
}


def _event_color(event: str) -> str:
    """Pick a color based on event emoji prefix.

    Per the graphite redesign, recent-event chips for tool / search /
    edit output are metadata about *what* the agent did, not state. The
    only event categories that earn colour are real state changes:
    success (green), warning (amber), and danger (kept off the palette
    here — error rows surface through ``_highlight_log_line`` instead).
    """
    if event.startswith("⚡"):
        return GREEN
    if event.startswith("⚠"):
        return YELLOW
    return FG2


def _section_header(text: Text, title: str, *, preferences: UiPreferences) -> None:
    """Render a clean section header with box-drawing decoration."""
    text.append(ui_symbol("section-lead", preferences=preferences), style=FG4)
    text.append(title.upper(), style=f"bold {FG3}")
    text.append(ui_symbol("section-fill", preferences=preferences), style=FG4)
    text.append("\n", style=FG4)


_SHORT_STATUS: dict[AgentStatus, str] = {
    AgentStatus.RUNNING: "run",
    AgentStatus.IDLE: "idle",
    AgentStatus.WAITING_INPUT: "input",
    AgentStatus.BLOCKED: "block",
    AgentStatus.ERROR: "error",
    AgentStatus.DEAD: "dead",
    AgentStatus.COMPLETED: "done",
    AgentStatus.DISCOVERED: "disc",
    AgentStatus.STARTING: "start",
    AgentStatus.UNKNOWN: "?",
}


def _short_status(status: AgentStatus) -> str:
    return _SHORT_STATUS.get(status, "?")


def _status_display(agent: DashboardAgentListItemView) -> tuple[str, str]:
    operator_status = _resolved_operator_status(agent)
    style_lookup = {
        OperatorStatusKind.STARTING: f"bold {AQUA}",
        OperatorStatusKind.WORKING: FG3,
        OperatorStatusKind.WAITING_INPUT: f"bold {ORANGE}",
        OperatorStatusKind.BLOCKED: f"bold {SEVERITY_ERROR}",
        OperatorStatusKind.REVIEW_READY: f"bold {ORANGE}",
        OperatorStatusKind.FAILED: f"bold {SEVERITY_ERROR}",
        OperatorStatusKind.TERMINATED: f"bold {YELLOW}",
        OperatorStatusKind.STALE: f"bold {YELLOW}",
        OperatorStatusKind.COMPLETED: FG4,
    }
    return (operator_status.display_label, style_lookup[operator_status.kind])


_STATUS_DOT_COLORS: dict[OperatorStatusKind, str] = {
    OperatorStatusKind.STARTING: AQUA,
    OperatorStatusKind.WORKING: GREEN,
    OperatorStatusKind.WAITING_INPUT: ORANGE,
    OperatorStatusKind.BLOCKED: SEVERITY_ERROR,
    OperatorStatusKind.REVIEW_READY: ORANGE,
    OperatorStatusKind.FAILED: SEVERITY_ERROR,
    OperatorStatusKind.TERMINATED: YELLOW,
    OperatorStatusKind.STALE: YELLOW,
    OperatorStatusKind.COMPLETED: FG4,
}


def _status_dot_style(agent: DashboardAgentListItemView) -> str:
    """Raw color for the status dot in the agent list (no 'bold ' prefix)."""
    operator_status = _resolved_operator_status(agent)
    return _STATUS_DOT_COLORS.get(operator_status.kind, FG4)


def _resolved_operator_status(agent: DashboardAgentListItemView) -> OperatorStatus:
    if agent.operator_status is not None:
        return agent.operator_status
    return describe_operator_status(
        agent_status=agent.status,
        needs_attention=agent.needs_attention,
        attention_reason=agent.attention_reason,
        idle_seconds=agent.idle_seconds,
        is_potentially_stuck=agent.is_potentially_stuck,
        task_title=agent.task_title,
        current_activity=agent.current_activity,
    )


def _list_status_text(agent: DashboardAgentListItemView) -> str:
    operator_status = _resolved_operator_status(agent)
    return _LIST_STATUS_LABELS[operator_status.kind]


def _activity_summary(agent: DashboardAgentListItemView, *, limit: int = 52) -> str:
    operator_status = _resolved_operator_status(agent)
    if operator_status.kind in {
        OperatorStatusKind.WAITING_INPUT,
        OperatorStatusKind.BLOCKED,
        OperatorStatusKind.REVIEW_READY,
        OperatorStatusKind.FAILED,
        OperatorStatusKind.STALE,
    }:
        summary = operator_status.reason
    elif operator_status.kind is OperatorStatusKind.STARTING:
        summary = agent.current_activity or operator_status.reason
    else:
        summary = agent.current_activity or agent.task_title or operator_status.reason
    return _truncate(summary, limit)


def _focus_summary(agent: DashboardAgentListItemView) -> tuple[str, str]:
    operator_status = _resolved_operator_status(agent)
    if operator_status.kind in {
        OperatorStatusKind.WAITING_INPUT,
        OperatorStatusKind.BLOCKED,
        OperatorStatusKind.REVIEW_READY,
        OperatorStatusKind.FAILED,
        OperatorStatusKind.STALE,
    }:
        summary = operator_status.reason
        style = _status_display(agent)[1]
    elif operator_status.kind is OperatorStatusKind.STARTING:
        summary = agent.current_activity or operator_status.headline
        style = AQUA
    else:
        summary = agent.current_activity or agent.task_title or operator_status.headline
        style = FG1
    return _truncate(summary, 56), style


def _detail_subtitle(
    item: DashboardAgentListItemView,
    operator_status: OperatorStatus,
) -> str:
    """Pick the single-line subtitle for the dominant detail banner.

    Operator priority order: attention reason (when present and the
    agent needs review/intervention) outranks current tool activity,
    which outranks the high-level task title. Falls back to the
    operator status headline ("running"/"waiting"/…) so the line
    never renders empty when there is enough room for it.
    """
    if (
        item.needs_attention
        and item.attention_reason
        and operator_status.kind
        in {
            OperatorStatusKind.WAITING_INPUT,
            OperatorStatusKind.BLOCKED,
            OperatorStatusKind.REVIEW_READY,
            OperatorStatusKind.FAILED,
            OperatorStatusKind.STALE,
        }
    ):
        return _truncate(item.attention_reason, 72)
    activity = (item.current_activity or "").strip()
    if activity:
        return _truncate(activity, 72)
    title = (item.task_title or "").strip()
    if title:
        return _truncate(title, 72)
    if operator_status.reason:
        return _truncate(operator_status.reason, 72)
    return ""


def _display_name(
    agent: DashboardAgentListItemView,
    all_agents: tuple[DashboardAgentListItemView, ...],
) -> str:
    """Pick a unique, human-readable display name for the agent list."""
    name = agent.name
    duplicates = tuple(a for a in all_agents if a.name == name)
    if len(duplicates) <= 1:
        return name
    for values, suffix in (
        (tuple(item.worktree_name for item in duplicates), agent.worktree_name),
        (tuple(item.repo_name for item in duplicates), agent.repo_name),
        (tuple(item.window_name for item in duplicates), agent.window_name),
        (tuple(item.pane_id for item in duplicates), agent.pane_id),
    ):
        if suffix is None or suffix == name:
            continue
        if values.count(suffix) == 1:
            return f"{name}/{suffix}"
    return name


def _humanize_event_kind(kind: str | None) -> str:
    if kind is None:
        return "-"
    return kind.replace("_", " ")


def _format_idle(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    return f"{seconds // 3600}h{(seconds % 3600) // 60}m"


def _format_duration(started_at: datetime) -> str:
    """Format agent uptime from start to now."""
    now = datetime.now(UTC)
    delta = now - started_at
    total_secs = int(delta.total_seconds())
    if total_secs < 0:
        return "-"
    if total_secs < 60:
        return f"{total_secs}s"
    if total_secs < 3600:
        return f"{total_secs // 60}m {total_secs % 60}s"
    hours = total_secs // 3600
    minutes = (total_secs % 3600) // 60
    return f"{hours}h {minutes}m"


def _format_cost(value: str | None) -> str:
    if value is None:
        return "-"
    try:
        return f"${float(value):.2f}"
    except (ValueError, TypeError):
        return f"${value}"


def _resolved_token_total(agent: DashboardAgentListItemView) -> int | None:
    if agent.token_total is not None:
        return agent.token_total
    if agent.token_input is not None and agent.token_output is not None:
        return agent.token_input + agent.token_output
    return None


def _usage_badges(agent: DashboardAgentListItemView) -> tuple[tuple[str, str], ...]:
    badges: list[tuple[str, str]] = []
    token_total = _resolved_token_total(agent)
    # Usage badges are numeric metadata, not state. The graphite
    # palette routes them to the gray family so they read as quiet
    # context next to the loud status banner. Cost is the only badge
    # that earns a touch of warmth (FG, slightly brighter) because
    # an operator scanning for cost regressions cares about it more
    # than tok/in/out counts.
    if token_total is not None:
        badges.append((f"{token_total:,} tok", FG2))
    else:
        if agent.token_input is not None:
            badges.append((f"in {agent.token_input:,}", FG2))
        if agent.token_output is not None:
            badges.append((f"out {agent.token_output:,}", FG2))
    cost = _format_cost(agent.estimated_cost_usd)
    if cost != "-":
        badges.append((cost, FG))
    return tuple(badges)


def _append_badges(
    text: Text,
    badges: Sequence[tuple[str, str]],
    *,
    separator: str = " · ",
) -> None:
    for index, (label, style) in enumerate(badges):
        if index:
            text.append(separator, style=FG4)
        text.append(label, style=f"bold {style}")


def _format_session(agent: DashboardSelectedAgentView) -> str:
    session = agent.open_session_id or agent.item.latest_session_id
    if session is None:
        return "-"
    if agent.session_count <= 1:
        return session
    return f"{session} ({agent.session_count} total)"


def _row_style(agent: DashboardAgentListItemView, *, selected: bool) -> str:
    if selected:
        return f"on {SELECTED_ROW_BG}"
    if agent.status in {AgentStatus.COMPLETED, AgentStatus.DEAD}:
        return "dim"
    if agent.needs_attention:
        return f"on {ATTENTION_ROW_BG}"
    return ""


class StatusBar(Static):
    """Compact 1-line dashboard summary — btop-inspired health strip."""

    def set_state(
        self,
        health: DashboardHealthSummary,
        metrics: Sequence[DashboardMetricView],
        selected: DashboardAgentListItemView | None = None,
    ) -> None:
        preferences = resolve_ui_preferences(self)
        separator = pipe_separator(preferences)
        emphasis = ui_symbol("badge", preferences=preferences)
        status_dot = "o" if preferences.glyphs is UiGlyphs.ASCII else "●"
        waiting_glyph = "!" if preferences.glyphs is UiGlyphs.ASCII else "▲"
        blocked_glyph = "#" if preferences.glyphs is UiGlyphs.ASCII else "■"
        error_glyph = "x" if preferences.glyphs is UiGlyphs.ASCII else "✗"
        active_marker = status_dot if preferences.glyphs is UiGlyphs.ASCII else emphasis
        metric_lookup = {metric.key: metric.value for metric in metrics}
        tone_label, tone_color = _HEALTH_TONE_STYLES[health.tone]
        line = Text()
        line.append(" ", style=FG4)
        line.append(tone_label, style=f"bold {tone_color}")

        # Primary section: always show active count right after the
        # tone so the eye lands on "how many are working" first.
        line.append("   ")
        line.append(f"{active_marker} ", style=f"bold {GREEN}")
        line.append(f"{health.active_agents} active", style=f"bold {GREEN}")

        review_agents = max(
            health.attention_agents
            - health.waiting_input_agents
            - health.blocked_agents
            - health.error_agents,
            0,
        )
        if review_agents:
            line.append(separator, style=FG4)
            line.append(f"{active_marker} ", style=f"bold {ORANGE}")
            line.append(f"{review_agents} review", style=f"bold {ORANGE}")

        if health.waiting_input_agents:
            line.append(separator, style=FG4)
            line.append(f"{waiting_glyph} ", style=f"bold {ORANGE}")
            line.append(f"{health.waiting_input_agents} waiting", style=f"bold {ORANGE}")
        if health.blocked_agents:
            line.append(separator, style=FG4)
            line.append(f"{blocked_glyph} ", style=f"bold {SEVERITY_ERROR}")
            line.append(f"{health.blocked_agents} blocked", style=f"bold {SEVERITY_ERROR}")
        if health.error_agents:
            line.append(separator, style=FG4)
            line.append(f"{error_glyph} ", style=f"bold {SEVERITY_ERROR}")
            line.append(f"{health.error_agents} failed", style=f"bold {SEVERITY_ERROR}")

        # Trailing: total agents (quiet) and tokens (aqua accent).
        line.append(separator, style=FG4)
        line.append(f"{health.total_agents} agents", style=FG4)

        if "tokens" in metric_lookup:
            line.append(separator, style=FG4)
            line.append("tokens ", style=FG4)
            # Token total is metadata, not state. The graphite redesign
            # keeps it in primary text instead of bold AQUA so the
            # active / review / waiting count badges remain the only
            # bold-coloured cells in the bar.
            line.append(f"{metric_lookup['tokens']:,}", style=f"bold {FG}")
        if selected is not None:
            focus, focus_style = _focus_summary(selected)
            usage_badges = _usage_badges(selected)
            if focus or usage_badges:
                line.append(separator, style=FG4)
                line.append("focus ", style=FG4)
                line.append(selected.name, style=f"bold {FG}")
                if focus:
                    line.append(" → ", style=FG4)
                    line.append(focus, style=focus_style)
                if usage_badges:
                    line.append(item_separator(preferences), style=FG4)
                    _append_badges(
                        line,
                        usage_badges,
                        separator=item_separator(preferences),
                    )
        self.update(line)


class FilterBar(Vertical):
    def compose(self) -> ComposeResult:
        yield Input(placeholder="/ filter", id="dashboard-filter-input")
        yield Static(classes="filter-summary", id="dashboard-filter-summary")

    def set_query(self, value: str | None) -> None:
        self.query_one(Input).value = value or ""

    def focus_input(self) -> None:
        self.query_one(Input).focus()

    def set_state(
        self,
        *,
        filter_text: str | None,
        visible_agents: int,
        total_agents: int,
        attention_only: bool,
        include_completed: bool,
        sort_label: str,
    ) -> None:
        preferences = resolve_ui_preferences(self)
        separator = pipe_separator(preferences)
        summary = Text()
        summary.append(f"{visible_agents}", style=f"bold {FG1}")
        summary.append(f"/{total_agents} visible", style=FG4)
        summary.append(separator, style=FG4)
        summary.append("sort ", style=FG4)
        # Sort label is metadata, not a primary surface. Keep it in
        # primary text instead of AQUA so colour stays reserved for
        # the attention / filter chips above.
        summary.append(sort_label, style=FG2)
        if attention_only:
            summary.append(separator, style=FG4)
            summary.append("attention", style=f"bold {ORANGE}")
        if not include_completed:
            summary.append(separator, style=FG4)
            summary.append("hide-done", style=FG4)
        if filter_text and filter_text.strip():
            summary.append(separator, style=FG4)
            summary.append("query ", style=FG4)
            # Query value is metadata. YELLOW is reserved for the
            # stale / warning state — using it on the filter chip
            # made every search look like an alert.
            summary.append(filter_text.strip(), style=FG2)
        else:
            summary.append(separator, style=FG4)
            summary.append("search name, branch, task, or status", style=FG4)
        self.query_one("#dashboard-filter-summary", Static).update(summary)


@dataclass(frozen=True, slots=True)
class _AgentRow:
    agent: DashboardAgentListItemView

    @property
    def parent_agent_id(self) -> str:
        return self.agent.agent_id


@dataclass(frozen=True, slots=True)
class _SubAgentHeaderRow:
    parent_agent_id: str
    count: int = 0
    loading: bool = False


@dataclass(frozen=True, slots=True)
class _SubAgentRow:
    parent_agent_id: str
    subagent: DashboardSubAgentView


_Row = _AgentRow | _SubAgentHeaderRow | _SubAgentRow


class AgentListPanel(Static, can_focus=True):
    """Compact agent list — clean selection, status glyphs, activity hint.

    Supports per-row expand/collapse for showing a parent agent's
    **active** sub-agent tree. Sub-agent rows are fully navigable:
    the cursor steps over them, and the right-hand pane still shows
    the owning parent agent's detail (sub-agents have no pane of their
    own).
    """

    class AgentSelected(Message):
        def __init__(self, agent_id: str) -> None:
            super().__init__()
            self.agent_id = agent_id

    class SubAgentHighlighted(Message):
        """Emitted when the cursor lands on (or leaves) a sub-agent row.

        ``subagent`` is ``None`` when the cursor is on a non-subagent
        row — the screen should show the regular agent detail in that
        case. When present, the screen should render the sub-agent
        detail (prompt, result, etc.) instead.
        """

        def __init__(self, subagent: DashboardSubAgentView | None) -> None:
            super().__init__()
            self.subagent = subagent

    class ExpandRequested(Message):
        """Emitted when the user expands a row whose sub-agent tree

        hasn't been loaded yet. The screen is responsible for kicking
        off a worker-thread load and feeding the result back via
        :meth:`set_subagents`.
        """

        def __init__(self, agent_id: str) -> None:
            super().__init__()
            self.agent_id = agent_id

    def __init__(self, *, widget_id: str | None = None, classes: str | None = None) -> None:
        super().__init__(id=widget_id, classes=classes)
        self._agents: tuple[DashboardAgentListItemView, ...] = ()
        # ``_selected_index`` indexes into ``_rows`` (all visible rows,
        # including sub-agent child rows), not into ``_agents``. The
        # parent agent id is derived via :attr:`selected_agent_id`.
        self._selected_index = 0
        self._expanded: set[str] = set()
        self._subagents: dict[str, DashboardSubAgentTreeView] = {}
        self._loading: set[str] = set()
        self._rows: tuple[_Row, ...] = ()

    def on_mount(self) -> None:
        pass

    def set_agents(
        self,
        agents: Sequence[DashboardAgentListItemView],
        *,
        selected_agent_id: str | None,
    ) -> None:
        self._agents = tuple(agents)
        live_ids = {agent.agent_id for agent in self._agents}
        self._expanded &= live_ids
        self._loading &= live_ids
        for stale in set(self._subagents) - live_ids:
            self._subagents.pop(stale, None)

        if not self._agents:
            self._selected_index = 0
            self._rows = ()
            empty = Text()
            empty.append("\n  no agents found\n", style=f"bold {FG3}")
            empty.append("  press ", style=FG4)
            empty.append("r", style=f"bold {BLUE}")
            empty.append(" to scan", style=FG4)
            self.update(empty)
            return
        # Capture whatever the cursor was on before the refresh. We
        # want to keep the user on the *exact* row they were looking
        # at — including sub-agent children — not just the parent.
        prior_parent = self._parent_agent_id_at(self._selected_index)
        prior_sub = self.selected_subagent
        prior_is_header = self._is_header_row(self._selected_index)
        self._rebuild_rows()
        # Explicit selection from the caller wins only when it changes
        # the parent focus; if the caller is just echoing back the
        # same parent we already had, don't clobber the in-parent
        # position the user has navigated to.
        if selected_agent_id is not None and selected_agent_id != prior_parent:
            self._selected_index = self._row_index_for_agent(selected_agent_id)
        else:
            target_parent = prior_parent or selected_agent_id
            sub_call_id = prior_sub.tool_call_id if prior_sub is not None else None
            self._selected_index = self._row_index_for_position(
                parent_agent_id=target_parent,
                sub_tool_call_id=sub_call_id,
                prefer_header=prior_is_header,
            )
        self._refresh_table()
        self._post_selection(self._selected_index)

    def move_cursor(self, delta: int) -> str | None:
        if not self._rows:
            return None
        target = max(0, min(len(self._rows) - 1, self._selected_index + delta))
        # Skip the "active sub-agents" header — it's a decorative label,
        # not a selectable row. Advance in the same direction the user
        # moved (falling back to the other direction if we're cornered
        # at a boundary) until we land on a real row.
        step = 1 if delta >= 0 else -1
        if self._is_header_row(target):
            probe = target + step
            while 0 <= probe < len(self._rows) and self._is_header_row(probe):
                probe += step
            if 0 <= probe < len(self._rows):
                target = probe
            else:
                # Cornered — try the opposite direction instead of
                # leaving the cursor parked on the header.
                back = target - step
                while 0 <= back < len(self._rows) and self._is_header_row(back):
                    back -= step
                if 0 <= back < len(self._rows):
                    target = back
        self._selected_index = target
        with contextlib.suppress(NoActiveAppError):
            self.focus()
        self._refresh_table()
        self._post_selection(self._selected_index)
        return self._parent_agent_id_at(self._selected_index)

    @property
    def selected_agent_id(self) -> str | None:
        """Return the id of the parent agent owning the highlighted row.

        Sub-agent rows still resolve to their parent agent so that the
        right-hand pane keeps showing useful context (sub-agents don't
        have their own tmux pane to talk about).
        """
        if not self._rows or self._selected_index >= len(self._rows):
            return None
        return self._parent_agent_id_at(self._selected_index)

    @property
    def selected_subagent(self) -> DashboardSubAgentView | None:
        """The sub-agent under the cursor, or ``None`` when on a parent row."""
        if not self._rows or self._selected_index >= len(self._rows):
            return None
        row = self._rows[self._selected_index]
        return row.subagent if isinstance(row, _SubAgentRow) else None

    def focus_list(self) -> None:
        self.focus()

    def toggle_expand(self) -> str | None:
        """Expand or collapse the parent agent of the selected row.

        Works whether the cursor is on the parent row itself or on one
        of its sub-agent children. Collapsing from within a sub-agent
        row snaps the cursor back to the parent so the user doesn't
        end up on an invisible index.
        """
        agent_id = self.selected_agent_id
        if agent_id is None:
            return None
        if agent_id in self._expanded:
            self._expanded.discard(agent_id)
            self._loading.discard(agent_id)
            self._rebuild_rows()
            self._selected_index = self._row_index_for_agent(agent_id)
            self._refresh_table()
            return agent_id
        self._expanded.add(agent_id)
        if agent_id not in self._subagents:
            self._loading.add(agent_id)
            self.post_message(self.ExpandRequested(agent_id))
        self._rebuild_rows()
        self._refresh_table()
        return agent_id

    def set_subagents(self, agent_id: str, tree: DashboardSubAgentTreeView) -> None:
        """Feed a loaded sub-agent tree back into the widget."""
        # Preserve the cursor position the same way :meth:`set_agents`
        # does — rebuilding rows invalidates indices, and we don't
        # want the user to lose their place when a tree refresh lands.
        prior_parent = self._parent_agent_id_at(self._selected_index)
        prior_sub = self.selected_subagent
        prior_is_header = self._is_header_row(self._selected_index)
        self._subagents[agent_id] = tree
        self._loading.discard(agent_id)
        self._rebuild_rows()
        if self._rows:
            self._selected_index = self._row_index_for_position(
                parent_agent_id=prior_parent,
                sub_tool_call_id=prior_sub.tool_call_id if prior_sub is not None else None,
                prefer_header=prior_is_header,
            )
        self._refresh_table()

    def _post_selection(self, index: int | None) -> None:
        if index is None or index >= len(self._rows):
            return
        agent_id = self._parent_agent_id_at(index)
        if agent_id is not None:
            self.post_message(self.AgentSelected(agent_id))
        self.post_message(self.SubAgentHighlighted(self.selected_subagent))

    def _parent_agent_id_at(self, index: int) -> str | None:
        if not self._rows or index >= len(self._rows):
            return None
        row = self._rows[index]
        return row.parent_agent_id

    def _row_index_for_agent(self, agent_id: str | None) -> int:
        if agent_id is None:
            return 0
        for idx, row in enumerate(self._rows):
            if isinstance(row, _AgentRow) and row.agent.agent_id == agent_id:
                return idx
        return 0

    def _row_index_for_position(
        self,
        *,
        parent_agent_id: str | None,
        sub_tool_call_id: str | None,
        prefer_header: bool,
    ) -> int:
        """Restore the cursor to the row that was focused before a refresh.

        Prefers the specific sub-agent (by tool_call_id), falls back to
        the sub-agent header, then to the parent row itself, so a
        periodic refresh never drags the user back up to the parent
        row while they're browsing children.
        """
        if parent_agent_id is None:
            return 0
        parent_idx = 0
        header_idx: int | None = None
        for idx, row in enumerate(self._rows):
            if isinstance(row, _AgentRow) and row.agent.agent_id == parent_agent_id:
                parent_idx = idx
            elif isinstance(row, _SubAgentHeaderRow) and row.parent_agent_id == parent_agent_id:
                header_idx = idx
            elif (
                sub_tool_call_id is not None
                and isinstance(row, _SubAgentRow)
                and row.parent_agent_id == parent_agent_id
                and row.subagent.tool_call_id == sub_tool_call_id
            ):
                return idx
        if prefer_header and header_idx is not None:
            return header_idx
        return parent_idx

    def _is_header_row(self, index: int) -> bool:
        if not self._rows or index >= len(self._rows):
            return False
        return isinstance(self._rows[index], _SubAgentHeaderRow)

    def _rebuild_rows(self) -> None:
        rows: list[_Row] = []
        for agent in self._agents:
            rows.append(_AgentRow(agent=agent))
            if agent.agent_id not in self._expanded:
                continue
            tree = self._subagents.get(agent.agent_id)
            is_loading = agent.agent_id in self._loading
            if is_loading:
                rows.append(_SubAgentHeaderRow(parent_agent_id=agent.agent_id, loading=True))
                continue
            running = tuple(tree.running) if tree is not None else ()
            rows.append(_SubAgentHeaderRow(parent_agent_id=agent.agent_id, count=len(running)))
            for sub in running:
                rows.append(_SubAgentRow(parent_agent_id=agent.agent_id, subagent=sub))
        self._rows = tuple(rows)

    def _refresh_table(self) -> None:
        self.update(self._build_table())

    def _build_table(self) -> Table:
        preferences = resolve_ui_preferences(self)
        comfortable = preferences.density is UiDensity.COMFORTABLE
        status_dot = "o" if preferences.glyphs is UiGlyphs.ASCII else "●"
        # Keep rows in sync with ``_agents`` defensively — some legacy
        # tests poke ``_agents`` directly without going through
        # :meth:`set_agents`, and we don't want to silently render an
        # empty table in that case.
        if not self._rows and self._agents:
            self._rebuild_rows()
        table = Table(
            expand=True,
            box=None,
            header_style=f"bold {FG4}",
            border_style=FG4,
            pad_edge=False,
            show_edge=False,
            show_header=True,
            padding=(0, 1, 0, 0),
        )
        table.add_column("", width=2, no_wrap=True)
        table.add_column("", width=2, no_wrap=True)
        table.add_column("agent", min_width=12, no_wrap=True, ratio=2)
        table.add_column("doing", min_width=16, no_wrap=True, ratio=3, overflow="ellipsis")
        table.add_column(
            "status",
            min_width=18 if comfortable else 11,
            no_wrap=True,
            ratio=1,
            overflow="ellipsis",
        )
        for index, row in enumerate(self._rows):
            is_selected = index == self._selected_index
            selected_glyph = ui_symbol("selected", preferences=preferences)
            indicator = (
                Text(f"{selected_glyph} ", style=f"bold {BLUE}") if is_selected else Text("  ")
            )
            if isinstance(row, _AgentRow):
                agent = row.agent
                row_style = _row_style(agent, selected=is_selected)
                base_name = _display_name(agent, self._agents)
                running_count = self._running_subagent_count(agent.agent_id)
                display_name = _agent_display(
                    base_name,
                    expanded=agent.agent_id in self._expanded,
                    running_count=running_count,
                    preferences=preferences,
                )
                dot_color = _status_dot_style(agent)
                name_text = Text(display_name, style=f"bold {FG}" if is_selected else f"bold {FG2}")
                if agent.subtask_count > 0:
                    subtask_glyph = ui_symbol("background-task", preferences=preferences)
                    name_text.append(
                        f" {subtask_glyph}{agent.subtask_count}",
                        style=f"bold {YELLOW}",
                    )
                usage_badges = _usage_badges(agent)
                if comfortable:
                    meta_parts = tuple(
                        value
                        for value in (agent.branch, agent.worktree_name, agent.repo_name)
                        if value
                    )
                    if meta_parts:
                        name_text.append("\n  ", style=FG4)
                        name_text.append(
                            item_separator(preferences).join(meta_parts[:3]), style=FG4
                        )
                    if usage_badges:
                        name_text.append("\n  ", style=FG4)
                        _append_badges(
                            name_text,
                            usage_badges,
                            separator=item_separator(preferences),
                        )
                elif usage_badges:
                    name_text.append("  ", style=FG4)
                    _append_badges(name_text, usage_badges, separator=" ")
                is_attention_like = agent.needs_attention or agent.status in {
                    AgentStatus.DISCOVERED,
                    AgentStatus.STARTING,
                }
                activity = Text(
                    _activity_summary(agent, limit=74 if comfortable else 52),
                    style=(
                        _status_display(agent)[1]
                        if is_attention_like
                        else (FG1 if is_selected else FG3)
                    ),
                    overflow="ellipsis",
                )
                if comfortable:
                    detail_parts = (
                        f"seen {format_short_timestamp(agent.last_seen_at)}",
                        f"idle {_format_idle(agent.idle_seconds)}",
                    )
                    activity.append("\n", style=FG4)
                    activity.append(item_separator(preferences).join(detail_parts), style=FG4)
                status_style = _status_display(agent)[1]
                status_text = Text(
                    _list_status_text(agent),
                    style=status_style,
                    overflow="ellipsis",
                )
                if comfortable and agent.last_event_kind is not None:
                    status_text.append("\n", style=FG4)
                    status_text.append(
                        _truncate(_humanize_event_kind(agent.last_event_kind), 18),
                        style=FG4,
                    )
                table.add_row(
                    indicator,
                    Text(status_dot, style=f"bold {dot_color}"),
                    name_text,
                    activity,
                    status_text,
                    style=row_style,
                )
            elif isinstance(row, _SubAgentHeaderRow):
                table.add_row(
                    *_render_subagent_header_row(
                        row,
                        is_selected=is_selected,
                        preferences=preferences,
                    )
                )
            else:
                table.add_row(
                    *_render_subagent_row(
                        row.subagent,
                        is_selected=is_selected,
                        preferences=preferences,
                    )
                )
        return table

    def _running_subagent_count(self, agent_id: str) -> int | None:
        if agent_id not in self._expanded:
            return None
        if agent_id in self._loading:
            return None
        tree = self._subagents.get(agent_id)
        return 0 if tree is None else len(tree.running)


def _agent_display(
    base_name: str,
    *,
    expanded: bool,
    running_count: int | None,
    preferences: UiPreferences,
) -> str:
    glyph_name = "expanded" if expanded else "collapsed"
    glyph = f"{ui_symbol(glyph_name, preferences=preferences)} "
    if running_count is not None and running_count > 0:
        return f"{glyph}{base_name}{item_separator(preferences)}{running_count}"
    return f"{glyph}{base_name}"


def _render_subagent_header_row(
    row: _SubAgentHeaderRow,
    *,
    is_selected: bool,
    preferences: UiPreferences,
) -> tuple[Text, Text, Text, Text, Text]:
    selected_glyph = ui_symbol("selected", preferences=preferences)
    indicator = Text(f"{selected_glyph} ", style=f"bold {BLUE}") if is_selected else Text("  ")
    if row.loading:
        label = Text("    loading active sub-agents…", style=FG4)
        return (indicator, Text(""), label, Text(""), Text(""))
    if row.count == 0:
        label = Text("    no active sub-agents", style=FG4)
        return (indicator, Text(""), label, Text(""), Text(""))
    label = Text()
    label.append("    ", style=FG4)
    label.append("active sub-agents ", style=f"bold {FG3}")
    label.append(f"({row.count})", style=FG4)
    return (indicator, Text(""), label, Text(""), Text(""))


def _render_subagent_row(
    subagent: DashboardSubAgentView,
    *,
    is_selected: bool,
    preferences: UiPreferences,
) -> tuple[Text, Text, Text, Text, Text]:
    # Only active sub-agents are rendered — completed ones are
    # filtered out upstream in :meth:`AgentListPanel._rebuild_rows`.
    comfortable = preferences.density is UiDensity.COMFORTABLE
    glyph = ui_symbol("subagent", preferences=preferences)
    # Sub-agent rows are children of an agent in the list. Tone the
    # connector glyph down to FG3 (muted label tier) so it reads as
    # tree structure, not as a primary action surface.
    glyph_color = FG3
    duration = _format_subagent_duration(subagent)
    label = Text()
    label.append("    ", style=FG4)
    label.append(f"{glyph} ", style=f"bold {glyph_color}")
    label.append(subagent.display_name, style=FG2 if subagent.is_running else FG3)
    call_suffix = _shorten_tool_call_id(subagent.tool_call_id)
    if call_suffix:
        label.append(f"  {call_suffix}", style=FG4)
    if comfortable:
        detail = subagent.description or subagent.agent_type or subagent.mode
        if detail:
            label.append("\n      ", style=FG4)
            label.append(_truncate(detail, 52), style=FG4)
    selected_glyph = ui_symbol("selected", preferences=preferences)
    indicator = Text(f"{selected_glyph} ", style=f"bold {BLUE}") if is_selected else Text("  ")
    duration_text = Text(duration, style=FG4)
    if comfortable and subagent.mode:
        duration_text.append("\n", style=FG4)
        duration_text.append(subagent.mode, style=FG4)
    return (
        indicator,
        Text("o" if preferences.glyphs is UiGlyphs.ASCII else "●", style=f"bold {GREEN}"),
        label,
        duration_text,
        Text("", style=FG4),
    )


def _shorten_tool_call_id(tool_call_id: str) -> str:
    # Copilot CLI ids look like ``call_xx3xSlNrWPvwRsjbTuFv5cN1``.
    # The prefix is noise in the dashboard; trim to the last 6 chars.
    if not tool_call_id:
        return ""
    tail = tool_call_id.rsplit("_", 1)[-1] if "_" in tool_call_id else tool_call_id
    return f"#{tail[-6:]}"


def _format_subagent_duration(subagent: DashboardSubAgentView) -> str:
    if subagent.completed_at is not None:
        elapsed = (subagent.completed_at - subagent.started_at).total_seconds()
    else:
        now = datetime.now(UTC)
        started = subagent.started_at
        if started.tzinfo is None:
            started = started.replace(tzinfo=UTC)
        elapsed = (now - started).total_seconds()
    if elapsed < 0:
        elapsed = 0
    if elapsed < 60:
        return f"{int(elapsed)}s"
    if elapsed < 3600:
        return f"{int(elapsed // 60)}m"
    return f"{int(elapsed // 3600)}h"


class AgentDetailPanel(Static):
    """Selected-agent focus section — grouped fields, compact header."""

    def set_agent(self, agent: DashboardSelectedAgentView | None) -> None:
        preferences = resolve_ui_preferences(self)
        result = Text()
        if agent is None:
            _section_header(result, "agent detail", preferences=preferences)
            result.append("  no agent selected\n", style=FG4)
            self.update(result)
            return
        item = agent.item
        operator_status = _resolved_operator_status(item)
        _, status_style = _status_display(item)
        bold_status_style = (
            status_style if status_style.startswith("bold ") else f"bold {status_style}"
        )
        plain_status_style = status_style.removeprefix("bold ").strip() or FG2

        # ── dominant banner: agent identity + canonical status ──
        # Operators reported the previous one-line "name | status"
        # header was visually indistinguishable from the metadata rows
        # below it, so the eye had to scan the whole panel to figure
        # out *which* agent was selected and *what* it was doing. A
        # heavier banner (large glyph, all-caps name, uppercase
        # canonical status, severity-colored bar) makes the answer
        # obvious before the eye reaches the body fields.
        glyph_char, glyph_color = status_glyph_parts(item.status, preferences=preferences)
        bar_glyph = "│" if preferences.glyphs is UiGlyphs.RICH else "|"
        result.append(f" {bar_glyph} ", style=f"bold {plain_status_style}")
        result.append(f"{glyph_char} ", style=f"bold {glyph_color}")
        result.append(item.name.upper(), style=f"bold {FG}")
        result.append("   ")
        result.append(operator_status.display_label, style=bold_status_style)
        result.append("\n")

        # ── secondary line: short context (task, activity, reason) ──
        # Picks the most operator-relevant string in priority order so
        # the banner subtitle answers "what is this agent doing right
        # now?" without forcing the operator to read further.
        subtitle = _detail_subtitle(item, operator_status)
        if subtitle:
            result.append(f" {bar_glyph} ", style=f"bold {plain_status_style}")
            result.append(subtitle, style=FG1)
            result.append("\n")

        # ── task line: high-level work the agent was assigned ──
        # Operators want a separate "Task:" field so they can read
        # "what is this agent supposed to be doing" independent of
        # the live tool activity. Only render when the title is
        # genuinely distinct from the subtitle (which usually carries
        # the live activity), otherwise we duplicate the same text.
        task_title = (item.task_title or "").strip()
        if task_title and task_title != subtitle:
            result.append(f" {bar_glyph} ", style=f"bold {plain_status_style}")
            result.append("Task: ", style=FG4)
            result.append(_truncate(task_title, 64), style=FG3)
            result.append("\n")

        usage_badges = _usage_badges(item)
        if usage_badges:
            result.append(f" {bar_glyph} ", style=f"bold {plain_status_style}")
            _append_badges(result, usage_badges, separator=item_separator(preferences))
            result.append("\n")
        result.append("\n")

        # ── activity line (pulled from former ActivityPanel) ──
        # Only show when activity differs from the banner subtitle and
        # the task title above so we don't duplicate the same string
        # in three consecutive lines.
        activity = item.current_activity or operator_status.reason
        if activity and activity != subtitle and activity != task_title:
            result.append("  ")
            # Activity arrow is a quiet directional cue, not a primary
            # action. Demote to FG3 so it reads as muted prefix.
            result.append(
                f"{ui_symbol('detail-arrow', preferences=preferences)} ",
                style=FG3,
            )
            result.append(activity, style=FG1)
            result.append("\n")

        # ── attention line — always visible when the agent needs it ──
        # The activity line above may be populated (e.g. "Reviewing layout"),
        # which used to mask the operator-visible reason ("waiting for
        # confirmation", "merge conflict", "runaway cost"). Surface the
        # reason on its own line so the user can act without scrolling
        # through the recent events list.
        if (
            item.needs_attention
            and item.attention_reason
            and item.attention_reason != activity
            and item.attention_reason != subtitle
        ):
            result.append("  ")
            result.append("! ", style=f"bold {status_style}")
            result.append(item.attention_reason, style=status_style)
            result.append("\n")

        # ── compact metadata rows ──
        # Graphite redesign: branch / repo / worktree / window / pane
        # are all metadata. Move them to the FG2 family so colour stays
        # reserved for the status banner above and the primary action
        # chip below. The previous PURPLE branch + BLUE pane chips made
        # the detail panel look like a kids' colouring book.
        _append_inline_fields(
            result,
            (
                ("branch", item.branch, FG2),
                ("repo", item.repo_name, FG2),
                ("worktree", item.worktree_name, FG2),
            ),
            preferences=preferences,
        )

        session_display = _format_session(agent)
        if len(session_display) > 32:
            session_display = session_display[:29] + "…"
        copilot_id = agent.copilot_session_id or ""
        if copilot_id and len(copilot_id) > 18:
            copilot_id = copilot_id[:15] + "…"
        _append_inline_fields(
            result,
            (
                ("session", session_display, FG4),
                ("copilot", copilot_id, FG4),
                ("event", _humanize_event_kind(agent.latest_event_kind), FG2),
            ),
            preferences=preferences,
        )
        _append_inline_fields(
            result,
            (
                ("window", item.window_name, FG2),
                ("pane", item.pane_id, FG2),
            ),
            preferences=preferences,
        )

        token_total = _resolved_token_total(item)
        cost = _format_cost(item.estimated_cost_usd)
        pulse = item.sparkline if item.sparkline.strip() else ""
        _append_inline_fields(
            result,
            (
                ("uptime", _format_duration(item.started_at), FG2),
                ("idle", _format_idle(item.idle_seconds), FG2),
                ("pulse", pulse, FG2),
            ),
            preferences=preferences,
        )

        if (
            token_total is not None
            or item.token_input is not None
            or item.token_output is not None
            or cost != "-"
        ):
            # Usage rows: the field-row helper carries its own muted
            # label tier. Values are numeric metadata, not state — keep
            # them all in FG / FG2 so the section reads as one calm
            # block instead of a four-colour stoplight.
            result.append("  usage\n", style=f"bold {FG3}")
            _field_row(
                result,
                "total",
                f"{token_total:,}" if token_total is not None else None,
                FG,
            )
            _field_row(
                result,
                "input",
                f"{item.token_input:,}" if item.token_input is not None else None,
                FG2,
            )
            _field_row(
                result,
                "output",
                f"{item.token_output:,}" if item.token_output is not None else None,
                FG2,
            )
            _field_row(result, "cost", cost if cost != "-" else None, FG2)

        # ── recent parsed events (deduplicated) ──
        if agent.recent_events:
            seen: set[str] = set()
            unique: list[str] = []
            for event in agent.recent_events:
                if event not in seen:
                    seen.add(event)
                    unique.append(event)
            visible_events = tuple(_truncate(event, 28) for event in unique[-2:])
            result.append("  recent ", style=FG4)
            for index, event in enumerate(visible_events):
                if index:
                    result.append(item_separator(preferences), style=FG4)
                result.append(event, style=_event_color(event))
            result.append("\n")

        # ── subtasks section ──
        _render_subtask_section(
            result,
            item.subtask_count,
            agent.sub_tasks,
            preferences=preferences,
        )
        _render_action_shortcuts(
            result,
            (
                (("p", "console"), ("m", "message"), ("v", "live"), ("i", "interrupt")),
                (("K", "kill pane"), ("R", "rename"), ("W", "move"), ("w", "worktree")),
                (("l", "logs"), ("S", "stop visible")),
            ),
            preferences=preferences,
            primary=_primary_action_for(operator_status.kind),
        )

        self.update(result)

    def set_subagent(self, subagent: DashboardSubAgentView | None) -> None:
        """Render sub-agent focus.

        Background sub-agents don't have their own session directory:
        the launch ack is a useless one-liner, and the real picture is
        the read_agent interactions + terminal metrics the parent
        records. For them we render a structured metrics-and-
        interactions block. Everything else (foreground sub-agents,
        ones we have no metrics for yet) keeps the prompt+result
        rendering so we don't regress on the common case.
        """
        preferences = resolve_ui_preferences(self)
        result = Text()
        _section_header(result, "sub-agent detail", preferences=preferences)
        if subagent is None:
            result.append("  no sub-agent selected\n", style=FG4)
            self.update(result)
            return

        is_running = subagent.is_running
        is_failure = subagent.success is False or subagent.error_message is not None
        if preferences.glyphs is UiGlyphs.ASCII:
            glyph = ">" if is_running else ("x" if is_failure else "v")
        else:
            glyph = "▶" if is_running else ("✗" if is_failure else "✓")
        glyph_color = AQUA if is_running else (SEVERITY_ERROR if is_failure else GREEN)
        status_label = "running" if is_running else ("failed" if is_failure else "completed")
        status_style = glyph_color

        result.append(f"  {glyph} ", style=f"bold {glyph_color}")
        name = subagent.task_name or subagent.display_name
        result.append(name, style=f"bold {FG}")
        result.append("  ")
        result.append(status_label, style=status_style)
        if subagent.mode:
            result.append("  ")
            result.append(f"[{subagent.mode}]", style=FG4)
        result.append("\n")

        if subagent.description:
            result.append("  ")
            # Match the dashboard activity arrow: muted prefix.
            result.append(
                f"{ui_symbol('detail-arrow', preferences=preferences)} ",
                style=FG3,
            )
            result.append(subagent.description, style=FG1)
            result.append("\n")

        result.append("\n")
        # Sub-agent type / id / duration are metadata. Keep PURPLE for
        # the type field would re-introduce the rainbow we're trying
        # to remove, so move everything to FG2.
        _field_row(result, "type", subagent.agent_type or subagent.agent_name, FG2)
        _field_row(result, "id", subagent.tool_call_id[:16], FG4)
        _field_row(result, "duration", _format_subagent_duration(subagent), FG2)

        has_structured = _has_structured_signals(subagent)
        if has_structured:
            _render_subagent_metrics(result, subagent)

        if subagent.prompt:
            result.append("\n")
            result.append("  input\n", style=f"bold {FG3}")
            snippet = _truncate(subagent.prompt, 800)
            for line in snippet.splitlines() or [snippet]:
                result.append(f"  {line}\n", style=FG2)

        if has_structured:
            _render_subagent_interactions(result, subagent, preferences=preferences)
        elif subagent.result_content:
            result.append("\n")
            # For background agents the parent's "result" is only a
            # launch acknowledgement — the real agent output lives in
            # the sub-agent's own session. Label it so operators don't
            # mistake the ack for the actual result.
            is_background = (subagent.mode or "").lower() == "background"
            if is_background and is_running:
                header = "  launch ack (background — output lives in sub-session)\n"
            elif is_background:
                header = "  launch ack\n"
            else:
                header = "  output\n"
            result.append(header, style=f"bold {FG3}")
            snippet = _truncate(subagent.result_content, 1200)
            for line in snippet.splitlines() or [snippet]:
                result.append(f"  {line}\n", style=FG1)

        _render_action_shortcuts(
            result,
            (
                (("p", "parent console"), ("i", "stop parent"), ("K", "kill parent pane")),
                (("w", "parent worktree"), ("l", "logs")),
            ),
            preferences=preferences,
        )

        self.update(result)


def _has_structured_signals(subagent: DashboardSubAgentView) -> bool:
    """True when we have anything richer than the launch ack.

    Metrics, an error, or observed read_agent interactions all count.
    When none of these are present we fall back to the prompt+result
    rendering — there's nothing new to show.
    """
    return bool(
        subagent.read_interactions
        or subagent.total_tokens is not None
        or subagent.duration_ms is not None
        or subagent.total_tool_calls is not None
        or subagent.model
        or subagent.error_message
    )


def _render_subagent_metrics(text: Text, subagent: DashboardSubAgentView) -> None:
    if subagent.duration_ms is not None:
        _field_row(text, "elapsed", _format_duration_ms(subagent.duration_ms), FG2)
    if subagent.total_tokens is not None:
        _field_row(text, "tokens", f"{subagent.total_tokens:,}", FG)
    if subagent.total_tool_calls is not None:
        _field_row(text, "tools", str(subagent.total_tool_calls), FG2)
    if subagent.model:
        _field_row(text, "model", subagent.model, FG2)
    if subagent.error_message:
        text.append("  error     ", style=FG3)
        text.append(f"{_truncate(subagent.error_message, 400)}\n", style=SEVERITY_ERROR)


def _render_subagent_interactions(
    text: Text,
    subagent: DashboardSubAgentView,
    *,
    preferences: UiPreferences,
) -> None:
    interactions = subagent.read_interactions
    text.append("\n")
    text.append(f"  interactions ({len(interactions)})\n", style=f"bold {FG3}")
    if not interactions:
        text.append("  —\n", style=FG4)
        return
    # Cap what we render so a runaway coordinator can't push everything
    # else off-screen. The adapter already caps in-memory storage.
    visible = interactions[-10:]
    for interaction in visible:
        ts = interaction.timestamp.astimezone(UTC).strftime("%H:%M:%S")
        text.append(f"  {ui_symbol('collapsed', preferences=preferences)} ", style=FG3)
        text.append(ts, style=FG4)
        text.append("  read_agent(", style=FG2)
        text.append(interaction.arguments_summary, style=FG1)
        text.append(")\n", style=FG2)
        if interaction.result_content:
            snippet = _truncate(interaction.result_content, 400)
            lines = snippet.splitlines() or [snippet]
            for line in lines[:4]:
                text.append("      ", style=FG4)
                text.append(f"{line}\n", style=FG2)


def _format_duration_ms(duration_ms: int) -> str:
    if duration_ms < 1000:
        return f"{duration_ms}ms"
    seconds = duration_ms / 1000
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes = int(seconds // 60)
    remainder = int(seconds - minutes * 60)
    return f"{minutes}m{remainder:02d}s"


def _field_row(text: Text, label: str, value: str | None, style: str) -> None:
    """Render a single labeled field row. Skip if value is empty."""
    if not value or value == "-":
        return
    text.append(f"  {label:<10}", style=FG4)
    text.append(f"{value}\n", style=style)


def _append_inline_fields(
    text: Text,
    fields: Sequence[tuple[str, str | None, str]],
    *,
    preferences: UiPreferences,
) -> None:
    visible = tuple(
        (label, value, style) for label, value, style in fields if value and value != "-"
    )
    if not visible:
        return
    text.append("  ")
    for index, (label, value, style) in enumerate(visible):
        if index:
            text.append(pipe_separator(preferences), style=FG4)
        text.append(label, style=FG4)
        text.append(" ", style=FG4)
        text.append(value, style=style)
    text.append("\n")


_SUBTASK_STATUS_STYLES: dict[str, str] = {
    "running": f"bold {GREEN}",
    "completed": FG4,
    "idle": YELLOW,
    "failed": SEVERITY_ERROR,
    "cancelled": FG4,
}


def _render_subtask_section(
    text: Text,
    count: int,
    tasks: tuple[DashboardSubTaskView, ...],
    *,
    preferences: UiPreferences,
) -> None:
    """Render subtasks section in the detail panel."""
    if count <= 0:
        return
    text.append("\n")
    _section_header(text, "subtasks", preferences=preferences)
    text.append(
        f"  {ui_symbol('background-task', preferences=preferences)} ", style=f"bold {YELLOW}"
    )
    text.append(f"{count} background task{'s' if count != 1 else ''}\n", style=FG1)

    if not tasks:
        text.append("  ", style="")
        text.append("details unavailable from parent pane\n", style=f"italic {FG4}")
        return

    known = len(tasks)
    if known < count:
        text.append(f"  known details for {known} of {count}\n", style=FG4)

    for i, task in enumerate(tasks):
        is_last = i == len(tasks) - 1
        connector = ui_symbol(
            "connector-last" if is_last else "connector-mid",
            preferences=preferences,
        )
        text.append(f"  {connector} ", style=FG4)
        # Sub-task type label is metadata; status colour next to it is
        # what carries state. Keep type in primary text to avoid the
        # rainbow effect.
        text.append(task.agent_type_label, style=f"bold {FG}")
        if task.model:
            short_model = task.model.split("-")[-1] if "-" in task.model else task.model
            text.append(f" ({short_model})", style=FG3)
        text.append("  ")
        style = _SUBTASK_STATUS_STYLES.get(task.status, FG2)
        text.append(task.status, style=style)
        if task.description:
            desc = task.description[:40] + "…" if len(task.description) > 40 else task.description
            text.append(f"  {desc}", style=FG4)
        text.append("\n")

    unknown = count - known
    if unknown > 0:
        connector = ui_symbol("connector-last", preferences=preferences)
        text.append(f"  {connector} {unknown} task{'s' if unknown != 1 else ''} ", style=FG4)
        text.append("(details unknown)\n", style=f"italic {FG4}")


_PRIMARY_ACTION_BY_KIND: dict[OperatorStatusKind, tuple[str, str]] = {
    # The agent has a question for the operator. Send a message
    # immediately rather than opening the console (which is a slower
    # context switch). The message screen also surfaces the existing
    # output so the operator has the context they need.
    OperatorStatusKind.WAITING_INPUT: ("m", "send message"),
    # The agent has work the operator should look at — review the
    # diff/output before deciding what to do next.
    OperatorStatusKind.REVIEW_READY: ("p", "open console"),
    # Stuck-but-running. Resume kicks the agent forward; the operator
    # can interrupt afterwards if the resume reveals a real problem.
    OperatorStatusKind.BLOCKED: ("R", "resume"),
    # Hands-off agents the operator probably wants to peek at.
    OperatorStatusKind.STALE: ("p", "open console"),
    OperatorStatusKind.FAILED: ("p", "open console"),
    # Healthy agents — the most common interaction is opening the
    # live mirror to confirm what the agent is doing.
    OperatorStatusKind.WORKING: ("v", "live mirror"),
    OperatorStatusKind.STARTING: ("v", "live mirror"),
    # Terminal states. ``c`` (mark complete) is the one operator
    # action that meaningfully changes the dashboard; opening or
    # killing terminated panes is rarely the right next step.
    OperatorStatusKind.COMPLETED: ("c", "mark complete"),
    OperatorStatusKind.TERMINATED: ("c", "mark complete"),
}


def _primary_action_for(kind: OperatorStatusKind) -> tuple[str, str] | None:
    """Return the contextual primary action for the selected agent.

    See ``_PRIMARY_ACTION_BY_KIND`` for the per-status mapping.
    Returns ``None`` for kinds we deliberately leave un-suggested so
    the operator falls back to the generic ACTIONS list.
    """
    return _PRIMARY_ACTION_BY_KIND.get(kind)


def _render_action_shortcuts(
    text: Text,
    rows: Sequence[Sequence[tuple[str, str]]],
    *,
    preferences: UiPreferences,
    primary: tuple[str, str] | None = None,
) -> None:
    """Render an ``ACTIONS`` section with optional state-aware primary action.

    Empty rows are dropped before the section header is written so we
    never produce an "ACTIONS" header with no body — operators read
    that as broken UI rather than as "no actions available".

    ``primary``, when supplied, is rendered on its own line at the
    top of the section in a brighter accent style. It expresses the
    one action the operator is most likely to want next given the
    selected agent's state (resume on stale, message on waiting,
    open on running, …) so the dashboard answers "what should I do
    now?" without forcing the operator to scan every shortcut.
    """
    visible_rows = [row for row in rows if row]
    if not visible_rows and primary is None:
        return
    text.append("\n")
    _section_header(text, "actions", preferences=preferences)
    if primary is not None:
        key, label = primary
        text.append("  ▸ ", style=f"bold {AQUA}")
        text.append(key, style=f"bold {AQUA}")
        text.append(f" {label}", style=f"bold {FG}")
        text.append("   primary\n", style=FG4)
    for row in visible_rows:
        text.append("  ", style=FG4)
        for index, (key, label) in enumerate(row):
            if index:
                text.append(item_separator(preferences), style=FG4)
            text.append(key, style=f"bold {BLUE}")
            text.append(f" {label}", style=FG2)
        text.append("\n")


def _truncate(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[: limit - 1].rstrip() + "…"


class LogPreviewPanel(Static):
    """Recent pane output rendered with the same intent as ``tail -f``.

    Two render paths share the panel:

    * **Raw tmux capture** — the dashboard's live-tail loop writes
      ``tmux_capture`` rows that are the unfiltered ``capture-pane
      -p -e -J`` output. We render those via :meth:`Text.from_ansi`
      so colours, dim/bold runs, embedded tables, and prompt glyphs
      reach the operator unchanged. No per-line timestamp prefix,
      no source badge, no severity re-colouring — the section header
      carries the freshness metadata so the body reads as a faithful
      replica of the actual tmux pane.

    * **Stored log chunks** — when the live tail isn't wired up
      (e.g. agent without a tmux pane), the controller falls back to
      ``log_chunks`` rows from SQLite. Those keep the legacy
      timestamp / source-badge formatting because the data shape is
      different and the panel is acting as a discrete log viewer
      rather than a pane mirror.

    The panel is a non-scrolling ``Static`` that pins the most recent
    output to the *top* of its render. If the supplied preview
    overflows the panel's visible height, ``Static`` would clip from
    the bottom — hiding the freshest lines (the opposite of a
    ``tail -f`` panel). To fix that we cache the last view, render
    with ``no_wrap=True`` so each preview line is exactly one visual
    row, and tail the preview to the available rows on every paint
    plus on resize. The result is that the last N lines that
    physically fit in the panel are always visible, and longer
    history is only ever truncated from the *top*.
    """

    _DEFAULT_PREVIEW_ROWS = 24

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self._cached_agent: DashboardSelectedAgentView | None = None
        # Track when the *content* last changed so the header can
        # show "unchanged for Ns" without lying — the live-tail loop
        # ticks on a fixed interval and resends identical text when
        # the pane is idle. We stamp the actual change time, not the
        # capture time.
        self._raw_signature: tuple[str, ...] | None = None
        self._raw_last_change_at: datetime | None = None
        self._raw_last_seen_at: datetime | None = None

    def set_logs(self, agent: DashboardSelectedAgentView | None) -> None:
        self._cached_agent = agent
        preferences = resolve_ui_preferences(self)
        if agent is None:
            self._raw_signature = None
            self._raw_last_change_at = None
            self._raw_last_seen_at = None
            result = Text(no_wrap=True, overflow="ellipsis")
            _section_header(result, "output preview", preferences=preferences)
            result.append("  no recent output\n", style=FG4)
            self.update(result)
            return
        if not agent.log_preview:
            self._raw_signature = None
            self._raw_last_change_at = None
            self._raw_last_seen_at = None
            result = Text(no_wrap=True, overflow="ellipsis")
            _section_header(result, "output preview", preferences=preferences)
            if _resolved_operator_status(agent.item).kind is OperatorStatusKind.STARTING:
                # "Waiting for first output" is a transient placeholder.
                # Keep it muted so it doesn't compete with steady-state
                # log content.
                result.append("  launching — waiting for first output…\n", style=FG3)
            else:
                result.append("  no recent output\n", style=FG4)
            self.update(result)
            return

        if _is_raw_tmux_capture(agent.log_preview):
            self._render_raw_tmux(agent, preferences=preferences)
            return

        # Legacy stored-log render path — kept for non-tmux sources
        # (e.g. assistant transcript falling back to SQLite log_chunks
        # when the agent has no live pane). Reset the raw freshness
        # state so we don't carry stale "unchanged for Ns" data into
        # a future raw render.
        self._raw_signature = None
        self._raw_last_change_at = None
        self._raw_last_seen_at = None
        self._render_stored_logs(agent, preferences=preferences)

    def _render_raw_tmux(
        self,
        agent: DashboardSelectedAgentView,
        *,
        preferences: UiPreferences,
    ) -> None:
        # Most recent capture wins for "last update time" — even when
        # the content is identical to the previous tick, the captured
        # timestamp tells us tmux *was* polled. The content signature
        # tracks whether the *bytes* changed, which is a separate
        # signal the operator wants ("pane is alive but idle").
        last_line = agent.log_preview[-1]
        latest_seen_at = last_line.captured_at
        signature = tuple(line.content for line in agent.log_preview)
        if signature != self._raw_signature:
            self._raw_signature = signature
            self._raw_last_change_at = latest_seen_at
        self._raw_last_seen_at = latest_seen_at

        # ``no_wrap=True`` keeps each preview line on a single visual
        # row so the row-budget computation below is exact. Long
        # rows are truncated horizontally rather than wrapping into
        # the next row and pushing freshly-arrived rows out of the
        # bottom of the panel. tmux already laid the pane out at its
        # own width; if the panel is narrower, ellipsis is honest.
        result = Text(no_wrap=True, overflow="ellipsis")
        _raw_output_header(
            result,
            preferences=preferences,
            last_seen_at=latest_seen_at,
            last_change_at=self._raw_last_change_at,
        )

        height = self.size.height
        # ``-3`` reserves rows for the header line, the freshness
        # subtitle, and one row of bottom padding so the last preview
        # line is never visually touching the panel border. Leaves
        # room for a one-line truncation footer when present.
        budget = max(height - 3, 1) if height > 0 else self._DEFAULT_PREVIEW_ROWS
        total = len(agent.log_preview)
        truncated = total > budget
        if truncated:
            # Reserve one more row for the "showing last N · scroll"
            # footer so the footer doesn't push the freshest line
            # off-screen.
            budget = max(budget - 1, 1)
        visible_lines = agent.log_preview[-budget:]
        for line in visible_lines:
            # ``Text.from_ansi`` parses CSI SGR sequences into Rich
            # spans, preserving colours, bold, dim, italic, and the
            # box-drawing colours agents draw tables with. Empty
            # rows render as an actual blank line — the visual gap
            # that tmux laid out for the operator stays intact.
            rendered = Text.from_ansi(line.content, no_wrap=True, overflow="ellipsis")
            result.append("  ")
            result.append_text(rendered)
            result.append("\n")
        if truncated:
            shown = len(visible_lines)
            footer = f"  showing last {shown} of {total} lines · ↑ for full pane\n"
            result.append(footer, style=FG4)
        self.update(result)

    def _render_stored_logs(
        self,
        agent: DashboardSelectedAgentView,
        *,
        preferences: UiPreferences,
    ) -> None:
        result = Text(no_wrap=True, overflow="ellipsis")
        _section_header(result, "output preview", preferences=preferences)
        height = self.size.height
        budget = max(height - 2, 1) if height > 0 else self._DEFAULT_PREVIEW_ROWS
        visible_lines = agent.log_preview[-budget:]
        last_ts = ""
        src_map = {
            "stdout": "out",
            "stderr": "err",
            "tmux": "tmx",
            "tmux_capture": "tmx",
            "assistant": "ai",
            "user": "you",
            "system": "sys",
        }
        for line in visible_lines:
            ts = format_short_timestamp(line.captured_at)
            src = src_map.get(line.source, line.source[:3])
            source_style = _LOG_SOURCE_STYLES.get(line.source, FG2)
            content = _strip_ansi(line.content)
            # Timestamp grouping: show only when time changes
            if ts != last_ts:
                result.append(f"  {ts} ", style=FG4)
                last_ts = ts
            else:
                result.append("           ", style=FG4)
            result.append(f"{src:<3} ", style=source_style)
            # Light syntax highlighting for log content
            result.append(_highlight_log_line(content, source_style))
            result.append("\n")
        self.update(result)

    def on_resize(self, _event: events.Resize) -> None:
        # Re-tail the preview to the new height so the freshest lines
        # remain visible after a layout change (e.g. terminal resize,
        # toggling the help overlay, switching UI density preset).
        if self._cached_agent is not None:
            self.set_logs(self._cached_agent)


def _is_raw_tmux_capture(lines: Sequence[DashboardLogLineView]) -> bool:
    """Heuristic: every line came from the live tmux capture loop.

    The dashboard's live-tail path tags every emitted line with the
    ``tmux_capture`` source. Stored log_chunks use ``stdout`` /
    ``stderr`` / ``assistant`` etc. We require *all* visible lines to
    be tmux_capture so a transient mixed batch can't accidentally
    silence the freshness header.
    """
    return bool(lines) and all(line.source == "tmux_capture" for line in lines)


def _raw_output_header(
    result: Text,
    *,
    preferences: UiPreferences,
    last_seen_at: datetime,
    last_change_at: datetime | None,
) -> None:
    """Render the OUTPUT PREVIEW · raw tmux pane header + freshness line."""
    result.append(ui_symbol("section-lead", preferences=preferences), style=FG4)
    result.append("OUTPUT PREVIEW", style=f"bold {FG3}")
    result.append(" · ", style=FG4)
    result.append("raw tmux pane", style=FG3)
    result.append(ui_symbol("section-fill", preferences=preferences), style=FG4)
    result.append("\n", style=FG4)

    now = datetime.now(UTC)
    seen = format_short_timestamp(last_seen_at)
    parts = [f"updated {seen}"]
    age = (now - last_seen_at).total_seconds()
    if age >= 2:
        parts.append(f"{_format_delta_seconds(age)} ago")
    if last_change_at is not None and last_change_at != last_seen_at:
        unchanged_for = (last_seen_at - last_change_at).total_seconds()
        if unchanged_for >= 2:
            parts.append(f"unchanged for {_format_delta_seconds(unchanged_for)}")
    result.append("  " + " · ".join(parts) + "\n", style=FG4)


def _format_delta_seconds(seconds: float) -> str:
    """Compact ``Ns`` / ``NmMs`` / ``NhMm`` for the freshness header."""
    total = int(max(seconds, 0))
    if total < 60:
        return f"{total}s"
    if total < 3600:
        return f"{total // 60}m{total % 60:02d}s"
    return f"{total // 3600}h{(total % 3600) // 60:02d}m"


def _highlight_log_line(content: str, default_style: str) -> Text:
    """Apply light syntax highlighting to log line content."""
    text = Text()
    lower = content.lower()
    if any(kw in lower for kw in ("error", "fail", "traceback", "exception")):
        text.append(content, style=f"bold {SEVERITY_ERROR}")
    elif any(kw in lower for kw in ("warning", "warn", "deprecat")):
        text.append(content, style=YELLOW)
    elif content.startswith(("●", "✓", "✗", "│", "└", "├")):
        # Tool calls / tree output. The tree art is its own visual cue;
        # the graphite redesign keeps these in the muted-label tier so
        # the log preview reads as one calm block instead of a
        # rainbow.
        text.append(content, style=FG3)
    elif content.startswith(("$", ">", "λ")):
        # Command prompts — secondary signal, but worth a quiet hint.
        text.append(content, style=f"bold {FG2}")
    else:
        text.append(content, style=FG3)
    return text


class AlertPanel(Static):
    """Active attention items — rendered as a loud "needs attention" banner.

    The dashboard's primary job is to answer "what needs my action
    right now?". The previous "ALERTS" header sat at the bottom of the
    sidebar in the same neutral typography as every other section,
    which buried even critical-severity items below the agent output.

    This widget now:

    - Renames the section to **NEEDS ATTENTION** (matching the
      operator vocabulary the rest of the UI uses).
    - Adds a severity-coloured left bar to each row so the eye lands
      on actionable items first.
    - Collapses to ``display: none`` (via ``add_class('empty')``)
      when there is nothing to show, returning the vertical space to
      the output panel below it. The previous "no active alerts"
      placeholder wasted space telling the operator that the system
      had nothing to tell them.
    """

    def set_alerts(self, alerts: Sequence[DashboardAlertView]) -> None:
        preferences = resolve_ui_preferences(self)
        if not alerts:
            # Empty alert state — collapse the panel so the output
            # panel can claim the vertical real estate. The CSS
            # rule for ``.empty`` on ``#dashboard-alerts`` sets
            # ``display: none``.
            self.add_class("empty")
            self.update(Text(""))
            return
        self.remove_class("empty")
        joined = Text()
        _section_header(joined, "needs attention", preferences=preferences)
        bar_glyph = "│" if preferences.glyphs is UiGlyphs.RICH else "|"
        for alert in alerts[:5]:
            severity_style = _SEVERITY_STYLES.get(alert.severity, FG3)
            short_sev = alert.severity[:4].upper()
            joined.append(f" {bar_glyph} ", style=f"bold {severity_style}")
            joined.append(f"{short_sev:<4}", style=f"bold {severity_style}")
            joined.append(f" {alert.agent_name}", style=f"bold {FG1}")
            joined.append(f"  {alert.message}\n", style=FG3)
        self.update(joined)


__all__ = [
    "AgentDetailPanel",
    "AgentListPanel",
    "AlertPanel",
    "FilterBar",
    "LogPreviewPanel",
    "StatusBar",
]

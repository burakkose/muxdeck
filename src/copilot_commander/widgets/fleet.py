from __future__ import annotations

from collections.abc import Sequence

from rich.table import Table
from rich.text import Text
from textual.widgets import Static

from copilot_commander.controllers.fleet_controller import (
    FleetHistoryMetricView,
    FleetRecentActivityView,
    FleetRepoGroupView,
    FleetResourceView,
    FleetSearchHelperView,
    FleetSearchHitView,
    FleetState,
)
from copilot_commander.theme import AQUA, BLUE, FG, FG3, FG4, GREEN, ORANGE, RED, YELLOW
from copilot_commander.widgets.common import format_short_timestamp, status_glyph_char

_SEVERITY_STYLES = {
    "info": FG,
    "warning": f"bold {ORANGE}",
    "error": f"bold {RED}",
}

_TONE_STYLES = {
    "healthy": GREEN,
    "warning": ORANGE,
    "critical": RED,
}


class FleetSummaryBar(Static):
    def set_state(self, state: FleetState) -> None:
        health = state.health
        line = Text()
        line.append(" fleet ", style=FG4)
        line.append(health.message, style=f"bold {_TONE_STYLES[health.tone]}")
        line.append("  │  ", style=FG4)
        line.append(f"groups {state.total_groups}", style=FG)
        line.append("  │  ", style=FG4)
        line.append(f"visible {state.total_visible_agents}", style=FG)
        line.append("  │  ", style=FG4)
        line.append(f"attention {health.attention_agents}", style=f"bold {ORANGE}")
        line.append("  │  ", style=FG4)
        line.append(f"dirty {health.dirty_worktrees}", style=f"bold {YELLOW}")
        if health.orphan_local_sessions:
            line.append("  │  ", style=FG4)
            line.append(f"orphans {health.orphan_local_sessions}", style=f"bold {ORANGE}")
        self.update(line)


class FleetGroupsPanel(Static):
    def set_groups(self, groups: Sequence[FleetRepoGroupView]) -> None:
        if not groups:
            self.update(Text("No fleet groups match the current filter", style=FG4))
            return
        table = Table(box=None, expand=True, padding=(0, 1), header_style=f"bold {FG4}")
        table.add_column("Repo", ratio=2, no_wrap=True)
        table.add_column("Ag", width=3, justify="right")
        table.add_column("!", width=3, justify="right")
        table.add_column("WT", width=4, justify="right")
        table.add_column("Sess", width=5, justify="right")
        table.add_column("Lead tasks", ratio=4)
        for group in groups:
            lead_agents = "; ".join(
                f"{status_glyph_char(agent.status)} {agent.name} · {agent.task_title[:28]}"
                for agent in group.agents[:3]
            )
            table.add_row(
                group.repo_label,
                str(group.agent_count),
                str(group.attention_count),
                str(group.worktree_count),
                str(group.session_count + group.orphan_local_session_count),
                lead_agents,
            )
        self.update(table)


class FleetHistoryPanel(Static):
    def set_history(
        self,
        metrics: Sequence[FleetHistoryMetricView],
        recent_activity: Sequence[FleetRecentActivityView],
    ) -> None:
        content = Text()
        content.append(" analytics\n", style=f"bold {BLUE}")
        for metric in metrics:
            content.append(f"{metric.label:<13}", style=FG4)
            content.append(metric.value, style=f"bold {FG}")
            content.append(f"  {metric.detail}\n", style=FG3)
        content.append("\n recent\n", style=f"bold {BLUE}")
        if not recent_activity:
            content.append("no recent fleet activity", style=FG4)
        for item in recent_activity:
            content.append(f"{format_short_timestamp(item.occurred_at):<9}", style=FG4)
            content.append(item.title, style=_SEVERITY_STYLES[item.severity])
            content.append(f"  {item.detail}\n", style=FG3)
        self.update(content)


class FleetSearchPanel(Static):
    def set_search(
        self,
        *,
        query: str | None,
        helpers: Sequence[FleetSearchHelperView],
        hits: Sequence[FleetSearchHitView],
    ) -> None:
        content = Text()
        content.append(" helpers\n", style=f"bold {BLUE}")
        if helpers:
            for helper in helpers:
                content.append(f"/{helper.query:<12}", style=f"bold {AQUA}")
                content.append(helper.label, style=f"bold {FG}")
                content.append(f" · {helper.match_count} · {helper.detail}\n", style=FG3)
        else:
            content.append("no helper shortcuts yet\n", style=FG4)
        content.append("\n search\n", style=f"bold {BLUE}")
        if not query:
            content.append(
                "type in the filter to search agents, worktrees, sessions, and local state",
                style=FG4,
            )
        elif not hits:
            content.append(f"no matches for {query!r}", style=FG4)
        else:
            for hit in hits:
                content.append(f"{hit.kind:<8}", style=FG4)
                content.append(hit.title, style=FG)
                content.append(f"\n         {hit.detail}\n", style=FG3)
        self.update(content)


class FleetResourcesPanel(Static):
    def set_resources(self, resources: Sequence[FleetResourceView]) -> None:
        content = Text()
        content.append(" resources\n", style=f"bold {BLUE}")
        for resource in resources:
            content.append(f"{resource.label:<14}", style=FG4)
            content.append(resource.value, style=f"bold {_TONE_STYLES[resource.tone]}")
            content.append(f"  {resource.detail}\n", style=FG3)
        self.update(content)


__all__ = [
    "FleetGroupsPanel",
    "FleetHistoryPanel",
    "FleetResourcesPanel",
    "FleetSearchPanel",
    "FleetSummaryBar",
]

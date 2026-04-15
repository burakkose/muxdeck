from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Final, Protocol, cast, runtime_checkable

from copilot_commander.domain.enums import AgentStatus
from copilot_commander.domain.models import Agent
from copilot_commander.domain.value_objects import ensure_aware_datetime, utc_now
from copilot_commander.exceptions import GitCommandError, TmuxCommandError
from copilot_commander.perf import timed
from copilot_commander.services.discovery_service import PaneDiscovery, PaneDiscoveryReport
from copilot_commander.services.monitoring_service import MonitoringDiscovery, MonitoringReport
from copilot_commander.types import Clock

_log = logging.getLogger(__name__)

_NON_REPOSITORY_SNIPPETS: Final[tuple[str, ...]] = (
    "not a git repository",
    "outside repository",
    "cannot chdir",
    "no such file or directory",
)


@runtime_checkable
class RuntimeDiscoveryPort(Protocol):
    def discover_panes(self) -> PaneDiscoveryReport:
        """Discover panes from tmux."""


@runtime_checkable
class RuntimeMonitoringPort(Protocol):
    def monitor_discoveries(
        self,
        discoveries: Sequence[MonitoringDiscovery],
        /,
    ) -> MonitoringReport:
        """Persist and evaluate discovery results."""


@runtime_checkable
class RuntimeGitPort(Protocol):
    def discover_repo_root(self, cwd: str | Path, /) -> Path:
        """Resolve the repository root for a working directory."""

    def current_branch(self, cwd: str | Path, /) -> str | None:
        """Resolve the current branch for a working directory."""


@runtime_checkable
class RuntimeAgentStore(Protocol):
    def list_agents(self) -> Sequence[Agent]:
        """Return all stored agents."""

    def upsert_agent(self, agent: Agent, /) -> None:
        """Update an existing agent record."""


@dataclass(frozen=True, slots=True)
class RuntimeSyncWarning:
    message: str
    pane_id: str | None = None


@dataclass(frozen=True, slots=True)
class RuntimeSyncReport:
    discovery_report: PaneDiscoveryReport | None = None
    monitoring_report: MonitoringReport | None = None
    warnings: tuple[RuntimeSyncWarning, ...] = ()
    error: str | None = None

    @property
    def observed_panes(self) -> int:
        if self.discovery_report is None:
            return 0
        return len(self.discovery_report.panes)

    @property
    def discovered_agents(self) -> int:
        if self.discovery_report is None:
            return 0
        return len(self.discovery_report.managed_agents) + len(
            self.discovery_report.unmanaged_probable_agents
        )

    @property
    def persisted_agents(self) -> int:
        if self.monitoring_report is None:
            return 0
        return len(self.monitoring_report.results)


class RuntimeSynchronizer:
    def __init__(
        self,
        discovery: RuntimeDiscoveryPort,
        monitoring: RuntimeMonitoringPort,
        git: RuntimeGitPort,
        *,
        agent_store: RuntimeAgentStore | None = None,
        dead_grace_period_sec: int = 10,
        clock: Clock = utc_now,
    ) -> None:
        self._discovery = discovery
        self._monitoring = monitoring
        self._git = git
        self._agent_store = agent_store
        self._dead_grace_period_sec = dead_grace_period_sec
        self._clock = clock

    def refresh(self) -> RuntimeSyncReport:
        try:
            with timed("sync.discovery"):
                discovery_report = self._discovery.discover_panes()
        except TmuxCommandError as exc:
            return RuntimeSyncReport(error=self._format_tmux_error(exc))

        warnings: list[RuntimeSyncWarning] = []
        with timed("sync.enrich"):
            enriched_panes = tuple(
                self._enrich_discovery(discovery, warnings=warnings)
                for discovery in discovery_report.panes
            )
        refreshed_report = PaneDiscoveryReport(
            discovered_at=discovery_report.discovered_at,
            panes=enriched_panes,
            managed_agents=tuple(
                pane for pane in enriched_panes if pane.classification == "managed_agent"
            ),
            unmanaged_probable_agents=tuple(
                pane for pane in enriched_panes if pane.classification == "unmanaged_probable_agent"
            ),
            non_agent_panes=tuple(
                pane for pane in enriched_panes if pane.classification == "non_agent_pane"
            ),
        )
        with timed("sync.monitoring"):
            monitoring_report = self._monitoring.monitor_discoveries(
                cast(Sequence[MonitoringDiscovery], refreshed_report.panes)
            )
        self._reap_stale_agents(refreshed_report, warnings=warnings)
        return RuntimeSyncReport(
            discovery_report=refreshed_report,
            monitoring_report=monitoring_report,
            warnings=tuple(warnings),
        )

    def _reap_stale_agents(
        self,
        report: PaneDiscoveryReport,
        *,
        warnings: list[RuntimeSyncWarning],
    ) -> None:
        """Mark agents as DEAD when their tmux pane no longer exists."""
        if self._agent_store is None:
            return
        live_pane_ids = frozenset(pane.snapshot.pane_id for pane in report.panes)
        now = ensure_aware_datetime(self._clock(), field_name="value")
        terminal_statuses = {AgentStatus.DEAD, AgentStatus.COMPLETED}
        for agent in self._agent_store.list_agents():
            if agent.status in terminal_statuses:
                continue
            if agent.tmux_pane_id in live_pane_ids:
                continue
            elapsed = (now - agent.last_seen_at).total_seconds()
            if elapsed < self._dead_grace_period_sec:
                continue
            dead_agent = Agent(
                id=agent.id,
                name=agent.name,
                tmux_session_name=agent.tmux_session_name,
                tmux_window_id=agent.tmux_window_id,
                tmux_window_name=agent.tmux_window_name,
                tmux_pane_id=agent.tmux_pane_id,
                pane_tty=agent.pane_tty,
                cwd=agent.cwd,
                repo_root=agent.repo_root,
                worktree_path=agent.worktree_path,
                branch=agent.branch,
                task_title=agent.task_title,
                task_summary=agent.task_summary,
                copilot_session_id=agent.copilot_session_id,
                pid=agent.pid,
                status=AgentStatus.DEAD,
                started_at=agent.started_at,
                last_activity_at=agent.last_activity_at,
                last_seen_at=agent.last_seen_at,
                idle_seconds=agent.idle_seconds,
                needs_attention=True,
                attention_reason="tmux pane no longer exists",
                token_input=agent.token_input,
                token_output=agent.token_output,
                token_total=agent.token_total,
                estimated_cost_usd=agent.estimated_cost_usd,
            )
            try:
                self._agent_store.upsert_agent(dead_agent)
                _log.info("reaped stale agent %s (pane %s)", agent.id, agent.tmux_pane_id)
            except Exception:
                _log.exception("failed to reap agent %s", agent.id)
                warnings.append(
                    RuntimeSyncWarning(
                        message=f"failed to reap stale agent {agent.id}",
                        pane_id=agent.tmux_pane_id,
                    )
                )

    def _enrich_discovery(
        self,
        discovery: PaneDiscovery,
        /,
        *,
        warnings: list[RuntimeSyncWarning],
    ) -> PaneDiscovery:
        snapshot = discovery.snapshot
        if snapshot.pane_current_path is None or snapshot.repo_root is not None:
            return discovery
        try:
            repo_root = str(self._git.discover_repo_root(snapshot.pane_current_path))
            branch = self._git.current_branch(snapshot.pane_current_path)
        except GitCommandError as exc:
            if self._is_non_repository_error(exc):
                return discovery
            warnings.append(
                RuntimeSyncWarning(
                    message=self._format_git_error(exc),
                    pane_id=snapshot.pane_id,
                )
            )
            return discovery
        enriched_snapshot = replace(snapshot, repo_root=repo_root, branch=branch)
        return replace(discovery, snapshot=enriched_snapshot)

    def _format_git_error(self, exc: GitCommandError) -> str:
        detail = (exc.stderr or str(exc)).strip()
        return f"git context unavailable: {detail}"

    def _format_tmux_error(self, exc: TmuxCommandError) -> str:
        detail = (exc.stderr or str(exc)).strip()
        return f"tmux discovery failed: {detail}"

    def _is_non_repository_error(self, exc: GitCommandError) -> bool:
        stderr = (exc.stderr or "").casefold()
        return any(snippet in stderr for snippet in _NON_REPOSITORY_SNIPPETS)


__all__ = [
    "RuntimeSyncReport",
    "RuntimeSyncWarning",
    "RuntimeSynchronizer",
]

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Final, Protocol, cast, runtime_checkable

from copilot_commander.exceptions import GitCommandError, TmuxCommandError
from copilot_commander.services.discovery_service import PaneDiscovery, PaneDiscoveryReport
from copilot_commander.services.monitoring_service import MonitoringDiscovery, MonitoringReport

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
    ) -> None:
        self._discovery = discovery
        self._monitoring = monitoring
        self._git = git

    def refresh(self) -> RuntimeSyncReport:
        try:
            discovery_report = self._discovery.discover_panes()
        except TmuxCommandError as exc:
            return RuntimeSyncReport(error=self._format_tmux_error(exc))

        warnings: list[RuntimeSyncWarning] = []
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
        monitoring_report = self._monitoring.monitor_discoveries(
            cast(Sequence[MonitoringDiscovery], refreshed_report.panes)
        )
        return RuntimeSyncReport(
            discovery_report=refreshed_report,
            monitoring_report=monitoring_report,
            warnings=tuple(warnings),
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

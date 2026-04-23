from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Final, Literal, Protocol, cast, runtime_checkable

from muxdeck.adapters.copilot_session_resolver import CopilotSessionResolution
from muxdeck.domain.enums import AgentStatus
from muxdeck.domain.models import Agent
from muxdeck.domain.value_objects import ensure_aware_datetime, utc_now
from muxdeck.exceptions import GitCommandError, TmuxCommandError
from muxdeck.parsers.copilot_output_parser import CopilotTaskEvidence
from muxdeck.perf import timed
from muxdeck.services.discovery_service import PaneDiscovery, PaneDiscoveryReport
from muxdeck.services.monitoring_service import MonitoringDiscovery, MonitoringReport
from muxdeck.services.subtask_registry import SubTaskRegistry
from muxdeck.types import Clock

_log = logging.getLogger(__name__)

_NON_REPOSITORY_SNIPPETS: Final[tuple[str, ...]] = (
    "not a git repository",
    "outside repository",
    "cannot chdir",
    "no such file or directory",
)
_CAPTURE_BRANCH_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"\[⎇\s+(?P<branch>[^\]]+)\]"),
    re.compile(
        r"PS\s+(?:\[[^\]]+\]\s+)?[A-Za-z]:\\[^\[]*?\[(?P<branch>[^\]]+)\]>",
    ),
)
_CAPTURE_BRANCH_DECORATION: Final[re.Pattern[str]] = re.compile(r"[*%+!~]+$")


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


@runtime_checkable
class RuntimeWorktreeSyncPort(Protocol):
    def sync_worktrees_from_git(
        self,
        repo_roots: Sequence[Path],
    ) -> object:
        """Discover and upsert worktrees from git for the given repo roots."""


@runtime_checkable
class RuntimeSubAgentSnapshot(Protocol):
    @property
    def display_name(self) -> str: ...

    @property
    def agent_name(self) -> str: ...

    @property
    def model(self) -> str | None: ...

    @property
    def task_name(self) -> str | None: ...

    @property
    def prompt(self) -> str | None: ...

    @property
    def description(self) -> str | None: ...

    @property
    def is_running(self) -> bool: ...

    @property
    def success(self) -> bool | None: ...

    @property
    def error_message(self) -> str | None: ...


@runtime_checkable
class RuntimeSubAgentTree(Protocol):
    @property
    def running(self) -> Sequence[RuntimeSubAgentSnapshot]: ...

    @property
    def recent(self) -> Sequence[RuntimeSubAgentSnapshot]: ...


@runtime_checkable
class RuntimeSubAgentReaderPort(Protocol):
    def read(self, session_id: str) -> RuntimeSubAgentTree | None:
        """Return the sub-agent tree for a Copilot session."""


@runtime_checkable
class RuntimeSessionResolverPort(Protocol):
    def resolve(self, pane_pid: int | None, /) -> CopilotSessionResolution:
        """Resolve a live Copilot session id for a pane pid."""

    def resolve_for_pid(self, pane_pid: int | None, /) -> str | None:
        """Resolve a live Copilot session id for a pane pid."""


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


@dataclass(frozen=True, slots=True)
class _GitContext:
    repo_root: str
    branch: str | None


class RuntimeSynchronizer:
    def __init__(
        self,
        discovery: RuntimeDiscoveryPort,
        monitoring: RuntimeMonitoringPort,
        git: RuntimeGitPort,
        *,
        agent_store: RuntimeAgentStore | None = None,
        worktree_sync: RuntimeWorktreeSyncPort | None = None,
        subtask_registry: SubTaskRegistry | None = None,
        subagent_reader: RuntimeSubAgentReaderPort | None = None,
        session_resolver: RuntimeSessionResolverPort | None = None,
        dead_grace_period_sec: int = 10,
        clock: Clock = utc_now,
    ) -> None:
        self._discovery = discovery
        self._monitoring = monitoring
        self._git = git
        self._agent_store = agent_store
        self._worktree_sync = worktree_sync
        self._subtask_registry = subtask_registry
        self._subagent_reader = subagent_reader
        self._session_resolver = session_resolver
        self._dead_grace_period_sec = dead_grace_period_sec
        self._clock = clock

    def refresh(self) -> RuntimeSyncReport:
        try:
            with timed("sync.discovery"):
                discovery_report = self._discovery.discover_panes()
        except TmuxCommandError as exc:
            return RuntimeSyncReport(error=self._format_tmux_error(exc))

        warnings: list[RuntimeSyncWarning] = []
        git_context_cache: dict[str, _GitContext | None] = {}
        with timed("sync.enrich"):
            enriched_panes = tuple(
                self._enrich_discovery(
                    discovery,
                    warnings=warnings,
                    git_context_cache=git_context_cache,
                )
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
        self._sync_worktrees(git_context_cache, warnings=warnings)
        self._update_subtasks(refreshed_report)
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
        """Mark agents as DEAD when their tmux pane no longer exists.

        Also covers the "operator killed copilot CLI but the shell pane
        survived" case: discovery demotes such panes to
        ``non_agent_pane``, monitoring then skips them, and we reap
        the stored agent here so it stops appearing on the dashboard.
        """
        if self._agent_store is None:
            return
        live_pane_ids = frozenset(pane.snapshot.pane_id for pane in report.panes)
        # Panes that are still alive but no longer count as agents
        # (copilot exited, replaced by another AI CLI, etc.). Their
        # managed_agent records must be reaped so they don't linger.
        non_agent_pane_ids = frozenset(pane.snapshot.pane_id for pane in report.non_agent_panes)
        now = ensure_aware_datetime(self._clock(), field_name="value")
        terminal_statuses = {AgentStatus.DEAD, AgentStatus.COMPLETED}
        for agent in self._agent_store.list_agents():
            if agent.status in terminal_statuses:
                continue
            pane_missing = agent.tmux_pane_id not in live_pane_ids
            pane_no_longer_agent = agent.tmux_pane_id in non_agent_pane_ids
            if not pane_missing and not pane_no_longer_agent:
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
                needs_attention=False,
                attention_reason=None,
                token_input=agent.token_input,
                token_output=agent.token_output,
                token_total=agent.token_total,
                estimated_cost_usd=agent.estimated_cost_usd,
            )
            try:
                self._agent_store.upsert_agent(dead_agent)
                reason = "pane gone" if pane_missing else "copilot CLI exited"
                _log.info(
                    "reaped stale agent %s (pane %s, reason: %s)",
                    agent.id,
                    agent.tmux_pane_id,
                    reason,
                )
            except Exception:
                _log.exception("failed to reap agent %s", agent.id)
                warnings.append(
                    RuntimeSyncWarning(
                        message=f"failed to reap stale agent {agent.id}",
                        pane_id=agent.tmux_pane_id,
                    )
                )

    def _sync_worktrees(
        self,
        git_context_cache: dict[str, _GitContext | None],
        *,
        warnings: list[RuntimeSyncWarning],
    ) -> None:
        """Sync git worktrees into the store from discovered repo roots."""
        if self._worktree_sync is None:
            return
        repo_roots: list[Path] = []
        for ctx in git_context_cache.values():
            if ctx is not None:
                repo_roots.append(Path(ctx.repo_root))
        if not repo_roots:
            return
        try:
            with timed("sync.worktrees"):
                self._worktree_sync.sync_worktrees_from_git(repo_roots)
        except Exception:
            _log.exception("worktree sync failed")
            warnings.append(RuntimeSyncWarning(message="worktree sync failed"))

    def _update_subtasks(self, report: PaneDiscoveryReport) -> None:
        """Feed task evidence from discovered panes into the subtask registry."""
        if self._subtask_registry is None:
            return
        for pane in report.panes:
            evidence = pane.session_evidence
            bg_count = getattr(evidence, "background_task_count", 0)
            task_ev = getattr(evidence, "task_evidence", ())
            if self._subagent_reader is not None and (not task_ev or bg_count == 0):
                session_id = self._resolve_copilot_session_id(pane)
                if session_id is not None:
                    tree = self._subagent_reader.read(session_id)
                    if tree is not None:
                        if not task_ev:
                            task_ev = _task_evidence_from_tree(tree)
                        bg_count = max(bg_count, len(tree.running))
            if evidence is None and not task_ev:
                continue
            self._subtask_registry.update(
                pane.snapshot.pane_id,
                task_ev,
                bg_count,
            )
        self._subtask_registry.expire_all()

    def _resolve_copilot_session_id(self, pane: PaneDiscovery, /) -> str | None:
        if self._session_resolver is not None:
            resolution = self._session_resolver.resolve(pane.snapshot.pane_pid)
            if resolution.session_id is not None:
                return resolution.session_id
            if resolution.is_ambiguous:
                return None
        evidence = pane.session_evidence
        candidate_ids = _unique_session_ids(
            tuple(getattr(evidence, "session_ids", ())) if evidence is not None else ()
        )
        if len(candidate_ids) == 1:
            return candidate_ids[0]
        if len(candidate_ids) > 1:
            return None
        if pane.managed_agent is not None and pane.managed_agent.copilot_session_id is not None:
            return pane.managed_agent.copilot_session_id
        matched_session = getattr(pane, "matched_session", None)
        if matched_session is not None:
            return getattr(matched_session, "copilot_session_id", None)
        if evidence is not None:
            return evidence.copilot_session_id
        return None

    def _enrich_discovery(
        self,
        discovery: PaneDiscovery,
        /,
        *,
        warnings: list[RuntimeSyncWarning],
        git_context_cache: dict[str, _GitContext | None],
    ) -> PaneDiscovery:
        snapshot = discovery.snapshot
        pane_current_path = snapshot.pane_current_path
        capture_branch = _infer_capture_branch(discovery.captured_output)
        if pane_current_path is None or snapshot.repo_root is not None:
            return _apply_capture_branch(discovery, capture_branch)
        stored_context = self._stored_git_context(discovery, pane_current_path)
        if stored_context is not None:
            git_context_cache[pane_current_path] = stored_context
            return _apply_capture_branch(
                self._apply_git_context(discovery, stored_context),
                capture_branch,
            )
        cached_context = git_context_cache.get(pane_current_path)
        if pane_current_path in git_context_cache:
            if cached_context is None:
                return _apply_capture_branch(discovery, capture_branch)
            return _apply_capture_branch(
                self._apply_git_context(discovery, cached_context),
                capture_branch,
            )
        try:
            git_context = _GitContext(
                repo_root=str(self._git.discover_repo_root(pane_current_path)),
                branch=self._git.current_branch(pane_current_path),
            )
        except GitCommandError as exc:
            if self._is_non_repository_error(exc):
                git_context_cache[pane_current_path] = None
                return _apply_capture_branch(discovery, capture_branch)
            warnings.append(
                RuntimeSyncWarning(
                    message=self._format_git_error(exc),
                    pane_id=snapshot.pane_id,
                )
            )
            return _apply_capture_branch(discovery, capture_branch)
        git_context_cache[pane_current_path] = git_context
        return _apply_capture_branch(
            self._apply_git_context(discovery, git_context),
            capture_branch,
        )

    def _apply_git_context(
        self,
        discovery: PaneDiscovery,
        git_context: _GitContext,
        /,
    ) -> PaneDiscovery:
        enriched_snapshot = replace(
            discovery.snapshot,
            repo_root=git_context.repo_root,
            branch=git_context.branch,
        )
        return replace(discovery, snapshot=enriched_snapshot)

    def _stored_git_context(
        self,
        discovery: PaneDiscovery,
        pane_current_path: str,
        /,
    ) -> _GitContext | None:
        matched_context = discovery.matched_context
        if (
            matched_context is not None
            and matched_context.worktree_path == pane_current_path
            and matched_context.repo_root is not None
        ):
            return _GitContext(
                repo_root=matched_context.repo_root,
                branch=matched_context.branch,
            )
        managed_agent = discovery.managed_agent
        if managed_agent is None or managed_agent.repo_root is None:
            return None
        if pane_current_path not in {managed_agent.cwd, managed_agent.worktree_path}:
            return None
        return _GitContext(
            repo_root=managed_agent.repo_root,
            branch=managed_agent.branch,
        )

    def _format_git_error(self, exc: GitCommandError) -> str:
        detail = (exc.stderr or str(exc)).strip()
        return f"git context unavailable: {detail}"

    def _format_tmux_error(self, exc: TmuxCommandError) -> str:
        detail = (exc.stderr or str(exc)).strip()
        return f"tmux discovery failed: {detail}"

    def _is_non_repository_error(self, exc: GitCommandError) -> bool:
        stderr = (exc.stderr or "").casefold()
        return any(snippet in stderr for snippet in _NON_REPOSITORY_SNIPPETS)


def _infer_capture_branch(captured_output: str | None, /) -> str | None:
    if captured_output is None:
        return None
    for raw_line in reversed(captured_output.splitlines()):
        line = raw_line.strip()
        if not line:
            continue
        for pattern in _CAPTURE_BRANCH_PATTERNS:
            match = pattern.search(line)
            if match is None:
                continue
            branch = _normalize_capture_branch(match.group("branch"))
            if branch is not None:
                return branch
    return None


def _task_evidence_from_tree(tree: RuntimeSubAgentTree, /) -> tuple[CopilotTaskEvidence, ...]:
    tasks: list[CopilotTaskEvidence] = []
    for snapshot in (*tree.running, *tree.recent):
        description = (
            snapshot.task_name
            or snapshot.prompt
            or snapshot.description
            or snapshot.display_name
            or snapshot.agent_name
        )
        status: Literal["running", "completed", "failed"] = "running"
        if not snapshot.is_running:
            if snapshot.success is False or snapshot.error_message:
                status = "failed"
            else:
                status = "completed"
        tasks.append(
            CopilotTaskEvidence(
                agent_type_label=snapshot.display_name or snapshot.agent_name,
                model=snapshot.model,
                description=description,
                status=status,
            )
        )
    return tuple(tasks)


def _unique_session_ids(session_ids: tuple[str, ...], /) -> tuple[str, ...]:
    unique: list[str] = []
    seen: set[str] = set()
    for session_id in session_ids:
        if session_id in seen:
            continue
        seen.add(session_id)
        unique.append(session_id)
    return tuple(unique)


def _normalize_capture_branch(value: str, /) -> str | None:
    normalized = value.strip()
    if not normalized:
        return None
    normalized = _CAPTURE_BRANCH_DECORATION.sub("", normalized).strip()
    return normalized or None


def _apply_capture_branch(discovery: PaneDiscovery, branch: str | None, /) -> PaneDiscovery:
    if branch is None or discovery.snapshot.branch == branch:
        return discovery
    return replace(discovery, snapshot=replace(discovery.snapshot, branch=branch))


__all__ = [
    "RuntimeSyncReport",
    "RuntimeSyncWarning",
    "RuntimeSynchronizer",
]

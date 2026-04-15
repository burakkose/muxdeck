from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Literal, Protocol, cast, runtime_checkable

from copilot_commander.domain.enums import AgentStatus
from copilot_commander.domain.models import Agent
from copilot_commander.domain.value_objects import ensure_aware_datetime, utc_now
from copilot_commander.services.agent_service import AgentFactInput, PaneClassification
from copilot_commander.types import Clock


@runtime_checkable
class MonitoringDiscovery(Protocol):
    @property
    def classification(
        self,
    ) -> Literal["managed_agent", "unmanaged_probable_agent", "non_agent_pane"]:
        """Return the discovery classification."""

    @property
    def snapshot(self) -> MonitoringSnapshot:
        """Return pane snapshot metadata."""

    @property
    def session_evidence(self) -> MonitoringEvidence | None:
        """Return parsed Copilot evidence."""

    @property
    def captured_output(self) -> str | None:
        """Return captured pane text."""

    @property
    def managed_agent(self) -> Agent | None:
        """Return a matched managed agent when present."""


@runtime_checkable
class MonitoringAgentRecorder(Protocol):
    def persist_agent_facts(self, facts: AgentFactInput, /) -> object:
        """Persist agent/session/event/log facts for an observation."""


@runtime_checkable
class MonitoringSnapshot(Protocol):
    tmux_session_name: str
    tmux_window_id: str
    tmux_window_name: str | None
    pane_id: str
    pane_tty: str | None
    pane_current_path: str | None
    pane_pid: int | None
    pane_dead: bool | None


@runtime_checkable
class MonitoringUsage(Protocol):
    input_tokens: int | None
    output_tokens: int | None
    total_tokens: int | None
    cost: Decimal | None
    currency: str | None


@runtime_checkable
class MonitoringParseResult(Protocol):
    boundaries: Sequence[object]


@runtime_checkable
class MonitoringEvidence(Protocol):
    latest_usage: MonitoringUsage | None
    copilot_session_id: str | None
    blocking_issue_kinds: Sequence[str]
    error_messages: Sequence[str]
    parse_result: MonitoringParseResult


@dataclass(frozen=True, slots=True)
class MonitoringThresholds:
    waiting_input_after_seconds: int = 30
    idle_after_seconds: int = 300
    attention_idle_after_seconds: int = 900
    error_after_seconds: int = 0

    def __post_init__(self) -> None:
        for field_name in (
            "waiting_input_after_seconds",
            "idle_after_seconds",
            "attention_idle_after_seconds",
            "error_after_seconds",
        ):
            value = getattr(self, field_name)
            if value < 0:
                msg = f"{field_name} must be non-negative"
                raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class StatusHeuristicInput:
    started_at: datetime
    observed_at: datetime
    previous_last_activity_at: datetime | None = None
    pane_dead: bool = False
    activity_observed: bool = False
    blocking_issue_kinds: tuple[str, ...] = ()
    error_messages: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "started_at",
            ensure_aware_datetime(self.started_at, field_name="started_at"),
        )
        object.__setattr__(
            self,
            "observed_at",
            ensure_aware_datetime(self.observed_at, field_name="observed_at"),
        )
        if self.previous_last_activity_at is not None:
            object.__setattr__(
                self,
                "previous_last_activity_at",
                ensure_aware_datetime(
                    self.previous_last_activity_at,
                    field_name="previous_last_activity_at",
                ),
            )


@dataclass(frozen=True, slots=True)
class StatusHeuristicResult:
    status: AgentStatus
    idle_seconds: int
    last_activity_at: datetime | None
    needs_attention: bool
    attention_reason: str | None = None


@dataclass(frozen=True, slots=True)
class MonitoringResult:
    discovery: MonitoringDiscovery
    evaluation: StatusHeuristicResult
    persisted: object | None


@dataclass(frozen=True, slots=True)
class MonitoringReport:
    monitored_at: datetime
    results: tuple[MonitoringResult, ...]


class MonitoringService:
    def __init__(
        self,
        agent_recorder: MonitoringAgentRecorder,
        *,
        thresholds: MonitoringThresholds | None = None,
        clock: Clock = utc_now,
    ) -> None:
        self._agent_recorder = agent_recorder
        self._thresholds = MonitoringThresholds() if thresholds is None else thresholds
        self._clock = clock

    def evaluate(self, payload: StatusHeuristicInput, /) -> StatusHeuristicResult:
        return compute_status_heuristics(payload, thresholds=self._thresholds)

    def monitor_discoveries(
        self,
        discoveries: Sequence[MonitoringDiscovery],
        /,
    ) -> MonitoringReport:
        monitored_at = ensure_aware_datetime(self._clock(), field_name="value")
        results: list[MonitoringResult] = []
        for discovery in discoveries:
            classification = discovery.classification
            if classification == "non_agent_pane":
                continue
            existing_agent = discovery.managed_agent
            session_evidence = discovery.session_evidence
            snapshot = discovery.snapshot
            started_at = existing_agent.started_at if existing_agent is not None else monitored_at
            heuristic_input = StatusHeuristicInput(
                started_at=started_at,
                observed_at=monitored_at,
                previous_last_activity_at=(
                    existing_agent.last_activity_at if existing_agent is not None else None
                ),
                pane_dead=bool(getattr(snapshot, "pane_dead", False)),
                activity_observed=_has_activity_signal(session_evidence),
                blocking_issue_kinds=(
                    tuple(getattr(session_evidence, "blocking_issue_kinds", ()))
                    if session_evidence is not None
                    else ()
                ),
                error_messages=(
                    tuple(getattr(session_evidence, "error_messages", ()))
                    if session_evidence is not None
                    else ()
                ),
            )
            evaluation = self.evaluate(heuristic_input)
            facts = self._build_agent_fact_input(
                discovery,
                monitored_at=monitored_at,
                evaluation=evaluation,
                existing_agent=existing_agent,
            )
            persisted = self._agent_recorder.persist_agent_facts(facts)
            results.append(
                MonitoringResult(
                    discovery=discovery,
                    evaluation=evaluation,
                    persisted=persisted,
                )
            )
        return MonitoringReport(monitored_at=monitored_at, results=tuple(results))

    def _build_agent_fact_input(
        self,
        discovery: MonitoringDiscovery,
        /,
        *,
        monitored_at: datetime,
        evaluation: StatusHeuristicResult,
        existing_agent: Agent | None,
    ) -> AgentFactInput:
        snapshot = discovery.snapshot
        session_evidence = discovery.session_evidence
        latest_usage = session_evidence.latest_usage if session_evidence is not None else None
        estimated_cost: Decimal | None = None
        if latest_usage is not None and latest_usage.currency in {None, "USD"}:
            estimated_cost = latest_usage.cost
        current_path = snapshot.pane_current_path
        cwd = current_path or (existing_agent.cwd if existing_agent is not None else "/")
        worktree_path = current_path or (
            existing_agent.worktree_path if existing_agent is not None else None
        )
        copilot_session_id = session_evidence.copilot_session_id if session_evidence else None
        if copilot_session_id is None and existing_agent is not None:
            copilot_session_id = existing_agent.copilot_session_id
        classification = cast(PaneClassification, discovery.classification)
        return AgentFactInput(
            classification=classification,
            agent_id=existing_agent.id if existing_agent is not None else None,
            tmux_session_name=snapshot.tmux_session_name,
            tmux_window_id=snapshot.tmux_window_id,
            tmux_window_name=snapshot.tmux_window_name,
            tmux_pane_id=snapshot.pane_id,
            pane_tty=snapshot.pane_tty,
            cwd=cwd,
            repo_root=existing_agent.repo_root if existing_agent is not None else None,
            worktree_path=worktree_path,
            branch=existing_agent.branch if existing_agent is not None else None,
            name=existing_agent.name if existing_agent is not None else None,
            task_title=existing_agent.task_title if existing_agent is not None else None,
            task_summary=existing_agent.task_summary if existing_agent is not None else None,
            copilot_session_id=copilot_session_id,
            pid=snapshot.pane_pid,
            observed_at=monitored_at,
            last_activity_at=evaluation.last_activity_at,
            status=evaluation.status,
            idle_seconds=evaluation.idle_seconds,
            needs_attention=evaluation.needs_attention,
            attention_reason=evaluation.attention_reason,
            token_input=latest_usage.input_tokens if latest_usage is not None else None,
            token_output=latest_usage.output_tokens if latest_usage is not None else None,
            token_total=latest_usage.total_tokens if latest_usage is not None else None,
            estimated_cost_usd=estimated_cost,
            capture_text=discovery.captured_output,
            blocking_issue_kinds=(
                tuple(session_evidence.blocking_issue_kinds) if session_evidence is not None else ()
            ),
            error_messages=tuple(session_evidence.error_messages) if session_evidence else (),
        )


def compute_status_heuristics(
    payload: StatusHeuristicInput,
    /,
    *,
    thresholds: MonitoringThresholds | None = None,
) -> StatusHeuristicResult:
    applied_thresholds = MonitoringThresholds() if thresholds is None else thresholds
    last_activity_at = (
        payload.observed_at if payload.activity_observed else payload.previous_last_activity_at
    )
    idle_reference = last_activity_at or payload.started_at
    idle_seconds = max(0, int((payload.observed_at - idle_reference).total_seconds()))

    if payload.pane_dead:
        return StatusHeuristicResult(
            status=AgentStatus.DEAD,
            idle_seconds=idle_seconds,
            last_activity_at=last_activity_at,
            needs_attention=True,
            attention_reason="tmux pane is dead",
        )

    blocking_kind = _first_blocking_kind(payload.blocking_issue_kinds)
    if (
        blocking_kind == "waiting_for_confirmation"
        and idle_seconds >= applied_thresholds.waiting_input_after_seconds
    ):
        return StatusHeuristicResult(
            status=AgentStatus.WAITING_INPUT,
            idle_seconds=idle_seconds,
            last_activity_at=last_activity_at,
            needs_attention=True,
            attention_reason="waiting for confirmation input",
        )
    if blocking_kind is not None and blocking_kind != "waiting_for_confirmation":
        return StatusHeuristicResult(
            status=AgentStatus.BLOCKED,
            idle_seconds=idle_seconds,
            last_activity_at=last_activity_at,
            needs_attention=True,
            attention_reason=_blocking_attention_reason(blocking_kind),
        )
    if payload.error_messages and idle_seconds >= applied_thresholds.error_after_seconds:
        return StatusHeuristicResult(
            status=AgentStatus.ERROR,
            idle_seconds=idle_seconds,
            last_activity_at=last_activity_at,
            needs_attention=True,
            attention_reason=payload.error_messages[0],
        )
    if idle_seconds >= applied_thresholds.attention_idle_after_seconds:
        return StatusHeuristicResult(
            status=AgentStatus.IDLE,
            idle_seconds=idle_seconds,
            last_activity_at=last_activity_at,
            needs_attention=True,
            attention_reason=f"idle for {idle_seconds}s",
        )
    if idle_seconds >= applied_thresholds.idle_after_seconds:
        return StatusHeuristicResult(
            status=AgentStatus.IDLE,
            idle_seconds=idle_seconds,
            last_activity_at=last_activity_at,
            needs_attention=False,
            attention_reason=None,
        )
    return StatusHeuristicResult(
        status=AgentStatus.RUNNING,
        idle_seconds=idle_seconds,
        last_activity_at=last_activity_at,
        needs_attention=False,
        attention_reason=None,
    )


def _first_blocking_kind(blocking_issue_kinds: Sequence[str], /) -> str | None:
    return blocking_issue_kinds[0] if blocking_issue_kinds else None


def _blocking_attention_reason(kind: str, /) -> str:
    if kind == "authentication_issue":
        return "authentication issue requires attention"
    if kind == "merge_conflict":
        return "merge conflict requires intervention"
    if kind == "rate_limit":
        return "rate limit is blocking progress"
    if kind == "tool_failure":
        return "tool failure detected"
    return kind.replace("_", " ")


def _has_activity_signal(session_evidence: MonitoringEvidence | None, /) -> bool:
    if session_evidence is None:
        return False
    return any(
        (
            session_evidence.copilot_session_id is not None,
            bool(session_evidence.latest_usage),
            bool(session_evidence.blocking_issue_kinds),
            bool(session_evidence.error_messages),
            bool(session_evidence.parse_result.boundaries),
        )
    )

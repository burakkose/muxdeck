from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import PurePosixPath
from typing import Literal, Protocol, cast, runtime_checkable

from muxdeck.adapters.copilot_session_resolver import CopilotSessionResolution
from muxdeck.domain.enums import AgentStatus
from muxdeck.domain.models import Agent
from muxdeck.domain.value_objects import ensure_aware_datetime, utc_now
from muxdeck.services.agent_service import AgentFactInput, PaneClassification
from muxdeck.types import Clock


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
class MonitoringSessionResolver(Protocol):
    def resolve(self, pane_pid: int | None, /) -> CopilotSessionResolution:
        """Resolve a live Copilot session id for a pane pid."""

    def resolve_for_pid(self, pane_pid: int | None, /) -> str | None:
        """Resolve a live Copilot session id for a pane pid."""


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
    repo_root: str | None
    branch: str | None


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
    ui_markers: Sequence[object]
    activity_markers: Sequence[object]


@runtime_checkable
class MonitoringEvidence(Protocol):
    latest_usage: MonitoringUsage | None
    copilot_session_id: str | None
    blocking_issue_kinds: Sequence[str]
    error_messages: Sequence[str]
    parse_result: MonitoringParseResult


@runtime_checkable
class MonitoringLocalSessionUsage(Protocol):
    input_tokens: int | None
    output_tokens: int | None


@runtime_checkable
class MonitoringLocalSession(Protocol):
    session_id: str
    usage: MonitoringLocalSessionUsage | None


@runtime_checkable
class MonitoringLocalSessionStore(Protocol):
    def discover(self, *, force: bool = False) -> Sequence[MonitoringLocalSession]:
        """Return cached or freshly scanned local Copilot sessions."""


@dataclass(frozen=True, slots=True)
class MonitoringThresholds:
    waiting_input_after_seconds: int = 30
    idle_after_seconds: int = 300
    attention_idle_after_seconds: int = 900
    # ERROR is a noisy classification: `_ERROR_PATTERNS` match any
    # "error:" / "fatal:" / "exception" / "traceback" line in the
    # scrollback, and those appear routinely in normal agent work
    # (git output, compiler messages, stack traces being discussed).
    # We require the pane to be genuinely quiet for a while *and* no
    # fresh activity to be observed before flipping to ERROR, so
    # ambient error text from routine tool calls doesn't mislabel a
    # working agent as failed.
    error_after_seconds: int = 300

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
    session_exit_reason: str | None = None

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
        session_resolver: MonitoringSessionResolver | None = None,
        local_session_store: MonitoringLocalSessionStore | None = None,
        thresholds: MonitoringThresholds | None = None,
        clock: Clock = utc_now,
    ) -> None:
        self._agent_recorder = agent_recorder
        self._session_resolver = session_resolver
        self._local_session_store = local_session_store
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
        local_usage_by_session_id = self._build_local_usage_index()
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
                session_exit_reason=(
                    "marked_complete"
                    if existing_agent is not None and existing_agent.status == AgentStatus.COMPLETED
                    else None
                ),
            )
            evaluation = self.evaluate(heuristic_input)
            facts = self._build_agent_fact_input(
                discovery,
                monitored_at=monitored_at,
                evaluation=evaluation,
                existing_agent=existing_agent,
                local_usage_by_session_id=local_usage_by_session_id,
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
        local_usage_by_session_id: Mapping[str, MonitoringLocalSessionUsage],
    ) -> AgentFactInput:
        snapshot = discovery.snapshot
        session_evidence = discovery.session_evidence
        latest_usage = session_evidence.latest_usage if session_evidence is not None else None
        estimated_cost: Decimal | None = None
        if latest_usage is not None and latest_usage.currency in {None, "USD"}:
            estimated_cost = latest_usage.cost
        current_path = snapshot.pane_current_path
        repo_root = getattr(snapshot, "repo_root", None)
        branch = getattr(snapshot, "branch", None)
        cwd = current_path or (existing_agent.cwd if existing_agent is not None else "/")
        worktree_path = current_path or (
            existing_agent.worktree_path if existing_agent is not None else None
        )
        copilot_session_id = self._resolve_copilot_session_id(
            snapshot=snapshot,
            session_evidence=session_evidence,
            existing_agent=existing_agent,
        )
        token_input = latest_usage.input_tokens if latest_usage is not None else None
        token_output = latest_usage.output_tokens if latest_usage is not None else None
        token_total = latest_usage.total_tokens if latest_usage is not None else None
        if token_total is None and (token_input is not None or token_output is not None):
            token_total = (token_input or 0) + (token_output or 0)
        if latest_usage is None and copilot_session_id is not None:
            local_usage = local_usage_by_session_id.get(copilot_session_id)
            if local_usage is not None:
                # The persisted agent snapshot only tracks prompt/completion
                # tokens today, so the dashboard can reuse these fields without
                # paying an extra filesystem read during refresh.
                token_input = local_usage.input_tokens
                token_output = local_usage.output_tokens
                if token_input is not None or token_output is not None:
                    token_total = (token_input or 0) + (token_output or 0)
        latest_activity = (
            _extract_latest_activity(session_evidence.parse_result)
            if session_evidence is not None
            else None
        )
        task_title = (
            existing_agent.task_title if existing_agent is not None else None
        ) or latest_activity
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
            repo_root=repo_root
            or (existing_agent.repo_root if existing_agent is not None else None),
            worktree_path=worktree_path,
            branch=branch or (existing_agent.branch if existing_agent is not None else None),
            name=_derive_agent_name(
                repo_root=repo_root
                or (existing_agent.repo_root if existing_agent is not None else None),
                cwd=cwd,
                existing_name=existing_agent.name if existing_agent is not None else None,
            ),
            task_title=task_title,
            task_summary=existing_agent.task_summary if existing_agent is not None else None,
            copilot_session_id=copilot_session_id,
            pid=snapshot.pane_pid,
            observed_at=monitored_at,
            last_activity_at=evaluation.last_activity_at,
            status=evaluation.status,
            idle_seconds=evaluation.idle_seconds,
            needs_attention=evaluation.needs_attention,
            attention_reason=evaluation.attention_reason,
            token_input=token_input,
            token_output=token_output,
            token_total=token_total,
            estimated_cost_usd=estimated_cost,
            capture_text=discovery.captured_output,
            blocking_issue_kinds=(
                tuple(session_evidence.blocking_issue_kinds) if session_evidence is not None else ()
            ),
            error_messages=tuple(session_evidence.error_messages) if session_evidence else (),
        )

    def _build_local_usage_index(self) -> dict[str, MonitoringLocalSessionUsage]:
        if self._local_session_store is None:
            return {}
        return {
            session.session_id: session.usage
            for session in self._local_session_store.discover()
            if session.usage is not None
        }

    def _resolve_copilot_session_id(
        self,
        *,
        snapshot: MonitoringSnapshot,
        session_evidence: MonitoringEvidence | None,
        existing_agent: Agent | None,
    ) -> str | None:
        candidate_ids = _candidate_session_ids(session_evidence)
        if self._session_resolver is not None:
            resolution = self._session_resolver.resolve(snapshot.pane_pid)
            if resolution.session_id is not None:
                return resolution.session_id
            if resolution.is_ambiguous:
                return None
        if len(candidate_ids) == 1:
            return candidate_ids[0]
        if len(candidate_ids) > 1:
            return None
        if existing_agent is not None:
            return existing_agent.copilot_session_id
        return None


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

    # A completed agent stays completed while its pane is quiet — even if
    # the pane is still alive and old error text lingers in the scrollback.
    # Only fall through to the live-classification branches if the pane
    # produced fresh activity after mark-complete, so the user can resume
    # the same pane without it being frozen in a terminal state.
    if payload.session_exit_reason == "marked_complete" and not payload.activity_observed:
        return StatusHeuristicResult(
            status=AgentStatus.COMPLETED,
            idle_seconds=idle_seconds,
            last_activity_at=last_activity_at,
            needs_attention=False,
            attention_reason=None,
        )

    if payload.pane_dead:
        return StatusHeuristicResult(
            status=AgentStatus.DEAD,
            idle_seconds=idle_seconds,
            last_activity_at=last_activity_at,
            needs_attention=True,
            attention_reason="tmux pane is dead",
        )

    blocking_kind = _first_blocking_kind(payload.blocking_issue_kinds)
    # WAITING_INPUT is the only "agent needs the user" state. The
    # scrollback has to mention a confirmation prompt *and* the pane
    # has to have been quiet long enough that we're confident the
    # agent is waiting (not just emitting the prompt as part of normal
    # output). Without the idle gate a fresh line like
    # "Would you like me to..." would flip status mid-response.
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
    # Everything else that the output parser calls a "blocking" kind
    # (tool_failure, merge_conflict, authentication_issue, rate_limit)
    # is noise for status classification. A tool call that returns a
    # non-zero exit code, an "stderr:" line, or the word "sign in"
    # appearing anywhere in the scrollback are all part of normal
    # agent work — classifying the agent as BLOCKED because of them
    # flips the dashboard even while the agent is actively producing
    # output. Fresh activity is the canonical "working" signal; let
    # the idle / RUNNING branches below handle classification. These
    # kinds can still surface as attention reasons once the agent
    # goes quiet (see the error / idle branches).
    # ERROR classification is intentionally conservative. We only flip
    # to ERROR when:
    #   * the pane has no fresh activity (`activity_observed` is false),
    #   * the idle gate (`error_after_seconds`) has elapsed, and
    #   * error evidence is still present.
    # Without these gates, a single `error:` line sitting in the
    # scrollback from a routine `git` / build / tool call flagged the
    # whole agent as failed even while it was actively producing
    # output. See also `_has_activity_signal`, which no longer treats
    # error lines as an activity signal for the same reason.
    if (
        payload.error_messages
        and not payload.activity_observed
        and idle_seconds >= applied_thresholds.error_after_seconds
    ):
        reason = payload.error_messages[0]
        if blocking_kind is not None and blocking_kind != "waiting_for_confirmation":
            reason = _blocking_attention_reason(blocking_kind)
        return StatusHeuristicResult(
            status=AgentStatus.ERROR,
            idle_seconds=idle_seconds,
            last_activity_at=last_activity_at,
            needs_attention=True,
            attention_reason=reason,
        )
    if idle_seconds >= applied_thresholds.attention_idle_after_seconds:
        reason = f"idle for {idle_seconds}s"
        if blocking_kind is not None and blocking_kind != "waiting_for_confirmation":
            # The agent went quiet while one of these signals was on
            # screen — worth surfacing as the attention reason even
            # though we don't flip status on it by itself.
            reason = _blocking_attention_reason(blocking_kind)
        return StatusHeuristicResult(
            status=AgentStatus.IDLE,
            idle_seconds=idle_seconds,
            last_activity_at=last_activity_at,
            needs_attention=True,
            attention_reason=reason,
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
    """Detect current activity from recent output only.

    Signals that persist forever in scrollback (session_id, historical usage,
    old activity markers, `error:` / `fatal:` lines from past tool output)
    are NOT indicators of current work.  We look for evidence of *ongoing*
    work: recent activity markers near the tail of output, or the Copilot
    CLI's "Esc to cancel" UI marker which is only rendered while the agent
    is actively generating.
    """
    if session_evidence is None:
        return False

    pr = session_evidence.parse_result

    # "Esc to cancel" in the last few lines means the agent is actively generating.
    if pr.ui_markers:
        last_marker = pr.ui_markers[-1]
        if getattr(last_marker, "kind", "") == "esc_to_cancel":
            return True

    # Activity markers (file reads, writes, tool calls) — only count if they
    # appear in the tail of the output (last ~30 lines).
    if pr.activity_markers:
        max_line = 0
        for m in pr.activity_markers:
            span = getattr(m, "span", None)
            if span is not None:
                line = getattr(span, "start_line", 0)
                if line > max_line:
                    max_line = line
        for m in pr.ui_markers:
            span = getattr(m, "span", None)
            if span is not None:
                line = getattr(span, "start_line", 0)
                if line > max_line:
                    max_line = line

        if max_line > 0:
            tail_threshold = max(1, max_line - 30)
            for m in pr.activity_markers:
                span = getattr(m, "span", None)
                if span is not None and getattr(span, "start_line", 0) >= tail_threshold:
                    return True

    # `error:` / `fatal:` / `exception` / `traceback` lines from the
    # parser are captured from the entire scrollback and routinely
    # appear in normal agent work (git output, compiler messages,
    # stack traces being reviewed).  They are NOT a reliable signal
    # of fresh activity, so we do not treat them as one.
    return False


def _extract_latest_activity(
    parse_result: MonitoringParseResult,
    /,
) -> str | None:
    """Get the most recent activity description from parsed output."""
    if not parse_result.activity_markers:
        return None
    marker = parse_result.activity_markers[-1]
    return getattr(marker, "activity", None)


def _candidate_session_ids(session_evidence: MonitoringEvidence | None, /) -> tuple[str, ...]:
    if session_evidence is None:
        return ()
    unique: list[str] = []
    seen: set[str] = set()
    for candidate in getattr(session_evidence, "session_ids", ()):
        if not isinstance(candidate, str) or not candidate or candidate in seen:
            continue
        seen.add(candidate)
        unique.append(candidate)
    session_id = session_evidence.copilot_session_id
    if session_id is not None and session_id not in seen:
        unique.insert(0, session_id)
    return tuple(unique)


def _derive_agent_name(
    *,
    repo_root: str | None,
    cwd: str,
    existing_name: str | None,
) -> str | None:
    """Derive a meaningful agent name from paths.

    Prefer repo name over cwd basename over existing (process) name.
    Always re-derive so that stale process names get overwritten once
    repo_root becomes available.
    """
    if repo_root:
        name = PurePosixPath(repo_root).name
        if name:
            return name
    if cwd and cwd != "/":
        name = PurePosixPath(cwd).name
        if name:
            return name
    return existing_name

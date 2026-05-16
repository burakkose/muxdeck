from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Literal, Protocol, runtime_checkable

from muxdeck.adapters.copilot_adapter import (
    CopilotCommandDetection,
    CopilotSessionEvidence,
)
from muxdeck.adapters.sqlite_store import SessionContextRecord
from muxdeck.adapters.tmux_adapter import TmuxPaneMetadata
from muxdeck.domain.models import Agent, Session
from muxdeck.domain.value_objects import ensure_aware_datetime, utc_now
from muxdeck.parsers.tmux_parser import TmuxPaneRecord
from muxdeck.perf import timed
from muxdeck.types import Clock

PaneClassification = Literal["managed_agent", "unmanaged_probable_agent", "non_agent_pane"]
PaneMetadataLike = TmuxPaneMetadata | TmuxPaneRecord

_SHELL_COMMANDS: frozenset[str] = frozenset(
    {
        "bash",
        "zsh",
        "fish",
        "sh",
        "dash",
        "ksh",
        "csh",
        "tcsh",
        "ash",
        "nu",
        "nushell",
        "pwsh",
        "powershell",
        "elvish",
        "xonsh",
        "ion",
    }
)

# AI CLI tools that are NOT GitHub Copilot but produce similar output.
_NON_COPILOT_AI_COMMANDS: frozenset[str] = frozenset(
    {
        "claude",
        "aider",
        "cursor",
        "cody",
        "continue",
    }
)


@runtime_checkable
class DiscoveryTmuxGateway(Protocol):
    def list_panes(self) -> object:
        """Return pane metadata records or a parser result with a panes attribute."""

    def capture_pane(
        self,
        target_pane: str,
        /,
        *,
        start_line: str | int | None = None,
        end_line: str | int | None = None,
        join_wrapped_lines: bool = False,
    ) -> str:
        """Capture pane output."""


@runtime_checkable
class DiscoveryCopilotGateway(Protocol):
    def detect_command(self, candidate: str | tuple[str, ...], /) -> CopilotCommandDetection:
        """Return Copilot command detection metadata."""

    def interpret_output(self, output: str, /) -> CopilotSessionEvidence:
        """Parse captured Copilot output."""


@runtime_checkable
class DiscoveryStore(Protocol):
    def get_agent_by_pane_id(self, pane_id: str, /) -> Agent | None:
        """Return a stored agent by pane identifier."""

    def get_agent_by_copilot_session_id(self, copilot_session_id: str, /) -> Agent | None:
        """Return a stored agent by Copilot session identifier."""

    def get_session_by_copilot_session_id(self, copilot_session_id: str, /) -> Session | None:
        """Return a stored session by Copilot session identifier."""

    def get_session_context_by_tmux_pane_id(
        self,
        tmux_pane_id: str,
        /,
    ) -> SessionContextRecord | None:
        """Return a stored session context by tmux pane identifier."""


@runtime_checkable
class DiscoveryProcessInspector(Protocol):
    def get_child_cmdlines(self, pid: int, /) -> tuple[str, ...]:
        """Return command lines of descendant processes."""


@dataclass(frozen=True, slots=True)
class DiscoveryPaneSnapshot:
    pane_id: str
    tmux_session_name: str
    tmux_window_id: str
    tmux_window_name: str | None = None
    pane_tty: str | None = None
    pane_current_path: str | None = None
    pane_current_command: str | None = None
    pane_pid: int | None = None
    pane_active: bool | None = None
    pane_dead: bool | None = None
    pane_activity: int | None = None
    repo_root: str | None = None
    branch: str | None = None

    @classmethod
    def from_tmux_record(cls, record: PaneMetadataLike, /) -> DiscoveryPaneSnapshot:
        if isinstance(record, TmuxPaneMetadata):
            return cls(
                pane_id=record.pane_id,
                tmux_session_name=record.session_name or "unknown",
                tmux_window_id=record.window_id or "unknown",
                tmux_window_name=record.window_name,
                pane_tty=record.pane_tty,
                pane_current_path=record.pane_current_path,
                pane_current_command=record.pane_current_command,
                pane_pid=record.pane_pid,
                pane_active=record.pane_active,
                pane_dead=record.pane_dead,
                pane_activity=record.pane_activity,
            )
        return cls(
            pane_id=record.pane_id,
            tmux_session_name=record.session_name or "unknown",
            tmux_window_id=record.window_id or "unknown",
            tmux_window_name=record.window_name,
            pane_tty=record.pane_tty,
            pane_current_path=record.pane_current_path,
            pane_current_command=record.pane_current_command,
            pane_pid=record.pane_pid,
            pane_active=record.pane_active,
            pane_dead=None,
            pane_activity=record.pane_activity,
        )


@dataclass(frozen=True, slots=True)
class PaneDiscovery:
    snapshot: DiscoveryPaneSnapshot
    discovered_at: datetime
    classification: PaneClassification
    reasons: tuple[str, ...]
    command_detection: CopilotCommandDetection
    captured_output: str | None = None
    session_evidence: CopilotSessionEvidence | None = None
    managed_agent: Agent | None = None
    matched_session: Session | None = None
    matched_context: SessionContextRecord | None = None
    confidence: Decimal = Decimal("0.5000")

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "discovered_at",
            ensure_aware_datetime(self.discovered_at, field_name="discovered_at"),
        )


@dataclass(frozen=True, slots=True)
class PaneDiscoveryReport:
    discovered_at: datetime
    panes: tuple[PaneDiscovery, ...]
    managed_agents: tuple[PaneDiscovery, ...]
    unmanaged_probable_agents: tuple[PaneDiscovery, ...]
    non_agent_panes: tuple[PaneDiscovery, ...]


def classify_pane(
    snapshot: DiscoveryPaneSnapshot,
    /,
    *,
    discovered_at: datetime | None = None,
    command_detection: CopilotCommandDetection,
    session_evidence: CopilotSessionEvidence | None = None,
    captured_output: str | None = None,
    managed_agent: Agent | None = None,
    matched_session: Session | None = None,
    matched_context: SessionContextRecord | None = None,
) -> PaneDiscovery:
    reasons: list[str] = []
    if managed_agent is not None:
        reasons.append("matched stored agent")
    if matched_session is not None:
        reasons.append("matched stored session")
    if matched_context is not None:
        reasons.append("matched stored session context")
    if command_detection.is_likely_copilot:
        reasons.append(f"command:{command_detection.reason}")
    if _has_session_signal(session_evidence):
        reasons.append("captured Copilot evidence")

    is_non_copilot = _is_non_copilot_command(snapshot.pane_current_command)
    # Detect "operator killed copilot" panes: the pane is alive and now
    # the foreground process is a known shell, AND the process tree
    # walk found no copilot child. We deliberately ignore
    # ``session_evidence`` here — the scrollback still contains the
    # session id, banner, and usage from the just-exited copilot run,
    # so any "evidence" signal would falsely keep the agent alive
    # forever after copilot CLI was terminated. The shell+no-copilot
    # combination is the authoritative live signal.
    copilot_no_longer_running_in_managed_pane = (
        managed_agent is not None
        and not command_detection.is_likely_copilot
        and _is_shell_command(snapshot.pane_current_command)
    )
    if is_non_copilot and not command_detection.is_likely_copilot:
        # Pane now runs a known non-copilot AI CLI (e.g. claude, aider).
        # Override even if we have a stored agent record from before.
        classification: PaneClassification = "non_agent_pane"
        confidence = Decimal("0.9500")
        reasons.append("known non-copilot AI CLI")
    elif copilot_no_longer_running_in_managed_pane:
        # Operator terminated the copilot CLI; the pane survived as a
        # plain shell. Demote so the agent can be reaped instead of
        # lingering on the dashboard as if it were still running.
        classification = "non_agent_pane"
        confidence = Decimal("0.9000")
        reasons.append("copilot CLI no longer running in pane")
    elif managed_agent is not None or matched_session is not None or matched_context is not None:
        classification = "managed_agent"
        confidence = Decimal("0.9900")
    elif command_detection.is_likely_copilot:
        classification = "unmanaged_probable_agent"
        confidence = Decimal("0.8400")
    elif _is_shell_command(snapshot.pane_current_command):
        # Shell panes need process-tree copilot match or hard evidence
        # (session id, usage, blocking, errors). Scrollback markers from
        # old sessions are not enough.
        if _has_strong_session_signal(session_evidence):
            classification = "unmanaged_probable_agent"
            confidence = Decimal("0.7500")
        else:
            classification = "non_agent_pane"
            confidence = Decimal("0.9500")
            reasons.append("no Copilot signal")
    elif _has_session_signal(session_evidence):
        # Non-shell process (e.g. node) with any Copilot evidence.
        classification = "unmanaged_probable_agent"
        confidence = Decimal("0.7900")
    else:
        classification = "non_agent_pane"
        confidence = Decimal("0.9500")
        reasons.append("no Copilot signal")

    return PaneDiscovery(
        snapshot=snapshot,
        discovered_at=utc_now() if discovered_at is None else discovered_at,
        classification=classification,
        reasons=tuple(reasons),
        command_detection=command_detection,
        captured_output=(captured_output.rstrip() or None) if captured_output is not None else None,
        session_evidence=session_evidence,
        managed_agent=managed_agent,
        matched_session=matched_session,
        matched_context=matched_context,
        confidence=confidence,
    )


@dataclass(frozen=True, slots=True)
class _PaneCaptureCacheEntry:
    """Memoized per-pane subprocess work for :class:`DiscoveryService`.

    A cache hit lets the next discovery cycle skip ``capture-pane``
    (a subprocess fork per pane) and ``interpret_output`` (an ANSI /
    regex parse over the captured scrollback). Hits are only valid
    while the underlying pane is unchanged — see :meth:`matches`.
    """

    pane_pid: int | None
    pane_tty: str | None
    pane_activity: int | None
    command_detection: CopilotCommandDetection
    captured_output: str
    session_evidence: CopilotSessionEvidence | None

    def matches(self, snapshot: DiscoveryPaneSnapshot, /) -> bool:
        """Return True when ``snapshot`` describes the same pane state.

        ``pane_activity`` must be present *and* equal: tmux returns
        the field as epoch seconds, and a missing or zero value
        (legacy tmux or a pane that has never written output) is
        normalized to ``None`` by the parser. Treating ``None`` as
        "unknown — re-capture" keeps the optimization safe across
        environments.
        """
        if snapshot.pane_activity is None or self.pane_activity is None:
            return False
        if snapshot.pane_activity != self.pane_activity:
            return False
        if snapshot.pane_pid != self.pane_pid:
            return False
        return snapshot.pane_tty == self.pane_tty


class DiscoveryService:
    def __init__(
        self,
        tmux: DiscoveryTmuxGateway,
        copilot: DiscoveryCopilotGateway,
        store: DiscoveryStore,
        *,
        process_inspector: DiscoveryProcessInspector | None = None,
        capture_start_line: int = -200,
        ignore_pane_ids: frozenset[str] = frozenset(),
        clock: Clock = utc_now,
    ) -> None:
        self._tmux = tmux
        self._copilot = copilot
        self._store = store
        self._process_inspector = process_inspector
        self._capture_start_line = capture_start_line
        self._ignore_pane_ids = ignore_pane_ids
        self._clock = clock
        # Cache of per-pane capture work keyed by pane_id. Holds the
        # subprocess-derived state (captured output, command detection,
        # session evidence) so the next cycle can skip the
        # ``capture-pane`` fork + ``interpret_output`` parse when the
        # pane reports the same ``pane_activity`` timestamp it had
        # last time. Invalidated when ``pane_pid`` or ``pane_tty``
        # change so a respawned pane is always re-discovered.
        self._capture_cache: dict[str, _PaneCaptureCacheEntry] = {}

    def discover_panes(self) -> PaneDiscoveryReport:
        with timed("discovery.total"):
            discovered_at = ensure_aware_datetime(self._clock(), field_name="value")
            seen_pane_ids: set[str] = set()
            panes_list: list[PaneDiscovery] = []
            for record in self._iter_panes():
                discovery = self._discover_single(record, discovered_at=discovered_at)
                panes_list.append(discovery)
                seen_pane_ids.add(discovery.snapshot.pane_id)
            self._evict_missing_panes(seen_pane_ids)
            panes = tuple(panes_list)
            managed = tuple(pane for pane in panes if pane.classification == "managed_agent")
            probable = tuple(
                pane for pane in panes if pane.classification == "unmanaged_probable_agent"
            )
            non_agent = tuple(pane for pane in panes if pane.classification == "non_agent_pane")
            return PaneDiscoveryReport(
                discovered_at=discovered_at,
                panes=panes,
                managed_agents=managed,
                unmanaged_probable_agents=probable,
                non_agent_panes=non_agent,
            )

    def _discover_single(
        self,
        record: PaneMetadataLike,
        /,
        *,
        discovered_at: datetime,
    ) -> PaneDiscovery:
        snapshot = DiscoveryPaneSnapshot.from_tmux_record(record)
        # SQL ownership lookups always run so a store mutation
        # (delete, reassign, rename) is reflected immediately, even
        # when the A1 capture cache or A5 skip below short-circuit
        # the subprocess work. These calls are sub-millisecond.
        matched_context = self._store.get_session_context_by_tmux_pane_id(snapshot.pane_id)
        managed_agent = self._store.get_agent_by_pane_id(snapshot.pane_id)
        cached = self._capture_cache.get(snapshot.pane_id)
        if cached is not None and not cached.matches(snapshot):
            cached = None
        if cached is not None:
            command_detection = cached.command_detection
            captured_output = cached.captured_output
            session_evidence = cached.session_evidence
        else:
            command_detection = self._copilot.detect_command(snapshot.pane_current_command or "")
            if (
                not command_detection.is_likely_copilot
                and self._process_inspector is not None
                and snapshot.pane_pid is not None
            ):
                with timed("discovery.process_tree"):
                    command_detection = self._detect_via_process_tree(
                        snapshot.pane_pid,
                        fallback=command_detection,
                    )
            if self._can_skip_capture(
                snapshot,
                command_detection=command_detection,
                managed_agent=managed_agent,
                matched_context=matched_context,
            ):
                # A5: pane is provably non-agent. classify_pane will
                # deterministically return non_agent_pane regardless of
                # scrollback contents (see ``is_non_copilot`` branch),
                # so the capture-pane fork and interpret_output parse
                # would be pure overhead.
                captured_output = ""
                session_evidence = None
            else:
                with timed("discovery.capture_pane"):
                    captured_output = self._tmux.capture_pane(
                        snapshot.pane_id,
                        start_line=self._capture_start_line,
                        join_wrapped_lines=True,
                    )
                session_evidence = None
                if captured_output.strip():
                    with timed("discovery.interpret_output"):
                        session_evidence = self._copilot.interpret_output(captured_output)
            self._capture_cache[snapshot.pane_id] = _PaneCaptureCacheEntry(
                pane_pid=snapshot.pane_pid,
                pane_tty=snapshot.pane_tty,
                pane_activity=snapshot.pane_activity,
                command_detection=command_detection,
                captured_output=captured_output,
                session_evidence=session_evidence,
            )
        matched_session: Session | None = None
        if session_evidence is not None and session_evidence.copilot_session_id is not None:
            matched_session = self._store.get_session_by_copilot_session_id(
                session_evidence.copilot_session_id
            )
            if managed_agent is None:
                managed_agent = self._store.get_agent_by_copilot_session_id(
                    session_evidence.copilot_session_id
                )
        return classify_pane(
            snapshot,
            discovered_at=discovered_at,
            command_detection=command_detection,
            session_evidence=session_evidence,
            captured_output=captured_output,
            managed_agent=managed_agent,
            matched_session=matched_session,
            matched_context=matched_context,
        )

    @staticmethod
    def _can_skip_capture(
        snapshot: DiscoveryPaneSnapshot,
        *,
        command_detection: CopilotCommandDetection,
        managed_agent: Agent | None,
        matched_context: SessionContextRecord | None,
    ) -> bool:
        """Return True when the pane is provably not an agent.

        Conservative on purpose — the pane scrollback might still
        contain Copilot evidence, so we only skip the subprocess
        capture when *all four* of these are true:

        * the foreground command is a known non-Copilot AI CLI
          (claude, aider, cursor, cody, continue),
        * the in-process command + process-tree detection both
          agree this isn't Copilot,
        * no stored agent owns this pane id,
        * no stored session context owns this pane id.

        Under those conditions :func:`classify_pane` always returns
        ``non_agent_pane`` regardless of scrollback evidence, so the
        capture-pane fork and interpret_output parse are pure waste.
        """
        if command_detection.is_likely_copilot:
            return False
        if managed_agent is not None or matched_context is not None:
            return False
        return _is_non_copilot_command(snapshot.pane_current_command)

    def _evict_missing_panes(self, seen_pane_ids: set[str]) -> None:
        stale = [pane_id for pane_id in self._capture_cache if pane_id not in seen_pane_ids]
        for pane_id in stale:
            del self._capture_cache[pane_id]

    def _detect_via_process_tree(
        self,
        pane_pid: int,
        *,
        fallback: CopilotCommandDetection,
    ) -> CopilotCommandDetection:
        assert self._process_inspector is not None
        try:
            child_cmdlines = self._process_inspector.get_child_cmdlines(pane_pid)
        except Exception:
            return fallback
        for cmdline in child_cmdlines:
            detection = self._copilot.detect_command(cmdline)
            if detection.is_likely_copilot:
                return detection
        return fallback

    def _iter_panes(self) -> tuple[PaneMetadataLike, ...]:
        listed = self._tmux.list_panes()
        panes = getattr(listed, "panes", listed)
        if not isinstance(panes, Sequence):
            return ()
        return tuple(
            pane
            for pane in panes
            if isinstance(pane, TmuxPaneMetadata | TmuxPaneRecord)
            and getattr(pane, "pane_id", None) not in self._ignore_pane_ids
        )


def _has_session_signal(evidence: CopilotSessionEvidence | None, /) -> bool:
    if evidence is None:
        return False
    return any(
        (
            evidence.copilot_session_id is not None,
            bool(evidence.usage_snapshots),
            bool(evidence.blocking_issue_kinds),
            bool(evidence.error_messages),
            bool(evidence.parse_result.boundaries),
            bool(evidence.parse_result.ui_markers),
        )
    )


def _has_strong_session_signal(evidence: CopilotSessionEvidence | None, /) -> bool:
    """Require hard evidence — session id, usage, blocking issues, or errors.

    Weak signals like UI markers and transcript boundaries from old
    scrollback are not enough on their own.
    """
    if evidence is None:
        return False
    return any(
        (
            evidence.copilot_session_id is not None,
            bool(evidence.usage_snapshots),
            bool(evidence.blocking_issue_kinds),
            bool(evidence.error_messages),
        )
    )


def _is_shell_command(command: str | None, /) -> bool:
    if command is None:
        return False
    return command.strip().lower() in _SHELL_COMMANDS


def _is_non_copilot_command(command: str | None, /) -> bool:
    if command is None:
        return False
    return command.strip().lower() in _NON_COPILOT_AI_COMMANDS

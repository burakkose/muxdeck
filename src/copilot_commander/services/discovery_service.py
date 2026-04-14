from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Literal, Protocol, runtime_checkable

from copilot_commander.adapters.copilot_adapter import (
    CopilotCommandDetection,
    CopilotSessionEvidence,
)
from copilot_commander.adapters.sqlite_store import SessionContextRecord
from copilot_commander.adapters.tmux_adapter import TmuxPaneMetadata
from copilot_commander.domain.models import Agent, Session
from copilot_commander.domain.value_objects import ensure_aware_datetime, utc_now
from copilot_commander.parsers.tmux_parser import TmuxPaneRecord
from copilot_commander.types import Clock

PaneClassification = Literal["managed_agent", "unmanaged_probable_agent", "non_agent_pane"]
PaneMetadataLike = TmuxPaneMetadata | TmuxPaneRecord


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

    if managed_agent is not None or matched_session is not None or matched_context is not None:
        classification: PaneClassification = "managed_agent"
        confidence = Decimal("0.9900")
    elif command_detection.is_likely_copilot or _has_session_signal(session_evidence):
        classification = "unmanaged_probable_agent"
        confidence = Decimal("0.8400") if command_detection.is_likely_copilot else Decimal("0.7900")
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


class DiscoveryService:
    def __init__(
        self,
        tmux: DiscoveryTmuxGateway,
        copilot: DiscoveryCopilotGateway,
        store: DiscoveryStore,
        *,
        capture_start_line: int = -200,
        clock: Clock = utc_now,
    ) -> None:
        self._tmux = tmux
        self._copilot = copilot
        self._store = store
        self._capture_start_line = capture_start_line
        self._clock = clock

    def discover_panes(self) -> PaneDiscoveryReport:
        discovered_at = ensure_aware_datetime(self._clock(), field_name="value")
        panes = tuple(
            self._discover_single(record, discovered_at=discovered_at)
            for record in self._iter_panes()
        )
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
        command_detection = self._copilot.detect_command(snapshot.pane_current_command or "")
        captured_output = self._tmux.capture_pane(
            snapshot.pane_id,
            start_line=self._capture_start_line,
            join_wrapped_lines=True,
        )
        session_evidence = None
        if captured_output.strip():
            session_evidence = self._copilot.interpret_output(captured_output)
        matched_context = self._store.get_session_context_by_tmux_pane_id(snapshot.pane_id)
        managed_agent = self._store.get_agent_by_pane_id(snapshot.pane_id)
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

    def _iter_panes(self) -> tuple[PaneMetadataLike, ...]:
        listed = self._tmux.list_panes()
        panes = getattr(listed, "panes", listed)
        if not isinstance(panes, Sequence):
            return ()
        return tuple(pane for pane in panes if isinstance(pane, TmuxPaneMetadata | TmuxPaneRecord))


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
        )
    )

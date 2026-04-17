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
from copilot_commander.perf import timed
from copilot_commander.types import Clock

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

    is_non_copilot = _is_non_copilot_command(snapshot.pane_current_command)
    if is_non_copilot and not command_detection.is_likely_copilot:
        # Pane now runs a known non-copilot AI CLI (e.g. claude, aider).
        # Override even if we have a stored agent record from before.
        classification: PaneClassification = "non_agent_pane"
        confidence = Decimal("0.9500")
        reasons.append("known non-copilot AI CLI")
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

    def discover_panes(self) -> PaneDiscoveryReport:
        with timed("discovery.total"):
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
        with timed("discovery.capture_pane"):
            history_output = self._tmux.capture_pane(
                snapshot.pane_id,
                start_line=self._capture_start_line,
                join_wrapped_lines=True,
            )
            # tmux ``capture-pane -S <history> -E -`` reads from the
            # main screen's scrollback buffer. For panes whose foreground
            # process uses the alternate screen buffer (any full-screen
            # TUI — including ``copilot`` itself when launched from
            # pwsh on WSL) the alternate buffer's live content is NOT
            # part of that scrollback and is silently omitted, so the
            # capture stays frozen on whatever the main buffer last
            # showed (typically the ``copilot`` command the user typed
            # at the shell prompt). A second capture without ``-S``/``-E``
            # targets the currently visible screen, which is the
            # alternate buffer when one is active. We append it so the
            # log preview reflects what's really on screen and the
            # equality check downstream sees fresh content as the TUI
            # updates.
            visible_output = self._tmux.capture_pane(
                snapshot.pane_id,
                join_wrapped_lines=True,
            )
            captured_output = _merge_history_and_visible(history_output, visible_output)
        session_evidence = None
        if captured_output.strip():
            with timed("discovery.interpret_output"):
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


def _merge_history_and_visible(history: str, visible: str, /) -> str:
    """Return history+visible, deduping when history already ends with visible.

    For panes whose foreground process uses the *main* screen buffer the
    history capture (``-S -<n> -E -``) already includes the currently
    visible rows, so the visible re-capture is a strict suffix of
    history; appending it would just duplicate the last ~24 lines. For
    panes on the *alternate* screen buffer the history capture omits
    the live alt content entirely, so appending visible is what
    actually unblocks the pipeline.

    Comparing on rstripped, non-empty lines makes the suffix detection
    robust to trailing blank padding that tmux sometimes adds to one
    capture but not the other.
    """
    if not visible.strip():
        return history
    if not history.strip():
        return visible
    visible_lines = [line.rstrip() for line in visible.splitlines() if line.strip()]
    if not visible_lines:
        return history
    history_lines = [line.rstrip() for line in history.splitlines() if line.strip()]
    if (
        len(history_lines) >= len(visible_lines)
        and history_lines[-len(visible_lines) :] == visible_lines
    ):
        return history
    separator = "" if history.endswith("\n") else "\n"
    return f"{history}{separator}{visible}"


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

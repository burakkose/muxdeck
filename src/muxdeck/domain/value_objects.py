# ruff: noqa: E501

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import cast
from uuid import uuid4

from muxdeck.constants import DEFAULT_CURRENCY
from muxdeck.domain.enums import EvidenceKind
from muxdeck.exceptions import DomainValidationError
from muxdeck.types import CommandArgs, ConfidenceValue, JsonValue

_ZERO = Decimal("0")
_ONE = Decimal("1")
_MILLION = Decimal("1000000")
_CURRENCY_QUANTUM = Decimal("0.000001")
_CONFIDENCE_QUANTUM = Decimal("0.0001")


def utc_now() -> datetime:
    return datetime.now(UTC)


def ensure_aware_datetime(value: datetime, *, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        msg = f"{field_name} must be timezone-aware"
        raise DomainValidationError(msg)
    return value.astimezone(UTC)


def ensure_non_empty_text(value: str, *, field_name: str) -> str:
    normalized = value.strip()
    if not normalized:
        msg = f"{field_name} must be a non-empty string"
        raise DomainValidationError(msg)
    return normalized


def ensure_non_negative_int(value: int, *, field_name: str) -> int:
    if isinstance(value, bool) or value < 0:
        msg = f"{field_name} must be a non-negative integer"
        raise DomainValidationError(msg)
    return value


def ensure_non_negative_decimal(
    value: Decimal | str | int | float,
    *,
    field_name: str,
    quantize: Decimal | None = _CURRENCY_QUANTUM,
) -> Decimal:
    try:
        decimal_value = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        msg = f"{field_name} must be a valid decimal value"
        raise DomainValidationError(msg) from exc
    if decimal_value < _ZERO:
        msg = f"{field_name} must be non-negative"
        raise DomainValidationError(msg)
    if quantize is not None:
        return decimal_value.quantize(quantize)
    return decimal_value


def ensure_confidence(value: ConfidenceValue, *, field_name: str = "confidence") -> Decimal:
    confidence = ensure_non_negative_decimal(
        value, field_name=field_name, quantize=_CONFIDENCE_QUANTUM
    )
    if confidence > _ONE:
        msg = f"{field_name} must be between 0 and 1"
        raise DomainValidationError(msg)
    return confidence


def _new_identifier(prefix: str) -> str:
    return f"{prefix}-{uuid4()}"


@dataclass(frozen=True, slots=True, order=True)
class AgentId:
    value: str = field(default_factory=lambda: _new_identifier("agent"))

    def __post_init__(self) -> None:
        object.__setattr__(self, "value", ensure_non_empty_text(self.value, field_name="agent_id"))

    @classmethod
    def generate(cls) -> AgentId:
        return cls()

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True, order=True)
class WorktreeId:
    value: str = field(default_factory=lambda: _new_identifier("worktree"))

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "value", ensure_non_empty_text(self.value, field_name="worktree_id")
        )

    @classmethod
    def generate(cls) -> WorktreeId:
        return cls()

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True, order=True)
class SessionId:
    value: str = field(default_factory=lambda: _new_identifier("session"))

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "value", ensure_non_empty_text(self.value, field_name="session_id")
        )

    @classmethod
    def generate(cls) -> SessionId:
        return cls()

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True, order=True)
class EventId:
    value: str = field(default_factory=lambda: _new_identifier("event"))

    def __post_init__(self) -> None:
        object.__setattr__(self, "value", ensure_non_empty_text(self.value, field_name="event_id"))

    @classmethod
    def generate(cls) -> EventId:
        return cls()

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True, order=True)
class LogChunkId:
    value: str = field(default_factory=lambda: _new_identifier("logchunk"))

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "value", ensure_non_empty_text(self.value, field_name="log_chunk_id")
        )

    @classmethod
    def generate(cls) -> LogChunkId:
        return cls()

    def __str__(self) -> str:
        return self.value


def ensure_agent_id(value: AgentId | str, *, field_name: str = "agent_id") -> AgentId:
    if isinstance(value, AgentId):
        return value
    return AgentId(ensure_non_empty_text(value, field_name=field_name))


def ensure_worktree_id(
    value: WorktreeId | str,
    *,
    field_name: str = "worktree_id",
) -> WorktreeId:
    if isinstance(value, WorktreeId):
        return value
    return WorktreeId(ensure_non_empty_text(value, field_name=field_name))


def ensure_session_id(value: SessionId | str, *, field_name: str = "session_id") -> SessionId:
    if isinstance(value, SessionId):
        return value
    return SessionId(ensure_non_empty_text(value, field_name=field_name))


def ensure_event_id(value: EventId | str, *, field_name: str = "event_id") -> EventId:
    if isinstance(value, EventId):
        return value
    return EventId(ensure_non_empty_text(value, field_name=field_name))


def ensure_log_chunk_id(
    value: LogChunkId | str,
    *,
    field_name: str = "log_chunk_id",
) -> LogChunkId:
    if isinstance(value, LogChunkId):
        return value
    return LogChunkId(ensure_non_empty_text(value, field_name=field_name))


@dataclass(frozen=True, slots=True)
class TokenUsage:
    input_tokens: int = 0
    output_tokens: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "input_tokens",
            ensure_non_negative_int(self.input_tokens, field_name="input_tokens"),
        )
        object.__setattr__(
            self,
            "output_tokens",
            ensure_non_negative_int(self.output_tokens, field_name="output_tokens"),
        )

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def __add__(self, other: object) -> TokenUsage:
        if not isinstance(other, TokenUsage):
            return NotImplemented
        return TokenUsage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
        )


@dataclass(frozen=True, slots=True)
class CostBreakdown:
    input_cost: Decimal | str | int | float = _ZERO
    output_cost: Decimal | str | int | float = _ZERO
    currency: str = DEFAULT_CURRENCY
    estimated: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "input_cost",
            ensure_non_negative_decimal(self.input_cost, field_name="input_cost"),
        )
        object.__setattr__(
            self,
            "output_cost",
            ensure_non_negative_decimal(self.output_cost, field_name="output_cost"),
        )
        object.__setattr__(
            self, "currency", ensure_non_empty_text(self.currency, field_name="currency")
        )

    @property
    def total_cost(self) -> Decimal:
        input_cost = cast(Decimal, self.input_cost)
        output_cost = cast(Decimal, self.output_cost)
        return (input_cost + output_cost).quantize(_CURRENCY_QUANTUM)

    def __add__(self, other: object) -> CostBreakdown:
        if not isinstance(other, CostBreakdown):
            return NotImplemented
        if self.currency != other.currency:
            msg = "cost breakdown currency mismatch"
            raise DomainValidationError(msg)
        return CostBreakdown(
            input_cost=cast(Decimal, self.input_cost) + cast(Decimal, other.input_cost),
            output_cost=cast(Decimal, self.output_cost) + cast(Decimal, other.output_cost),
            currency=self.currency,
            estimated=self.estimated and other.estimated,
        )


@dataclass(frozen=True, slots=True)
class TokenPricing:
    input_token_cost_per_1m: Decimal | str | int | float = _ZERO
    output_token_cost_per_1m: Decimal | str | int | float = _ZERO
    currency: str = DEFAULT_CURRENCY

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "input_token_cost_per_1m",
            ensure_non_negative_decimal(
                self.input_token_cost_per_1m,
                field_name="input_token_cost_per_1m",
            ),
        )
        object.__setattr__(
            self,
            "output_token_cost_per_1m",
            ensure_non_negative_decimal(
                self.output_token_cost_per_1m,
                field_name="output_token_cost_per_1m",
            ),
        )
        object.__setattr__(
            self, "currency", ensure_non_empty_text(self.currency, field_name="currency")
        )

    def estimate_cost(self, usage: TokenUsage) -> CostBreakdown:
        input_rate = cast(Decimal, self.input_token_cost_per_1m)
        output_rate = cast(Decimal, self.output_token_cost_per_1m)
        return CostBreakdown(
            input_cost=(Decimal(usage.input_tokens) * input_rate) / _MILLION,
            output_cost=(Decimal(usage.output_tokens) * output_rate) / _MILLION,
            currency=self.currency,
            estimated=True,
        )


@dataclass(frozen=True, slots=True)
class ParserEvidence:
    source: str
    kind: EvidenceKind = EvidenceKind.RAW
    confidence: ConfidenceValue = _ONE
    summary: str | None = None
    raw_value: JsonValue | None = None
    derived_value: JsonValue | None = None
    observed_at: datetime | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "source", ensure_non_empty_text(self.source, field_name="source"))
        object.__setattr__(self, "confidence", ensure_confidence(self.confidence))
        if self.summary is not None:
            object.__setattr__(
                self, "summary", ensure_non_empty_text(self.summary, field_name="summary")
            )
        if self.observed_at is not None:
            object.__setattr__(
                self,
                "observed_at",
                ensure_aware_datetime(self.observed_at, field_name="observed_at"),
            )
        if self.kind is EvidenceKind.RAW:
            if self.raw_value is None and self.summary is None:
                msg = "raw evidence requires raw_value or summary"
                raise DomainValidationError(msg)
            if self.derived_value is not None:
                msg = "raw evidence cannot include derived_value"
                raise DomainValidationError(msg)
        if self.kind is EvidenceKind.DERIVED:
            if self.derived_value is None and self.summary is None:
                msg = "derived evidence requires derived_value or summary"
                raise DomainValidationError(msg)
            if self.raw_value is not None:
                msg = "derived evidence cannot include raw_value"
                raise DomainValidationError(msg)


@dataclass(frozen=True, slots=True)
class CommandResult:
    command: CommandArgs
    exit_code: int
    started_at: datetime
    finished_at: datetime
    stdout: str = ""
    stderr: str = ""
    cwd: Path | None = None

    def __post_init__(self) -> None:
        if not self.command:
            msg = "command must not be empty"
            raise DomainValidationError(msg)
        object.__setattr__(
            self, "started_at", ensure_aware_datetime(self.started_at, field_name="started_at")
        )
        object.__setattr__(
            self, "finished_at", ensure_aware_datetime(self.finished_at, field_name="finished_at")
        )
        if self.finished_at < self.started_at:
            msg = "finished_at cannot precede started_at"
            raise DomainValidationError(msg)
        if self.cwd is not None:
            object.__setattr__(self, "cwd", self.cwd.expanduser().resolve(strict=False))

    @property
    def succeeded(self) -> bool:
        return self.exit_code == 0

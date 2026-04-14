from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
import json
from typing import Protocol

from copilot_commander.adapters.copilot_adapter import CopilotSessionEvidence
from copilot_commander.config import AppConfig
from copilot_commander.constants import DEFAULT_CURRENCY
from copilot_commander.domain.events import Event
from copilot_commander.domain.models import Session
from copilot_commander.domain.value_objects import TokenUsage, utc_now
from copilot_commander.exceptions import PersistenceError
from copilot_commander.types import JsonValue

_COST_EVENT_KIND = "costing.usage_recorded"


class CostingStorePort(Protocol):
    def append_events(self, events: Sequence[Event], /) -> None: ...

    def list_events(
        self,
        /,
        *,
        agent_id: str | None = None,
        session_id: str | None = None,
    ) -> Sequence[Event]: ...

    def get_session(self, session_id: str, /) -> Session | None: ...


@dataclass(frozen=True, slots=True)
class CostBucket:
    currency: str
    estimated: bool
    input_cost: Decimal
    output_cost: Decimal

    @property
    def total_cost(self) -> Decimal:
        return self.input_cost + self.output_cost


@dataclass(frozen=True, slots=True)
class CostEvidence:
    source: str
    observed_at: datetime
    raw_payload: JsonValue


@dataclass(frozen=True, slots=True)
class CostFact:
    session_id: str
    agent_id: str
    observed_at: datetime
    input_tokens: int | None
    output_tokens: int | None
    total_tokens: int | None
    actual_cost: CostBucket | None
    estimated_cost: CostBucket | None
    evidence: CostEvidence


@dataclass(frozen=True, slots=True)
class CostRecordResult:
    event: Event
    fact: CostFact


@dataclass(frozen=True, slots=True)
class CostAggregate:
    scope: str
    key: str
    input_tokens: int
    output_tokens: int
    total_tokens: int
    cost_buckets: tuple[CostBucket, ...]
    evidence_count: int
    actual_evidence_count: int
    estimated_evidence_count: int


class CostingService:
    def __init__(
        self,
        *,
        config: AppConfig,
        store: CostingStorePort,
        clock: callable = utc_now,
    ) -> None:
        self._config = config
        self._store = store
        self._clock = clock

    def derive_usage_fact(
        self,
        session_id: str,
        agent_id: str,
        evidence: CopilotSessionEvidence,
        *,
        source: str = "copilot_output",
        observed_at: datetime | None = None,
    ) -> CostFact:
        snapshot = evidence.latest_usage or (evidence.usage_snapshots[-1] if evidence.usage_snapshots else None)
        if snapshot is None:
            msg = "copilot evidence does not contain a usage snapshot"
            raise PersistenceError(msg)
        input_tokens = snapshot.input_tokens
        output_tokens = snapshot.output_tokens
        if snapshot.total_tokens is not None:
            total_tokens = snapshot.total_tokens
        elif input_tokens is not None or output_tokens is not None:
            total_tokens = (input_tokens or 0) + (output_tokens or 0)
        else:
            total_tokens = None
        actual_cost = None
        if snapshot.cost is not None:
            actual_cost = CostBucket(
                currency=snapshot.currency or DEFAULT_CURRENCY,
                estimated=False,
                input_cost=Decimal("0"),
                output_cost=snapshot.cost,
            )
        estimated_cost = None
        if self._config.costing.estimation_enabled and input_tokens is not None and output_tokens is not None:
            estimate = self._config.costing.pricing.estimate_cost(
                TokenUsage(input_tokens=input_tokens, output_tokens=output_tokens)
            )
            estimated_cost = CostBucket(
                currency=estimate.currency,
                estimated=True,
                input_cost=estimate.input_cost,
                output_cost=estimate.output_cost,
            )
        observed = observed_at or self._clock()
        return CostFact(
            session_id=session_id,
            agent_id=agent_id,
            observed_at=observed,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
            actual_cost=actual_cost,
            estimated_cost=estimated_cost,
            evidence=CostEvidence(
                source=source,
                observed_at=observed,
                raw_payload=self._raw_payload(source=source, evidence=evidence),
            ),
        )

    def record_usage_evidence(
        self,
        session_id: str,
        evidence: CopilotSessionEvidence,
        *,
        agent_id: str | None = None,
        source: str = "copilot_output",
        observed_at: datetime | None = None,
    ) -> CostRecordResult:
        session = self._store.get_session(session_id)
        if session is None:
            msg = f"unknown session: {session_id}"
            raise PersistenceError(msg)
        fact = self.derive_usage_fact(
            session_id,
            agent_id or session.agent_id,
            evidence,
            source=source,
            observed_at=observed_at,
        )
        event = Event(
            occurred_at=fact.observed_at,
            agent_id=fact.agent_id,
            session_id=fact.session_id,
            kind=_COST_EVENT_KIND,
            payload_json=self._serialize_fact(fact),
        )
        self._store.append_events((event,))
        return CostRecordResult(event=event, fact=fact)

    def summarize_session(self, session_id: str) -> CostAggregate:
        return self._aggregate(
            scope="session",
            key=session_id,
            events=self._store.list_events(session_id=session_id),
        )

    def summarize_agent(self, agent_id: str) -> CostAggregate:
        return self._aggregate(
            scope="agent",
            key=agent_id,
            events=self._store.list_events(agent_id=agent_id),
        )

    def summarize_day(self, day: date | datetime | str) -> CostAggregate:
        normalized_day = self._normalize_day(day)
        all_events = self._store.list_events()
        return self._aggregate(
            scope="day",
            key=normalized_day.isoformat(),
            events=[event for event in all_events if event.occurred_at.date() == normalized_day],
        )

    def _aggregate(self, *, scope: str, key: str, events: Sequence[Event]) -> CostAggregate:
        facts = [fact for event in events if (fact := self._deserialize_fact(event)) is not None]
        bucket_map: dict[tuple[str, bool], CostBucket] = {}
        input_tokens = 0
        output_tokens = 0
        total_tokens = 0
        actual_count = 0
        estimated_count = 0
        for fact in facts:
            input_tokens += fact.input_tokens or 0
            output_tokens += fact.output_tokens or 0
            if fact.total_tokens is not None:
                total_tokens += fact.total_tokens
            elif fact.input_tokens is not None or fact.output_tokens is not None:
                total_tokens += (fact.input_tokens or 0) + (fact.output_tokens or 0)
            if fact.actual_cost is not None:
                actual_count += 1
                self._merge_bucket(bucket_map, fact.actual_cost)
            if fact.estimated_cost is not None:
                estimated_count += 1
                self._merge_bucket(bucket_map, fact.estimated_cost)
        cost_buckets = tuple(
            bucket_map[key_]
            for key_ in sorted(bucket_map, key=lambda item: (item[0], item[1]))
        )
        return CostAggregate(
            scope=scope,
            key=key,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
            cost_buckets=cost_buckets,
            evidence_count=len(facts),
            actual_evidence_count=actual_count,
            estimated_evidence_count=estimated_count,
        )

    def _merge_bucket(
        self,
        bucket_map: dict[tuple[str, bool], CostBucket],
        bucket: CostBucket,
    ) -> None:
        key = (bucket.currency, bucket.estimated)
        current = bucket_map.get(key)
        if current is None:
            bucket_map[key] = bucket
            return
        bucket_map[key] = CostBucket(
            currency=bucket.currency,
            estimated=bucket.estimated,
            input_cost=current.input_cost + bucket.input_cost,
            output_cost=current.output_cost + bucket.output_cost,
        )

    def _serialize_fact(self, fact: CostFact) -> str:
        return json.dumps(
            {
                "raw_evidence": fact.evidence.raw_payload,
                "derived_fact": {
                    "session_id": fact.session_id,
                    "agent_id": fact.agent_id,
                    "observed_at": fact.observed_at.isoformat(),
                    "input_tokens": fact.input_tokens,
                    "output_tokens": fact.output_tokens,
                    "total_tokens": fact.total_tokens,
                    "actual_cost": self._bucket_payload(fact.actual_cost),
                    "estimated_cost": self._bucket_payload(fact.estimated_cost),
                },
            },
            sort_keys=True,
            separators=(",", ":"),
        )

    def _deserialize_fact(self, event: Event) -> CostFact | None:
        if event.kind != _COST_EVENT_KIND:
            return None
        payload = json.loads(event.payload_json)
        derived = payload.get("derived_fact", {})
        raw = payload.get("raw_evidence", {})
        return CostFact(
            session_id=str(derived.get("session_id") or event.session_id or ""),
            agent_id=str(derived.get("agent_id") or event.agent_id or ""),
            observed_at=datetime.fromisoformat(str(derived["observed_at"])),
            input_tokens=self._optional_int(derived.get("input_tokens")),
            output_tokens=self._optional_int(derived.get("output_tokens")),
            total_tokens=self._optional_int(derived.get("total_tokens")),
            actual_cost=self._payload_to_bucket(derived.get("actual_cost")),
            estimated_cost=self._payload_to_bucket(derived.get("estimated_cost")),
            evidence=CostEvidence(
                source=str(raw.get("source") or "copilot_output"),
                observed_at=event.occurred_at,
                raw_payload=raw,
            ),
        )

    def _payload_to_bucket(self, payload: JsonValue) -> CostBucket | None:
        if not isinstance(payload, dict):
            return None
        currency = payload.get("currency")
        estimated = payload.get("estimated")
        input_cost = payload.get("input_cost")
        output_cost = payload.get("output_cost")
        if not isinstance(currency, str) or not isinstance(estimated, bool):
            return None
        return CostBucket(
            currency=currency,
            estimated=estimated,
            input_cost=Decimal(str(input_cost or 0)),
            output_cost=Decimal(str(output_cost or 0)),
        )

    def _bucket_payload(self, bucket: CostBucket | None) -> JsonValue:
        if bucket is None:
            return None
        return {
            "currency": bucket.currency,
            "estimated": bucket.estimated,
            "input_cost": str(bucket.input_cost),
            "output_cost": str(bucket.output_cost),
        }

    def _raw_payload(self, *, source: str, evidence: CopilotSessionEvidence) -> JsonValue:
        return {
            "source": source,
            "copilot_session_id": evidence.copilot_session_id,
            "session_ids": list(evidence.session_ids),
            "blocking_issue_kinds": list(evidence.blocking_issue_kinds),
            "error_messages": list(evidence.error_messages),
            "usage_snapshots": [
                {
                    "input_tokens": snapshot.input_tokens,
                    "output_tokens": snapshot.output_tokens,
                    "total_tokens": snapshot.total_tokens,
                    "cost": None if snapshot.cost is None else str(snapshot.cost),
                    "currency": snapshot.currency,
                }
                for snapshot in evidence.usage_snapshots
            ],
        }

    def _normalize_day(self, value: date | datetime | str) -> date:
        if isinstance(value, datetime):
            return value.date()
        if isinstance(value, date):
            return value
        return date.fromisoformat(value)

    def _optional_int(self, value: JsonValue) -> int | None:
        return value if isinstance(value, int) else None


__all__ = [
    "CostAggregate",
    "CostBucket",
    "CostEvidence",
    "CostFact",
    "CostRecordResult",
    "CostingService",
]

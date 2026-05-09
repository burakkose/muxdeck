"""Synthetic benchmark runner for hot dashboard and sync paths.

Run from the repository root:

    source .venv/bin/activate
    PYTHONPATH=src python tests/benchmarks/dashboard_benchmarks.py

The cases are intentionally warm, in-memory benchmarks. They validate that
critical monitoring and dashboard page-load paths stay cheap on realistic
fixture sizes without depending on tmux, the network, or filesystem scans.
"""

from __future__ import annotations

import gc
import math
import statistics
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

from muxdeck.adapters.copilot_adapter import CopilotAdapter
from muxdeck.adapters.sqlite_store import SessionContextRecord
from muxdeck.controllers.dashboard_controller import DashboardController
from muxdeck.domain.enums import AgentStatus
from muxdeck.domain.events import Event, LogChunk
from muxdeck.domain.models import Agent, Session, Worktree
from muxdeck.domain.value_objects import CommandResult
from muxdeck.services.agent_service import AgentFactInput
from muxdeck.services.discovery_service import DiscoveryPaneSnapshot, PaneDiscovery
from muxdeck.services.monitoring_service import (
    MonitoringDiscovery,
    MonitoringLocalSessionStore,
    MonitoringService,
)
from muxdeck.widgets.dashboard import (
    AgentDetailPanel,
    AgentListPanel,
    AlertPanel,
    LogPreviewPanel,
    StatusBar,
)


@dataclass(frozen=True, slots=True)
class BenchmarkCase:
    name: str
    description: str
    iterations: int
    warmups: int
    max_avg_ms: float
    max_p95_ms: float
    func: Callable[[], int]


@dataclass(frozen=True, slots=True)
class BenchmarkResult:
    name: str
    iterations: int
    avg_ms: float
    p95_ms: float
    max_ms: float


class DummyRunner:
    def run(
        self,
        command: Sequence[str],
        /,
        *,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        timeout_sec: float | None = None,
    ) -> CommandResult:
        del command, cwd, env, timeout_sec
        raise AssertionError("unexpected subprocess call during benchmark")


@dataclass(slots=True)
class FakeRecorder:
    recorded: list[AgentFactInput]

    def persist_agent_facts(self, facts: AgentFactInput, /) -> AgentFactInput:
        self.recorded.append(facts)
        return facts


@dataclass(slots=True)
class FakeLocalSessionUsage:
    input_tokens: int | None = None
    output_tokens: int | None = None


@dataclass(slots=True)
class FakeLocalSession:
    session_id: str
    usage: FakeLocalSessionUsage | None = None


@dataclass(slots=True)
class FakeLocalSessionStore:
    sessions: tuple[FakeLocalSession, ...]

    def discover(self, *, force: bool = False) -> tuple[FakeLocalSession, ...]:
        del force
        return self.sessions


class BenchmarkDashboardStore:
    def __init__(self) -> None:
        self.agents: dict[str, Agent] = {}
        self.sessions: dict[str, Session] = {}
        self.contexts: dict[str, SessionContextRecord] = {}
        self.events: list[Event] = []
        self.logs: list[LogChunk] = []
        self.worktrees: dict[str, Worktree] = {}

    def list_agents(self) -> tuple[Agent, ...]:
        return tuple(
            sorted(
                self.agents.values(),
                key=lambda agent: agent.last_seen_at,
                reverse=True,
            )
        )

    def get_agent(self, agent_id: str, /) -> Agent | None:
        return self.agents.get(agent_id)

    def list_sessions(self, agent_id: str | None = None, /) -> tuple[Session, ...]:
        sessions = tuple(
            sorted(
                self.sessions.values(),
                key=lambda session: session.created_at,
                reverse=True,
            )
        )
        if agent_id is None:
            return sessions
        return tuple(session for session in sessions if session.agent_id == agent_id)

    def get_latest_session_for_agent(self, agent_id: str, /) -> Session | None:
        sessions = self.list_sessions(agent_id)
        return sessions[0] if sessions else None

    def count_sessions_for_agent(self, agent_id: str, /) -> int:
        return len(self.list_sessions(agent_id))

    def get_open_session_for_agent(self, agent_id: str, /) -> Session | None:
        return next(
            (session for session in self.list_sessions(agent_id) if session.ended_at is None), None
        )

    def get_session_context(self, session_id: str, /) -> SessionContextRecord | None:
        return self.contexts.get(session_id)

    def list_events_for_session(self, session_id: str, /) -> tuple[Event, ...]:
        return tuple(event for event in self.events if event.session_id == session_id)

    def get_latest_event_for_session(self, session_id: str, /) -> Event | None:
        events = self.list_events_for_session(session_id)
        return events[-1] if events else None

    def list_log_chunks(self, session_id: str, /) -> tuple[LogChunk, ...]:
        return tuple(chunk for chunk in self.logs if chunk.session_id == session_id)

    def get_latest_log_chunk(self, session_id: str, /) -> LogChunk | None:
        chunks = self.list_log_chunks(session_id)
        return chunks[-1] if chunks else None

    def list_recent_log_chunks(
        self,
        session_id: str,
        /,
        *,
        limit: int = 20,
    ) -> tuple[LogChunk, ...]:
        return self.list_log_chunks(session_id)[-limit:]

    def get_worktree(self, worktree_id: str, /) -> Worktree | None:
        return self.worktrees.get(worktree_id)


def _p95(samples: Sequence[float]) -> float:
    ordered = sorted(samples)
    index = min(len(ordered) - 1, max(0, math.ceil(len(ordered) * 0.95) - 1))
    return ordered[index]


def _render_length(renderable: object) -> int:
    plain = getattr(renderable, "plain", None)
    if isinstance(plain, str):
        return len(plain)
    return len(str(renderable))


def run_case(case: BenchmarkCase) -> BenchmarkResult:
    for _ in range(case.warmups):
        case.func()

    samples: list[float] = []
    was_enabled = gc.isenabled()
    gc.disable()
    try:
        for _ in range(case.iterations):
            start = time.perf_counter()
            case.func()
            samples.append((time.perf_counter() - start) * 1000.0)
    finally:
        if was_enabled:
            gc.enable()

    return BenchmarkResult(
        name=case.name,
        iterations=case.iterations,
        avg_ms=statistics.fmean(samples),
        p95_ms=_p95(samples),
        max_ms=max(samples),
    )


def build_monitoring_case() -> BenchmarkCase:
    now = datetime(2025, 1, 1, 12, tzinfo=UTC)
    recorder = FakeRecorder(recorded=[])
    copilot = CopilotAdapter(DummyRunner())
    discoveries: list[PaneDiscovery] = []
    local_sessions: list[FakeLocalSession] = []
    for index in range(32):
        session_id = f"copilot-{index:03d}"
        evidence = copilot.interpret_output(
            f"Copilot session id: {session_id}\nreading src/module_{index}.py\n"
        )
        discoveries.append(
            PaneDiscovery(
                snapshot=DiscoveryPaneSnapshot(
                    pane_id=f"%{index + 1}",
                    tmux_session_name="muxdeck",
                    tmux_window_id=f"@{index + 1}",
                    tmux_window_name="agents",
                    pane_current_path=f"/repo/worktrees/task-{index:03d}",
                    pane_current_command="copilot chat",
                    pane_pid=10_000 + index,
                    repo_root="/repo",
                    branch=f"task/{index:03d}",
                ),
                discovered_at=now,
                classification="unmanaged_probable_agent",
                reasons=("command:copilot_binary",),
                command_detection=copilot.detect_command("copilot chat"),
                captured_output=f"Copilot session id: {session_id}",
                session_evidence=evidence,
            )
        )
        local_sessions.append(
            FakeLocalSession(
                session_id=session_id,
                usage=FakeLocalSessionUsage(
                    input_tokens=100 + index,
                    output_tokens=40 + index,
                ),
            )
        )
    service = MonitoringService(
        recorder,
        local_session_store=cast(
            MonitoringLocalSessionStore,
            FakeLocalSessionStore(tuple(local_sessions)),
        ),
        clock=lambda: now,
    )
    monitoring_discoveries = cast(Sequence[MonitoringDiscovery], tuple(discoveries))

    def run() -> int:
        recorder.recorded.clear()
        report = service.monitor_discoveries(monitoring_discoveries)
        return len(report.results) + sum(facts.token_total or 0 for facts in recorder.recorded)

    return BenchmarkCase(
        name="monitoring.monitor_discoveries",
        description="Persist monitoring facts with cached local session usage.",
        iterations=80,
        warmups=10,
        max_avg_ms=12.0,
        max_p95_ms=18.0,
        func=run,
    )


def _build_dashboard_store(*, now: datetime, agent_count: int) -> BenchmarkDashboardStore:
    store = BenchmarkDashboardStore()
    for index in range(agent_count):
        agent_id = f"agent-{index:03d}"
        session_id = f"session-{index:03d}"
        pane_id = f"%{index + 1}"
        worktree_id = f"worktree-{index:03d}"
        last_seen = now - timedelta(seconds=index)
        last_activity = last_seen - timedelta(seconds=index % 20)
        status = AgentStatus.RUNNING
        needs_attention = index % 7 == 0
        attention_reason = "waiting for confirmation input" if needs_attention else None
        token_input = 400 + index
        token_output = 150 + index
        store.agents[agent_id] = Agent(
            id=agent_id,
            name=f"Planner {index:03d}",
            tmux_session_name="muxdeck",
            tmux_window_id=f"@{index + 1}",
            tmux_window_name=f"window-{index:03d}",
            tmux_pane_id=pane_id,
            cwd=f"/repo/worktrees/task-{index:03d}",
            repo_root="/repo",
            worktree_path=f"/repo/worktrees/task-{index:03d}",
            branch=f"task/{index:03d}",
            task_title=f"Review task {index:03d}",
            status=status,
            started_at=now - timedelta(minutes=10, seconds=index),
            last_activity_at=last_activity,
            last_seen_at=last_seen,
            idle_seconds=index % 45,
            needs_attention=needs_attention,
            attention_reason=attention_reason,
            token_input=token_input,
            token_output=token_output,
            token_total=token_input + token_output,
        )
        store.sessions[session_id] = Session(
            id=session_id,
            agent_id=agent_id,
            task_title=f"Review task {index:03d}",
            created_at=now - timedelta(minutes=20, seconds=index),
        )
        store.contexts[session_id] = SessionContextRecord(
            session_id=session_id,
            agent_id=agent_id,
            worktree_id=worktree_id,
            tmux_pane_id=pane_id,
            worktree_path=f"/repo/worktrees/task-{index:03d}",
            repo_root="/repo",
            branch=f"task/{index:03d}",
            updated_at=last_seen,
        )
        store.worktrees[worktree_id] = Worktree(
            id=worktree_id,
            repo_root="/repo",
            path=f"/repo/worktrees/task-{index:03d}",
            branch=f"task/{index:03d}",
            base_branch="main",
            last_seen_at=last_seen,
        )
        store.events.append(
            Event(
                id=f"event-{index:03d}",
                occurred_at=last_seen,
                agent_id=agent_id,
                session_id=session_id,
                kind="agent.updated",
                severity="warning" if needs_attention else "info",
                payload_json='{"kind":"activity"}',
            )
        )
        for chunk_index in range(12):
            store.logs.append(
                LogChunk(
                    id=f"log-{index:03d}-{chunk_index:02d}",
                    agent_id=agent_id,
                    session_id=session_id,
                    source="stdout",
                    sequence_no=chunk_index,
                    captured_at=last_seen + timedelta(milliseconds=chunk_index),
                    content=(
                        f"step {chunk_index}: inspecting file_{index:03d}.py\n"
                        f"line {chunk_index}: completed check\n"
                    ),
                )
            )
    return store


def build_dashboard_build_case() -> tuple[BenchmarkCase, DashboardController]:
    now = datetime(2025, 1, 1, 12, tzinfo=UTC)
    store = _build_dashboard_store(now=now, agent_count=48)
    controller = DashboardController(store, clock=lambda: now)

    def run() -> int:
        state = controller.build_state(selected_agent_id="agent-000", preview_line_limit=8)
        selected = state.selected_agent
        return len(state.agents) + (len(selected.log_preview) if selected is not None else 0)

    case = BenchmarkCase(
        name="dashboard.build_state",
        description="Build dashboard list, metrics, alerts, and selected detail.",
        iterations=60,
        warmups=8,
        max_avg_ms=18.0,
        max_p95_ms=28.0,
        func=run,
    )
    return case, controller


def build_dashboard_render_case(controller: DashboardController) -> BenchmarkCase:
    state = controller.build_state(selected_agent_id="agent-000", preview_line_limit=8)

    def run() -> int:
        selected_item = state.selected_agent.item if state.selected_agent is not None else None
        status_bar = StatusBar()
        status_bar.set_state(state.health, state.metrics, selected_item)

        list_panel = AgentListPanel(widget_id="bench-list")
        list_panel._agents = state.agents
        list_panel._selected_index = 0
        table = list_panel._build_table()

        detail_panel = AgentDetailPanel()
        detail_panel.set_agent(state.selected_agent)

        log_panel = LogPreviewPanel()
        log_panel.set_logs(state.selected_agent)

        alert_panel = AlertPanel()
        alert_panel.set_alerts(state.alerts)

        status_render = status_bar.render()
        detail_render = detail_panel.render()
        log_render = log_panel.render()
        alert_render = alert_panel.render()
        return (
            len(table.columns)
            + _render_length(status_render)
            + _render_length(detail_render)
            + _render_length(log_render)
            + _render_length(alert_render)
        )

    return BenchmarkCase(
        name="dashboard.prepare_widgets",
        description="Prepare dashboard widget renderables for a page refresh.",
        iterations=40,
        warmups=5,
        max_avg_ms=25.0,
        max_p95_ms=35.0,
        func=run,
    )


def main() -> int:
    cases: list[BenchmarkCase] = []
    monitoring_case = build_monitoring_case()
    build_case, controller = build_dashboard_build_case()
    render_case = build_dashboard_render_case(controller)
    cases.extend((monitoring_case, build_case, render_case))

    print("name                          avg ms   p95 ms   max ms   budget")
    print("----------------------------  -------  -------  -------  ----------------")

    failures: list[str] = []
    for case in cases:
        result = run_case(case)
        budget = f"avg<={case.max_avg_ms:.1f} p95<={case.max_p95_ms:.1f}"
        print(
            f"{result.name:<28}  {result.avg_ms:>7.2f}  {result.p95_ms:>7.2f}  "
            f"{result.max_ms:>7.2f}  {budget}"
        )
        if result.avg_ms > case.max_avg_ms or result.p95_ms > case.max_p95_ms:
            failures.append(f"{result.name}: avg {result.avg_ms:.2f}ms / p95 {result.p95_ms:.2f}ms")

    if failures:
        print("\nbenchmark thresholds exceeded:")
        for failure in failures:
            print(f"  - {failure}")
        return 1

    print("\nall benchmark thresholds satisfied")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

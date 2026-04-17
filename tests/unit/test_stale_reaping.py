# ruff: noqa: ANN001,ANN201,ANN202,ANN003,E501,F841

"""Tests for stale agent reaping and agent name derivation."""

from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta

from copilot_commander.domain.enums import AgentStatus
from copilot_commander.domain.models import Agent
from copilot_commander.services.monitoring_service import _derive_agent_name

_TS = datetime(2025, 1, 1, tzinfo=UTC)


def _make_agent(
    *,
    agent_id: str = "agent-1",
    pane_id: str = "%1",
    status: AgentStatus = AgentStatus.RUNNING,
    last_seen_at: datetime = _TS,
    name: str = "node",
) -> Agent:
    return Agent(
        id=agent_id,
        name=name,
        tmux_session_name="main",
        tmux_window_id="@0",
        tmux_pane_id=pane_id,
        cwd="/home/user/project",
        status=status,
        started_at=_TS,
        last_seen_at=last_seen_at,
        idle_seconds=0,
        needs_attention=False,
    )


class TestDeriveAgentName(unittest.TestCase):
    def test_prefers_repo_root_basename(self):
        result = _derive_agent_name(
            repo_root="/home/user/projects/tachyon",
            cwd="/home/user/projects/tachyon",
            existing_name="node",
        )
        assert result == "tachyon"

    def test_falls_back_to_cwd_basename(self):
        result = _derive_agent_name(
            repo_root=None,
            cwd="/home/user/projects/myapp",
            existing_name="node",
        )
        assert result == "myapp"

    def test_falls_back_to_existing_name(self):
        result = _derive_agent_name(
            repo_root=None,
            cwd="/",
            existing_name="copilot",
        )
        assert result == "copilot"

    def test_returns_none_when_all_empty(self):
        result = _derive_agent_name(
            repo_root=None,
            cwd="/",
            existing_name=None,
        )
        assert result is None

    def test_repo_root_overrides_existing_process_name(self):
        """Once repo_root is known, 'node' gets replaced."""
        result = _derive_agent_name(
            repo_root="/home/user/muxdeck",
            cwd="/home/user/muxdeck",
            existing_name="node",
        )
        assert result == "muxdeck"


class TestStaleAgentReaping(unittest.TestCase):
    def test_reaps_agent_whose_pane_is_gone(self):
        from copilot_commander.services.discovery_service import PaneDiscoveryReport
        from copilot_commander.services.runtime_service import RuntimeSynchronizer

        now = _TS + timedelta(seconds=60)
        stale_agent = _make_agent(
            pane_id="%99",
            last_seen_at=_TS,
        )
        upserted: list[Agent] = []

        class FakeDiscovery:
            def discover_panes(self):
                return PaneDiscoveryReport(
                    discovered_at=now,
                    panes=(),
                    managed_agents=(),
                    unmanaged_probable_agents=(),
                    non_agent_panes=(),
                )

        class FakeMonitoring:
            def monitor_discoveries(self, discoveries, /):
                from copilot_commander.services.monitoring_service import MonitoringReport

                return MonitoringReport(monitored_at=now, results=())

        class FakeGit:
            def discover_repo_root(self, cwd, /):
                raise NotImplementedError

            def current_branch(self, cwd, /):
                raise NotImplementedError

        class FakeAgentStore:
            def list_agents(self):
                return [stale_agent]

            def upsert_agent(self, agent, /):
                upserted.append(agent)

        sync = RuntimeSynchronizer(
            FakeDiscovery(),
            FakeMonitoring(),
            FakeGit(),
            agent_store=FakeAgentStore(),
            dead_grace_period_sec=10,
            clock=lambda: now,
        )
        report = sync.refresh()
        assert len(upserted) == 1
        assert upserted[0].status == AgentStatus.DEAD
        # Reaping used to set needs_attention=True with a "tmux pane
        # no longer exists" reason, but a post-mortem pane is not an
        # actionable signal — alerting on every reaped agent drowned
        # the dashboard in false positives. The agent is still
        # recorded as DEAD for history; it just no longer triggers
        # an alert.
        assert upserted[0].needs_attention is False
        assert upserted[0].attention_reason is None

    def test_respects_grace_period(self):
        from copilot_commander.services.discovery_service import PaneDiscoveryReport
        from copilot_commander.services.runtime_service import RuntimeSynchronizer

        now = _TS + timedelta(seconds=5)
        recent_agent = _make_agent(
            pane_id="%99",
            last_seen_at=_TS,
        )
        upserted: list[Agent] = []

        class FakeDiscovery:
            def discover_panes(self):
                return PaneDiscoveryReport(
                    discovered_at=now,
                    panes=(),
                    managed_agents=(),
                    unmanaged_probable_agents=(),
                    non_agent_panes=(),
                )

        class FakeMonitoring:
            def monitor_discoveries(self, discoveries, /):
                from copilot_commander.services.monitoring_service import MonitoringReport

                return MonitoringReport(monitored_at=now, results=())

        class FakeGit:
            def discover_repo_root(self, cwd, /):
                raise NotImplementedError

            def current_branch(self, cwd, /):
                raise NotImplementedError

        class FakeAgentStore:
            def list_agents(self):
                return [recent_agent]

            def upsert_agent(self, agent, /):
                upserted.append(agent)

        sync = RuntimeSynchronizer(
            FakeDiscovery(),
            FakeMonitoring(),
            FakeGit(),
            agent_store=FakeAgentStore(),
            dead_grace_period_sec=10,
            clock=lambda: now,
        )
        sync.refresh()
        assert len(upserted) == 0, "Should not reap within grace period"

    def test_skips_already_dead_agents(self):
        from copilot_commander.services.discovery_service import PaneDiscoveryReport
        from copilot_commander.services.runtime_service import RuntimeSynchronizer

        now = _TS + timedelta(seconds=60)
        dead_agent = _make_agent(
            pane_id="%99",
            status=AgentStatus.DEAD,
            last_seen_at=_TS,
        )
        upserted: list[Agent] = []

        class FakeDiscovery:
            def discover_panes(self):
                return PaneDiscoveryReport(
                    discovered_at=now,
                    panes=(),
                    managed_agents=(),
                    unmanaged_probable_agents=(),
                    non_agent_panes=(),
                )

        class FakeMonitoring:
            def monitor_discoveries(self, discoveries, /):
                from copilot_commander.services.monitoring_service import MonitoringReport

                return MonitoringReport(monitored_at=now, results=())

        class FakeGit:
            def discover_repo_root(self, cwd, /):
                raise NotImplementedError

            def current_branch(self, cwd, /):
                raise NotImplementedError

        class FakeAgentStore:
            def list_agents(self):
                return [dead_agent]

            def upsert_agent(self, agent, /):
                upserted.append(agent)

        sync = RuntimeSynchronizer(
            FakeDiscovery(),
            FakeMonitoring(),
            FakeGit(),
            agent_store=FakeAgentStore(),
            dead_grace_period_sec=10,
            clock=lambda: now,
        )
        sync.refresh()
        assert len(upserted) == 0, "Should skip already-dead agents"

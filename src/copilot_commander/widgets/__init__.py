from copilot_commander.widgets.common import KeyHintFooter, TabBar
from copilot_commander.widgets.dashboard import (
    ActivityPanel,
    AgentDetailPanel,
    AgentListPanel,
    AlertPanel,
    FleetHealthPanel,
    StatusBar,
)
from copilot_commander.widgets.replay import (
    ReplayDetailPanel,
    ReplayMarkerListPanel,
    ReplayTranscriptPanel,
)
from copilot_commander.widgets.setup import DoctorDetailPanel, SetupSummaryPanel, SocketListPanel
from copilot_commander.widgets.worktrees import (
    ConflictPanel,
    StartIntentPanel,
    WorktreeDetailPanel,
    WorktreeListPanel,
)

__all__ = [
    "ActivityPanel",
    "AgentDetailPanel",
    "AgentListPanel",
    "AlertPanel",
    "ConflictPanel",
    "DoctorDetailPanel",
    "FleetHealthPanel",
    "KeyHintFooter",
    "ReplayDetailPanel",
    "ReplayMarkerListPanel",
    "ReplayTranscriptPanel",
    "SetupSummaryPanel",
    "SocketListPanel",
    "StartIntentPanel",
    "StatusBar",
    "TabBar",
    "WorktreeDetailPanel",
    "WorktreeListPanel",
]

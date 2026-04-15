# ruff: noqa: I001,PT009,PT027

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
import shutil
import unittest

from copilot_commander.config import AppConfig, CostingConfig, load_config
from copilot_commander.constants import DEFAULT_WORKSPACE_ROOT
from copilot_commander.exceptions import ConfigValidationError


class ConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.runtime_dir = Path(__file__).resolve().parent / "_runtime"
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        self.addCleanup(self._cleanup_runtime_dir)

    def _cleanup_runtime_dir(self) -> None:
        if self.runtime_dir.exists():
            shutil.rmtree(self.runtime_dir)

    def test_load_config_uses_psd_defaults_when_missing(self) -> None:
        env = {
            "HOME": str(self.runtime_dir / "home"),
            "XDG_CONFIG_HOME": str(self.runtime_dir / "config-home"),
            "XDG_STATE_HOME": str(self.runtime_dir / "state-home"),
        }

        config = load_config(env=env)

        self.assertEqual(
            config.config_file,
            (self.runtime_dir / "config-home/copilot-commander/config.toml").resolve(),
        )
        self.assertEqual(
            config.paths.state_dir,
            (self.runtime_dir / "state-home/copilot-commander").resolve(),
        )
        self.assertEqual(
            config.paths.database_path,
            (self.runtime_dir / "state-home/copilot-commander/commander.db").resolve(),
        )
        self.assertEqual(
            config.paths.fallback_database_path,
            (self.runtime_dir / "home/.copilot-commander/commander.db").resolve(),
        )
        self.assertEqual(
            config.paths.workspace_root,
            (self.runtime_dir / "home/code/worktrees").resolve(),
        )
        self.assertEqual(config.general.discovery_interval_sec, 2)
        self.assertEqual(config.general.capture_interval_sec, 2)
        self.assertEqual(config.general.idle_threshold_sec, 45)
        self.assertEqual(config.general.dead_grace_period_sec, 10)
        self.assertEqual(config.general.log_preview_lines, 200)
        self.assertEqual(config.general.default_base_branch, "main")
        self.assertEqual(config.naming.branch_prefix, "task/")
        self.assertEqual(config.naming.worktree_pattern, "{repo}--{slug}")
        self.assertEqual(config.naming.agent_name_pattern, "{repo}:{slug}")
        self.assertEqual(config.costing.default_input_token_cost_per_1m, Decimal("0.000000"))
        self.assertEqual(config.costing.default_output_token_cost_per_1m, Decimal("0.000000"))
        self.assertTrue(config.costing.estimation_enabled)
        self.assertIsNone(config.tmux.socket_path)
        self.assertEqual(DEFAULT_WORKSPACE_ROOT, "~/code/worktrees")

    def test_load_config_parses_psd_sections_and_relative_paths(self) -> None:
        config_dir = self.runtime_dir / "config-root"
        config_dir.mkdir(parents=True, exist_ok=True)
        config_file = config_dir / "config.toml"
        config_file.write_text(
            """
[general]
discovery_interval_sec = 3
capture_interval_sec = 5
idle_threshold_sec = 60
dead_grace_period_sec = 12
log_preview_lines = 80
default_base_branch = "develop"

[paths]
state_dir = "./state"
workspace_root = "./worktrees"
database_path = "./state/data/commander.sqlite3"
fallback_database_path = "./legacy/commander.sqlite3"

[naming]
branch_prefix = "feat/"
worktree_pattern = "{repo}-{slug}"
agent_name_pattern = "{repo}/{slug}"

[costing]
default_input_token_cost_per_1m = "1.25"
default_output_token_cost_per_1m = "8.5"
estimation_enabled = true

[tmux]
socket_path = "./tmux/custom.sock"
""".strip(),
            encoding="utf-8",
        )

        config = load_config(config_file)

        self.assertEqual(config.general.discovery_interval_sec, 3)
        self.assertEqual(config.general.default_base_branch, "develop")
        self.assertEqual(config.paths.state_dir, (config_dir / "state").resolve())
        self.assertEqual(config.paths.workspace_root, (config_dir / "worktrees").resolve())
        self.assertEqual(
            config.paths.database_path,
            (config_dir / "state/data/commander.sqlite3").resolve(),
        )
        self.assertEqual(
            config.paths.fallback_database_path,
            (config_dir / "legacy/commander.sqlite3").resolve(),
        )
        self.assertEqual(config.naming.branch_prefix, "feat/")
        self.assertEqual(config.naming.worktree_name(repo="muxdeck", slug="abc"), "muxdeck-abc")
        self.assertEqual(config.naming.agent_name(repo="muxdeck", slug="abc"), "muxdeck/abc")
        self.assertEqual(config.costing.default_input_token_cost_per_1m, Decimal("1.250000"))
        self.assertEqual(config.costing.default_output_token_cost_per_1m, Decimal("8.500000"))
        self.assertTrue(config.costing.estimation_enabled)
        self.assertEqual(config.tmux.socket_path, (config_dir / "tmux/custom.sock").resolve())

    def test_costing_config_exposes_token_pricing(self) -> None:
        costing = CostingConfig(
            default_input_token_cost_per_1m="1",
            default_output_token_cost_per_1m="2",
        )

        pricing = costing.pricing

        self.assertEqual(pricing.input_token_cost_per_1m, Decimal("1.000000"))
        self.assertEqual(pricing.output_token_cost_per_1m, Decimal("2.000000"))

    def test_invalid_config_values_raise_validation_error(self) -> None:
        config_dir = self.runtime_dir / "invalid-config"
        config_dir.mkdir(parents=True, exist_ok=True)
        config_file = config_dir / "config.toml"
        config_file.write_text(
            """
[general]
discovery_interval_sec = 0

[naming]
worktree_pattern = "{repo}"
""".strip(),
            encoding="utf-8",
        )

        with self.assertRaises(ConfigValidationError):
            load_config(config_file)

    def test_unknown_config_keys_raise_validation_error(self) -> None:
        config_file = self.runtime_dir / "unknown-key.toml"
        config_file.write_text(
            """
[general]
discovery_interval_sec = 2
unexpected = 1
""".strip(),
            encoding="utf-8",
        )

        with self.assertRaises(ConfigValidationError):
            load_config(config_file)

    def test_load_config_rejects_directory_paths(self) -> None:
        config_dir = self.runtime_dir / "directory-config"
        config_dir.mkdir(parents=True, exist_ok=True)

        with self.assertRaises(ConfigValidationError):
            load_config(config_dir)

    def test_app_config_default_builds_expected_sections(self) -> None:
        env = {"HOME": str(self.runtime_dir / "home")}

        config = AppConfig.default(env=env)

        self.assertEqual(
            config.paths.workspace_root,
            (self.runtime_dir / "home/code/worktrees").resolve(),
        )
        self.assertEqual(
            config.paths.database_path,
            (self.runtime_dir / "home/.local/state/copilot-commander/commander.db").resolve(),
        )
        self.assertEqual(
            config.paths.fallback_database_path,
            (self.runtime_dir / "home/.copilot-commander/commander.db").resolve(),
        )


if __name__ == "__main__":
    unittest.main()

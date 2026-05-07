# ruff: noqa: I001,PT009,PT027

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
import shutil
import unittest

from muxdeck.config import AppConfig, CostingConfig, load_config
from muxdeck.constants import DEFAULT_WORKSPACE_ROOT
from muxdeck.exceptions import ConfigValidationError


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
            (self.runtime_dir / "config-home/muxdeck/config.toml").resolve(),
        )
        self.assertEqual(
            config.paths.state_dir,
            (self.runtime_dir / "state-home/muxdeck").resolve(),
        )
        self.assertEqual(
            config.paths.database_path,
            (self.runtime_dir / "state-home/muxdeck/muxdeck.db").resolve(),
        )
        self.assertEqual(
            config.paths.fallback_database_path,
            (self.runtime_dir / "home/.muxdeck/muxdeck.db").resolve(),
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
database_path = "./state/data/muxdeck.sqlite3"
fallback_database_path = "./legacy/muxdeck.sqlite3"

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
            (config_dir / "state/data/muxdeck.sqlite3").resolve(),
        )
        self.assertEqual(
            config.paths.fallback_database_path,
            (config_dir / "legacy/muxdeck.sqlite3").resolve(),
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
            (self.runtime_dir / "home/.local/state/muxdeck/muxdeck.db").resolve(),
        )
        self.assertEqual(
            config.paths.fallback_database_path,
            (self.runtime_dir / "home/.muxdeck/muxdeck.db").resolve(),
        )


class ConfigValidationBranchTests(unittest.TestCase):
    """Cover specific validation branches in config parsing."""

    def setUp(self) -> None:
        self.runtime_dir = Path(__file__).resolve().parent / "_runtime_config_branches"
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        self.addCleanup(self._cleanup)

    def _cleanup(self) -> None:
        if self.runtime_dir.exists():
            shutil.rmtree(self.runtime_dir)

    def _write(self, name: str, body: str) -> Path:
        path = self.runtime_dir / name
        path.write_text(body.strip() + "\n", encoding="utf-8")
        return path

    def test_capture_interval_must_be_positive(self) -> None:
        path = self._write(
            "bad-capture.toml",
            """
            [general]
            capture_interval_sec = 0
            """,
        )
        with self.assertRaises(ConfigValidationError):
            load_config(path)

    def test_idle_threshold_must_be_positive(self) -> None:
        path = self._write(
            "bad-idle.toml",
            """
            [general]
            idle_threshold_sec = 0
            """,
        )
        with self.assertRaises(ConfigValidationError):
            load_config(path)

    def test_dead_grace_period_must_be_positive(self) -> None:
        path = self._write(
            "bad-grace.toml",
            """
            [general]
            dead_grace_period_sec = 0
            """,
        )
        with self.assertRaises(ConfigValidationError):
            load_config(path)

    def test_log_preview_lines_must_be_positive(self) -> None:
        path = self._write(
            "bad-preview.toml",
            """
            [general]
            log_preview_lines = 0
            """,
        )
        with self.assertRaises(ConfigValidationError):
            load_config(path)

    def test_max_runtime_minutes_must_be_positive_int(self) -> None:
        path = self._write(
            "bad-runtime.toml",
            """
            [general]
            max_runtime_minutes = 0
            """,
        )
        with self.assertRaises(ConfigValidationError):
            load_config(path)

    def test_naming_pattern_requires_repo_and_slug(self) -> None:
        path = self._write(
            "bad-naming.toml",
            """
            [naming]
            worktree_pattern = "no-placeholders"
            """,
        )
        with self.assertRaises(ConfigValidationError):
            load_config(path)

    def test_invalid_toml_raises_config_error(self) -> None:
        from muxdeck.exceptions import ConfigError

        path = self._write("bad.toml", "this = is = not = valid")
        with self.assertRaises(ConfigError):
            load_config(path)

    def test_general_section_must_be_table(self) -> None:
        path = self._write(
            "table-shape.toml",
            """
            general = "not-a-table"
            """,
        )
        # Missing-section means ``general`` resolves to a string and
        # ``_require_table`` raises.
        with self.assertRaises(ConfigValidationError):
            load_config(path)

    def test_string_field_must_be_non_empty(self) -> None:
        path = self._write(
            "blank-string.toml",
            """
            [general]
            default_base_branch = "   "
            """,
        )
        with self.assertRaises(ConfigValidationError):
            load_config(path)

    def test_max_cost_negative_rejected(self) -> None:
        from muxdeck.exceptions import ValidationError

        path = self._write(
            "neg-cost.toml",
            """
            [general]
            max_cost_usd = -1
            """,
        )
        # Note: this currently bubbles ``DomainValidationError`` (from the
        # underlying ``ensure_non_negative_decimal``) rather than
        # ``ConfigValidationError``. Both inherit from ``ValidationError``.
        with self.assertRaises(ValidationError):
            load_config(path)


class ParseHelperTests(unittest.TestCase):
    """Direct tests of internal config parsers."""

    def test_parse_optional_decimal_returns_none_for_none(self) -> None:
        from muxdeck.config import _parse_optional_decimal

        self.assertIsNone(_parse_optional_decimal(None, field_name="x"))

    def test_parse_optional_decimal_rejects_bool_and_non_numeric(self) -> None:
        from muxdeck.config import _parse_optional_decimal

        with self.assertRaises(ConfigValidationError):
            _parse_optional_decimal(True, field_name="x")
        with self.assertRaises(ConfigValidationError):
            _parse_optional_decimal(["nope"], field_name="x")

    def test_parse_optional_decimal_accepts_numeric_string(self) -> None:
        from muxdeck.config import _parse_optional_decimal

        self.assertEqual(_parse_optional_decimal("0.5", field_name="x"), Decimal("0.500000"))

    def test_parse_optional_positive_int_returns_none_for_none(self) -> None:
        from muxdeck.config import _parse_optional_positive_int

        self.assertIsNone(_parse_optional_positive_int(None, field_name="x"))

    def test_parse_optional_positive_int_validates_when_present(self) -> None:
        from muxdeck.config import _parse_optional_positive_int

        self.assertEqual(_parse_optional_positive_int(5, field_name="x"), 5)
        with self.assertRaises(ConfigValidationError):
            _parse_optional_positive_int(0, field_name="x")
        with self.assertRaises(ConfigValidationError):
            _parse_optional_positive_int(True, field_name="x")

    def test_require_table_rejects_non_dict(self) -> None:
        from muxdeck.config import _require_table

        self.assertEqual(_require_table(None, section="x"), {})
        with self.assertRaises(ConfigValidationError):
            _require_table([], section="x")

    def test_require_non_empty_string_rejects_blank(self) -> None:
        from muxdeck.config import _require_non_empty_string

        with self.assertRaises(ConfigValidationError):
            _require_non_empty_string("   ", field_name="x")
        with self.assertRaises(ConfigValidationError):
            _require_non_empty_string(123, field_name="x")
        self.assertEqual(_require_non_empty_string("  hi  ", field_name="x"), "hi")

    def test_require_bool_rejects_non_bool(self) -> None:
        from muxdeck.config import _require_bool

        with self.assertRaises(ConfigValidationError):
            _require_bool("yes", field_name="x")
        self.assertTrue(_require_bool(True, field_name="x"))


if __name__ == "__main__":
    unittest.main()

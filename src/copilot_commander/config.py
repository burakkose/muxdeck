# ruff: noqa: I001

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any
import tomllib

from copilot_commander.constants import (
    DEFAULT_AGENT_NAME_PATTERN,
    DEFAULT_BASE_BRANCH,
    DEFAULT_BRANCH_PREFIX,
    DEFAULT_CAPTURE_INTERVAL_SEC,
    DEFAULT_COST_ESTIMATION_ENABLED,
    DEFAULT_DEAD_GRACE_PERIOD_SEC,
    DEFAULT_DISCOVERY_INTERVAL_SEC,
    DEFAULT_IDLE_THRESHOLD_SEC,
    DEFAULT_INPUT_TOKEN_COST_PER_1M,
    DEFAULT_LOG_PREVIEW_LINES,
    DEFAULT_OUTPUT_TOKEN_COST_PER_1M,
    DEFAULT_WORKTREE_PATTERN,
    default_config_path,
    default_database_path,
    default_fallback_database_path,
    default_state_dir,
    default_workspace_root,
)
from copilot_commander.domain.value_objects import TokenPricing, ensure_non_negative_decimal
from copilot_commander.exceptions import ConfigError, ConfigValidationError
from copilot_commander.types import PathLike

_TOP_LEVEL_KEYS = frozenset({"general", "paths", "naming", "costing"})
_GENERAL_KEYS = frozenset(
    {
        "discovery_interval_sec",
        "capture_interval_sec",
        "idle_threshold_sec",
        "dead_grace_period_sec",
        "log_preview_lines",
        "default_base_branch",
    }
)
_PATH_KEYS = frozenset({"state_dir", "workspace_root", "database_path", "fallback_database_path"})
_NAMING_KEYS = frozenset({"branch_prefix", "worktree_pattern", "agent_name_pattern"})
_COSTING_KEYS = frozenset(
    {
        "default_input_token_cost_per_1m",
        "default_output_token_cost_per_1m",
        "estimation_enabled",
    }
)


def _resolve_path(value: PathLike, *, base_dir: Path | None = None) -> Path:
    candidate = Path(value).expanduser()
    if not candidate.is_absolute() and base_dir is not None:
        candidate = base_dir / candidate
    return candidate.resolve(strict=False)


def _require_table(value: object, *, section: str) -> Mapping[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        msg = f"{section} must be a TOML table"
        raise ConfigValidationError(msg)
    return value


def _require_non_empty_string(value: object, *, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        msg = f"{field_name} must be a non-empty string"
        raise ConfigValidationError(msg)
    return value.strip()


def _require_positive_int(value: object, *, field_name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        msg = f"{field_name} must be a positive integer"
        raise ConfigValidationError(msg)
    return value


def _require_bool(value: object, *, field_name: str) -> bool:
    if not isinstance(value, bool):
        msg = f"{field_name} must be a boolean"
        raise ConfigValidationError(msg)
    return value


def _reject_unknown_keys(
    mapping: Mapping[str, Any],
    *,
    section: str,
    allowed: frozenset[str],
) -> None:
    unknown = sorted(set(mapping) - allowed)
    if unknown:
        msg = f"{section} contains unknown keys: {', '.join(unknown)}"
        raise ConfigValidationError(msg)


@dataclass(frozen=True, slots=True)
class GeneralConfig:
    discovery_interval_sec: int = DEFAULT_DISCOVERY_INTERVAL_SEC
    capture_interval_sec: int = DEFAULT_CAPTURE_INTERVAL_SEC
    idle_threshold_sec: int = DEFAULT_IDLE_THRESHOLD_SEC
    dead_grace_period_sec: int = DEFAULT_DEAD_GRACE_PERIOD_SEC
    log_preview_lines: int = DEFAULT_LOG_PREVIEW_LINES
    default_base_branch: str = DEFAULT_BASE_BRANCH

    def __post_init__(self) -> None:
        if self.discovery_interval_sec <= 0:
            msg = "general.discovery_interval_sec must be positive"
            raise ConfigValidationError(msg)
        if self.capture_interval_sec <= 0:
            msg = "general.capture_interval_sec must be positive"
            raise ConfigValidationError(msg)
        if self.idle_threshold_sec <= 0:
            msg = "general.idle_threshold_sec must be positive"
            raise ConfigValidationError(msg)
        if self.dead_grace_period_sec <= 0:
            msg = "general.dead_grace_period_sec must be positive"
            raise ConfigValidationError(msg)
        if self.log_preview_lines <= 0:
            msg = "general.log_preview_lines must be positive"
            raise ConfigValidationError(msg)
        object.__setattr__(
            self,
            "default_base_branch",
            _require_non_empty_string(
                self.default_base_branch,
                field_name="general.default_base_branch",
            ),
        )


@dataclass(frozen=True, slots=True)
class PathsConfig:
    state_dir: Path
    workspace_root: Path
    database_path: Path
    fallback_database_path: Path

    def __post_init__(self) -> None:
        object.__setattr__(self, "state_dir", self.state_dir.expanduser().resolve(strict=False))
        object.__setattr__(
            self,
            "workspace_root",
            self.workspace_root.expanduser().resolve(strict=False),
        )
        object.__setattr__(
            self,
            "database_path",
            self.database_path.expanduser().resolve(strict=False),
        )
        object.__setattr__(
            self,
            "fallback_database_path",
            self.fallback_database_path.expanduser().resolve(strict=False),
        )


@dataclass(frozen=True, slots=True)
class NamingConfig:
    branch_prefix: str = DEFAULT_BRANCH_PREFIX
    worktree_pattern: str = DEFAULT_WORKTREE_PATTERN
    agent_name_pattern: str = DEFAULT_AGENT_NAME_PATTERN

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "branch_prefix",
            _require_non_empty_string(self.branch_prefix, field_name="naming.branch_prefix"),
        )
        object.__setattr__(
            self,
            "worktree_pattern",
            _require_non_empty_string(
                self.worktree_pattern,
                field_name="naming.worktree_pattern",
            ),
        )
        object.__setattr__(
            self,
            "agent_name_pattern",
            _require_non_empty_string(
                self.agent_name_pattern,
                field_name="naming.agent_name_pattern",
            ),
        )
        for field_name, pattern in {
            "naming.worktree_pattern": self.worktree_pattern,
            "naming.agent_name_pattern": self.agent_name_pattern,
        }.items():
            if "{repo}" not in pattern or "{slug}" not in pattern:
                msg = f"{field_name} must contain {{repo}} and {{slug}} placeholders"
                raise ConfigValidationError(msg)

    def worktree_name(self, *, repo: str, slug: str) -> str:
        return self.worktree_pattern.format(repo=repo, slug=slug)

    def agent_name(self, *, repo: str, slug: str) -> str:
        return self.agent_name_pattern.format(repo=repo, slug=slug)


@dataclass(frozen=True, slots=True)
class CostingConfig:
    default_input_token_cost_per_1m: Decimal | str | int | float = DEFAULT_INPUT_TOKEN_COST_PER_1M
    default_output_token_cost_per_1m: Decimal | str | int | float = DEFAULT_OUTPUT_TOKEN_COST_PER_1M
    estimation_enabled: bool = DEFAULT_COST_ESTIMATION_ENABLED

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "default_input_token_cost_per_1m",
            ensure_non_negative_decimal(
                self.default_input_token_cost_per_1m,
                field_name="costing.default_input_token_cost_per_1m",
            ),
        )
        object.__setattr__(
            self,
            "default_output_token_cost_per_1m",
            ensure_non_negative_decimal(
                self.default_output_token_cost_per_1m,
                field_name="costing.default_output_token_cost_per_1m",
            ),
        )
        object.__setattr__(
            self,
            "estimation_enabled",
            _require_bool(self.estimation_enabled, field_name="costing.estimation_enabled"),
        )

    @property
    def pricing(self) -> TokenPricing:
        return TokenPricing(
            input_token_cost_per_1m=self.default_input_token_cost_per_1m,
            output_token_cost_per_1m=self.default_output_token_cost_per_1m,
        )


@dataclass(frozen=True, slots=True)
class AppConfig:
    paths: PathsConfig
    general: GeneralConfig = field(default_factory=GeneralConfig)
    naming: NamingConfig = field(default_factory=NamingConfig)
    costing: CostingConfig = field(default_factory=CostingConfig)
    config_file: Path = field(default_factory=default_config_path)

    @classmethod
    def default(
        cls,
        *,
        config_file: Path | None = None,
        env: Mapping[str, str] | None = None,
    ) -> AppConfig:
        resolved_config_file = default_config_path(env) if config_file is None else config_file
        return cls(
            paths=PathsConfig(
                state_dir=default_state_dir(env),
                workspace_root=default_workspace_root(env),
                database_path=default_database_path(env),
                fallback_database_path=default_fallback_database_path(env),
            ),
            config_file=resolved_config_file.expanduser().resolve(strict=False),
        )

    @classmethod
    def from_mapping(
        cls,
        raw: Mapping[str, Any],
        *,
        config_file: Path,
        env: Mapping[str, str] | None = None,
    ) -> AppConfig:
        base_dir = config_file.parent
        _reject_unknown_keys(raw, section="config", allowed=_TOP_LEVEL_KEYS)
        general_section = _require_table(raw.get("general"), section="general")
        paths_section = _require_table(raw.get("paths"), section="paths")
        naming_section = _require_table(raw.get("naming"), section="naming")
        costing_section = _require_table(raw.get("costing"), section="costing")
        _reject_unknown_keys(general_section, section="general", allowed=_GENERAL_KEYS)
        _reject_unknown_keys(paths_section, section="paths", allowed=_PATH_KEYS)
        _reject_unknown_keys(naming_section, section="naming", allowed=_NAMING_KEYS)
        _reject_unknown_keys(costing_section, section="costing", allowed=_COSTING_KEYS)

        try:
            general = GeneralConfig(
                discovery_interval_sec=_require_positive_int(
                    general_section.get("discovery_interval_sec", DEFAULT_DISCOVERY_INTERVAL_SEC),
                    field_name="general.discovery_interval_sec",
                ),
                capture_interval_sec=_require_positive_int(
                    general_section.get("capture_interval_sec", DEFAULT_CAPTURE_INTERVAL_SEC),
                    field_name="general.capture_interval_sec",
                ),
                idle_threshold_sec=_require_positive_int(
                    general_section.get("idle_threshold_sec", DEFAULT_IDLE_THRESHOLD_SEC),
                    field_name="general.idle_threshold_sec",
                ),
                dead_grace_period_sec=_require_positive_int(
                    general_section.get("dead_grace_period_sec", DEFAULT_DEAD_GRACE_PERIOD_SEC),
                    field_name="general.dead_grace_period_sec",
                ),
                log_preview_lines=_require_positive_int(
                    general_section.get("log_preview_lines", DEFAULT_LOG_PREVIEW_LINES),
                    field_name="general.log_preview_lines",
                ),
                default_base_branch=_require_non_empty_string(
                    general_section.get("default_base_branch", DEFAULT_BASE_BRANCH),
                    field_name="general.default_base_branch",
                ),
            )
            state_dir = _resolve_path(
                paths_section.get("state_dir", default_state_dir(env)),
                base_dir=base_dir,
            )
            paths = PathsConfig(
                state_dir=state_dir,
                workspace_root=_resolve_path(
                    paths_section.get("workspace_root", default_workspace_root(env)),
                    base_dir=base_dir,
                ),
                database_path=_resolve_path(
                    paths_section.get("database_path", state_dir / default_database_path(env).name),
                    base_dir=base_dir,
                ),
                fallback_database_path=_resolve_path(
                    paths_section.get(
                        "fallback_database_path",
                        default_fallback_database_path(env),
                    ),
                    base_dir=base_dir,
                ),
            )
            naming = NamingConfig(
                branch_prefix=_require_non_empty_string(
                    naming_section.get("branch_prefix", DEFAULT_BRANCH_PREFIX),
                    field_name="naming.branch_prefix",
                ),
                worktree_pattern=_require_non_empty_string(
                    naming_section.get("worktree_pattern", DEFAULT_WORKTREE_PATTERN),
                    field_name="naming.worktree_pattern",
                ),
                agent_name_pattern=_require_non_empty_string(
                    naming_section.get("agent_name_pattern", DEFAULT_AGENT_NAME_PATTERN),
                    field_name="naming.agent_name_pattern",
                ),
            )
            costing = CostingConfig(
                default_input_token_cost_per_1m=costing_section.get(
                    "default_input_token_cost_per_1m",
                    DEFAULT_INPUT_TOKEN_COST_PER_1M,
                ),
                default_output_token_cost_per_1m=costing_section.get(
                    "default_output_token_cost_per_1m",
                    DEFAULT_OUTPUT_TOKEN_COST_PER_1M,
                ),
                estimation_enabled=_require_bool(
                    costing_section.get("estimation_enabled", DEFAULT_COST_ESTIMATION_ENABLED),
                    field_name="costing.estimation_enabled",
                ),
            )
        except (TypeError, ValueError) as exc:
            msg = f"invalid configuration values in {config_file}"
            raise ConfigValidationError(msg) from exc

        return cls(
            general=general,
            paths=paths,
            naming=naming,
            costing=costing,
            config_file=config_file.expanduser().resolve(strict=False),
        )


def load_config(
    path: PathLike | None = None,
    *,
    env: Mapping[str, str] | None = None,
) -> AppConfig:
    config_path = _resolve_path(default_config_path(env) if path is None else path)
    if config_path.exists() and config_path.is_dir():
        msg = f"configuration path must be a file: {config_path}"
        raise ConfigValidationError(msg)
    if not config_path.exists():
        return AppConfig.default(config_file=config_path, env=env)
    try:
        with config_path.open("rb") as handle:
            raw = tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        msg = f"invalid TOML in {config_path}"
        raise ConfigError(msg) from exc
    return AppConfig.from_mapping(raw, config_file=config_path, env=env)

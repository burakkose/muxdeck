# ruff: noqa: I001

from __future__ import annotations

from collections.abc import Mapping
import os
from pathlib import Path

APP_NAME = "muxdeck"
PACKAGE_NAME = "muxdeck"
CONFIG_FILE_NAME = "config.toml"
DEFAULT_CURRENCY = "USD"
DEFAULT_DATABASE_FILE_NAME = "muxdeck.db"
LEGACY_STATE_DIR_NAME = ".muxdeck"

DEFAULT_BASE_BRANCH = "main"
DEFAULT_DISCOVERY_INTERVAL_SEC = 2
DEFAULT_CAPTURE_INTERVAL_SEC = 2
DEFAULT_IDLE_THRESHOLD_SEC = 45
DEFAULT_DEAD_GRACE_PERIOD_SEC = 10
DEFAULT_LOG_PREVIEW_LINES = 200

DEFAULT_WORKSPACE_ROOT = "~/code/worktrees"
DEFAULT_BRANCH_PREFIX = "task/"
DEFAULT_WORKTREE_PATTERN = "{repo}--{slug}"
DEFAULT_AGENT_NAME_PATTERN = "{repo}:{slug}"

DEFAULT_INPUT_TOKEN_COST_PER_1M = 0
DEFAULT_OUTPUT_TOKEN_COST_PER_1M = 0
DEFAULT_COST_ESTIMATION_ENABLED = True


def _env(env: Mapping[str, str] | None = None) -> Mapping[str, str]:
    return os.environ if env is None else env


def _home_dir(env: Mapping[str, str] | None = None) -> Path:
    environment = _env(env)
    configured = environment.get("HOME")
    if configured:
        return Path(configured).expanduser()
    return Path.home()


def default_config_home(env: Mapping[str, str] | None = None) -> Path:
    environment = _env(env)
    configured = environment.get("XDG_CONFIG_HOME")
    if configured:
        return Path(configured).expanduser()
    return _home_dir(environment) / ".config"


def default_state_home(env: Mapping[str, str] | None = None) -> Path:
    environment = _env(env)
    configured = environment.get("XDG_STATE_HOME")
    if configured:
        return Path(configured).expanduser()
    return _home_dir(environment) / ".local" / "state"


def default_cache_home(env: Mapping[str, str] | None = None) -> Path:
    environment = _env(env)
    configured = environment.get("XDG_CACHE_HOME")
    if configured:
        return Path(configured).expanduser()
    return _home_dir(environment) / ".cache"


def default_cache_dir(env: Mapping[str, str] | None = None) -> Path:
    return default_cache_home(env) / APP_NAME


def default_config_path(env: Mapping[str, str] | None = None) -> Path:
    return default_config_home(env) / APP_NAME / CONFIG_FILE_NAME


def default_state_dir(env: Mapping[str, str] | None = None) -> Path:
    return default_state_home(env) / APP_NAME


def default_database_path(env: Mapping[str, str] | None = None) -> Path:
    return default_state_dir(env) / DEFAULT_DATABASE_FILE_NAME


def default_fallback_state_dir(env: Mapping[str, str] | None = None) -> Path:
    return _home_dir(env) / LEGACY_STATE_DIR_NAME


def default_fallback_database_path(env: Mapping[str, str] | None = None) -> Path:
    return default_fallback_state_dir(env) / DEFAULT_DATABASE_FILE_NAME


def default_workspace_root(env: Mapping[str, str] | None = None) -> Path:
    return Path(DEFAULT_WORKSPACE_ROOT.replace("~", str(_home_dir(env)), 1)).expanduser()

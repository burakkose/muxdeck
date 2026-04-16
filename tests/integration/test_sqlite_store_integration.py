# ruff: noqa: PT009,PT027

from __future__ import annotations

import shutil
import sqlite3
import unittest
from pathlib import Path

from copilot_commander.adapters.sqlite_store import SQLiteStore
from copilot_commander.config import AppConfig


class SQLiteStoreIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.runtime_dir = Path(__file__).resolve().parent / "_runtime_sqlite"
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        self.addCleanup(self._cleanup_runtime_dir)

    def _cleanup_runtime_dir(self) -> None:
        if self.runtime_dir.exists():
            shutil.rmtree(self.runtime_dir)

    def test_bootstraps_file_database_with_wal_foreign_keys_and_indexes(self) -> None:
        database_path = self.runtime_dir / "commander.db"

        with SQLiteStore(AppConfig.default(), database_path=database_path) as store:
            self.assertEqual(store.database_path, database_path.resolve())
            self.assertTrue(store.foreign_keys_enabled)
            self.assertEqual(store.journal_mode, "wal")
            self.assertEqual(
                store.applied_migrations(),
                ("0001_initial.sql", "0002_add_tasks.sql"),
            )

        connection = sqlite3.connect(database_path)
        self.addCleanup(connection.close)

        journal_mode = connection.execute("PRAGMA journal_mode").fetchone()
        migrations = connection.execute(
            "SELECT version FROM migrations ORDER BY version ASC"
        ).fetchall()
        indexes = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            ).fetchall()
        }
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }

        self.assertIsNotNone(journal_mode)
        self.assertEqual(journal_mode[0].lower(), "wal")
        self.assertEqual(
            migrations,
            [("0001_initial.sql",), ("0002_add_tasks.sql",)],
        )
        expected_tables = {
            "migrations",
            "agents",
            "worktrees",
            "sessions",
            "events",
            "log_chunks",
            "tasks",
            "settings",
            "cache_entries",
        }
        self.assertTrue(expected_tables.issubset(tables))
        self.assertTrue(
            {
                "idx_agents_tmux_pane_id",
                "idx_agents_copilot_session_id",
                "idx_sessions_agent_id",
                "idx_sessions_copilot_session_id",
                "idx_events_agent_id",
                "idx_events_session_id",
                "idx_events_occurred_at",
                "idx_log_chunks_agent_id",
                "idx_log_chunks_session_id",
                "idx_tasks_status",
                "idx_tasks_repo_root",
                "idx_tasks_assigned_agent_id",
                "idx_tasks_assigned_worktree_id",
            }.issubset(indexes)
        )


if __name__ == "__main__":
    unittest.main()

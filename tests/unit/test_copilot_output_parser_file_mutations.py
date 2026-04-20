# ruff: noqa: PT009

from __future__ import annotations

import unittest

from muxdeck.parsers.copilot_output_parser import parse_copilot_output


class FileMutationParserTests(unittest.TestCase):
    def test_detects_add_modify_delete_and_rename_banners(self) -> None:
        output = "\n".join(
            (
                "Editing file: src/auth.py",
                "Created file: tests/test_auth.py",
                "Deleted: legacy/old.py",
                "Renamed src/a.py -> src/b.py",
            )
        )

        result = parse_copilot_output(output)
        actions = [(m.action, m.path) for m in result.file_mutations]

        self.assertIn(("modify", "src/auth.py"), actions)
        self.assertIn(("add", "tests/test_auth.py"), actions)
        self.assertIn(("delete", "legacy/old.py"), actions)
        self.assertIn(("rename", "src/a.py"), actions)
        self.assertIn(("rename", "src/b.py"), actions)

    def test_ignores_unrelated_prose_mentioning_files(self) -> None:
        output = "\n".join(
            (
                "I will look at src/auth.py to understand it.",
                "The test file tests/test_auth.py already exists.",
                "deleted some commits earlier (no banner here).",
            )
        )

        result = parse_copilot_output(output)

        self.assertEqual(result.file_mutations, ())


class ToolCallParserTests(unittest.TestCase):
    def test_detects_tool_call_banners(self) -> None:
        output = "\n".join(
            (
                "Tool: ripgrep",
                "Bash(command='pytest -q')",
                "Calling tool: github.search_code(args='copilot')",
            )
        )

        result = parse_copilot_output(output)
        names = [t.name for t in result.tool_calls]

        self.assertIn("ripgrep", names)
        self.assertIn("Bash", names)
        self.assertIn("github.search_code", names)
        bash_call = next(t for t in result.tool_calls if t.name == "Bash")
        self.assertEqual(bash_call.args, "command='pytest -q'")

    def test_returns_empty_when_no_tool_banners(self) -> None:
        output = "Just some narrative text without any tool calls."

        result = parse_copilot_output(output)

        self.assertEqual(result.tool_calls, ())


if __name__ == "__main__":
    unittest.main()

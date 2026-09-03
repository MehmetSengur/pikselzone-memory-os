"""Regression coverage for the V2.3 reference hook fragments.

The canonical installer is `memory register`; these fragment files are a
placeholder-filled reference of the shape it produces.
"""
from __future__ import annotations

import json
import unittest
from pathlib import Path


class TestHookInstallParity(unittest.TestCase):
    def test_fragments_match_the_v2_3_command_shape(self) -> None:
        root = Path(__file__).resolve().parents[2]
        expected_events = {
            "SessionStart", "SessionEnd", "PreCompact", "Stop", "UserPromptSubmit",
        }
        for runtime in ("claude", "codex"):
            fragment = root / "memory_v1" / "operator" / f"{runtime}-hooks.fragment.json"
            parsed = json.loads(fragment.read_text(encoding="utf-8"))
            hooks = parsed["hooks"]
            self.assertEqual(expected_events, set(hooks))
            for event, groups in hooks.items():
                command = groups[0]["hooks"][0]["command"]
                self.assertNotIn("cd ", command)  # runs in the session's real cwd
                self.assertTrue(command.startswith("PYTHONPATH={{MEMORY_OS_ROOT}} "), command)
                self.assertIn("memory_v1.hook_runner", command)
                self.assertIn(f"--runtime {runtime} --event {event}", command)
                self.assertIn("--project {{PROJECT}} --project-root {{PROJECT_ROOT}}", command)


if __name__ == "__main__":
    unittest.main()

"""Regression coverage for generated workstation hook fragments."""
from __future__ import annotations

import json
import unittest
from pathlib import Path


class TestHookInstallParity(unittest.TestCase):
    def test_fragments_execute_the_canonical_v2_implementation(self) -> None:
        root = Path(__file__).resolve().parents[2]
        expected_prefix = f"cd {root} && python3 -m memory_v1.hook_runner"
        expected_events = {"SessionStart", "PreCompact", "SessionEnd"}

        for runtime in ("claude", "codex"):
            fragment = root / "memory_v1" / "operator" / f"{runtime}-hooks.fragment.json"
            parsed = json.loads(fragment.read_text(encoding="utf-8"))
            hooks = parsed["hooks"]
            self.assertEqual(expected_events, set(hooks))
            for event, groups in hooks.items():
                command = groups[0]["hooks"][0]["command"]
                self.assertTrue(command.startswith(expected_prefix), command)
                self.assertIn(f"--runtime {runtime} --event {event}", command)


if __name__ == "__main__":
    unittest.main()

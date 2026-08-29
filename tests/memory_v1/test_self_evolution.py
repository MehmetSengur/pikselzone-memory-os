"""Unit tests for Controlled Memory-Engine Self-Modification (SB2-08)."""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from memory_v1.core import PolicyError
from memory_v1.self_evolution import EvolutionProposal, SelfEvolutionEngine


class TestSelfEvolutionEngine(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name).resolve()
        
        # Initialize a temporary git repository for testing self-evolution
        subprocess.run(["git", "init", "-b", "feat/test-evolution"], cwd=self.root, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@pikselzone.com"], cwd=self.root, check=True)
        subprocess.run(["git", "config", "user.name", "Test Agent"], cwd=self.root, check=True)

        # Create a sample engine file and a simple test
        (self.root / "engine.py").write_text('def compute(a, b):\n    return a + b\n', encoding="utf-8")
        (self.root / "test_engine.py").write_text(
            'import unittest\nfrom engine import compute\n\n'
            'class TestCalc(unittest.TestCase):\n'
            '    def test_add(self):\n'
            '        self.assertEqual(compute(2, 3), 5)\n\n'
            'if __name__ == "__main__":\n'
            '    unittest.main()\n',
            encoding="utf-8",
        )

        subprocess.run(["git", "add", "engine.py", "test_engine.py"], cwd=self.root, check=True)
        subprocess.run(["git", "commit", "-m", "initial commit"], cwd=self.root, check=True)

        self.engine = SelfEvolutionEngine(self.root)
        self.engine.ensure_state_dirs()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    # 1. Protected branch rejection
    def test_protected_branch_rejected(self):
        proposal = EvolutionProposal(
            proposal_id="EVO-01",
            target_relative_path="engine.py",
            proposed_content="def compute(a, b): return a * b\n",
            justification="Testing branch protection",
        )
        with patch.object(self.engine, "get_current_branch", return_value="main"):
            with self.assertRaises(PolicyError) as ctx:
                self.engine.apply_evolution(proposal)
            self.assertIn("cannot-self-modify-protected-branch", str(ctx.exception))

    # 2. Failed test gating triggers automatic rollback
    def test_failing_modification_rolled_back(self):
        initial_content = (self.root / "engine.py").read_text(encoding="utf-8")
        head_before = self.engine.get_head_commit()

        # Propose a broken implementation (multiply instead of add)
        proposal = EvolutionProposal(
            proposal_id="EVO-FAIL-01",
            target_relative_path="engine.py",
            proposed_content="def compute(a, b):\n    return a * b\n",
            justification="Accidentally broke compute logic",
            targeted_tests=["python3 test_engine.py"],
        )

        result = self.engine.apply_evolution(proposal)
        self.assertEqual(result.status, "rolled_back")
        self.assertFalse(result.tests_passed)
        self.assertIn("test-gate-failed", result.error)

        # File must be restored to initial content
        current_content = (self.root / "engine.py").read_text(encoding="utf-8")
        self.assertEqual(current_content, initial_content)

        # Git commit must remain unchanged
        head_after = self.engine.get_head_commit()
        self.assertEqual(head_before, head_after)

        # Audit log must record the rollback
        log_data = json.loads(self.engine.log_file.read_text(encoding="utf-8"))
        self.assertTrue(any(e["status"] == "rolled_back" for e in log_data["history"]))

    # 3. Successful test gating commits and records receipt
    def test_successful_modification_applied_and_committed(self):
        head_before = self.engine.get_head_commit()

        # Propose a valid enhancement that passes the test
        enhanced_content = (
            "def compute(a, b):\n"
            "    # Enhanced with type checking and documentation\n"
            "    return int(a) + int(b)\n"
        )
        proposal = EvolutionProposal(
            proposal_id="EVO-SUCCESS-01",
            target_relative_path="engine.py",
            proposed_content=enhanced_content,
            justification="Enhanced compute function with integer conversion and docs",
            targeted_tests=["python3 test_engine.py"],
        )

        result = self.engine.apply_evolution(proposal)
        self.assertEqual(result.status, "applied")
        self.assertTrue(result.tests_passed)
        self.assertIsNotNone(result.final_commit)
        self.assertNotEqual(head_before, result.final_commit)

        # File content must now be the enhanced version
        current_content = (self.root / "engine.py").read_text(encoding="utf-8")
        self.assertEqual(current_content, enhanced_content)

        # Receipt file must exist
        self.assertTrue(Path(result.receipt_file).is_file())


if __name__ == "__main__":
    unittest.main()

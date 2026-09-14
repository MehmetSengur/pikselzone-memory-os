"""A mislearned rule candidate can be retired with backup, ledger and revert."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from memory_v1.companion import CompanionManager
from memory_v1.core import MemoryConfig, PolicyError
from memory_v1.memory_repair import RETIRED_PREFIX, retire_rule_candidate, revert_repair

TEXT = "Dosya artık yerinde değil; hafızandan cevap ver."


class RetireRuleCandidateTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name).resolve()
        self.vault = root / "vault"
        (self.vault / "companion").mkdir(parents=True)

        def config(role, state):
            base = {"vault_path": str(self.vault), "state_path": str(root / state),
                    "can_write_event_memory": True, "provider": {"mode": "runtime-native"}}
            if role == "memory-engine":
                return MemoryConfig.from_dict({**base, "role": role, "runtimes": ["hermes"],
                                               "transcript_roots": {"hermes": [str(root)]}, "can_run_compiler": True})
            return MemoryConfig.from_dict({**base, "role": role, "runtimes": ["claude", "codex"],
                                           "transcript_roots": {"claude": [str(root)], "codex": [str(root)]},
                                           "can_run_compiler": False,
                                           "models": {"flush": "gpt-5.6-luna", "compiler": "gpt-5.6-terra"}})

        self.engine = config("memory-engine", "vps-state")
        self.workstation = config("workstation", "mac-state")
        companion = CompanionManager(self.vault)
        companion.ensure_companion_files()
        companion.record_rule_candidate(TEXT, "Kullanıcı tercihi", "hermes-81e48bfe")
        companion.record_rule_candidate("Çünkü artık terminal değilde uygulamalara dönmek istiyorum.", "tercih", "claude-a")
        self.rules = self.vault / "companion" / "Kurallar.md"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_candidate_is_retired_with_provenance_and_others_are_kept(self):
        ledger = retire_rule_candidate(self.engine, TEXT, classification="task-instruction-in-test-session",
                                       evidence="Desktop recall test prompt")
        content = self.rules.read_text(encoding="utf-8")
        texts = [c.text for c in CompanionManager(self.vault).read_rule_candidates()]
        self.assertNotIn(TEXT, texts)
        self.assertIn("Çünkü artık terminal değilde uygulamalara dönmek istiyorum.", texts)
        self.assertIn(f"{RETIRED_PREFIX} {TEXT} | **sınıf:** task-instruction-in-test-session", content)
        self.assertIn(f"**bakım:** {ledger['repair_id']}", content)
        written = json.loads((self.engine.state_path / "evidence" / f"memory-repair-{ledger['repair_id']}.json").read_text())
        self.assertEqual(["hermes-81e48bfe"], written["candidates_retired"][0]["sources"])

    def test_retirement_can_be_reverted(self):
        before = self.rules.read_bytes()
        ledger = retire_rule_candidate(self.engine, TEXT, classification="c", evidence="e")
        revert_repair(self.engine, ledger["repair_id"])
        self.assertEqual(before, self.rules.read_bytes())

    def test_workstation_may_not_rewrite_the_shared_rules(self):
        with self.assertRaises(PolicyError):
            retire_rule_candidate(self.workstation, TEXT, classification="c", evidence="e")

    def test_unknown_text_changes_nothing(self):
        before = self.rules.read_bytes()
        with self.assertRaises(PolicyError):
            retire_rule_candidate(self.engine, "Böyle bir aday yok.", classification="c", evidence="e")
        self.assertEqual(before, self.rules.read_bytes())

    def test_reason_is_required(self):
        with self.assertRaises(PolicyError):
            retire_rule_candidate(self.engine, TEXT, classification="", evidence="e")


if __name__ == "__main__":
    unittest.main()

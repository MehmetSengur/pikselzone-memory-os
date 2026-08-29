"""Unit tests for automatic rule learning, deduplication, and conflict reconciliation (SB2-04)."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from memory_v1.companion import CompanionManager
from memory_v1.rule_learner import RuleLearner, calculate_overlap


class TestRuleLearner(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.vault = Path(self.temp_dir.name).resolve()
        self.companion = CompanionManager(self.vault)
        self.companion.ensure_companion_files()
        self.learner = RuleLearner(self.companion)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    # 1. Detection of explicit directives
    def test_explicit_directive_detection(self):
        text = "Bundan sonra tüm bash scriptlerinde set -euo pipefail kullan."
        rules = self.learner.extract_rules_from_text(text)
        self.assertEqual(len(rules), 1)
        self.assertTrue(rules[0].is_explicit)
        self.assertIn("pipefail", rules[0].rule_text)

    # 2. Detection of correction directives
    def test_correction_directive_detection(self):
        text = "Bunu böyle yapma, testleri daima src dizinine paralel tut."
        rules = self.learner.extract_rules_from_text(text)
        self.assertGreaterEqual(len(rules), 1)
        self.assertTrue(any("paralel" in r.rule_text for r in rules))

    # 3. Deduplication prevents adding duplicate rules
    def test_deduplication(self):
        initial_count = len(self.companion.read_rules())
        turns_1 = [("user", "Bundan sonra kod yazarken daima tip ipuçlarını ekle.")]
        count_1 = self.learner.learn_from_transcript(turns_1, source_session="sess-1")
        self.assertEqual(count_1, 1)

        # Same or near identical rule in session 2
        turns_2 = [("user", "Bundan sonra kod yazarken daima tip ipuçlarını (type hints) ekle.")]
        count_2 = self.learner.learn_from_transcript(turns_2, source_session="sess-2")
        self.assertEqual(count_2, 0)  # Must be deduplicated

        rules = self.companion.read_rules()
        self.assertEqual(len(rules), initial_count + 1)

    # 4. Conflict reconciliation replaces old rule and archives it
    def test_conflict_reconciliation(self):
        turns_1 = [("user", "Bundan sonra testleri unittest framework'ü ile yaz.")]
        self.learner.learn_from_transcript(turns_1, source_session="sess-1")
        
        # Verify initial rule is active
        rules = self.companion.read_rules()
        self.assertTrue(any("unittest" in r.text for r in rules))

        # User updates preference: replace unittest with pytest
        turns_2 = [("user", "Bundan sonra testleri unittest yerine pytest ile yaz.")]
        self.learner.learn_from_transcript(turns_2, source_session="sess-2")

        # Active rules must now have pytest, not unittest
        active_rules = self.companion.read_rules()
        self.assertTrue(any("pytest" in r.text for r in active_rules))
        self.assertFalse(any("unittest framework'ü ile yaz" in r.text for r in active_rules))

        # Kurallar.md must contain archived old rule
        content = (self.companion.companion_dir / "Kurallar.md").read_text(encoding="utf-8")
        self.assertIn("## Arşivlenmiş / Geçersiz Kılınmış Kurallar", content)
        self.assertIn("unittest", content)

    # 5. Overlap calculation
    def test_calculate_overlap(self):
        t1 = "Bundan sonra kodları her zaman formatla"
        t2 = "Bundan sonra kodları formatla"
        overlap = calculate_overlap(t1, t2)
        self.assertGreater(overlap, 0.60)


if __name__ == "__main__":
    unittest.main()

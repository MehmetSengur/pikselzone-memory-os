"""Runs the V2.3 associative-recall acceptance harness inside the test suite.

The harness builds its own disposable fixture vault; it never touches the live
vault, registry, index or any production state.
"""
from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

_HARNESS = _REPO_ROOT / "scripts" / "acceptance_associative_recall.py"
_spec = importlib.util.spec_from_file_location("pz_acceptance_assoc", _HARNESS)
harness = importlib.util.module_from_spec(_spec)
# Register before exec so dataclasses can resolve __module__.
sys.modules["pz_acceptance_assoc"] = harness
_spec.loader.exec_module(harness)


class AcceptanceAssociativeRecallTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.report = harness.run()

    def test_verdict_is_pass(self) -> None:
        self.assertEqual(self.report["VERDICT"], "PASS", self.report["_failures"])

    def test_positive_recall_finds_twoberries_case_without_naming_it(self) -> None:
        self.assertEqual(self.report["POSITIVE_RECALL"], "PASS")
        self.assertIn("twoberries", self.report["POSITIVE_MATCH_SOURCE"])
        self.assertIn("meta-catalog-id-mismatch", self.report["_positive_slugs"])

    def test_positive_injection_respects_max_items(self) -> None:
        self.assertLessEqual(self.report["POSITIVE_INJECTION_COUNT"], harness.MAX_ITEMS)
        self.assertGreaterEqual(self.report["POSITIVE_INJECTION_COUNT"], 1)

    def test_injected_case_keeps_problem_attempt_outcome_why(self) -> None:
        self.assertTrue(self.report["_positive_carries_arc"])

    def test_noise_baseline(self) -> None:
        self.assertEqual(self.report["NOISE_PROMPTS"], len(harness._NOISE_PROMPTS))
        self.assertEqual(self.report["CLEARLY_IRRELEVANT_INJECTIONS"], 0)
        self.assertLessEqual(self.report["NOISE_RATE"], harness.NOISE_RATE_LIMIT)

    def test_noise_prompts_actually_reach_the_scorer(self) -> None:
        """A clean noise baseline must come from scoring, not from the trivial gate."""
        from memory_v1.recall import _TRIVIAL_PROMPTS, _tokenize
        gated = [
            p for p in harness._NOISE_PROMPTS
            if (lambda n: not n or n in _TRIVIAL_PROMPTS or len(n) < 12 or len(_tokenize(n)) < 3)(
                " ".join(p.lower().split())
            )
        ]
        self.assertEqual(gated, [], "these noise prompts never reached the scorer")

    def test_index_first_no_full_graph_scan(self) -> None:
        self.assertEqual(self.report["FULL_GRAPH_SCAN"], "NO")
        self.assertLessEqual(self.report["_concept_reads"], harness.MAX_CONCEPT_READS)
        self.assertGreaterEqual(self.report["_index_reads"], 1)

    def test_fail_open_on_missing_or_corrupt_index(self) -> None:
        self.assertEqual(self.report["FAIL_OPEN"], "PASS")


if __name__ == "__main__":
    unittest.main()

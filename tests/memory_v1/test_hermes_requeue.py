"""Operator requeue of permanent Hermes finalize verdicts: plan, apply, revert."""
from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

from memory_v1.core import MemoryConfig, PolicyError
from memory_v1.hermes_requeue import apply_requeue_plan, build_requeue_plan, revert_requeue

PLUGIN = Path(__file__).resolve().parents[2] / "hermes_plugins" / "pz-memory-v1" / "finalize_retry.py"
_spec = importlib.util.spec_from_file_location("pz_finalize_retry_for_requeue", PLUGIN)
finalize_retry = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(finalize_retry)


class HermesRequeueTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        (self.root / "vault").mkdir()
        self.data = self.root / "hermes-data"
        self.config = MemoryConfig.from_dict({
            "role": "memory-engine", "vault_path": str(self.root / "vault"),
            "state_path": str(self.root / "state"), "runtimes": ["hermes"],
            "transcript_roots": {"hermes": [str(self.data)]},
            "can_write_event_memory": True, "can_run_compiler": True,
            "provider": {"mode": "runtime-native"},
        })
        self.base = str(self.data / "memory-v1")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _fail(self, session: str, reason: str) -> Path:
        record = finalize_retry.record_failure(
            self.base, session_id=session, source_sha="a" * 64, profile="pz-engineering",
            database=str(self.data / "profiles/pz-engineering/state.db"), reason_code=reason,
        )
        path = Path(finalize_retry.record_path(self.base, session, "a" * 64, record["database"]))
        self.assertTrue(path.is_file())
        return path

    def test_plan_selects_fixed_causes_and_changes_nothing(self):
        schema = self._fail("s-schema", "schema")
        trust = self._fail("s-trust", "trust-denied")
        self._fail("s-auth", "auth")
        self._fail("s-hold", "unknown")
        before = {p: p.read_bytes() for p in (schema, trust)}
        plan = build_requeue_plan(self.config)
        self.assertEqual({"s-schema", "s-trust"}, {row["session_id"] for row in plan["records"]})
        self.assertEqual(before, {p: p.read_bytes() for p in before})

    def test_apply_makes_records_due_for_the_plugins_own_retry(self):
        path = self._fail("s-schema", "schema")
        self.assertFalse(finalize_retry.is_due(json.loads(path.read_text())))
        ledger = apply_requeue_plan(self.config, build_requeue_plan(self.config))
        self.assertEqual(1, ledger["summary"]["requeued"])
        record = json.loads(path.read_text())
        self.assertEqual("retry-scheduled", record["status"])
        self.assertEqual({"status": "permanent", "reason_code": "schema"}, record["requeued_from"])
        self.assertTrue(finalize_retry.is_due(record))
        self.assertTrue((self.root / "state/evidence" / f"hermes-{ledger['requeue_id']}.json").is_file())

        # Failing again is bounded: the plugin's next verdict is permanent once more.
        again = finalize_retry.record_failure(
            self.base, session_id="s-schema", source_sha="a" * 64,
            database=record["database"], reason_code="schema")
        self.assertEqual("permanent", again["status"])
        self.assertEqual(2, again["attempts"])

    def test_record_changed_after_the_plan_is_left_alone(self):
        path = self._fail("s-schema", "schema")
        plan = build_requeue_plan(self.config)
        self._fail("s-schema", "schema")  # a newer verdict rewrites the record
        ledger = apply_requeue_plan(self.config, plan)
        self.assertEqual(0, ledger["summary"]["requeued"])
        self.assertEqual("changed-since-plan", ledger["skipped"][0]["reason"])
        self.assertEqual("permanent", json.loads(path.read_text())["status"])

    def test_revert_restores_only_untouched_records(self):
        kept = self._fail("s-one", "schema")
        moved = self._fail("s-two", "schema")
        original = kept.read_bytes()
        ledger = apply_requeue_plan(self.config, build_requeue_plan(self.config))
        moved.unlink()  # the retry settled it meanwhile
        result = revert_requeue(self.config, ledger["requeue_id"])
        self.assertEqual(1, result["restored"])
        self.assertEqual(original, kept.read_bytes())
        self.assertEqual("resolved-since", result["left"][0]["reason"])
        self.assertFalse(moved.exists())

    def test_foreign_plan_is_refused(self):
        self._fail("s-schema", "schema")
        plan = build_requeue_plan(self.config)
        plan["retry_dir"] = "/elsewhere"
        with self.assertRaises(PolicyError):
            apply_requeue_plan(self.config, plan)


if __name__ == "__main__":
    unittest.main()

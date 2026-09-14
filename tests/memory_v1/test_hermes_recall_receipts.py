"""Hermes startup evidence is accepted only with this session's native recall receipt."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from memory_v1.core import MemoryConfig
from memory_v1.publisher import publish_outbox
from memory_v1.recall import _hermes_recall_receipt_problem


def _receipt(path: Path, **overrides) -> Path:
    value = {
        "schema": "pikselzone-memory-lifecycle-receipt-v1", "session_id": "20260915_desktop",
        "hook_name": "pre_llm_call", "native_invoke": True,
    }
    value.update(overrides)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


class RecallReceiptArtifactTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.receipts = Path(self._tmp.name) / "state" / "receipts"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_native_receipt_for_this_session_is_accepted(self):
        path = _receipt(self.receipts / "pre_llm_call" / "20260915_desktop.json")
        self.assertEqual("", _hermes_recall_receipt_problem(str(path), "20260915_desktop"))

    def test_receipt_of_another_session_is_rejected(self):
        path = _receipt(self.receipts / "pre_llm_call" / "20260915_desktop.json", session_id="20260915_cli")
        self.assertEqual("other-session", _hermes_recall_receipt_problem(str(path), "20260915_desktop"))

    def test_missing_receipt_is_rejected(self):
        missing = self.receipts / "pre_llm_call" / "20260915_desktop.json"
        self.assertEqual("unreadable", _hermes_recall_receipt_problem(str(missing), "20260915_desktop"))
        self.assertEqual("not-a-pre_llm_call-receipt", _hermes_recall_receipt_problem("", "20260915_desktop"))

    def test_session_end_receipt_cannot_stand_in(self):
        shared = _receipt(self.receipts / "20260915_desktop.json", hook_name="on_session_end")
        self.assertEqual("not-a-pre_llm_call-receipt", _hermes_recall_receipt_problem(str(shared), "20260915_desktop"))
        wrong_hook = _receipt(self.receipts / "pre_llm_call" / "20260915_desktop.json", hook_name="on_session_end")
        self.assertEqual("other-hook", _hermes_recall_receipt_problem(str(wrong_hook), "20260915_desktop"))

    def test_receipt_not_written_from_hermes_dispatch_is_rejected(self):
        path = _receipt(self.receipts / "pre_llm_call" / "20260915_desktop.json", native_invoke=False)
        self.assertEqual("not-invoked-by-hermes", _hermes_recall_receipt_problem(str(path), "20260915_desktop"))


class PerSessionEvidencePromotionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        (self.root / "vault" / "daily").mkdir(parents=True)
        self.state = self.root / "state"
        self.state.mkdir()
        self.config = MemoryConfig.from_dict({
            "role": "memory-engine", "vault_path": str(self.root / "vault"), "state_path": str(self.state),
            "runtimes": ["hermes"], "transcript_roots": {"hermes": [str(self.root / "hermes-data")]},
            "can_write_event_memory": True, "can_run_compiler": True, "provider": {"mode": "runtime-native"},
        })
        self.outbox = self.root / "hermes-data" / "memory-v1"
        (self.outbox / "outbox" / "events").mkdir(parents=True)
        self.sessions = self.outbox / "outbox" / "evidence" / "recall-hermes-sessions"
        self.sessions.mkdir(parents=True)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _evidence(self, name, session_key):
        (self.sessions / f"{name}.json").write_text(json.dumps({
            "schema": "pikselzone-memory-recall-evidence-v1", "runtime": "hermes",
            "session_key": session_key, "status": "pass",
        }), encoding="utf-8")

    def test_each_session_file_is_promoted_under_its_own_name(self):
        self._evidence("20260915_a", "20260915_a")
        self._evidence("20260915_b", "20260915_b")
        publish_outbox(self.config, outbox_root=self.outbox)
        promoted = self.state / "evidence" / "recall-hermes-sessions"
        self.assertEqual(["20260915_a.json", "20260915_b.json"], sorted(p.name for p in promoted.iterdir()))

    def test_file_claiming_another_session_is_not_promoted(self):
        self._evidence("20260915_a", "20260915_b")
        publish_outbox(self.config, outbox_root=self.outbox)
        self.assertFalse((self.state / "evidence" / "recall-hermes-sessions" / "20260915_a.json").exists())


if __name__ == "__main__":
    unittest.main()

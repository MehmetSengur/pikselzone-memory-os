"""Unit tests for Doctor -> Self-Healing Maintenance Engine (SB2-07)."""
from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path

from memory_v1.companion import CompanionManager, ThreadItem
from memory_v1.core import MemoryConfig
from memory_v1.doctor import run_self_healing


class TestSelfHealingEngine(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name).resolve()
        self.vault = self.root / "vault"
        self.state = self.root / "state"
        self.vault.mkdir(parents=True)
        self.state.mkdir(parents=True)
        (self.vault / "knowledge" / "concepts").mkdir(parents=True)
        (self.vault / "knowledge" / "connections").mkdir(parents=True)
        (self.state / "locks").mkdir(parents=True)
        (self.state / "sessions").mkdir(parents=True)
        (self.state / "outbox").mkdir(parents=True)
        (self.state / "health").mkdir(parents=True)

        self.config = MemoryConfig.from_dict({
            "role": "workstation",
            "vault_path": str(self.vault),
            "state_path": str(self.state),
            "runtimes": ["claude", "codex"],
            "transcript_roots": {
                "claude": [str(self.root)],
                "codex": [str(self.root)],
            },
            "can_write_event_memory": True,
            "can_run_compiler": False,
            "models": {"flush": "gpt-5.6-luna", "compiler": "gpt-5.6-terra"},
            "provider": {"mode": "runtime-native"},
            "context_budget_chars": 16000,
        })
        self.companion = CompanionManager(self.vault)
        self.companion.ensure_companion_files()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    # 1. Rebuild corrupted or missing knowledge/index.md
    def test_rebuild_knowledge_index(self):
        # Create 2 concept files
        c1 = self.vault / "knowledge" / "concepts" / "redis.md"
        c1.write_text("# Redis\n\n## Özet\nIn-memory veri yapısı deposu.\n", encoding="utf-8")
        c2 = self.vault / "knowledge" / "concepts" / "fastapi.md"
        c2.write_text("# FastAPI\n\n## Özet\nModern yüksek performanslı web çatısı.\n", encoding="utf-8")

        # Corrupt index.md
        idx = self.vault / "knowledge" / "index.md"
        idx.write_text("corrupted content", encoding="utf-8")

        res = run_self_healing(self.config)
        self.assertEqual(res["status"], "ok")
        self.assertIn("knowledge/index.md", res["repaired_items"])

        rebuilt_text = idx.read_text(encoding="utf-8")
        self.assertIn("# Knowledge Base Index", rebuilt_text)
        self.assertIn("redis", rebuilt_text)
        self.assertIn("fastapi", rebuilt_text)

    # 2. Repair orphan wikilinks by generating placeholder notes
    def test_repair_orphan_wikilinks(self):
        c1 = self.vault / "knowledge" / "concepts" / "capi.md"
        c1.write_text(
            "# CAPI\n\n## Özet\nConversion API.\n\n## İlgili Bağlantılar\n- [[concepts/meta-business-manager]]\n",
            encoding="utf-8",
        )

        res = run_self_healing(self.config)
        self.assertIn("knowledge/concepts/meta-business-manager.md", res["repaired_items"])

        # Placeholder file must now exist
        target = self.vault / "knowledge" / "concepts" / "meta-business-manager.md"
        self.assertTrue(target.is_file())
        content = target.read_text(encoding="utf-8")
        self.assertIn("healed-orphan", content)
        self.assertIn("öksüz wikilink onarımı", content)

    # 3. Clean up stale lock files (> 10 min old)
    def test_cleanup_stale_locks(self):
        stale_lock = self.state / "locks" / "session-old.lock"
        stale_lock.write_text("pid:99999", encoding="utf-8")
        # Set mtime back 20 minutes
        old_time = time.time() - 1200
        os.utime(stale_lock, (old_time, old_time))

        res = run_self_healing(self.config)
        self.assertIn("session-old.lock", res["repaired_items"])
        self.assertFalse(stale_lock.exists())

    # 4. Repair corrupted session state files
    def test_repair_corrupted_session_state(self):
        corrupt_state = self.state / "sessions" / "sess-broken.json"
        corrupt_state.write_text("{corrupt-json", encoding="utf-8")

        res = run_self_healing(self.config)
        self.assertIn("state/sessions/sess-broken.json", res["repaired_items"])

        parsed = json.loads(corrupt_state.read_text(encoding="utf-8"))
        self.assertEqual(parsed.get("status"), "recovered")
        self.assertEqual(parsed.get("session_key"), "sess-broken")

    # 5. Clean up stale outbox temporaries
    def test_cleanup_stale_outbox_tmp(self):
        stale_tmp = self.state / "outbox" / ".event-123.tmp"
        stale_tmp.write_text("abandoned temp content", encoding="utf-8")
        old_time = time.time() - 3600
        os.utime(stale_tmp, (old_time, old_time))

        res = run_self_healing(self.config)
        self.assertIn(".event-123.tmp", res["repaired_items"])
        self.assertFalse(stale_tmp.exists())

    # 6. Signed healing receipt generation
    def test_healing_receipt_produced(self):
        res = run_self_healing(self.config)
        self.assertTrue(Path(res["receipt_file"]).is_file())
        self.assertTrue(len(res["receipt_sha256"]) == 64)

        receipt = json.loads(Path(res["receipt_file"]).read_text(encoding="utf-8"))
        self.assertEqual(receipt["schema"], "pikselzone-self-healing-receipt-v1")
        self.assertEqual(receipt["status"], "success")


if __name__ == "__main__":
    unittest.main()

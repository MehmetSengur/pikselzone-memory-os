"""Unit tests for Second-Brain Companion Memory Schema (SB2-02)."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from memory_v1.companion import CompanionManager, LastSessionData, ThreadItem


class TestCompanionManager(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.vault = Path(self.temp_dir.name).resolve()
        self.manager = CompanionManager(self.vault)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    # 1. Initial seeds creation
    def test_ensure_companion_files_creates_all_seeds(self):
        self.manager.ensure_companion_files()
        c_dir = self.manager.companion_dir
        self.assertTrue((c_dir / "Core.md").is_file())
        self.assertTrue((c_dir / "Kurallar.md").is_file())
        self.assertTrue((c_dir / "Last-Session.md").is_file())
        self.assertTrue((c_dir / "Threads.md").is_file())
        self.assertTrue((c_dir / "Journal.md").is_file())

    # 2. Core read and update with secret redaction
    def test_core_read_and_update(self):
        self.manager.ensure_companion_files()
        content = self.manager.read_core()
        self.assertIn("Mehmet Emin Şengür", content)

        new_core = content + "\n- **Özel Not:** test note with secret sk-123456789012345678901234567890\n"
        self.manager.update_core(new_core)
        updated = self.manager.read_core()
        self.assertIn("test note", updated)
        self.assertNotIn("sk-123456789012345678901234567890", updated)
        self.assertIn("[REDACTED_SECRET]", updated)

    # 3. Read rules from Kurallar.md
    def test_read_rules(self):
        self.manager.ensure_companion_files()
        rules = self.manager.read_rules()
        self.assertGreaterEqual(len(rules), 3)
        self.assertTrue(any("otonomi" in r.text.lower() for r in rules))

    # 4. Add rule and prevent duplicate rules
    def test_add_rule_and_duplicate_prevention(self):
        self.manager.ensure_companion_files()
        added = self.manager.add_or_update_rule(
            rule_text="Her zaman test kodlarını src ile paralel tut.",
            reason="Mimari düzeni korumak.",
            source="canary-session-1",
        )
        self.assertTrue(added)

        rules = self.manager.read_rules()
        self.assertTrue(any("paralel tut" in r.text for r in rules))

        # Adding the same rule again must be rejected as duplicate
        duplicate_added = self.manager.add_or_update_rule(
            rule_text="Her zaman test kodlarını src ile paralel tut.",
            reason="Tekrar deneme",
        )
        self.assertFalse(duplicate_added)

    # 5. Last session write and read
    def test_last_session_lifecycle(self):
        self.manager.ensure_companion_files()
        session_data = LastSessionData(
            runtime="claude",
            session_id="sess-test-123",
            completed_items=["Implemented Codex rollout compatibility", "Added 10 regression tests"],
            decisions=["Keep old format as fallback", "Reject silent 0-turn transcripts"],
            pending_items=["SB2-03 startup recall update"],
            next_steps=["Integrate companion schema with recall bundle"],
            active_project="Pikselzone Memory OS",
            user_questions=[],
        )
        self.manager.write_last_session(session_data)
        content = self.manager.read_last_session()
        self.assertIn("sess-test-123", content)
        self.assertIn("Implemented Codex rollout compatibility", content)
        self.assertIn("Reject silent 0-turn transcripts", content)
        self.assertIn("Pikselzone Memory OS", content)

    # 6. Thread update and management
    def test_thread_update(self):
        self.manager.ensure_companion_files()
        thread = ThreadItem(
            thread_id="THREAD-SEO-01",
            title="SEO GEO A8 Entegrasyonu",
            status="active",
            context="SEO ve Harita optimizasyon motorunun entegrasyonu.",
            open_items=["Google Maps API audit", "Keyword cluster analizi"],
            blockers="API kota limiti bekleniyor",
        )
        self.manager.update_thread(thread)
        threads_content = self.manager.read_threads()
        self.assertIn("THREAD-SEO-01", threads_content)
        self.assertIn("SEO GEO A8 Entegrasyonu", threads_content)
        self.assertIn("Google Maps API audit", threads_content)
        self.assertIn("API kota limiti bekleniyor", threads_content)

    # 7. Archive resolved threads
    def test_archive_resolved_threads(self):
        self.manager.ensure_companion_files()
        thread = ThreadItem(
            thread_id="THREAD-DONE-99",
            title="Tamamlanan Görev",
            status="resolved",
            context="Eski iş tamamlandı.",
            open_items=[],
        )
        self.manager.update_thread(thread)
        archived_count = self.manager.archive_resolved_threads()
        self.assertEqual(archived_count, 1)

        # Active threads must no longer contain the resolved thread
        threads_content = self.manager.read_threads()
        self.assertNotIn("THREAD-DONE-99", threads_content)

        # Archive file must exist and contain it
        archive_path = self.manager.companion_dir / "Threads-Archive.md"
        self.assertTrue(archive_path.is_file())
        archive_content = archive_path.read_text(encoding="utf-8")
        self.assertIn("THREAD-DONE-99", archive_content)
        self.assertIn("Tamamlanan Görev", archive_content)

    # 8. Journal append and read latest
    def test_journal_append_and_read_latest(self):
        self.manager.ensure_companion_files()
        self.manager.append_journal_entry(
            title="SB2-02 Checkpoint Tamamlandı",
            narrative="Second Brain V2 için companion şeması başarıyla oluşturuldu ve test edildi.",
            runtime="codex",
        )
        latest = self.manager.read_latest_journal_entry()
        self.assertIn("SB2-02 Checkpoint Tamamlandı", latest)
        self.assertIn("companion şeması başarıyla oluşturuldu", latest)

    # 9. Support for existing '🔮 850-Companion' directory
    def test_avenox_style_directory_discovered(self):
        avenox_dir = self.vault / "🔮 850-Companion"
        avenox_dir.mkdir()
        mgr = CompanionManager(self.vault)
        self.assertEqual(mgr.companion_dir, avenox_dir)
        mgr.ensure_companion_files()
        self.assertTrue((avenox_dir / "Core.md").is_file())


if __name__ == "__main__":
    unittest.main()

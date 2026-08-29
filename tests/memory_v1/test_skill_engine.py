"""Unit tests for Self-Generating & Self-Updating Skills (SB2-06)."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from memory_v1.skill_engine import SkillEngine, WorkflowObservation


class TestSkillEngine(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.vault = Path(self.temp_dir.name).resolve()
        self.engine = SkillEngine(self.vault)
        self.engine.ensure_skills_dirs()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    # 1. Built-in skills creation
    def test_builtin_skills_present(self):
        doktor_file = self.vault / "skills" / "beyin-doktor" / "SKILL.md"
        import_file = self.vault / "skills" / "gecmis-import" / "SKILL.md"
        self.assertTrue(doktor_file.is_file())
        self.assertTrue(import_file.is_file())

        doktor_content = doktor_file.read_text(encoding="utf-8")
        self.assertIn("Skill: Beyin Doktor", doktor_content)
        self.assertIn("## 1. Ne Zaman Tetiklenir", doktor_content)

    # 2. Workflow candidate repetition and automatic synthesis
    def test_workflow_repetition_auto_generates_skill(self):
        obs_1 = WorkflowObservation(
            workflow_name="SEO Harita Denetimi",
            goal="Google Harita yerel SEO doğrulaması ve sıralama denetimi.",
            steps=["Google Maps API bağlantısını kontrol et", "Yerel anahtar kelimeleri tara"],
            tools_or_scripts=["maps_client.py", "curl"],
            session_id="sess-seo-1",
        )
        skill_path = self.engine.record_workflow_observation(obs_1)
        # 1st time: candidate recorded, not yet materialized
        self.assertIsNone(skill_path)

        # 2nd time: repeated workflow triggers auto-synthesis!
        obs_2 = WorkflowObservation(
            workflow_name="SEO Harita Denetimi",
            goal="Google Harita yerel SEO doğrulaması ve sıralama denetimi.",
            steps=["Google Maps API bağlantısını kontrol et", "Yerel anahtar kelimeleri tara", "Skor tablosunu üret"],
            tools_or_scripts=["maps_client.py", "rank_tracker"],
            session_id="sess-seo-2",
        )
        skill_path = self.engine.record_workflow_observation(obs_2)
        self.assertIsNotNone(skill_path)
        self.assertTrue(skill_path.is_file())
        self.assertEqual(skill_path.name, "SKILL.md")

        content = skill_path.read_text(encoding="utf-8")
        self.assertIn("name: \"seo-harita-denetimi\"", content)
        self.assertIn("## 1. Ne Zaman Tetiklenir", content)
        self.assertIn("## 2. Önkoşullar", content)
        self.assertIn("## 3. Adım Adım Çalışma Planı", content)
        self.assertIn("## 4. Kullanılacak Script / Araçlar", content)
        self.assertIn("## 5. Beklenen Çıktı", content)
        self.assertIn("## 6. Hata Durumunda Kurtarma Adımı", content)
        self.assertIn("## 7. Sürüm Geçmişi", content)
        self.assertIn("`rank_tracker`", content)

    # 3. Iterative skill updates with learnings & version bumping
    def test_update_skill_with_learnings(self):
        obs_1 = WorkflowObservation(
            workflow_name="CAPI Test Event Check",
            goal="Meta CAPI test event tool ile sunucu loglarını eşleştir.",
            steps=["Test event code al", "Payload gönder"],
            tools_or_scripts=["curl"],
            session_id="sess-capi-1",
        )
        self.engine.record_workflow_observation(obs_1)
        self.engine.record_workflow_observation(obs_1)  # materialized at v1.0.0

        # User shows new edge-case and parameter
        updated = self.engine.update_skill_with_learnings(
            slug="capi-test-event-check",
            new_param="--test-event-code TEST12345",
            edge_case="IP adresi whitelist'te değilse 403 hatası döner",
            recovery_tweak="403 alınırsa kurumsal proxy'yi devreye sok",
        )
        self.assertTrue(updated)

        skill_file = self.vault / "skills" / "capi-test-event-check" / "SKILL.md"
        content = skill_file.read_text(encoding="utf-8")

        # Check version bump
        self.assertIn('version: "1.1.0"', content)
        # Check added parameter and edge case
        self.assertIn("--test-event-code TEST12345", content)
        self.assertIn("IP adresi whitelist'te değilse", content)
        self.assertIn("kurumsal proxy'yi devreye sok", content)


if __name__ == "__main__":
    unittest.main()

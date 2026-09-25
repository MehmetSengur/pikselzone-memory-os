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
        # Repetition has to come from a separate session to materialize (v1.0.0).
        self.engine.record_workflow_observation(WorkflowObservation(
            workflow_name=obs_1.workflow_name, goal=obs_1.goal, steps=list(obs_1.steps),
            tools_or_scripts=list(obs_1.tools_or_scripts), session_id="sess-capi-2",
        ))

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


class TestSkillRepetitionProvenance(unittest.TestCase):
    """A skill needs repetition across sessions, not calls."""

    def setUp(self) -> None:
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.vault = Path(self._tmp.name).resolve()
        self.engine = SkillEngine(self.vault)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _obs(self, session_id: str) -> WorkflowObservation:
        return WorkflowObservation(
            workflow_name="Staging Deploy",
            goal="Staging ortamına dağıtım.",
            steps=["git pull origin staging", "docker compose up -d"],
            session_id=session_id,
        )

    def test_same_session_repetition_does_not_synthesize(self):
        # Every synthesized skill in the live vault came from one pasted prompt
        # observed twice inside a single session.
        self.assertIsNone(self.engine.record_workflow_observation(self._obs("sess-a")))
        self.assertIsNone(self.engine.record_workflow_observation(self._obs("sess-a")))
        self.assertFalse((self.vault / "skills" / "staging-deploy" / "SKILL.md").exists())

    def test_second_session_synthesizes_with_sources(self):
        self.engine.record_workflow_observation(self._obs("sess-a"))
        path = self.engine.record_workflow_observation(self._obs("sess-b"))
        self.assertIsNotNone(path)
        content = path.read_text(encoding="utf-8")
        self.assertIn('sources: ["sess-a", "sess-b"]', content)

    def test_retired_candidate_is_never_resynthesized(self):
        import json
        self.engine.record_workflow_observation(self._obs("sess-a"))
        state = json.loads(self.engine.candidates_file.read_text(encoding="utf-8"))
        state["candidates"]["staging-deploy"]["status"] = "retired"
        self.engine.candidates_file.write_text(json.dumps(state), encoding="utf-8")
        self.assertIsNone(self.engine.record_workflow_observation(self._obs("sess-b")))
        self.assertFalse((self.vault / "skills" / "staging-deploy" / "SKILL.md").exists())


if __name__ == "__main__":
    unittest.main()

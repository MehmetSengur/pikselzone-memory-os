"""Tests for Second Brain V2 startup recall bundle and targeted recall (SB2-03)."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from memory_v1.companion import CompanionManager, LastSessionData, ThreadItem
from memory_v1.core import MemoryConfig
from memory_v1.recall import build_startup_recall_bundle, targeted_recall


class TestSecondBrainRecall(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name).resolve()
        self.vault = self.root / "vault"
        self.state = self.root / "state"
        self.vault.mkdir(parents=True)
        self.state.mkdir(parents=True)
        (self.vault / "daily").mkdir(parents=True)
        (self.vault / "knowledge").mkdir(parents=True)
        (self.vault / "skills").mkdir(parents=True)

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

    # 1. Startup recall bundle contains companion memory tiers
    def test_startup_bundle_includes_companion_sections(self):
        # Update companion files with custom markers
        self.companion.update_core("# İkinci Beyin — Çekirdek Kimlik\n- **Kullanıcı:** Mehmet Emin Şengür (Pikselzone)\n- **Odak:** SEO GEO A8 ve E-Ticaret Otomasyonları")
        self.companion.add_or_update_rule("Her zaman bash scriptleri macOS ve Linux uyumlu yaz.", reason="Cross-platform taşınabilirlik")
        self.companion.write_last_session(LastSessionData(
            runtime="claude",
            session_id="sess-continuity-test",
            completed_items=["SB2-01 Codex rollout fix", "SB2-02 companion schema"],
            decisions=["Otonomi default yapıldı"],
            pending_items=["SB2-03 recall optimizasyonu"],
            next_steps=["Rules auto learning"],
            active_project="Pikselzone Memory OS",
        ))
        self.companion.append_journal_entry(
            title="Sistem Dönüşümü",
            narrative="Second Brain V2 mimarisine geçiş başarıyla devam ediyor.",
            runtime="claude",
        )

        bundle = build_startup_recall_bundle(self.config, runtime="claude", session_key="canary-session")
        
        # Verify content presence
        self.assertIn("Mehmet Emin Şengür", bundle.text)
        self.assertIn("SEO GEO A8", bundle.text)
        self.assertIn("macOS ve Linux uyumlu yaz", bundle.text)
        self.assertIn("sess-continuity-test", bundle.text)
        self.assertIn("Otonomi default yapıldı", bundle.text)
        self.assertIn("Sistem Dönüşümü", bundle.text)
        
        # Verify budget bound
        self.assertLessEqual(len(bundle.text), 16000)

    # 2. Targeted recall searches companion files
    def test_targeted_recall_finds_companion_rules(self):
        self.companion.add_or_update_rule(
            "Veritabanı silme işlemlerinde kesinlikle kullanıcı onayı iste.",
            reason="Veri kaybını önleme kuralı",
        )
        res = targeted_recall(self.config, query="veritabanı silme")
        self.assertGreaterEqual(res["items_count"], 1)
        self.assertIn("Veritabanı silme", res["markdown"])

    # 3. Targeted recall searches skills
    def test_targeted_recall_finds_skills(self):
        skill_dir = self.vault / "skills" / "seo-geo-audit"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            "# SEO GEO Audit Skill\n\n## Ne Zaman Kullanılır\nGoogle Harita ve yerel SEO denetimi yaparken.",
            encoding="utf-8",
        )
        res = targeted_recall(self.config, query="yerel SEO denetimi")
        self.assertGreaterEqual(res["items_count"], 1)
        self.assertIn("SEO GEO Audit", res["markdown"])

    # 4. Strict budget bounds maintained even with large companion files
    def test_budget_bounds_with_large_companion_files(self):
        # Create a large Core and Last-Session file
        large_core = "# Core Identity\n" + ("Bilgi satırı test context.\n" * 500)
        self.companion.update_core(large_core)

        bundle = build_startup_recall_bundle(self.config, runtime="codex", budget_chars=4000)
        self.assertLessEqual(len(bundle.text), 4000)


if __name__ == "__main__":
    unittest.main()

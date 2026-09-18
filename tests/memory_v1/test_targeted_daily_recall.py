"""Targeted recall reaches every section of a daily event; startup stays condensed."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from memory_v1.core import MemoryConfig
from memory_v1.events import EventWriter
from memory_v1.recall import build_startup_recall_bundle, targeted_recall


class TargetedDailyRecallTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name).resolve()
        self.vault = root / "vault"
        (self.vault / "companion").mkdir(parents=True)
        (self.vault / "companion" / "Core.md").write_text("# Core\n- Kullanıcı\n", encoding="utf-8")
        self.config = MemoryConfig.from_dict({
            "role": "workstation", "vault_path": str(self.vault), "state_path": str(root / "state"),
            "runtimes": ["claude", "codex"], "transcript_roots": {"claude": [str(root)], "codex": [str(root)]},
            "can_write_event_memory": True, "can_run_compiler": False,
            "models": {"flush": "gpt-5.6-luna", "compiler": "gpt-5.6-terra"}, "provider": {"mode": "runtime-native"},
        })
        day = self.vault / "daily" / "2026-09-14"
        day.mkdir(parents=True)
        (day / f"hermes-{'a' * 32}.md").write_text(EventWriter._render(
            runtime="hermes", agent_id="hermes-main", session_id="20260914_desktop", event="session_finalize",
            events_seen=["session_finalize"], created_at="2026-09-14T21:14:30+03:00",
            source_model="gpt-5.6-luna", source_provider="openai-codex", root_task_id="t", kanban_ids=[],
            source_digest="b" * 64, redaction_count=0,
            summary={
                "context": ["Örnek bakım planı üç kontrol içeriyor: disk sağlık taraması ve yedek geri okuma."],
                "important_conversations": ["Bakım penceresi Gece-Kuşu-7c485f ve iş emri WO-8820-7c485f aktarıldı."],
                "decisions": [], "learnings": ["Yedek geri okuma süresi ölçülmedi."],
                "open_items": ["Süre ölçülmeli."], "evidence": ["bakim-plani-test-7c485f.md satır 3-9"],
            },
        ), encoding="utf-8")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_identifier_outside_context_is_found_by_targeted_recall(self):
        result = targeted_recall(self.config, "bakım penceresi iş emri")
        self.assertIn("Gece-Kuşu-7c485f", result["markdown"])
        self.assertIn("WO-8820-7c485f", result["markdown"])
        self.assertIn("bakim-plani-test-7c485f.md", result["markdown"])

    def test_startup_bundle_keeps_the_condensed_form(self):
        bundle = build_startup_recall_bundle(self.config, runtime="hermes")
        self.assertIn("disk sağlık taraması", bundle.text)
        self.assertNotIn("WO-8820-7c485f", bundle.text)

    def test_unknown_placeholders_are_not_rendered(self):
        self.assertNotIn("- unknown", targeted_recall(self.config, "bakım planı")["markdown"])


if __name__ == "__main__":
    unittest.main()

class NativeTransportBudgetTests(unittest.TestCase):
    setUp = TargetedDailyRecallTests.setUp
    tearDown = TargetedDailyRecallTests.tearDown

    def test_startup_retains_targeted_fact_inside_native_spill_cap(self):
        from unittest.mock import patch
        from memory_v1.profile_integration import profile_recall
        settings={'mode':'normal','owner':'','project':'unscoped','projects':[],
                  'shared':True,'config_path':'/unused','base_dir':str(self.vault.parent)}
        (self.vault/'companion/Core.md').write_text('# Core\n' + 'identity context '*1000)
        (self.vault/'companion/Kurallar.md').write_text('# Kurallar\n' + 'bakım penceresi iş emri '*200)
        event = next((self.vault/'daily/2026-09-14').glob('*.md'))
        event.write_text(event.read_text().replace('Yedek geri okuma süresi ölçülmedi.',
            'Yedek geri okuma süresi ölçülmedi. ' + 'Kontrol sonucu bekleniyor. '*35))
        with patch('memory_v1.core.MemoryConfig.load',return_value=self.config), \
             patch('memory_v1.profile_integration._native_context_budget',return_value=10000), \
             patch('memory_v1.profile_integration.record_status') as status:
            result=profile_recall(settings,session_id='new',query='bakım penceresi iş emri',first=True,
                                  receipt_factory=lambda *a,**kw:None)
        self.assertLessEqual(len(result['context']),10000)
        self.assertIn('WO-8820-7c485f',result['context'])
        self.assertEqual(status.call_args.kwargs['evidence']['bundle_chars'],len(result['context']))

    def test_native_cap_honors_custom_enabled_and_disabled_spill(self):
        import sys,types
        from unittest.mock import patch
        from memory_v1.profile_integration import _native_context_budget
        spill={'enabled':True,'max_chars':2500}
        fake=types.SimpleNamespace(get_spill_config=lambda:spill)
        with patch.dict(sys.modules,{'tools.hook_output_spill':fake}):
            self.assertEqual(_native_context_budget(16000),2500)
            self.assertEqual(_native_context_budget(1500),1500)
            spill['enabled']=False
            self.assertEqual(_native_context_budget(16000),16000)

    def test_startup_can_reserve_targeted_slots_for_nonduplicate_sources(self):
        (self.vault/'companion/Kurallar.md').write_text('# Kurallar\nbakım penceresi iş emri\n')
        ordinary=targeted_recall(self.config,'bakım penceresi iş emri')
        unique=targeted_recall(self.config,'bakım penceresi iş emri',
                              exclude_sources=frozenset({'companion/Kurallar.md'}))
        self.assertIn('companion/Kurallar.md',[r['source'] for r in ordinary['results']])
        self.assertNotIn('companion/Kurallar.md',[r['source'] for r in unique['results']])
        self.assertIn('WO-8820-7c485f',unique['markdown'])
        self.assertTrue(any(a['reason']=='source-already-in-startup' for a in unique['selection_audit']))

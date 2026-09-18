import dataclasses
import unittest
from pathlib import Path
from tests.memory_v1.test_memory_events import MemoryFixture, FakeProvider, SUMMARY
from memory_v1.events import EventWriter, parse_event_artifact
from memory_v1.recall import targeted_recall, build_startup_recall_bundle

class ReliabilityPipelineTests(MemoryFixture):
    def config(self, **kw):
        return dataclasses.replace(super().config(**kw), memory={'project':'brand-a'})
    def event(self, project='brand-a'):
        p = FakeProvider({'status':'empty', **{k:[] for k in SUMMARY if k != 'status'}})
        event = EventWriter(self.config(), p).flush(runtime='codex', agent_id='a', session_id=project,
            event='session_end', project=project,
            transcript=[{'role':'user','content':'Karar: SKU 949 için paket 48 adet ve maliyet 18,75 TL.'}])
        self.assertEqual(len(p.calls), 1)
        return event
    def test_empty_summary_keeps_critical_value_in_normal_recall(self):
        event = self.event()
        self.assertEqual(len(parse_event_artifact(event.read_text())['critical_records']), 1)
        result = targeted_recall(self.config(), 'SKU 949 paket maliyet')
        self.assertIn('48 adet', result['markdown'])
        self.assertIn('18,75 TL', result['markdown'])
    def test_project_private_record_is_not_returned_for_another_project(self):
        self.event()
        other = dataclasses.replace(self.config(), memory={'project':'brand-b'})
        self.assertNotIn('18,75 TL', targeted_recall(other, 'SKU 949 paket')['markdown'])
        self.assertNotIn('18,75 TL', build_startup_recall_bundle(other, runtime='codex')['text'] if False else build_startup_recall_bundle(other, runtime='codex').text)
    def test_budget_never_reports_an_item_that_was_cut_off(self):
        self.event()
        result = targeted_recall(self.config(), 'SKU 949 paket', budget_chars=850)
        self.assertTrue(any(r['reason']=='budget-excluded' for r in result['selection_audit']))
        self.assertNotIn('18,75 TL', result['markdown'])
    def test_corrupt_source_does_not_hide_good_source(self):
        self.event()
        (self.vault/'daily'/'2026-09-18'/'broken.md').write_text('---\nbad\n')
        self.assertIn('18,75 TL', targeted_recall(self.config(), 'SKU 949 paket')['markdown'])

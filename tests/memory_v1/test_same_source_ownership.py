import json
import unittest
from pathlib import Path
from unittest import mock
from tests.memory_v1.test_hermes_finalize_retry import HermesPluginFixture

class SameSourceOwnershipTests(HermesPluginFixture, unittest.TestCase):
    def test_identical_session_and_turn_in_two_databases_are_two_checkpoints(self):
        with mock.patch.dict('os.environ', self.env):
            p = self.plugin
            text = 'USER: SKU 42 paket 24 adet\nASSISTANT: Tamam'
            p._stage_completed_turn_checkpoint('same', text, None, None, 0, database='/a/state.db')
            p._stage_completed_turn_checkpoint('same', text, None, None, 0, database='/b/state.db')
            paths = list((self.state/'checkpoints').glob('*.json'))
            self.assertEqual(len(paths), 2)
            p._clear_turn_checkpoints('same', database='/a/state.db', covered_transcript=text)
            remaining = list((self.state/'checkpoints').glob('*.json'))
            self.assertEqual(len(remaining), 1)
            self.assertEqual(json.loads(remaining[0].read_text())['database'], '/b/state.db')
    def test_same_session_and_digest_have_distinct_event_identity(self):
        a = self.plugin._deterministic_event_path('same', 'a'*64, '/a/state.db')
        b = self.plugin._deterministic_event_path('same', 'a'*64, '/b/state.db')
        self.assertNotEqual(a,b)

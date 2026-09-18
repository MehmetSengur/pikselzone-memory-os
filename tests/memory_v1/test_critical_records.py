import json
import unittest
from memory_v1.critical_records import extract_records, validate_records, render_records
from memory_v1.core import SchemaError

class CriticalRecordsTests(unittest.TestCase):
    def extract(self, text):
        return extract_records(text, runtime='hermes', session_id='same', owner='a'*64, project='brand')
    def test_values_survive_without_summary_or_second_model(self):
        r = self.extract('USER: Karar: SKU 949 için koli 24 adet, birim maliyet 18,75 TL.\nASSISTANT: Tamam.')
        self.assertIn('18,75 TL', render_records(r))
        self.assertEqual(r[0]['claim'], 'user-statement')
        self.assertTrue(r[0]['source_ref'].endswith('#message-0'))
        validate_records(r)
    def test_assistant_verified_claim_is_never_tool_evidence(self):
        r = self.extract('USER: Merhaba\nASSISTANT: Doğruladım: SKU 949 fiyat 12 TL.')
        self.assertEqual(r, [])
    def test_pasted_logs_and_canaries_do_not_become_decisions(self):
        for text in ('USER: ```\nSKU 949 fiyat 12 TL\n```', 'USER: Test canary PZ-TEST-12345 kalıcı kural oluşturma'):
            self.assertEqual(self.extract(text), [])
    def test_correction_keeps_both_values_and_link(self):
        r = self.extract('USER: SKU 949: koli 24 adet.\nASSISTANT: Tamam\nUSER: Düzeltme: SKU 949: koli 48 adet.')
        self.assertEqual(len(r), 2)
        self.assertIn(r[0]['id'], r[1]['supersedes'])
        self.assertIn('24 adet', render_records(r))
        self.assertIn('48 adet', render_records(r))
    def test_tampered_hash_rejected_and_secrets_redacted(self):
        r = self.extract('USER: Karar: password=supersecret123 ve SKU 9 maliyet 10 TL.')
        self.assertNotIn('supersecret123', json.dumps(r))
        r[0]['text'] += 'oops'
        with self.assertRaises(SchemaError): validate_records(r)
    def test_long_irrelevant_context_does_not_cut_critical_value(self):
        r = self.extract('USER: ' + 'Açıklama. ' * 3000 + ' Karar: paket 72 adet olacak.')
        self.assertIn('72 adet', render_records(r))

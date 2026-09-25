import unittest
from memory_v1.memory_policy import resolve_policy, access_reason

class PolicyTests(unittest.TestCase):
    def test_modes_retain_raw_and_have_explicit_effects(self):
        for mode in ('normal', 'economic', 'manual'):
            p = resolve_policy({'mode': mode})
            self.assertTrue(p['capture'])
            self.assertEqual(p['summarize'], mode != 'manual')
            self.assertEqual(p['compile'], mode != 'manual')
            self.assertEqual(p['recall'], mode != 'manual')
        self.assertFalse(resolve_policy({'no_memory': True})['capture'])
    def test_override_cannot_reenable_no_memory_or_grant_scope(self):
        p = resolve_policy({'no_memory': True, 'projects': ['a']}, {'no_memory': False, 'projects': ['b']})
        self.assertFalse(p['capture']); self.assertEqual(p['projects'], ['a'])
    def test_owner_and_project_require_independent_grants(self):
        p = resolve_policy({'owner': 'one', 'project': 'a'})
        self.assertEqual(access_reason({'owner': 'two', 'visibility': 'private'}, p), 'scope-excluded')
        self.assertEqual(access_reason({'project': 'b'}, p), 'scope-excluded')
        self.assertIsNone(access_reason({'visibility': 'shared'}, p))

class ExplicitProjectGrantsTests(unittest.TestCase):
    def test_project_share_requires_grant_and_private_owner_still_wins(self):
        from memory_v1.memory_policy import resolve_policy, access_reason
        p=resolve_policy({'owner':'reader','projects':['brand-a']})
        meta={'owner':'writer','project':'brand-a','visibility':'project'}
        self.assertIsNone(access_reason(meta,p))
        self.assertEqual(access_reason({**meta,'visibility':'private'},p),'scope-excluded')
        self.assertEqual(access_reason(meta,resolve_policy({'owner':'reader'})),'scope-excluded')

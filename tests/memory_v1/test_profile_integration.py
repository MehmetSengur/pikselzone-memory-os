import json
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import Mock

from memory_v1.profile_integration import install_discovery, profile_settings


class ProfileIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.plugin = self.root / 'code' / 'pz-memory-v1'
        self.plugin.mkdir(parents=True)
        (self.plugin / 'plugin.yaml').write_text('name: pz-memory-v1\nversion: 1.1.0\n')
        self.home = self.root / 'profiles' / 'random-82de'
        self.home.mkdir(parents=True)
        self.policy = {'schema': 'pz-memory-profiles-v1', 'plugin_path': str(self.plugin),
                       'base_dir': str(self.root / 'state'), 'profile_root': str(self.root),
                       'config_path': str(self.root / 'config.json'), 'grants': {}}
        self.config = {'model': 'unchanged', 'plugins': {'enabled': ['other']}}
        self.manifest = types.SimpleNamespace(name='pz-memory-v1', path=str(self.plugin), key='pz-memory-v1')
        self.mod = types.SimpleNamespace(collect_directory_manifests=lambda: [],
                        _get_enabled_plugins=lambda: {'other'}, _get_disabled_plugins=lambda: set(),
                        parse_manifest_file=lambda *a: self.manifest)

    def install(self):
        install_discovery(self.mod, policy=self.policy, get_home=lambda: self.home,
                          load_config=lambda: self.config)

    def test_new_native_profile_is_discovered_without_config_or_plugin_copy(self):
        before = json.dumps(self.config, sort_keys=True)
        self.install()
        self.assertEqual(self.mod.collect_directory_manifests(), [self.manifest])
        self.assertEqual(self.mod._get_enabled_plugins(), {'other', 'pz-memory-v1'})
        self.assertEqual(json.dumps(self.config, sort_keys=True), before)
        self.assertFalse((self.home / 'plugins').exists())

    def test_double_install_and_profile_switch_use_one_central_manifest(self):
        self.install(); self.install()
        self.home = self.root / 'profiles' / 'second'
        self.home.mkdir()
        self.assertEqual(len(self.mod.collect_directory_manifests()), 1)
        self.assertEqual(self.mod.collect_directory_manifests()[0].path, str(self.plugin))

    def test_disabled_and_no_memory_survive_reconciliation(self):
        for config in ({'plugins': {'disabled': ['pz-memory-v1']}}, {'pz_memory': {'no_memory': True}}):
            self.config = config
            self.install()
            self.assertEqual(self.mod.collect_directory_manifests(), [])
            self.assertNotIn('pz-memory-v1', self.mod._get_enabled_plugins())

    def test_service_metadata_excludes_capture_without_disabling_other_plugins(self):
        (self.home / 'service-profile.json').write_text(json.dumps({
            'schema': 'pikselzone-service-profile-v1', 'service': 'compiler'}))
        self.install()
        self.assertEqual(self.mod.collect_directory_manifests(), [])
        self.assertIn('other', self.mod._get_enabled_plugins())

    def test_profile_cannot_grant_itself_another_project(self):
        self.config = {'pz_memory': {'projects': ['secret-brand'], 'mode': 'economic'}}
        s = profile_settings(self.home, self.config, self.policy)
        self.assertEqual(s['projects'], [])
        self.assertEqual(s['project'], 'unscoped')
        self.assertEqual(s['mode'], 'economic')

    def test_profile_outside_trusted_root_is_not_enrolled(self):
        self.assertEqual(profile_settings(self.root.parent / 'outside', {}, self.policy)['status'], 'out-of-scope')

"""Service profiles stay out of user surfaces; user profiles are untouched."""
from __future__ import annotations

import json
import tempfile
import types
import unittest
from pathlib import Path

from memory_v1 import hermes_guards as guards


def _fake_profiles(root: Path) -> types.ModuleType:
    """The three hermes_cli.profiles functions the guard wraps, over a real directory tree."""
    module = types.ModuleType("hermes_cli.profiles")
    profiles_root = root / "profiles"

    def _iter_named_profile_dirs(*, live_only=True):
        return [p for p in sorted(profiles_root.iterdir()) if p.is_dir()]

    def get_profile_dir(name):
        return root if name == "default" else profiles_root / name

    def profile_exists(name):
        # Like Hermes 0.21.1: resolves through the module-level get_profile_dir.
        return Path(module.get_profile_dir(name)).is_dir()

    def list_profiles():  # resolves the helper through module globals, like Hermes does
        return ["default", *(p.name for p in module._iter_named_profile_dirs())]

    module._iter_named_profile_dirs = _iter_named_profile_dirs
    module.get_profile_dir = get_profile_dir
    module.profile_exists = profile_exists
    module.list_profiles = list_profiles
    return module


class ServiceProfileGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        for name in ("pz-orchestrator", "pz-sengur", "pz-memory-compiler"):
            (self.root / "profiles" / name).mkdir(parents=True)
        self.compiler = self.root / "profiles" / "pz-memory-compiler"
        (self.compiler / guards.SERVICE_MARKER).write_text(json.dumps({
            "schema": guards.SERVICE_SCHEMA, "service": "knowledge-compiler",
        }), encoding="utf-8")
        self.profiles = _fake_profiles(self.root)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _guard(self, process_home=None):
        guards.install_profile_guards(self.profiles, process_home=process_home or self.root)

    def test_service_profile_is_left_out_of_listings(self):
        self._guard()
        self.assertEqual(["default", "pz-orchestrator", "pz-sengur"], self.profiles.list_profiles())

    def test_service_profile_cannot_be_resolved_for_a_chat(self):
        self._guard()
        with self.assertRaises(guards.ServiceProfileRefused):
            self.profiles.get_profile_dir("pz-memory-compiler")
        self.assertFalse(self.profiles.profile_exists("pz-memory-compiler"))

    def test_user_profiles_and_switching_are_unchanged(self):
        self._guard()
        for name in ("pz-orchestrator", "pz-sengur", "default"):
            self.assertTrue(self.profiles.profile_exists(name))
            self.assertTrue(Path(self.profiles.get_profile_dir(name)).is_dir())

    def test_compiler_process_keeps_its_own_home(self):
        self._guard(process_home=self.compiler)
        self.assertEqual(self.compiler, self.profiles.get_profile_dir("pz-memory-compiler"))

    def test_marker_needs_the_schema_not_just_a_file(self):
        (self.root / "profiles" / "pz-sengur" / guards.SERVICE_MARKER).write_text("{}", encoding="utf-8")
        self._guard()
        self.assertTrue(self.profiles.profile_exists("pz-sengur"))

    def test_guard_is_idempotent(self):
        self._guard()
        wrapped = self.profiles.get_profile_dir
        self._guard()
        self.assertIs(wrapped, self.profiles.get_profile_dir)

    def test_refusal_is_a_file_not_found_error(self):
        # Hermes' tui gateway turns FileNotFoundError from profile resolution into
        # a normal "profile does not exist" reply rather than a server error.
        self.assertTrue(issubclass(guards.ServiceProfileRefused, FileNotFoundError))


class LauncherOrderTests(unittest.TestCase):
    def test_hermes_sees_its_own_argv_before_any_hermes_import(self):
        # The Telegram unit runs `pz-hermes -p pz-orchestrator gateway run`; Hermes
        # strips -p while hermes_cli.main is imported, so argv must already be set.
        import builtins
        import sys
        from unittest import mock

        seen = {}
        real_import = builtins.__import__

        def recording_import(name, *args, **kwargs):
            if name.startswith("hermes_cli") and "argv" not in seen:
                seen["argv"] = list(sys.argv)
            if name in {"hermes_cli.main", "hermes_cli", "hermes_constants"}:
                raise ImportError("stop before running Hermes")
            return real_import(name, *args, **kwargs)

        with mock.patch.object(sys, "argv", ["guards.py"]), mock.patch("builtins.__import__", side_effect=recording_import):
            with self.assertRaises(ImportError):
                guards.main(["-p", "pz-orchestrator", "gateway", "run"])
        self.assertEqual(["hermes", "-p", "pz-orchestrator", "gateway", "run"], seen["argv"])


if __name__ == "__main__":
    unittest.main()

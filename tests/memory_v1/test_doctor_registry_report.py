"""§2 addendum: doctor reports project registration and hook-install drift."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from memory_v1 import hook_install, project_registry
from memory_v1.core import MemoryConfig
from memory_v1.doctor import _project_registry_rows


class DoctorRegistryReportTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="pz-test-doctorreg-")
        self.root = Path(self._tmp.name).resolve()
        self.vault = self.root / "vault"
        self.vault.mkdir()
        self.state = self.root / "state"
        self.repo_a = self.root / "repo-a"
        self.repo_b = self.root / "repo-b"
        self.repo_a.mkdir()
        self.repo_b.mkdir()
        self.cfg = MemoryConfig.from_dict({
            "role": "workstation",
            "vault_path": str(self.vault),
            "state_path": str(self.state),
            "runtimes": ["codex", "claude"],
            "transcript_roots": {"codex": [str(self.root)], "claude": [str(self.root)]},
            "can_write_event_memory": True,
            "can_run_compiler": False,
            "provider": {"mode": "runtime-native"},
        })

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _rows(self) -> dict[str, dict]:
        return {r["check"]: r for r in _project_registry_rows(self.cfg)}

    def test_no_registrations_warns(self) -> None:
        rows = self._rows()
        self.assertEqual(rows["project_registry"]["status"], "warn")
        self.assertIn("no-registered-projects", rows["project_registry"]["detail"])

    def test_multi_root_project_is_summarised(self) -> None:
        for repo in (self.repo_a, self.repo_b):
            project_registry.register(self.state, repo, "luvaa")
            for rt in ("claude", "codex"):
                hook_install.install(
                    repo, runtime=rt, memory_os_root=self.root,
                    config_path=self.root / "c.json", project="luvaa",
                )
        rows = self._rows()
        self.assertEqual(rows["project_registry"]["status"], "pass")
        self.assertIn("2 roots", rows["project_registry"]["detail"])
        self.assertIn("luvaa=2", rows["project_registry"]["detail"])
        self.assertEqual(rows["project_hook_install"]["status"], "pass")

    def test_registered_without_hooks_is_flagged(self) -> None:
        project_registry.register(self.state, self.repo_a, "luvaa")
        rows = self._rows()
        self.assertEqual(rows["project_hook_install"]["status"], "warn")
        self.assertIn("luvaa:claude:missing=", rows["project_hook_install"]["detail"])

    def test_partial_hook_install_is_flagged(self) -> None:
        project_registry.register(self.state, self.repo_a, "luvaa")
        for rt, rel in (("claude", ".claude/settings.local.json"), ("codex", ".codex/hooks.json")):
            hook_install.install(
                self.repo_a, runtime=rt, memory_os_root=self.root,
                config_path=self.root / "c.json", project="luvaa",
            )
        target = self.repo_a / ".claude" / "settings.local.json"
        data = json.loads(target.read_text(encoding="utf-8"))
        del data["hooks"]["UserPromptSubmit"]
        target.write_text(json.dumps(data, indent=2), encoding="utf-8")
        rows = self._rows()
        self.assertEqual(rows["project_hook_install"]["status"], "warn")
        self.assertIn("missing=UserPromptSubmit", rows["project_hook_install"]["detail"])


if __name__ == "__main__":
    unittest.main()

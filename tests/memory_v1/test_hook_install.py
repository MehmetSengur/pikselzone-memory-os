"""Unit tests for memory_v1.hook_install + the register/unregister CLI (V2.3 §2)."""
from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from memory_v1 import hook_install as hi
from memory_v1 import project_registry as pr
from memory_v1.cli import main as cli_main

_WORKSTATION_CONFIG = {
    "role": "workstation",
    "runtimes": ["claude", "codex"],
    "can_write_event_memory": True,
    "can_run_compiler": False,
    "provider": {"mode": "runtime-native"},
    "transcript_roots": {"claude": [], "codex": []},  # filled in setUp
}


class TestHookInstall(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="pz-test-hookinstall-")
        self.root = Path(self._tmp.name).resolve()
        self.repo = self.root / "luvaa"
        (self.repo / ".claude").mkdir(parents=True)
        (self.repo / "src").mkdir()
        self.mos = self.root / "memory-os"
        self.mos.mkdir()
        self.cfg = self.mos / "cfg.json"

        (self.repo / ".claude" / "settings.local.json").write_text(
            json.dumps(
                {
                    "permissions": {"allow": ["Bash(git *)"]},
                    "hooks": {
                        "PreToolUse": [
                            {
                                "matcher": "Bash",
                                "hooks": [
                                    {"type": "command", "command": "guard.sh", "timeout": 5}
                                ],
                            }
                        ]
                    },
                },
                indent=2,
            ),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _claude(self) -> dict:
        return json.loads(
            (self.repo / ".claude" / "settings.local.json").read_text(encoding="utf-8")
        )

    def _do_install(self, runtime: str = "claude", project: str = "luvaa") -> bool:
        return hi.install(
            self.repo, runtime=runtime, memory_os_root=self.mos,
            config_path=self.cfg, project=project,
        )

    # --- install ----------------------------------------------------------
    def test_install_is_non_destructive_and_idempotent(self) -> None:
        self.assertTrue(self._do_install())
        self.assertFalse(self._do_install())  # second run = no change

        data = self._claude()
        self.assertEqual(data["permissions"], {"allow": ["Bash(git *)"]})
        self.assertIn("PreToolUse", data["hooks"])
        self.assertEqual(
            sorted(e for e in data["hooks"] if e != "PreToolUse"),
            sorted(hi.MEMORY_EVENTS),
        )

    def test_installed_command_shape(self) -> None:
        self._do_install()
        cmd = self._claude()["hooks"]["SessionStart"][0]["hooks"][0]["command"]
        self.assertNotIn("cd ", cmd)                       # no cwd change
        # A launcher, not `PYTHONPATH=... -m ...`: cwd would otherwise shadow
        # PYTHONPATH in any repo carrying its own memory_v1/.
        self.assertNotIn("PYTHONPATH=", cmd)
        self.assertTrue(cmd.startswith(str(self.mos / "scripts" / "pz-memory-hook")), cmd)
        self.assertIn(f"--config {self.cfg}", cmd)
        self.assertIn("--runtime claude --event SessionStart", cmd)
        self.assertIn("--project luvaa", cmd)
        self.assertIn(f"--project-root {self.repo}", cmd)

    def test_reinstall_with_new_project_replaces_only_our_entries(self) -> None:
        self._do_install(project="luvaa")
        self.assertTrue(self._do_install(project="twoberries"))
        cmds = [
            h["command"]
            for e in hi.MEMORY_EVENTS
            for entry in self._claude()["hooks"][e]
            for h in entry["hooks"]
        ]
        self.assertTrue(all("--project twoberries" in c for c in cmds))
        self.assertFalse(any("--project luvaa " in c for c in cmds))
        # exactly one entry per event (no accumulation)
        for e in hi.MEMORY_EVENTS:
            self.assertEqual(len(self._claude()["hooks"][e]), 1)

    def test_install_creates_codex_file(self) -> None:
        self.assertTrue(self._do_install(runtime="codex"))
        codex = json.loads((self.repo / ".codex" / "hooks.json").read_text(encoding="utf-8"))
        self.assertEqual(sorted(codex["hooks"]), sorted(hi.MEMORY_EVENTS))
        self.assertIn("--runtime codex", codex["hooks"]["Stop"][0]["hooks"][0]["command"])

    # --- uninstall ------------------------------------------------------
    def test_uninstall_removes_only_our_entries(self) -> None:
        self._do_install()
        self.assertTrue(hi.uninstall(self.repo, runtime="claude"))
        self.assertFalse(hi.uninstall(self.repo, runtime="claude"))  # idempotent

        data = self._claude()
        self.assertEqual(list(data["hooks"]), ["PreToolUse"])  # ours gone, guard kept
        self.assertEqual(data["permissions"], {"allow": ["Bash(git *)"]})

    def test_uninstall_prunes_empty_hooks_map(self) -> None:
        plain = self.root / "plain"
        (plain / ".claude").mkdir(parents=True)
        (plain / ".claude" / "settings.local.json").write_text("{}\n", encoding="utf-8")
        hi.install(
            plain, runtime="claude", memory_os_root=self.mos,
            config_path=self.cfg, project="luvaa",
        )
        hi.uninstall(plain, runtime="claude")
        self.assertEqual(
            json.loads((plain / ".claude" / "settings.local.json").read_text(encoding="utf-8")),
            {},
        )

    # --- gitignore check ----------------------------------------------
    def test_gitignore_unignored(self) -> None:
        self.assertEqual(
            sorted(hi.gitignore_unignored(self.repo)),
            [".claude/settings.local.json", ".codex/hooks.json"],
        )
        (self.repo / ".gitignore").write_text(
            ".claude/settings.local.json\n.codex/\n", encoding="utf-8"
        )
        self.assertEqual(hi.gitignore_unignored(self.repo), [])


class TestRegisterCli(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="pz-test-registercli-")
        self.root = Path(self._tmp.name).resolve()
        self.state = self.root / "state"
        self.vault = self.root / "vault"
        self.vault.mkdir()
        self.repo = self.root / "luvaa"
        self.repo.mkdir()
        self.cfg = self.root / "workstation.json"
        cfg = dict(_WORKSTATION_CONFIG)
        cfg["vault_path"] = str(self.vault)
        cfg["state_path"] = str(self.state)
        cfg["transcript_roots"] = {"claude": [str(self.root)], "codex": [str(self.root)]}
        self.cfg.write_text(json.dumps(cfg), encoding="utf-8")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _run(self, *argv: str) -> dict:
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cli_main(["--config", str(self.cfg), *argv])
        self.assertEqual(rc, 0, buf.getvalue())
        return json.loads(buf.getvalue())

    def test_register_then_unregister_roundtrip(self) -> None:
        out = self._run("register", str(self.repo), "--project", "luvaa")
        self.assertEqual(out["status"], "registered")
        self.assertEqual(out["project"], "luvaa")
        self.assertTrue(out["hooks_changed"]["claude"])
        self.assertTrue(out["hooks_changed"]["codex"])
        self.assertIn(".claude/settings.local.json", out["gitignore_not_excluding"])

        self.assertEqual([e.root for e in pr.lookup(self.state, "luvaa")], [str(self.repo)])
        self.assertTrue((self.repo / ".claude" / "settings.local.json").is_file())
        self.assertTrue((self.repo / ".codex" / "hooks.json").is_file())

        out2 = self._run("unregister", str(self.repo))
        self.assertEqual(out2["status"], "unregistered")
        self.assertTrue(out2["registry_removed"])
        self.assertEqual(pr.lookup(self.state, "luvaa"), [])


if __name__ == "__main__":
    unittest.main()

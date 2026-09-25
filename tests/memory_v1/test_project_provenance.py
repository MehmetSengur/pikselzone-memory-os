"""§3 + §4: project provenance on events and project-scoped continuity routing."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from memory_v1.adapters import checkpoint_hook, drain_checkpoint
from memory_v1.companion import CompanionManager
from memory_v1.core import MemoryConfig
from memory_v1.events import EventWriter, parse_event_artifact

_SUMMARY = {
    "status": "memory",
    "context": ["Luvaa Meta catalog work."],
    "important_conversations": ["Discussed content_ids mismatch."],
    "decisions": ["Verify feed identity strategy first."],
    "learnings": [
        "Problem: catalog match rate low. Denenen: parent feed + variant event id. "
        "Sonuç: basarisiz. Neden: feed/event identity misaligned."
    ],
    "open_items": ["Confirm against live feed."],
    "evidence": ["fixture:test-project-provenance"],
}


class _FakeProvider:
    last_source_model = "gpt-5.6-luna"
    last_source_provider = "chatgpt-subscription"

    def request(self, **kwargs):
        return json.loads(json.dumps(_SUMMARY))


class ProjectProvenanceTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="pz-test-provenance-")
        self.root = Path(self._tmp.name).resolve()
        self.vault = self.root / "vault"
        self.state = self.root / "state"
        self.vault.mkdir()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _config(self, runtimes: list[str]) -> MemoryConfig:
        return MemoryConfig.from_dict({
            "role": "workstation" if set(runtimes) == {"codex", "claude"} else "memory-engine",
            "vault_path": str(self.vault),
            "state_path": str(self.state),
            "runtimes": runtimes,
            "transcript_roots": {rt: [str(self.root)] for rt in runtimes},
            "can_write_event_memory": True,
            "can_run_compiler": False,
            "provider": {"mode": "runtime-native"},
        })

    def _transcript(self, name: str) -> Path:
        p = self.root / name
        p.write_text(
            "\n".join(json.dumps({"message": {"role": r, "content": c}}) for r, c in [
                ("user", "Meta catalog urunler eventlerle eslesmiyor, content_ids?"),
                ("assistant", "Feed ve event identity stratejisini birlikte kontrol edelim."),
            ]) + "\n",
            encoding="utf-8",
        )
        return p

    def _drain_once(self, config: MemoryConfig, runtime: str, *, project, continuity_scope):
        qp = checkpoint_hook(
            config, runtime=runtime,
            payload={
                "hook_event_name": "SessionEnd",
                "session_id": f"sess-{runtime}-{project}",
                "transcript_path": str(self._transcript(f"{runtime}.jsonl")),
                "model": "gpt-5.6-luna",
            },
            project=project, continuity_scope=continuity_scope,
        )
        return drain_checkpoint(config, qp, provider=_FakeProvider())

    # --- claude/codex: real project slug -------------------------------
    def test_claude_event_carries_project_and_scoped_continuity(self) -> None:
        cfg = self._config(["codex", "claude"])
        event_path = self._drain_once(cfg, "claude", project="luvaa", continuity_scope="luvaa")

        artifact = parse_event_artifact(event_path.read_text(encoding="utf-8"))
        self.assertEqual(artifact["project"], "luvaa")

        # Continuity routed to continuity/luvaa.md, NOT companion/Last-Session.md
        self.assertTrue((self.vault / "continuity" / "luvaa.md").is_file())
        self.assertFalse((self.vault / "companion" / "Last-Session.md").is_file())
        scoped = (self.vault / "continuity" / "luvaa.md").read_text(encoding="utf-8")
        self.assertIn("Aktif Proje", scoped)
        self.assertIn("luvaa", scoped)

    def test_second_project_does_not_touch_the_first(self) -> None:
        cfg = self._config(["codex", "claude"])
        self._drain_once(cfg, "claude", project="luvaa", continuity_scope="luvaa")
        lu_before = (self.vault / "continuity" / "luvaa.md").read_text(encoding="utf-8")
        self._drain_once(cfg, "codex", project="twoberries", continuity_scope="twoberries")

        self.assertTrue((self.vault / "continuity" / "twoberries.md").is_file())
        self.assertEqual(
            (self.vault / "continuity" / "luvaa.md").read_text(encoding="utf-8"), lu_before
        )

    # --- hermes: continuity_scope=hermes, project=unscoped -------------
    def test_hermes_event_is_unscoped_but_continuity_is_hermes(self) -> None:
        cfg = self._config(["hermes"])
        event_path = self._drain_once(
            cfg, "hermes", project="unscoped", continuity_scope="hermes"
        )
        artifact = parse_event_artifact(event_path.read_text(encoding="utf-8"))
        self.assertEqual(artifact["project"], "unscoped")

        self.assertTrue((self.vault / "continuity" / "hermes.md").is_file())
        # An unscoped Hermes session never creates a project continuity file.
        self.assertFalse((self.vault / "continuity" / "unscoped.md").exists())

    # --- default (no args) stays backward compatible -----------------
    def test_no_project_defaults_to_unscoped_and_shared_continuity(self) -> None:
        cfg = self._config(["codex", "claude"])
        qp = checkpoint_hook(
            cfg, runtime="claude",
            payload={
                "hook_event_name": "SessionEnd",
                "session_id": "sess-plain",
                "transcript_path": str(self._transcript("plain.jsonl")),
            },
        )
        event_path = drain_checkpoint(cfg, qp, provider=_FakeProvider())
        artifact = parse_event_artifact(event_path.read_text(encoding="utf-8"))
        self.assertEqual(artifact["project"], "unscoped")
        # falls back to the shared companion/Last-Session.md
        self.assertTrue((self.vault / "companion" / "Last-Session.md").is_file())
        self.assertFalse((self.vault / "continuity").exists())

    # --- §9: hermes bypasses the registry gate ----------------------
    def test_hermes_hook_runner_bypasses_registry_gate(self) -> None:
        import io, json as _json
        from unittest import mock
        import memory_v1.hook_runner as hook_runner

        cfg = self._config(["hermes"])
        payload = _json.dumps({
            "hook_event_name": "SessionEnd",
            "session_id": "sess-hermes-gate",
            "transcript_path": str(self._transcript("hermes-gate.jsonl")),
        })
        with mock.patch.object(hook_runner.MemoryConfig, "load", return_value=cfg), \
             mock.patch.object(hook_runner, "_spawn_drain") as spawn, \
             mock.patch("sys.stdin", io.StringIO(payload)):
            rc = hook_runner.main([
                "--config", str(self.root / "c.json"),
                "--runtime", "hermes", "--event", "SessionEnd",
            ])
        # No --project, no registry entry: hermes still captures.
        self.assertEqual(0, rc)
        spawn.assert_called_once()
        pending = list((self.root / "state" / "queue" / "pending").glob("*.json"))
        self.assertEqual(1, len(pending))
        checkpoint = _json.loads(pending[0].read_text(encoding="utf-8"))
        self.assertEqual(checkpoint["project"], "unscoped")
        self.assertEqual(checkpoint["continuity_scope"], "hermes")

    # --- CompanionManager scope validation --------------------------
    def test_companion_rejects_bad_scope(self) -> None:
        from memory_v1.core import SchemaError
        with self.assertRaises(SchemaError):
            CompanionManager(self.vault, continuity_scope="../evil")
        # hermes and slugs are fine
        CompanionManager(self.vault, continuity_scope="hermes")
        CompanionManager(self.vault, continuity_scope="luvaa")


if __name__ == "__main__":
    unittest.main()

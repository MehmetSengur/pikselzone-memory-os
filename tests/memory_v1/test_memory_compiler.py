from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import memory_v1.compiler as compiler_module
import memory_v1.core as core_module
from memory_v1.compiler import TerraCompiler
from memory_v1.core import MemoryConfig, MemoryError, PolicyError, SchemaError, exclusive_lock


class FakeProvider:
    def __init__(self, value):
        self.value = value
        self.calls = []

    def request(self, **kwargs):
        self.calls.append(kwargs)
        return json.loads(json.dumps(self.value))


class CompilerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="pz-memory-compiler-")
        self.root = Path(self.temp.name).resolve()
        self.vault = self.root / "vault"
        self.state = self.root / "state"
        self.vault.mkdir()
        self.config = MemoryConfig.from_dict({
            "role": "memory-engine", "vault_path": str(self.vault),
            "state_path": str(self.state), "runtimes": ["hermes"],
            "transcript_roots": {"hermes": [str(self.root)]},
            "can_write_event_memory": True, "can_run_compiler": True,
            "models": {"flush": "gpt-5.6-luna", "compiler": "gpt-5.6-terra"},
            "provider": {"key_env": "PZ_MEMORY_TEST_KEY"},
        })

    def tearDown(self):
        self.temp.cleanup()

    def event(self, name="hermes-a.md", text="Decision: preserve evidence."):
        path = self.vault / "daily" / "2026-08-27" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("""---
schema: "pikselzone-memory-event-v1"
runtime: "hermes"
agent_id: "hermes-main"
session_id: "fixture-session"
event: "session_end"
events_seen: ["session_end"]
created_at: "2026-08-27T12:00:00+03:00"
source_runtime: "hermes"
source_model: "gpt-5.6-luna"
root_task_id: "fixture"
kanban_ids: []
source_sha256: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
secret_redactions: 0
generated_by: "pikselzone-memory-v1"
authority: "derived-session-memory-not-operational-truth"
---

## Bağlam
- Fixture context.

## Önemli Konuşmalar
- Fixture conversation.

## Alınan Kararlar
- """ + text + """

## Öğrenilenler
- Fixture learning.

## Açık Konular
- unknown

## Kanıtlar
- fixture:test-memory-compiler
""", encoding="utf-8")
        return path

    @staticmethod
    def proposal(path="knowledge/concepts/evidence.md", content="# Evidence\n\nDerived."):
        return {"status": "changes", "writes": [{"path": path, "content": content}]}

    def test_terra_writes_only_allowed_knowledge_tree(self):
        self.event()
        provider = FakeProvider(self.proposal())
        changed = TerraCompiler(self.config, provider).compile()
        self.assertEqual([self.vault / "knowledge/concepts/evidence.md"], changed)
        self.assertEqual([], provider.calls[0].get("tools", []))
        self.assertIn("UNTRUSTED", provider.calls[0]["instruction"])

    def test_forbidden_write_rejected_without_live_change(self):
        self.event()
        provider = FakeProvider(self.proposal(".claude/hooks/overnight-guard.sh", "pwn"))
        with self.assertRaises(PolicyError):
            TerraCompiler(self.config, provider).compile()
        self.assertFalse((self.vault / ".claude").exists())
        self.assertFalse((self.vault / "knowledge").exists())

    def test_path_traversal_rejected(self):
        self.event()
        provider = FakeProvider(self.proposal("knowledge/concepts/../../scripts/x.md", "pwn"))
        with self.assertRaises(PolicyError):
            TerraCompiler(self.config, provider).compile()

    def test_invalid_schema_rejected(self):
        self.event()
        with self.assertRaises(SchemaError):
            TerraCompiler(self.config, FakeProvider({"status": "changes"})).compile()

    def test_invalid_event_artifact_never_reaches_provider(self):
        path = self.vault / "daily/2026-08-27/hermes-bad.md"
        path.parent.mkdir(parents=True)
        path.write_text("not an event\n")
        provider = FakeProvider({"status": "no_changes", "writes": []})
        with self.assertRaises(SchemaError):
            TerraCompiler(self.config, provider).compile()
        self.assertEqual([], provider.calls)

    def test_secret_in_compiler_output_is_rejected(self):
        self.event()
        proposal = self.proposal(content="# Evidence\n\napi_key=sk-testABCDEFGHIJKLMNOPQRSTUVWX")
        with self.assertRaises(PolicyError):
            TerraCompiler(self.config, FakeProvider(proposal)).compile()
        self.assertFalse((self.vault / "knowledge").exists())

    def test_unchanged_events_are_not_reingested(self):
        self.event()
        no_changes = FakeProvider({"status": "no_changes", "writes": []})
        compiler = TerraCompiler(self.config, no_changes)
        self.assertEqual([], compiler.compile())
        self.assertEqual([], compiler.compile())
        self.assertEqual(1, len(no_changes.calls))

    def test_sync_like_concurrent_event_files_are_both_seen(self):
        self.event("hermes-a.md", "Hermes fact")
        self.event("codex-b.md", "Codex fact")
        provider = FakeProvider({"status": "no_changes", "writes": []})
        TerraCompiler(self.config, provider).compile()
        prompt = provider.calls[0]["untrusted_input"]
        self.assertIn("hermes-a.md", prompt)
        self.assertIn("codex-b.md", prompt)

    def test_compiler_lock_second_process_exits_safely(self):
        self.event()
        lock_path = self.state / "locks" / "compiler.lock"
        with exclusive_lock(lock_path):
            with self.assertRaises(MemoryError):
                TerraCompiler(
                    self.config, FakeProvider({"status": "no_changes", "writes": []})
                ).compile()

    def test_symlink_event_is_rejected(self):
        target = self.root / "target.md"
        target.write_text("outside")
        daily = self.vault / "daily" / "2026-08-27"
        daily.mkdir(parents=True)
        (daily / "hermes-link.md").symlink_to(target)
        with self.assertRaises(PolicyError):
            TerraCompiler(
                self.config, FakeProvider({"status": "no_changes", "writes": []})
            ).compile()

    def test_workstation_cannot_run_compiler(self):
        workstation = MemoryConfig.from_dict({
            "role": "workstation", "vault_path": str(self.vault),
            "state_path": str(self.state), "runtimes": ["codex", "claude"],
            "transcript_roots": {
                "codex": [str(self.root)], "claude": [str(self.root)]
            },
            "can_write_event_memory": True, "can_run_compiler": False,
        })
        with self.assertRaises(PolicyError):
            TerraCompiler(
                workstation, FakeProvider({"status": "no_changes", "writes": []})
            ).compile()

    def test_existing_live_file_changed_contract(self):
        knowledge = self.vault / "knowledge/concepts"
        knowledge.mkdir(parents=True)
        live = knowledge / "evidence.md"
        live.write_text("old\n")
        self.event()
        changed = TerraCompiler(
            self.config, FakeProvider(self.proposal(content="new\n"))
        ).compile()
        self.assertEqual([live], changed)
        self.assertEqual("new\n", live.read_text())

    def test_partial_promotion_failure_rolls_back(self):
        concepts = self.vault / "knowledge/concepts"
        concepts.mkdir(parents=True)
        first = concepts / "a.md"
        second = concepts / "b.md"
        first.write_text("old-a\n")
        second.write_text("old-b\n")
        self.event()
        provider = FakeProvider({
            "status": "changes",
            "writes": [
                {"path": "knowledge/concepts/a.md", "content": "new-a"},
                {"path": "knowledge/concepts/b.md", "content": "new-b"},
            ],
        })
        original_atomic_write = compiler_module.atomic_write

        def fail_second_live(path, data, mode=0o600):
            if path == second:
                raise OSError("injected-promotion-failure")
            return original_atomic_write(path, data, mode=mode)

        with mock.patch("memory_v1.compiler.atomic_write", side_effect=fail_second_live):
            with self.assertRaises(OSError):
                TerraCompiler(self.config, provider).compile()
        self.assertEqual("old-a\n", first.read_text())
        self.assertEqual("old-b\n", second.read_text())
        state = json.loads((self.state / "compiler/state.json").read_text())
        self.assertEqual({}, state["ingested"])

    def test_promotion_parent_swap_cannot_redirect_output(self):
        self.event()
        outside = self.root / "promotion-outside"
        outside.mkdir()
        live_parent = self.vault / "knowledge/concepts"
        saved = self.root / "concepts-parent-saved"
        original_replace = core_module.os.replace
        matching_replaces = 0

        def swap_live_parent(source, destination, *args, **kwargs):
            nonlocal matching_replaces
            if isinstance(destination, str) and destination == "evidence.md":
                matching_replaces += 1
                if matching_replaces == 2:
                    live_parent.rename(saved)
                    live_parent.symlink_to(outside, target_is_directory=True)
            return original_replace(source, destination, *args, **kwargs)

        with mock.patch("memory_v1.core.os.replace", side_effect=swap_live_parent):
            with self.assertRaises(PolicyError):
                TerraCompiler(
                    self.config, FakeProvider(self.proposal())
                ).compile()
        self.assertEqual([], list(outside.iterdir()))


if __name__ == "__main__":
    unittest.main()

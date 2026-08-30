from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import importlib.util

from memory_v1.core import MemoryConfig, MemoryError, PolicyError, SchemaError, exclusive_lock, sha256_file
from memory_v1.knowledge_promoter import (
    load_compiler_state,
    promote_knowledge_outbox,
    select_and_stage_batch,
)


def load_knowledge_generator():
    p = Path(__file__).resolve().parent.parent.parent / "hermes_plugins" / "pz-memory-v1" / "knowledge_generator.py"
    spec = importlib.util.spec_from_file_location("pz_knowledge_generator", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class MockLlmSuccess:
    def __init__(self, writes=None, status="changes"):
        self.status = status
        self.writes = writes or [
            {
                "path": "knowledge/concepts/memory-hardening.md",
                "content": "---\ntitle: Memory Hardening\naliases: [hardening]\ntags: [memory, architecture]\nsources: [daily/2026-08-28/event-1.md]\ncreated: 2026-08-28\nupdated: 2026-08-28\nauthority: derived-memory-not-canonical\n---\n\n## Özet\nHardening summary.\n\n## Önemli Noktalar\n- Safe isolation\n\n## Detaylar\nDetails.\n\n## Kaynaklar\n- event-1\n",
            },
            {
                "path": "knowledge/index.md",
                "content": "# Knowledge Index\n\n| Article | Summary | Source | Updated |\n|---|---|---|---|\n| [[concepts/memory-hardening|Memory Hardening]] | Hardening summary | event-1 | 2026-08-28 |\n",
            },
        ]
        self.model = "gpt-5.4-mini"
        self.provider = "custom:pz-openai-serial"

    def complete_structured(self, **kwargs):
        class Response:
            pass
        r = Response()
        r.parsed = {"status": self.status, "writes": self.writes if self.status != "no_changes" else []}
        r.model = self.model
        r.provider = self.provider
        return r


class KnowledgePipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(prefix="pz-knowledge-test-")
        self.root = Path(self.temp_dir.name).resolve()
        self.vault = self.root / "vault"
        self.vault.mkdir()
        self.daily = self.vault / "daily" / "2026-08-28"
        self.daily.mkdir(parents=True)
        self.knowledge = self.vault / "knowledge"
        self.knowledge.mkdir(parents=True)

        self.state = self.root / "state"
        self.state.mkdir()
        self.hermes_data = self.root / "hermes-data"
        self.outbox_root = self.hermes_data / "memory-v1"
        self.outbox_root.mkdir(parents=True)

        self.config = MemoryConfig.from_dict({
            "role": "memory-engine",
            "vault_path": str(self.vault),
            "state_path": str(self.state),
            "runtimes": ["hermes"],
            "transcript_roots": {"hermes": [str(self.hermes_data)]},
            "can_write_event_memory": True,
            "can_run_compiler": True,
            "provider": {"mode": "runtime-native"},
        })

    def tearDown(self):
        self.temp_dir.cleanup()

    def _create_event(self, filename="hermes-e1.md", text="Decision: transient locks"):
        path = self.daily / filename
        content = f"""---
schema: "pikselzone-memory-event-v1"
runtime: "hermes"
agent_id: "hermes-main"
session_id: "sess-e1"
event: "session_end"
events_seen: ["session_end"]
created_at: "2026-08-28T12:00:00+03:00"
source_runtime: "hermes"
source_model: "gpt-5.4-mini"
source_provider: "custom"
root_task_id: "task-1"
kanban_ids: []
source_sha256: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
secret_redactions: 0
generated_by: "pikselzone-memory-v1"
authority: "derived-session-memory-not-operational-truth"
---

## Bağlam
- Test session

## Önemli Konuşmalar
- Discussion

## Alınan Kararlar
- {text}

## Öğrenilenler
- Learning

## Açık Konular
- unknown

## Kanıtlar
- proof
"""
        path.write_text(content, encoding="utf-8")
        return path

    def test_run1_generates_promotes_and_advances_ledger(self):
        self._create_event()
        # 1. Selector stages batch
        batch = select_and_stage_batch(self.config, outbox_root=self.outbox_root, max_events=10)
        self.assertIsNotNone(batch)
        self.assertEqual(1, len(batch["event_digests"]))

        # 2. Generator runs in container
        mock_llm = MockLlmSuccess()
        res_gen = load_knowledge_generator().generate_knowledge(base_dir=str(self.outbox_root), llm_client=mock_llm)
        self.assertEqual("ok", res_gen["status"])
        self.assertEqual(1, res_gen["llm_calls"])
        self.assertEqual(2, res_gen["outbox_writes"])

        # 3. Promoter runs on host
        res_prom = promote_knowledge_outbox(self.config, outbox_root=self.outbox_root)
        self.assertEqual("ok", res_prom["status"])
        self.assertEqual(2, len(res_prom["promoted"]))
        self.assertTrue((self.vault / "knowledge/concepts/memory-hardening.md").is_file())
        self.assertTrue((self.vault / "knowledge/index.md").is_file())

        # Ingestion ledger must advance
        state = load_compiler_state(self.state / "compiler" / "state.json")
        self.assertIn("daily/2026-08-28/hermes-e1.md", state["ingested"])

    def test_promoter_rejects_candidate_that_would_create_broken_graph_link(self):
        self._create_event()
        select_and_stage_batch(self.config, outbox_root=self.outbox_root, max_events=10)
        bad_writes = [{
            "path": "knowledge/concepts/bad.md",
            "content": "---\ntitle: Bad\naliases: []\n---\n# Bad\n[[concepts/missing]]\n",
        }]
        result = load_knowledge_generator().generate_knowledge(
            base_dir=str(self.outbox_root), llm_client=MockLlmSuccess(writes=bad_writes)
        )
        self.assertEqual("ok", result["status"])
        with self.assertRaisesRegex(PolicyError, "candidate-broken-or-noncanonical-wikilink"):
            promote_knowledge_outbox(self.config, outbox_root=self.outbox_root)
        self.assertFalse((self.vault / "knowledge/concepts/bad.md").exists())

    def test_compiler_snapshot_excludes_observed_conflicted_copy(self):
        self._create_event()
        conflict = self.knowledge / "index (Conflicted copy pz-hermes 202608301608).md"
        conflict.write_text("conflict-only-marker", encoding="utf-8")
        batch = select_and_stage_batch(self.config, outbox_root=self.outbox_root, max_events=10)
        self.assertIsNotNone(batch)
        self.assertNotIn("conflict-only-marker", batch["untrusted_existing_knowledge"])

    def test_run2_unchanged_input_causes_zero_model_calls_and_exact_noop(self):
        # Setup: run 1 already completed
        self.test_run1_generates_promotes_and_advances_ledger()

        # Run 2: selector called on unchanged daily events
        batch = select_and_stage_batch(self.config, outbox_root=self.outbox_root, max_events=10)
        self.assertIsNone(batch)  # Exact NO-OP

        # Generator invoked: no inbox batch
        mock_llm = mock.Mock()
        res_gen = load_knowledge_generator().generate_knowledge(base_dir=str(self.outbox_root), llm_client=mock_llm)
        self.assertEqual("no_batch", res_gen["status"])
        self.assertEqual(0, res_gen["llm_calls"])
        self.assertEqual(0, res_gen["outbox_writes"])
        mock_llm.complete_structured.assert_not_called()

        # Promoter invoked: no manifest
        res_prom = promote_knowledge_outbox(self.config, outbox_root=self.outbox_root)
        self.assertEqual("no_manifest", res_prom["status"])
        self.assertEqual([], res_prom["promoted"])

    def test_provider_failure_does_not_advance_ledger_and_leaves_retryable(self):
        self._create_event()
        batch = select_and_stage_batch(self.config, outbox_root=self.outbox_root, max_events=10)
        self.assertIsNotNone(batch)

        # Generator fails (simulating PluginLlm timeout)
        mock_llm = mock.Mock()
        mock_llm.complete_structured.side_effect = TimeoutError("PluginLlm provider timeout")

        with self.assertRaises(TimeoutError):
            load_knowledge_generator().generate_knowledge(base_dir=str(self.outbox_root), llm_client=mock_llm)

        # Batch file MUST remain for retry
        inbox_batch = self.outbox_root / "inbox" / "knowledge-batch.json"
        self.assertTrue(inbox_batch.is_file())

        # Promoter finds no manifest
        res_prom = promote_knowledge_outbox(self.config, outbox_root=self.outbox_root)
        self.assertEqual("no_manifest", res_prom["status"])

        # Ingestion ledger is NOT advanced
        state = load_compiler_state(self.state / "compiler" / "state.json")
        self.assertEqual({}, state["ingested"])

        # Second attempt with working provider succeeds
        mock_llm_ok = MockLlmSuccess()
        res_gen = load_knowledge_generator().generate_knowledge(base_dir=str(self.outbox_root), llm_client=mock_llm_ok)
        self.assertEqual("ok", res_gen["status"])
        res_prom = promote_knowledge_outbox(self.config, outbox_root=self.outbox_root)
        self.assertEqual("ok", res_prom["status"])
        state_after = load_compiler_state(self.state / "compiler" / "state.json")
        self.assertIn("daily/2026-08-28/hermes-e1.md", state_after["ingested"])

    def test_disallowed_path_rejected_and_quarantined(self):
        ev_file = self._create_event()
        real_sha = sha256_file(ev_file)

        # Attacker candidate trying to write outside knowledge/ directly in outbox
        cand_file = self.outbox_root / "outbox" / "knowledge" / "candidates" / ".claude" / "hooks" / "overnight-guard.sh"
        cand_file.parent.mkdir(parents=True, exist_ok=True)
        cand_file.write_text("# pwn\n", encoding="utf-8")
        cand_sha = hashlib.sha256(b"# pwn\n").hexdigest()

        manifest_file = self.outbox_root / "outbox" / "knowledge" / "manifest.json"
        manifest_file.parent.mkdir(parents=True, exist_ok=True)
        manifest_file.write_text(json.dumps({
            "schema": "pikselzone-knowledge-outbox-manifest-v1",
            "batch_id": "b-evil",
            "status": "candidates-staged",
            "source_digests": {"daily/2026-08-28/hermes-e1.md": real_sha},
            "model": "gpt-5.4-mini",
            "provider": "custom",
            "writes": [{"path": ".claude/hooks/overnight-guard.sh", "sha256": cand_sha}],
        }), encoding="utf-8")

        # Promoter must reject with PolicyError
        with self.assertRaises(PolicyError):
            promote_knowledge_outbox(self.config, outbox_root=self.outbox_root)

        # Zero files promoted, ledger not advanced
        self.assertFalse((self.vault / ".claude/hooks/overnight-guard.sh").exists())
        state = load_compiler_state(self.state / "compiler" / "state.json")
        self.assertEqual({}, state["ingested"])

    def test_symlink_candidate_rejected(self):
        self._create_event()
        select_and_stage_batch(self.config, outbox_root=self.outbox_root)
        load_knowledge_generator().generate_knowledge(base_dir=str(self.outbox_root), llm_client=MockLlmSuccess())

        # Tamper candidate file with symlink
        cand_file = self.outbox_root / "outbox" / "knowledge" / "candidates" / "knowledge" / "concepts" / "memory-hardening.md"
        cand_file.unlink()
        cand_file.symlink_to("/etc/passwd")

        with self.assertRaises(PolicyError) as cm:
            promote_knowledge_outbox(self.config, outbox_root=self.outbox_root)
        self.assertIn("candidate-symlink", str(cm.exception))

    def test_secret_candidate_rejected(self):
        self._create_event()
        select_and_stage_batch(self.config, outbox_root=self.outbox_root)

        bad_writes = [
            {
                "path": "knowledge/concepts/secrets.md",
                "content": "---\ntitle: Leak\nauthority: derived-memory-not-canonical\n---\n\nMy secret key is sk-abcdef1234567890abcdef1234567890\n",
            }
        ]
        load_knowledge_generator().generate_knowledge(base_dir=str(self.outbox_root), llm_client=MockLlmSuccess(writes=bad_writes))

        with self.assertRaises(PolicyError) as cm:
            promote_knowledge_outbox(self.config, outbox_root=self.outbox_root)
        self.assertIn("candidate-contains-secrets", str(cm.exception))

    def test_oversized_candidate_rejected(self):
        self._create_event()
        select_and_stage_batch(self.config, outbox_root=self.outbox_root)

        bad_writes = [
            {
                "path": "knowledge/concepts/huge.md",
                "content": "---\ntitle: Huge\nauthority: derived-memory-not-canonical\n---\n\n" + ("x" * 600 * 1024),
            }
        ]
        load_knowledge_generator().generate_knowledge(base_dir=str(self.outbox_root), llm_client=MockLlmSuccess(writes=bad_writes))

        with self.assertRaises(SchemaError) as cm:
            promote_knowledge_outbox(self.config, outbox_root=self.outbox_root)
        self.assertIn("candidate-oversized", str(cm.exception))

    def test_single_writer_lock_busy(self):
        self._create_event()
        lock_path = self.state / "locks" / "compiler.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)

        with exclusive_lock(lock_path):
            with self.assertRaises(MemoryError) as cm:
                promote_knowledge_outbox(self.config, outbox_root=self.outbox_root)
            self.assertIn("lock-busy", str(cm.exception))

    def test_malformed_structured_output_rejected(self):
        self._create_event()
        select_and_stage_batch(self.config, outbox_root=self.outbox_root)

        # Write a corrupt manifest into outbox
        manifest_file = self.outbox_root / "outbox" / "knowledge" / "manifest.json"
        manifest_file.parent.mkdir(parents=True, exist_ok=True)
        manifest_file.write_text("corrupt-json-manifest", encoding="utf-8")

        with self.assertRaises(SchemaError):
            promote_knowledge_outbox(self.config, outbox_root=self.outbox_root)

        state = load_compiler_state(self.state / "compiler" / "state.json")
        self.assertEqual({}, state["ingested"])

    def test_partial_candidate_generation_rejected(self):
        self._create_event()
        select_and_stage_batch(self.config, outbox_root=self.outbox_root)
        load_knowledge_generator().generate_knowledge(base_dir=str(self.outbox_root), llm_client=MockLlmSuccess())

        # Unlink one candidate file so manifest is out-of-sync
        cand = self.outbox_root / "outbox" / "knowledge" / "candidates" / "knowledge" / "index.md"
        cand.unlink()

        with self.assertRaises(PolicyError) as cm:
            promote_knowledge_outbox(self.config, outbox_root=self.outbox_root)
        self.assertIn("missing-candidate-file", str(cm.exception))

        state = load_compiler_state(self.state / "compiler" / "state.json")
        self.assertEqual({}, state["ingested"])

    def test_malformed_model_outputs_fail_closed(self):
        malformed_cases = [
            ("none-parsed", None),
            ("string-parsed", "not-json"),
            ("list-parsed", ["not", "dict"]),
            ("missing-status", {"writes": []}),
            ("unknown-status", {"status": "partial", "writes": []}),
            ("missing-writes", {"status": "changes"}),
            ("non-list-writes", {"status": "changes", "writes": "not-a-list"}),
            ("empty-writes-for-changes", {"status": "changes", "writes": []}),
            ("disallowed-path", {
                "status": "changes",
                "writes": [{"path": ".claude/evil.md", "content": "# Evil\n"}],
            }),
            ("no-changes-with-writes", {
                "status": "no_changes",
                "writes": [{"path": "knowledge/index.md", "content": "# Index\n"}],
            }),
        ]

        for label, parsed_val in malformed_cases:
            with self.subTest(case=label):
                self._create_event(filename=f"hermes-{label}.md")
                batch = select_and_stage_batch(self.config, outbox_root=self.outbox_root)
                self.assertIsNotNone(batch)

                class MockBadLlm:
                    def complete_structured(self, **kwargs):
                        class Response:
                            parsed = parsed_val
                            model = "gpt-5.4-mini"
                            provider = "custom:pz-openai-serial"
                        return Response()

                res_gen = load_knowledge_generator().generate_knowledge(
                    base_dir=str(self.outbox_root), llm_client=MockBadLlm()
                )
                self.assertEqual("blocked", res_gen["status"])

                # Inbox batch MUST remain for retry
                inbox_file = self.outbox_root / "inbox" / "knowledge-batch.json"
                self.assertTrue(inbox_file.is_file(), f"Inbox file deleted on {label}")

                # Manifest MUST NOT exist
                manifest_file = self.outbox_root / "outbox" / "knowledge" / "manifest.json"
                self.assertFalse(manifest_file.exists(), f"Manifest written on {label}")

                # Promoter MUST see no manifest and ledger MUST NOT advance
                res_prom = promote_knowledge_outbox(self.config, outbox_root=self.outbox_root)
                self.assertEqual("no_manifest", res_prom["status"])
                state = load_compiler_state(self.state / "compiler" / "state.json")
                self.assertEqual({}, state["ingested"])

                # Cleanup inbox for next iteration
                inbox_file.unlink()

    def test_multi_file_promotion_write_failure_rollback(self):
        self._create_event()
        select_and_stage_batch(self.config, outbox_root=self.outbox_root)

        # Pre-create an existing concept file in the vault with known content
        existing_concept = self.vault / "knowledge" / "concepts" / "memory-hardening.md"
        existing_concept.parent.mkdir(parents=True, exist_ok=True)
        original_bytes = b"# Original Concept Content\n"
        existing_concept.write_bytes(original_bytes)

        # Generator stages candidate files
        load_knowledge_generator().generate_knowledge(base_dir=str(self.outbox_root), llm_client=MockLlmSuccess())

        # Inject an OSError during the second file write
        from memory_v1 import knowledge_promoter
        original_atomic_write = knowledge_promoter.atomic_write

        call_count = [0]

        def failing_atomic_write(dest, data, mode=0o640):
            call_count[0] += 1
            if call_count[0] == 2:
                raise OSError("simulated-disk-full")
            return original_atomic_write(dest, data, mode=mode)

        with mock.patch("memory_v1.knowledge_promoter.atomic_write", side_effect=failing_atomic_write):
            with self.assertRaises(OSError):
                promote_knowledge_outbox(self.config, outbox_root=self.outbox_root)

        # Invariant 1: Existing concept file MUST be restored to original bytes
        self.assertEqual(original_bytes, existing_concept.read_bytes())

        # Invariant 2: Newly created files must be cleaned up (no partial promotion)
        self.assertFalse((self.vault / "knowledge" / "index.md").exists())

        # Invariant 3: Ingestion ledger MUST NOT have advanced
        state = load_compiler_state(self.state / "compiler" / "state.json")
        self.assertEqual({}, state["ingested"])

        # Invariant 4: Manifest and candidate files must be preserved in outbox for retry
        manifest_file = self.outbox_root / "outbox" / "knowledge" / "manifest.json"
        self.assertTrue(manifest_file.is_file())
        candidates_dir = self.outbox_root / "outbox" / "knowledge" / "candidates"
        self.assertTrue(candidates_dir.is_dir())

    def test_valid_no_changes_manifest_advances_ledger(self):
        ev = self._create_event()
        batch = select_and_stage_batch(self.config, outbox_root=self.outbox_root)
        self.assertIsNotNone(batch)

        class MockLlmNoChanges:
            def complete_structured(self, **kwargs):
                class Response:
                    parsed = {"status": "no_changes", "writes": []}
                    model = "gpt-5.4-mini"
                    provider = "custom:pz-openai-serial"
                return Response()

        res_gen = load_knowledge_generator().generate_knowledge(
            base_dir=str(self.outbox_root), llm_client=MockLlmNoChanges()
        )
        self.assertEqual("no_changes", res_gen["status"])

        res_prom = promote_knowledge_outbox(self.config, outbox_root=self.outbox_root)
        self.assertEqual("no_changes", res_prom["status"])
        self.assertEqual([], res_prom["promoted"])

        # Ingestion ledger MUST advance for the verified source event
        state = load_compiler_state(self.state / "compiler" / "state.json")
        self.assertIn("daily/2026-08-28/hermes-e1.md", state["ingested"])

        # Manifest must be cleanly removed
        manifest_file = self.outbox_root / "outbox" / "knowledge" / "manifest.json"
        self.assertFalse(manifest_file.exists())

    def test_malformed_no_changes_manifest_rejected(self):
        ev_file = self._create_event()
        real_sha = sha256_file(ev_file)
        select_and_stage_batch(self.config, outbox_root=self.outbox_root)
        manifest_file = self.outbox_root / "outbox" / "knowledge" / "manifest.json"
        manifest_file.parent.mkdir(parents=True, exist_ok=True)

        # Case 1: missing source_digests
        manifest_file.write_text(json.dumps({
            "schema": "pikselzone-knowledge-outbox-manifest-v1",
            "batch_id": "b1",
            "status": "no_changes",
            "source_digests": {},
            "model": "gpt-5.4-mini",
            "provider": "custom",
            "writes": [],
        }), encoding="utf-8")
        with self.assertRaises(SchemaError):
            promote_knowledge_outbox(self.config, outbox_root=self.outbox_root)

        # Case 2: missing model provenance
        manifest_file.write_text(json.dumps({
            "schema": "pikselzone-knowledge-outbox-manifest-v1",
            "batch_id": "b1",
            "status": "no_changes",
            "source_digests": {"daily/2026-08-28/hermes-e1.md": real_sha},
            "model": "",
            "provider": "custom",
            "writes": [],
        }), encoding="utf-8")
        with self.assertRaises(SchemaError):
            promote_knowledge_outbox(self.config, outbox_root=self.outbox_root)

        # Case 3: non-empty writes in no_changes manifest
        manifest_file.write_text(json.dumps({
            "schema": "pikselzone-knowledge-outbox-manifest-v1",
            "batch_id": "b1",
            "status": "no_changes",
            "source_digests": {"daily/2026-08-28/hermes-e1.md": real_sha},
            "model": "gpt-5.4-mini",
            "provider": "custom",
            "writes": [{"path": "knowledge/index.md"}],
        }), encoding="utf-8")
        with self.assertRaises(SchemaError):
            promote_knowledge_outbox(self.config, outbox_root=self.outbox_root)


if __name__ == "__main__":
    unittest.main()

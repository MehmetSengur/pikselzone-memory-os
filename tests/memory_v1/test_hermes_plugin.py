"""Unit tests for Hermes Memory V1 plugin and host publisher/promoter architecture."""
from __future__ import annotations

import datetime as dt
import hashlib
import importlib.util
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from memory_v1.core import MemoryConfig, PolicyError, session_key, sha256_file
from memory_v1.publisher import publish_outbox
from memory_v1.compiler import promote_knowledge_outbox
from memory_v1.events import parse_event_artifact


def load_hermes_plugin():
    p = Path(__file__).resolve().parent.parent.parent / "hermes_plugins" / "pz-memory-v1" / "__init__.py"
    spec = importlib.util.spec_from_file_location("pz_memory_v1", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class HermesPluginAndPublisherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="pz-plugin-test-")
        self.root = Path(self.temp.name).resolve()
        self.vault = self.root / "vault"
        self.state = self.root / "runtime-state"
        self.outbox_root = self.root / "hermes-data" / "memory-v1"
        self.events_outbox = self.outbox_root / "outbox" / "events"
        self.evidence_outbox = self.outbox_root / "outbox" / "evidence"
        self.knowledge_outbox = self.outbox_root / "outbox" / "knowledge"

        self.vault.mkdir(parents=True)
        (self.vault / "daily").mkdir(parents=True)
        (self.vault / "knowledge").mkdir(parents=True)
        self.state.mkdir(parents=True)
        (self.state / "locks").mkdir(parents=True)
        (self.state / "evidence").mkdir(parents=True)
        self.events_outbox.mkdir(parents=True)
        self.evidence_outbox.mkdir(parents=True)
        self.knowledge_outbox.mkdir(parents=True)

    def tearDown(self):
        self.temp.cleanup()

    def _make_config(self) -> MemoryConfig:
        raw = {
            "role": "memory-engine",
            "vault_path": str(self.vault),
            "state_path": str(self.state),
            "runtimes": ["hermes"],
            "transcript_roots": {"hermes": [str(self.root)]},
            "can_write_event_memory": True,
            "can_run_compiler": True,
            "provider": {"mode": "runtime-native"},
        }
        return MemoryConfig.from_dict(raw)

    def test_plugin_registration(self):
        plugin = load_hermes_plugin()
        registered = {}

        class MockCtx:
            def register_hook(self, name, cb):
                registered[name] = cb

        plugin.register(MockCtx())
        self.assertIn("on_session_end", registered)
        self.assertIn("on_session_finalize", registered)

    def test_plugin_never_chmods_shared_hermes_data_root(self):
        plugin_source = (Path(__file__).resolve().parent.parent.parent / "hermes_plugins" / "pz-memory-v1" / "__init__.py").read_text(encoding="utf-8")
        generator_source = (Path(__file__).resolve().parent.parent.parent / "hermes_plugins" / "pz-memory-v1" / "knowledge_generator.py").read_text(encoding="utf-8")
        self.assertNotIn('os.chmod(p, 0o750)', plugin_source)
        self.assertNotIn('os.chmod(p, 0o750)', generator_source)

    def test_permission_bootstrap_keeps_pzmemory_traversal_minimal(self):
        script = (Path(__file__).resolve().parent.parent.parent / "scripts" / "pz-memory-permissions-bootstrap").read_text(encoding="utf-8")
        self.assertIn('u:pzmemory:--x,g::---,m::--x', script)
        self.assertIn("grep -qx 'user:pzmemory:--x'", script)
        self.assertIn("grep -qx 'group::---'", script)
        self.assertIn("grep -qx 'mask::--x'", script)

    def test_operator_assets_restore_traversal_before_unprivileged_consumers(self):
        root = Path(__file__).resolve().parent.parent.parent
        compiler_step = (root / "scripts" / "pz-memory-compile-step").read_text(encoding="utf-8")
        publisher_unit = (root / "memory_v1" / "operator" / "pz-memory-publisher.service").read_text(encoding="utf-8")
        compiler_unit = (root / "scripts" / "pz-memory-compiler.service").read_text(encoding="utf-8")
        bootstrap_unit = (root / "memory_v1" / "operator" / "pz-memory-permissions-bootstrap.service").read_text(encoding="utf-8")
        self.assertEqual(1, compiler_step.count('/usr/local/sbin/pz-memory-permissions-bootstrap'))
        self.assertIn('/usr/bin/systemctl start --wait pz-memory-permissions-bootstrap.service', compiler_step)
        self.assertIn('Requires=pz-memory-permissions-bootstrap.service', publisher_unit)
        self.assertIn('Requires=pz-memory-permissions-bootstrap.service', compiler_unit)
        self.assertIn('ReadWritePaths=/srv/pz-hermes/hermes-data', bootstrap_unit)
        self.assertIn('NoNewPrivileges=true', bootstrap_unit)

    def test_double_flush_prevention(self):
        plugin = load_hermes_plugin()
        plugin._IN_MEMORY_COMPLETED.clear()
        plugin._IN_MEMORY_EXECUTING.clear()
        sess_id = "sess-test-double-flush-1"
        self.assertTrue(plugin._claim_session(sess_id))
        self.assertFalse(plugin._claim_session(sess_id))

    def test_recursion_guard(self):
        plugin = load_hermes_plugin()
        with mock.patch.dict(os.environ, {"PZ_MEMORY_INTERNAL_CALL": "1"}):
            self.assertTrue(plugin._is_internal_call())

    def test_secret_redaction(self):
        plugin = load_hermes_plugin()
        text = "My API key is sk-proj-1234567890abcdef1234567890 and Bearer eyJhbGciOiJIUzI1NiJ9.test.sig"
        redacted, count = plugin.redact_sensitive_text(text)
        self.assertEqual(2, count)
        self.assertNotIn("sk-proj-", redacted)
        self.assertNotIn("eyJhbGci", redacted)
        self.assertIn("[REDACTED_SECRET]", redacted)

    def test_publisher_promotes_event_and_evidence(self):
        cfg = self._make_config()
        sess_id = "sess-publisher-test-1"
        sess_hash = hashlib.sha256(sess_id.encode("utf-8")).hexdigest()[:32]

        event_content = (
            "---\n"
            "schema: \"pikselzone-memory-event-v1\"\n"
            "runtime: \"hermes\"\n"
            "agent_id: \"hermes-main\"\n"
            f"session_id: \"{sess_id}\"\n"
            "event: \"session_end\"\n"
            "events_seen: [\"session_end\"]\n"
            "created_at: \"2026-08-28T14:00:00+03:00\"\n"
            "source_runtime: \"hermes\"\n"
            "source_model: \"gpt-5.4-mini-2026-03-17\"\n"
            "source_provider: \"custom:pz-openai-serial\"\n"
            "root_task_id: \"unknown\"\n"
            "kanban_ids: []\n"
            "source_sha256: \"0000000000000000000000000000000000000000000000000000000000000000\"\n"
            "secret_redactions: 0\n"
            "generated_by: \"pikselzone-memory-v1\"\n"
            "authority: \"derived-session-memory-not-operational-truth\"\n"
            "---\n\n"
            "## Bağlam\n- Test bağlamı.\n\n"
            "## Önemli Konuşmalar\n- Test konuşması.\n\n"
            "## Alınan Kararlar\n- Test kararı.\n\n"
            "## Öğrenilenler\n- Test öğrenimi.\n\n"
            "## Açık Konular\n- Test konusu.\n\n"
            "## Kanıtlar\n- Test kanıtı.\n"
        )

        event_file = self.events_outbox / f"hermes-{sess_hash}.md"
        event_file.write_text(event_content, encoding="utf-8")
        event_sha = hashlib.sha256(event_file.read_bytes()).hexdigest()

        evidence_payload = {
            "schema": "pikselzone-memory-activation-evidence-v1",
            "runtime": "hermes",
            "status": "pass",
            "runtime_version": "0.19.0",
            "hook_config_sha256": "0" * 64,
            "smoke_session_key": sess_hash,
            "checkpoint_id": event_file.name,
            "provenance": "automatic-lifecycle-drain",
            "source_provider": "custom:pz-openai-serial",
            "checkpoint_mode": "0600",
            "event_path": f"{cfg.vault_path}/daily/2026-08-28/{event_file.name}",
            "event_sha256": event_sha,
            "duplicate_files": 0,
            "observed_at": "2026-08-28T14:00:00+03:00",
        }
        evidence_file = self.evidence_outbox / f"hermes-{sess_hash}.json"
        evidence_file.write_text(json.dumps(evidence_payload), encoding="utf-8")

        results = publish_outbox(cfg, outbox_root=self.outbox_root)
        self.assertEqual(1, len(results))
        self.assertEqual("published", results[0]["status"])

        promoted_event = self.vault / "daily" / "2026-08-28" / f"hermes-{sess_hash}.md"
        self.assertTrue(promoted_event.is_file())
        self.assertEqual(event_sha, sha256_file(promoted_event))
        self.assertFalse(event_file.exists())

        promoted_evidence = self.state / "evidence" / "hermes-lifecycle-smoke.json"
        self.assertTrue(promoted_evidence.is_file())

    def test_publisher_deduplication(self):
        cfg = self._make_config()
        sess_id = "sess-publisher-dedup-1"
        sess_hash = hashlib.sha256(sess_id.encode("utf-8")).hexdigest()[:32]

        event_content = (
            "---\n"
            "schema: \"pikselzone-memory-event-v1\"\n"
            "runtime: \"hermes\"\n"
            "agent_id: \"hermes-main\"\n"
            f"session_id: \"{sess_id}\"\n"
            "event: \"session_end\"\n"
            "events_seen: [\"session_end\"]\n"
            "created_at: \"2026-08-28T14:00:00+03:00\"\n"
            "source_runtime: \"hermes\"\n"
            "source_model: \"gpt-5.4-mini-2026-03-17\"\n"
            "source_provider: \"custom:pz-openai-serial\"\n"
            "root_task_id: \"unknown\"\n"
            "kanban_ids: []\n"
            "source_sha256: \"0000000000000000000000000000000000000000000000000000000000000000\"\n"
            "secret_redactions: 0\n"
            "generated_by: \"pikselzone-memory-v1\"\n"
            "authority: \"derived-session-memory-not-operational-truth\"\n"
            "---\n\n"
            "## Bağlam\n- Test bağlamı.\n\n"
            "## Önemli Konuşmalar\n- Test konuşması.\n\n"
            "## Alınan Kararlar\n- Test kararı.\n\n"
            "## Öğrenilenler\n- Test öğrenimi.\n\n"
            "## Açık Konular\n- Test konusu.\n\n"
            "## Kanıtlar\n- Test kanıtı.\n"
        )

        daily_dir = self.vault / "daily" / "2026-08-28"
        daily_dir.mkdir(parents=True, exist_ok=True)
        target = daily_dir / f"hermes-{sess_hash}.md"
        target.write_text(event_content, encoding="utf-8")

        event_file = self.events_outbox / f"hermes-{sess_hash}.md"
        event_file.write_text(event_content, encoding="utf-8")

        results = publish_outbox(cfg, outbox_root=self.outbox_root)
        self.assertEqual(1, len(results))
        self.assertEqual("deduplicated", results[0]["status"])
        self.assertFalse(event_file.exists())

    def test_publisher_rejects_symlinks(self):
        cfg = self._make_config()
        real_file = self.root / "real.md"
        real_file.write_text("content")
        symlink_file = self.events_outbox / "hermes-11111111111111111111111111111111.md"
        symlink_file.symlink_to(real_file)

        results = publish_outbox(cfg, outbox_root=self.outbox_root)
        self.assertEqual(1, len(results))
        self.assertEqual("error", results[0]["status"])
        self.assertIn("symlink", results[0]["error"])

    def test_promote_knowledge_outbox(self):
        cfg = self._make_config()

        (self.knowledge_outbox / "concepts").mkdir(parents=True, exist_ok=True)
        (self.knowledge_outbox / "index.md").write_text("# Knowledge Index\n", encoding="utf-8")
        (self.knowledge_outbox / "concepts" / "c_test.md").write_text("# Concept Test\n", encoding="utf-8")

        changed = promote_knowledge_outbox(cfg, outbox_knowledge_root=self.knowledge_outbox)
        self.assertEqual(2, len(changed))
        self.assertTrue((self.vault / "knowledge" / "index.md").is_file())
        self.assertTrue((self.vault / "knowledge" / "concepts" / "c_test.md").is_file())

        second_changed = promote_knowledge_outbox(cfg, outbox_knowledge_root=self.knowledge_outbox)
        self.assertEqual(0, len(second_changed))

    def test_promote_knowledge_rejects_disallowed_paths(self):
        cfg = self._make_config()
        disallowed = self.knowledge_outbox / "unauthorized.py"
        disallowed.write_text("print('attack')", encoding="utf-8")

        with self.assertRaises(PolicyError):
            promote_knowledge_outbox(cfg, outbox_knowledge_root=self.knowledge_outbox)

    def test_lifecycle_receipt_and_doctor_causal_chain(self):
        from memory_v1.doctor import _activation_evidence_valid
        cfg = self._make_config()
        plugin = load_hermes_plugin()

        sess_id = "sess-native-chain-1"
        sess_hash = hashlib.sha256(sess_id.encode("utf-8")).hexdigest()[:32]

        date_str = dt.datetime.now().astimezone().strftime("%Y-%m-%d")
        vault_daily = str(self.vault / "daily" / date_str)
        with mock.patch.dict(os.environ, {
            "PZ_MEMORY_TEST_MODE": "1",
            "PZ_MEMORY_BASE_DIR": str(self.outbox_root),
            "PZ_MEMORY_VAULT_DAILY": vault_daily,
        }):
            receipt = plugin._record_lifecycle_receipt(sess_id, "on_session_end")
            self.assertTrue(receipt["native_invoke"])
            self.assertEqual("on_session_end", receipt["hook_name"])

            summary = {
                "status": "ok",
                "context": ["Native E2E verification context"],
                "important_conversations": [],
                "decisions": ["Enforce causal receipt chain"],
                "learnings": ["Plugin authenticates invoke_hook"],
                "open_items": [],
                "evidence": ["test_evidence_1"],
            }
            event_path = plugin._render_and_stage_event(
                session_id=sess_id,
                summary=summary,
                source_model="gpt-5.4-mini-2026-03-17",
                source_provider="custom:pz-openai-serial",
                root_task_id="task-test-1",
                source_sha="a" * 64,
                redactions=0,
                hook_event="session_end",
                receipt=receipt,
            )
            self.assertIsNotNone(event_path)

        # Publish outbox
        results = publish_outbox(cfg, outbox_root=self.outbox_root)
        self.assertEqual(1, len(results))
        self.assertEqual("published", results[0]["status"])

        promoted_evidence_path = self.state / "evidence" / "hermes-lifecycle-smoke.json"
        self.assertTrue(promoted_evidence_path.is_file())
        evidence_data = json.loads(promoted_evidence_path.read_text(encoding="utf-8"))

        self.assertEqual("hermes-native-lifecycle", evidence_data.get("provenance"))
        self.assertIn("lifecycle_receipt", evidence_data)
        self.assertTrue(evidence_data["lifecycle_receipt"]["native_invoke"])

        # Validate with doctor
        self.assertTrue(_activation_evidence_valid(cfg, "hermes", promoted_evidence_path, None))

    def test_doctor_rejects_unverified_operator_call(self):
        from memory_v1.doctor import _activation_evidence_valid
        cfg = self._make_config()
        plugin = load_hermes_plugin()

        sess_id = "sess-operator-call-1"
        sess_hash = hashlib.sha256(sess_id.encode("utf-8")).hexdigest()[:32]

        with mock.patch.dict(os.environ, {"PZ_MEMORY_BASE_DIR": str(self.outbox_root)}):
            # No PZ_MEMORY_TEST_MODE and called directly -> native_invoke must be False
            receipt = plugin._record_lifecycle_receipt(sess_id, "on_session_end")
            self.assertFalse(receipt["native_invoke"])

            summary = {"status": "ok", "context": ["Fake context"]}
            event_path = plugin._render_and_stage_event(
                session_id=sess_id,
                summary=summary,
                source_model="gpt-5.4-mini-2026-03-17",
                source_provider="custom:pz-openai-serial",
                root_task_id="unknown",
                source_sha="b" * 64,
                redactions=0,
                hook_event="session_end",
                receipt=receipt,
            )

        results = publish_outbox(cfg, outbox_root=self.outbox_root)
        promoted_evidence_path = self.state / "evidence" / "hermes-lifecycle-smoke.json"
        # If published, doctor must reject it
        if promoted_evidence_path.exists():
            self.assertFalse(_activation_evidence_valid(cfg, "hermes", promoted_evidence_path, None))

    def test_transient_failure_recovery(self):
        plugin = load_hermes_plugin()
        sess_id = "sess-transient-failure-1"
        date_str = dt.datetime.now().astimezone().strftime("%Y-%m-%d")
        vault_daily = str(self.vault / "daily" / date_str)

        with mock.patch.dict(os.environ, {
            "PZ_MEMORY_TEST_MODE": "1",
            "PZ_MEMORY_BASE_DIR": str(self.outbox_root),
            "PZ_MEMORY_VAULT_DAILY": vault_daily,
        }):
            plugin._IN_MEMORY_COMPLETED.clear()
            plugin._IN_MEMORY_EXECUTING.clear()

            with mock.patch.object(plugin, "_get_session_transcript", return_value=("USER: hello\nASSISTANT: hi", "gpt-5.4-mini", "task-1", 0)):
                # 1. First run: summarizer fails (returns None)
                with mock.patch.object(plugin, "_summarize_with_hermes", return_value=(None, "", "")):
                    plugin.on_session_end(session_id=sess_id)
                    # Session must NOT be durably completed
                    self.assertFalse(plugin._is_session_completed(sess_id, locks_dir=str(self.outbox_root / "state" / "locks")))

                # 2. Second run: summarizer succeeds
                summary_ok = {
                    "status": "ok",
                    "context": ["Recovered context"],
                    "important_conversations": [],
                    "decisions": ["Retry on transient failure"],
                    "learnings": [],
                    "open_items": [],
                    "evidence": [],
                }
                with mock.patch.object(plugin, "_summarize_with_hermes", return_value=(summary_ok, "custom", "gpt-5.4-mini")):
                    plugin.on_session_finalize(session_id=sess_id)
                    # Now session MUST be durably completed
                    self.assertTrue(plugin._is_session_completed(sess_id, locks_dir=str(self.outbox_root / "state" / "locks")))

                # 3. Third run: duplicate invocation is ignored
                with mock.patch.object(plugin, "_render_and_stage_event") as mock_stage:
                    plugin.on_session_end(session_id=sess_id)
                    mock_stage.assert_not_called()

    def test_hermes_plugin_drift_detection(self):
        from memory_v1.doctor import _hermes_plugin_drift_rows
        cfg = self._make_config()

        # Set up mock hermes data root with global and profile plugins
        hermes_data = self.root / "mock-hermes-data"
        global_plugin = hermes_data / "plugins" / "pz-memory-v1"
        global_plugin.mkdir(parents=True)
        (global_plugin / "__init__.py").write_text("# global plugin\n", encoding="utf-8")
        (global_plugin / "plugin.yaml").write_text("name: pz-memory-v1\n", encoding="utf-8")

        prof_dir = hermes_data / "profiles" / "pz-orchestrator" / "plugins" / "pz-memory-v1"
        prof_dir.mkdir(parents=True)
        (prof_dir / "__init__.py").write_text("# global plugin\n", encoding="utf-8")
        (prof_dir / "plugin.yaml").write_text("name: pz-memory-v1\n", encoding="utf-8")

        # Point config to mock-hermes-data
        cfg.transcript_roots["hermes"] = [str(hermes_data)]

        # 1. Identical copies -> PASS
        rows = _hermes_plugin_drift_rows(cfg)
        self.assertEqual(1, len(rows))
        self.assertEqual("pass", rows[0]["status"])
        self.assertIn("identical:2-copies", rows[0]["detail"])

        # 2. Tampered profile copy -> FAIL
        (prof_dir / "__init__.py").write_text("# tampered plugin\n", encoding="utf-8")
        rows_drift = _hermes_plugin_drift_rows(cfg)
        self.assertEqual(1, len(rows_drift))
        self.assertEqual("fail", rows_drift[0]["status"])
        self.assertIn("drift-init:pz-orchestrator", rows_drift[0]["detail"])


if __name__ == "__main__":
    unittest.main()

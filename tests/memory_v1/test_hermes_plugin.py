"""Unit tests for Hermes Memory V1 plugin and host publisher/promoter architecture."""
from __future__ import annotations

import datetime as dt
import hashlib
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import types
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

    def _fake_discovery_runtime(
        self, sessions_by_profile, *, failing_profiles=(), active_profile="default",
        list_profiles_error=False,
    ):
        """Install only the supported Hermes SessionDB/profile interfaces."""
        profile_infos = []
        databases = {}
        homes = {}
        calls = {"search": [], "gets": [], "exports": [], "read_only": []}
        for profile_name, sessions in sessions_by_profile.items():
            home = self.root / "fake-hermes-profiles" / profile_name
            home.mkdir(parents=True, exist_ok=True)
            db_path = home / "state.db"
            db_path.touch()
            key = str(db_path.resolve())
            profile_infos.append(types.SimpleNamespace(name=profile_name, path=home))
            homes[profile_name] = home
            databases[key] = RuntimeError("profile-db-failure") if profile_name in failing_profiles else sessions

        class FakeSessionDB:
            def __init__(inner_self, db_path=None, read_only=False):
                key = str(Path(db_path).resolve())
                calls["read_only"].append(read_only)
                value = databases[key]
                if isinstance(value, Exception):
                    raise value
                inner_self.sessions = value
                inner_self.key = key

            def search_sessions(inner_self, source=None, limit=20, offset=0):
                calls["search"].append((inner_self.key, source, limit, offset))
                return [{"id": item["id"]} for item in inner_self.sessions[offset:offset + limit]]

            def get_session(inner_self, session_id):
                calls["gets"].append((inner_self.key, session_id))
                for item in inner_self.sessions:
                    if item["id"] == session_id:
                        return {key: value for key, value in item.items() if key != "messages"}
                return None

            def export_session(inner_self, session_id):
                calls["exports"].append((inner_self.key, session_id))
                for item in inner_self.sessions:
                    if item["id"] == session_id:
                        return dict(item)
                return None

            def close(inner_self):
                return None

        profiles_module = types.ModuleType("hermes_cli.profiles")
        def list_profiles():
            if list_profiles_error:
                raise RuntimeError("profile-list-failure")
            return profile_infos

        profiles_module.list_profiles = list_profiles
        profiles_module.get_profile_dir = lambda name: next(
            info.path for info in profile_infos if info.name == name
        )
        hermes_cli_module = types.ModuleType("hermes_cli")
        hermes_cli_module.profiles = profiles_module
        hermes_state_module = types.ModuleType("hermes_state")
        hermes_state_module.SessionDB = FakeSessionDB
        hermes_constants_module = types.ModuleType("hermes_constants")
        hermes_constants_module.get_hermes_home = lambda: homes[active_profile]
        patches = mock.patch.dict(sys.modules, {
            "hermes_state": hermes_state_module,
            "hermes_cli": hermes_cli_module,
            "hermes_cli.profiles": profiles_module,
            "hermes_constants": hermes_constants_module,
        })
        return patches, calls

    @staticmethod
    def _fake_session(session_id, messages):
        return {
            "id": session_id,
            "model": "gpt-5.4-mini",
            "handoff_state": "task-1",
            "messages": messages,
        }

    def test_plugin_registration(self):
        plugin = load_hermes_plugin()
        registered = []

        class MockCtx:
            def register_hook(self, name, cb):
                registered.append((name, cb))

        with mock.patch.object(plugin, "_discover_final_turn_checkpoints") as discover:
            plugin.register(MockCtx())
        self.assertEqual([
            ("on_session_start", plugin.on_session_start),
            ("pre_llm_call", plugin.pre_llm_call),
            ("on_session_end", plugin.on_session_end),
            ("on_session_finalize", plugin.on_session_finalize),
        ], registered)
        discover.assert_called_once_with()

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
        sync_metadata_unit = (root / "memory_v1" / "operator" / "pz-obsidian-sync.service.d" / "20-file-metadata.conf").read_text(encoding="utf-8")
        self.assertEqual(1, compiler_step.count('/usr/local/sbin/pz-memory-permissions-bootstrap'))
        self.assertIn('/usr/bin/systemctl start --wait pz-memory-permissions-bootstrap.service', compiler_step)
        self.assertIn('Requires=pz-memory-permissions-bootstrap.service', publisher_unit)
        self.assertIn('Requires=pz-memory-permissions-bootstrap.service', compiler_unit)
        self.assertIn('ReadWritePaths=/srv/pz-hermes/hermes-data', bootstrap_unit)
        self.assertIn('NoNewPrivileges=true', bootstrap_unit)
        self.assertIn('CapabilityBoundingSet=CAP_FOWNER', sync_metadata_unit)
        self.assertIn('AmbientCapabilities=CAP_FOWNER', sync_metadata_unit)
        self.assertNotIn('CAP_DAC_OVERRIDE', sync_metadata_unit)

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

    def test_sessiondb_turn_checkpoint_is_provider_free_and_idempotent(self):
        plugin = load_hermes_plugin()
        sess_id = "sess-turn-checkpoint-1"
        with mock.patch.dict(os.environ, {"PZ_MEMORY_BASE_DIR": str(self.outbox_root)}):
            with mock.patch.object(
                plugin, "_get_session_transcript",
                return_value=("USER: keep this\nASSISTANT: acknowledged", "gpt-5.4-mini", "task-1", 0),
            ), mock.patch.object(plugin, "_summarize_with_hermes") as summarize:
                self.assertTrue(plugin._stage_turn_checkpoint(sess_id))
                self.assertTrue(plugin._stage_turn_checkpoint(sess_id))
                summarize.assert_not_called()
            checkpoints = plugin._checkpoint_paths(sess_id)
            self.assertEqual(1, len(checkpoints))
            payload = json.loads(Path(checkpoints[0]).read_text(encoding="utf-8"))
            self.assertEqual("pikselzone-memory-turn-checkpoint-v2", payload["schema"])
            self.assertEqual("USER: keep this\nASSISTANT: acknowledged", payload["normalized_transcript"])

    def test_unrelated_historical_recent_sessions_remain_untracked(self):
        plugin = load_hermes_plugin()
        sessions = {
            "default": [self._fake_session("historic-1", [
                {"role": "user", "content": "historic user"},
                {"role": "assistant", "content": "historic assistant"},
            ])],
        }
        patches, calls = self._fake_discovery_runtime(sessions)
        with mock.patch.dict(os.environ, {"PZ_MEMORY_BASE_DIR": str(self.outbox_root)}), patches, \
             mock.patch.object(plugin, "_recover_pending_turn_checkpoints") as recover, \
             mock.patch.object(plugin, "_summarize_with_hermes") as summarize:
            plugin.on_session_start(session_id="different-current")
            recover.assert_not_called()
            summarize.assert_not_called()
            self.assertEqual([], calls["search"])
            self.assertTrue(all(calls["read_only"]))
            self.assertEqual([], calls["exports"])
            self.assertEqual([], plugin._checkpoint_paths("historic-1"))
            cursor = json.loads(Path(plugin._discovery_cursor_path()).read_text(encoding="utf-8"))
            self.assertEqual(1, len(cursor["sessions"]))
            self.assertEqual("different-current", next(iter(cursor["sessions"].values()))["session_id"])
            self.assertIsNone(next(iter(cursor["sessions"].values()))["last_turn_digest"])
            self.assertEqual([], [path for path in self.vault.rglob("*") if path.is_file()])

    def test_real_session_start_arms_before_sessiondb_row_exists(self):
        plugin = load_hermes_plugin()
        current = "pre-persist-current"
        sessions = {
            "default": [],
            "other": [self._fake_session("unrelated", [
                {"role": "user", "content": "historic user"},
                {"role": "assistant", "content": "historic assistant"},
            ])],
        }
        patches, calls = self._fake_discovery_runtime(sessions)
        active_db = self.root / "fake-hermes-profiles" / "default" / "state.db"
        with mock.patch.dict(os.environ, {"PZ_MEMORY_BASE_DIR": str(self.outbox_root)}), patches, \
             mock.patch.object(plugin, "_recover_pending_turn_checkpoints"), \
             mock.patch.object(plugin, "_summarize_with_hermes") as summarize:
            plugin.on_session_start(session_id=current)
            cursor = json.loads(Path(plugin._discovery_cursor_path()).read_text(encoding="utf-8"))
            self.assertEqual(1, len(cursor["sessions"]))
            entry = next(iter(cursor["sessions"].values()))
            self.assertEqual(current, entry["session_id"])
            self.assertEqual(str(active_db.resolve()), entry["database"])
            self.assertIsNone(entry["last_turn_digest"])
            self.assertEqual([], calls["exports"])
            self.assertEqual([], plugin._checkpoint_paths(current))
            self.assertEqual([], list(self.events_outbox.glob("*.md")))
            summarize.assert_not_called()

    def test_pre_persist_armed_session_recovers_completed_turn_on_next_start(self):
        plugin = load_hermes_plugin()
        current = "pre-persist-then-complete"
        sessions = {"default": []}
        patches, calls = self._fake_discovery_runtime(sessions)
        with mock.patch.dict(os.environ, {"PZ_MEMORY_BASE_DIR": str(self.outbox_root)}), patches, \
             mock.patch.object(plugin, "_recover_pending_turn_checkpoints"), \
             mock.patch.object(plugin, "_summarize_with_hermes") as summarize:
            plugin.on_session_start(session_id=current)
            sessions["default"].append(self._fake_session(current, [
                {"role": "user", "content": "one user"},
                {"role": "assistant", "content": "one assistant"},
            ]))
            plugin.on_session_start(session_id=current)
            turn = "USER: one user\nASSISTANT: one assistant"
            digest = hashlib.sha256(turn.encode("utf-8")).hexdigest()
            self.assertTrue(Path(plugin._checkpoint_destination(current, digest)).is_file())
            cursor = json.loads(Path(plugin._discovery_cursor_path()).read_text(encoding="utf-8"))
            self.assertEqual(digest, next(iter(cursor["sessions"].values()))["last_turn_digest"])
            self.assertEqual([(calls["exports"][0][0], current)], calls["exports"])
            summarize.assert_not_called()

    def test_existing_historical_session_first_real_start_is_baseline_not_replay(self):
        plugin = load_hermes_plugin()
        current = "historic-first-real-start"
        session = self._fake_session(current, [
            {"role": "user", "content": "historic user"},
            {"role": "assistant", "content": "historic assistant"},
        ])
        patches, _ = self._fake_discovery_runtime({"default": [session]})
        with mock.patch.dict(os.environ, {"PZ_MEMORY_BASE_DIR": str(self.outbox_root)}), patches, \
             mock.patch.object(plugin, "_recover_pending_turn_checkpoints"):
            plugin.on_session_start(session_id=current)
            historical = "USER: historic user\nASSISTANT: historic assistant"
            historical_digest = hashlib.sha256(historical.encode("utf-8")).hexdigest()
            cursor = json.loads(Path(plugin._discovery_cursor_path()).read_text(encoding="utf-8"))
            self.assertEqual(historical_digest, next(iter(cursor["sessions"].values()))["last_turn_digest"])
            self.assertEqual([], plugin._checkpoint_paths(current))
            session["messages"].extend([
                {"role": "user", "content": "new user"},
                {"role": "assistant", "content": "new assistant"},
            ])
            plugin.on_session_start(session_id=current)
            current_turn = "USER: new user\nASSISTANT: new assistant"
            current_digest = hashlib.sha256(current_turn.encode("utf-8")).hexdigest()
            self.assertTrue(Path(plugin._checkpoint_destination(current, current_digest)).is_file())
            self.assertFalse(Path(plugin._checkpoint_destination(current, historical_digest)).exists())

    def test_current_arm_survives_profile_discovery_failure(self):
        plugin = load_hermes_plugin()
        current = "arm-survives-profile-failure"
        patches, calls = self._fake_discovery_runtime(
            {"default": []}, list_profiles_error=True,
        )
        with mock.patch.dict(os.environ, {"PZ_MEMORY_BASE_DIR": str(self.outbox_root)}), patches, \
             mock.patch.object(plugin, "_recover_pending_turn_checkpoints"), \
             mock.patch.object(plugin, "_summarize_with_hermes") as summarize:
            plugin.on_session_start(session_id=current)
            cursor = json.loads(Path(plugin._discovery_cursor_path()).read_text(encoding="utf-8"))
            self.assertEqual(1, len(cursor["sessions"]))
            self.assertEqual(current, next(iter(cursor["sessions"].values()))["session_id"])
            self.assertEqual([], calls["search"])
            self.assertEqual([], calls["exports"])
            summarize.assert_not_called()

    def test_current_arm_survives_active_sessiondb_lookup_failure(self):
        plugin = load_hermes_plugin()
        current = "arm-survives-active-db-failure"
        patches, calls = self._fake_discovery_runtime(
            {"default": []}, failing_profiles={"default"},
        )
        with mock.patch.dict(os.environ, {"PZ_MEMORY_BASE_DIR": str(self.outbox_root)}), patches, \
             mock.patch.object(plugin, "_recover_pending_turn_checkpoints"), \
             mock.patch.object(plugin, "_summarize_with_hermes") as summarize:
            plugin.on_session_start(session_id=current)
            cursor = json.loads(Path(plugin._discovery_cursor_path()).read_text(encoding="utf-8"))
            self.assertEqual(1, len(cursor["sessions"]))
            self.assertEqual(current, next(iter(cursor["sessions"].values()))["session_id"])
            self.assertEqual([], calls["exports"])
            self.assertEqual([], plugin._checkpoint_paths(current))
            summarize.assert_not_called()

    def test_current_untracked_session_is_baseline_only(self):
        plugin = load_hermes_plugin()
        session = self._fake_session("resumed-historic", [
            {"role": "user", "content": "historic user"},
            {"role": "assistant", "content": "historic assistant"},
        ])
        patches, calls = self._fake_discovery_runtime({"default": [session]})
        with mock.patch.dict(os.environ, {"PZ_MEMORY_BASE_DIR": str(self.outbox_root)}), patches, \
             mock.patch.object(plugin, "_recover_pending_turn_checkpoints"), \
             mock.patch.object(plugin, "_summarize_with_hermes") as summarize:
            plugin.on_session_start(session_id="resumed-historic")
            turn = "USER: historic user\nASSISTANT: historic assistant"
            digest = hashlib.sha256(turn.encode("utf-8")).hexdigest()
            self.assertEqual([(calls["exports"][0][0], "resumed-historic")], calls["exports"])
            self.assertEqual([], plugin._checkpoint_paths("resumed-historic"))
            cursor = json.loads(Path(plugin._discovery_cursor_path()).read_text(encoding="utf-8"))
            self.assertEqual(1, len(cursor["sessions"]))
            self.assertEqual(digest, next(iter(cursor["sessions"].values()))["last_turn_digest"])
            summarize.assert_not_called()

    def test_only_tracked_or_current_sessions_are_exported(self):
        plugin = load_hermes_plugin()
        historic = [self._fake_session(f"historic-{index:02d}", [{"role": "user", "content": "u"}]) for index in range(18)]
        tracked = self._fake_session("tracked", [{"role": "user", "content": "tracked"}])
        current = self._fake_session("current", [{"role": "user", "content": "current"}])
        patches, calls = self._fake_discovery_runtime({"default": [*historic, tracked, current]})
        db_path = self.root / "fake-hermes-profiles" / "default" / "state.db"
        with mock.patch.dict(os.environ, {"PZ_MEMORY_BASE_DIR": str(self.outbox_root)}), patches, \
             mock.patch.object(plugin, "_recover_pending_turn_checkpoints"):
            identity, entry = plugin._discovery_entry(
                profile="default", database=db_path, session_id="tracked", digest=None,
                observed_at="2026-09-01T00:00:00+03:00",
            )
            self.assertTrue(plugin._write_discovery_cursor({
                "schema": plugin.DISCOVERY_CURSOR_SCHEMA, "sessions": {identity: entry},
            }))
            plugin.on_session_start(session_id="current")
            self.assertEqual([], calls["search"])
            self.assertEqual({"tracked", "current"}, {session_id for _, session_id in calls["exports"]})
            self.assertEqual(2, len(calls["exports"]))
            cursor = json.loads(Path(plugin._discovery_cursor_path()).read_text(encoding="utf-8"))
            self.assertEqual(2, len(cursor["sessions"]))
            self.assertFalse(any(session_id.startswith("historic-") for _, session_id in calls["exports"]))
            self.assertFalse(any(plugin._checkpoint_paths(f"historic-{index:02d}") for index in range(18)))

    def test_tracked_session_changed_digest_recovers(self):
        plugin = load_hermes_plugin()
        session = self._fake_session("tracked-crash", [{"role": "user", "content": "start"}])
        patches, calls = self._fake_discovery_runtime({"default": [session]})
        with mock.patch.dict(os.environ, {"PZ_MEMORY_BASE_DIR": str(self.outbox_root)}), patches, \
             mock.patch.object(plugin, "_recover_pending_turn_checkpoints"), \
             mock.patch.object(plugin, "_summarize_with_hermes") as summarize:
            plugin.on_session_start(session_id="tracked-crash")
            session["messages"].append({"role": "assistant", "content": "completed"})
            plugin.on_session_start(session_id="tracked-crash")
            turn = "USER: start\nASSISTANT: completed"
            digest = hashlib.sha256(turn.encode("utf-8")).hexdigest()
            self.assertTrue(Path(plugin._checkpoint_destination("tracked-crash", digest)).is_file())
            self.assertEqual(2, len(calls["exports"]))
            summarize.assert_not_called()

    def test_untracked_historical_session_can_be_resumed_without_replay(self):
        plugin = load_hermes_plugin()
        session = self._fake_session("historic-resumed", [
            {"role": "user", "content": "historic user"},
            {"role": "assistant", "content": "historic assistant"},
        ])
        patches, calls = self._fake_discovery_runtime({"default": [session]})
        with mock.patch.dict(os.environ, {"PZ_MEMORY_BASE_DIR": str(self.outbox_root)}), patches, \
             mock.patch.object(plugin, "_recover_pending_turn_checkpoints"), \
             mock.patch.object(plugin, "_summarize_with_hermes") as summarize:
            plugin.on_session_start(session_id="unrelated")
            self.assertEqual([], calls["exports"])
            plugin.on_session_start(session_id="historic-resumed")
            self.assertEqual([], plugin._checkpoint_paths("historic-resumed"))
            session["messages"].extend([
                {"role": "user", "content": "new user"},
                {"role": "assistant", "content": "new assistant"},
            ])
            plugin.on_session_start(session_id="historic-resumed")
            turn = "USER: new user\nASSISTANT: new assistant"
            digest = hashlib.sha256(turn.encode("utf-8")).hexdigest()
            self.assertTrue(Path(plugin._checkpoint_destination("historic-resumed", digest)).is_file())
            summarize.assert_not_called()

    def test_current_session_outside_recent_window_can_be_baselined_directly(self):
        plugin = load_hermes_plugin()
        historic = [self._fake_session(f"recent-{index:02d}", [{"role": "user", "content": "u"}]) for index in range(20)]
        current = self._fake_session("outside-window", [
            {"role": "user", "content": "current user"},
            {"role": "assistant", "content": "current assistant"},
        ])
        patches, calls = self._fake_discovery_runtime({"default": [*historic, current]})
        with mock.patch.dict(os.environ, {"PZ_MEMORY_BASE_DIR": str(self.outbox_root)}), patches, \
             mock.patch.object(plugin, "_recover_pending_turn_checkpoints"):
            plugin.on_session_start(session_id="outside-window")
            self.assertEqual([], calls["search"])
            self.assertEqual([(calls["exports"][0][0], "outside-window")], calls["exports"])
            cursor = json.loads(Path(plugin._discovery_cursor_path()).read_text(encoding="utf-8"))
            self.assertEqual(1, len(cursor["sessions"]))
            self.assertEqual([], plugin._checkpoint_paths("outside-window"))

    def test_native_plugin_startup_recovers_tracked_session_outside_recent_window(self):
        plugin = load_hermes_plugin()
        recent = [
            self._fake_session(f"recent-{index:02d}", [{"role": "user", "content": "u"}])
            for index in range(plugin.MAX_STARTUP_DISCOVERY_SESSIONS_PER_PROFILE)
        ]
        tracked = self._fake_session("tracked-outside-window", [
            {"role": "user", "content": "crashed user"},
            {"role": "assistant", "content": "durable assistant"},
        ])
        patches, calls = self._fake_discovery_runtime({"default": [*recent, tracked]})
        db_path = self.root / "fake-hermes-profiles" / "default" / "state.db"

        class MockCtx:
            def register_hook(self, name, callback):
                return None

        with mock.patch.dict(os.environ, {"PZ_MEMORY_BASE_DIR": str(self.outbox_root)}), patches, \
             mock.patch.object(plugin, "_recover_pending_turn_checkpoints") as recover, \
             mock.patch.object(plugin, "_summarize_with_hermes") as summarize:
            identity, entry = plugin._discovery_entry(
                profile="default", database=db_path, session_id="tracked-outside-window",
                digest=None, observed_at="2026-09-01T00:00:00+03:00",
            )
            self.assertTrue(plugin._write_discovery_cursor({
                "schema": plugin.DISCOVERY_CURSOR_SCHEMA, "sessions": {identity: entry},
            }))
            plugin.register(MockCtx())
            digest = hashlib.sha256(
                "USER: crashed user\nASSISTANT: durable assistant".encode("utf-8")
            ).hexdigest()
            self.assertTrue(Path(plugin._checkpoint_destination("tracked-outside-window", digest)).is_file())
            self.assertEqual([], calls["search"])
            self.assertEqual([(calls["exports"][0][0], "tracked-outside-window")], calls["exports"])
            self.assertFalse(any(session_id.startswith("recent-") for _, session_id in calls["exports"]))
            recover.assert_not_called()
            summarize.assert_not_called()

    def test_oversized_discovery_cursor_fails_closed(self):
        plugin = load_hermes_plugin()
        with mock.patch.dict(os.environ, {"PZ_MEMORY_BASE_DIR": str(self.outbox_root)}):
            cursor_path = Path(plugin._discovery_cursor_path())
            cursor_path.parent.mkdir(parents=True, exist_ok=True)
            cursor_path.write_text(json.dumps({
                "schema": plugin.DISCOVERY_CURSOR_SCHEMA,
                "sessions": {str(index): {} for index in range(
                    plugin.MAX_STARTUP_DISCOVERY_CURSOR_ENTRIES + 1
                )},
            }), encoding="utf-8")
            cursor, cursor_ok = plugin._load_discovery_cursor()
            self.assertFalse(cursor_ok)
            self.assertEqual({}, cursor)

    def test_startup_and_pre_llm_share_checkpoint_identity(self):
        plugin = load_hermes_plugin()
        session = self._fake_session("identity-parity", [{"role": "user", "content": "start"}])
        patches, _ = self._fake_discovery_runtime({"default": [session]})
        with mock.patch.dict(os.environ, {"PZ_MEMORY_BASE_DIR": str(self.outbox_root)}), patches, \
             mock.patch.object(plugin, "_recover_pending_turn_checkpoints"):
            plugin.on_session_start(session_id="identity-parity")
            session["messages"].append({"role": "assistant", "content": "completed"})
            plugin.on_session_start(session_id="identity-parity")
            expected = plugin._checkpoint_paths("identity-parity")
            with mock.patch.object(
                plugin, "_get_session_transcript",
                return_value=("USER: start\nASSISTANT: completed", "gpt-5.4-mini", "task-1", 0),
            ):
                self.assertTrue(plugin._stage_turn_checkpoint("identity-parity"))
            self.assertEqual(expected, plugin._checkpoint_paths("identity-parity"))

    def test_startup_discovery_skips_completed_and_user_only_sessions(self):
        plugin = load_hermes_plugin()
        completed = self._fake_session("already-completed", [{"role": "user", "content": "u"}])
        user_only = self._fake_session("user-only", [{"role": "user", "content": "u"}])
        patches, _ = self._fake_discovery_runtime({"default": [completed, user_only]})
        with mock.patch.dict(os.environ, {"PZ_MEMORY_BASE_DIR": str(self.outbox_root)}), patches, \
             mock.patch.object(plugin, "_recover_pending_turn_checkpoints"):
            plugin.on_session_start(session_id="already-completed")
            completed["messages"].append({"role": "assistant", "content": "done"})
            plugin._IN_MEMORY_COMPLETED.add("already-completed")
            plugin.on_session_start(session_id="already-completed")
            plugin.on_session_start(session_id="user-only")
            self.assertEqual([], plugin._checkpoint_paths("already-completed"))
            self.assertEqual([], plugin._checkpoint_paths("user-only"))

    def test_checkpoint_failure_leaves_tracked_digest_discoverable(self):
        plugin = load_hermes_plugin()
        session = self._fake_session("retry-discovery", [{"role": "user", "content": "u"}])
        patches, _ = self._fake_discovery_runtime({"default": [session]})
        with mock.patch.dict(os.environ, {"PZ_MEMORY_BASE_DIR": str(self.outbox_root)}), patches, \
             mock.patch.object(plugin, "_recover_pending_turn_checkpoints"):
            plugin.on_session_start(session_id="retry-discovery")
            session["messages"].append({"role": "assistant", "content": "done"})
            with mock.patch.object(plugin, "_stage_completed_turn_checkpoint", return_value=False):
                plugin.on_session_start(session_id="retry-discovery")
            cursor = json.loads(Path(plugin._discovery_cursor_path()).read_text(encoding="utf-8"))
            self.assertEqual(None, next(iter(cursor["sessions"].values()))["last_turn_digest"])
            plugin.on_session_start(session_id="retry-discovery")
            self.assertEqual(1, len(plugin._checkpoint_paths("retry-discovery")))

    def test_discovery_handoff_recovers_one_memory_event_without_replay(self):
        plugin = load_hermes_plugin()
        session = self._fake_session("discovery-memory", [{"role": "user", "content": "u"}])
        patches, _ = self._fake_discovery_runtime({"default": [session]})
        summary = {
            "status": "ok", "context": ["Recovered"], "important_conversations": [],
            "decisions": ["Recovered once"], "learnings": [], "open_items": [], "evidence": [],
        }
        with mock.patch.dict(os.environ, {"PZ_MEMORY_BASE_DIR": str(self.outbox_root)}), patches:
            plugin._discover_final_turn_checkpoints("discovery-memory")
            session["messages"].append({"role": "assistant", "content": "done"})
            plugin._discover_final_turn_checkpoints("discovery-memory")
            self.assertEqual(1, len(plugin._checkpoint_paths("discovery-memory")))
            with mock.patch.object(plugin, "_summarize_with_hermes", return_value=(summary, "custom", "gpt-5.4-mini")) as summarize:
                plugin._recover_pending_turn_checkpoints()
                self.assertEqual(1, summarize.call_count)
                plugin._discover_final_turn_checkpoints("discovery-memory")
                plugin._recover_pending_turn_checkpoints()
                self.assertEqual(1, summarize.call_count)
        self.assertFalse(plugin._checkpoint_paths("discovery-memory"))
        self.assertTrue(plugin._is_session_completed(
            "discovery-memory", locks_dir=str(self.outbox_root / "state" / "locks"),
        ))
        self.assertEqual(1, len(list(self.events_outbox.glob("*.md"))))

    def test_discovery_handoff_recovers_no_memory_without_event_or_replay(self):
        plugin = load_hermes_plugin()
        session = self._fake_session("discovery-empty", [{"role": "user", "content": "u"}])
        patches, _ = self._fake_discovery_runtime({"default": [session]})
        empty = {"status": "empty", "context": [], "important_conversations": [], "decisions": [], "learnings": [], "open_items": [], "evidence": []}
        with mock.patch.dict(os.environ, {"PZ_MEMORY_BASE_DIR": str(self.outbox_root)}), patches:
            plugin._discover_final_turn_checkpoints("discovery-empty")
            session["messages"].append({"role": "assistant", "content": "done"})
            plugin._discover_final_turn_checkpoints("discovery-empty")
            with mock.patch.object(plugin, "_summarize_with_hermes", return_value=(empty, "custom", "gpt-5.4-mini")) as summarize:
                plugin._recover_pending_turn_checkpoints()
                self.assertEqual(1, summarize.call_count)
                plugin._discover_final_turn_checkpoints("discovery-empty")
                plugin._recover_pending_turn_checkpoints()
                self.assertEqual(1, summarize.call_count)
        self.assertFalse(plugin._checkpoint_paths("discovery-empty"))
        self.assertTrue(plugin._is_session_completed(
            "discovery-empty", locks_dir=str(self.outbox_root / "state" / "locks"),
        ))
        self.assertEqual([], list(self.events_outbox.glob("*.md")))

    def test_startup_discovery_is_bounded_and_profile_failure_is_degraded(self):
        plugin = load_hermes_plugin()
        valid = [self._fake_session(f"bounded-{index}", [{"role": "user", "content": "u"}]) for index in range(25)]
        failing = [self._fake_session("bad-profile", [{"role": "user", "content": "u"}])]
        patches, calls = self._fake_discovery_runtime(
            {"broken": failing, "valid": valid}, failing_profiles={"broken"}, active_profile="valid",
        )
        with mock.patch.dict(os.environ, {"PZ_MEMORY_BASE_DIR": str(self.outbox_root)}), patches, \
             mock.patch.object(plugin, "_recover_pending_turn_checkpoints"):
            plugin.on_session_start(session_id="bounded-0")
            calls["search"].clear()
            calls["exports"].clear()
            plugin.on_session_start()
            self.assertEqual([], calls["search"])
            self.assertEqual([(calls["exports"][0][0], "bounded-0")], calls["exports"])
            cursor = json.loads(Path(plugin._discovery_cursor_path()).read_text(encoding="utf-8"))
            self.assertEqual(1, len(cursor["sessions"]))

    def test_startup_recovers_pending_sessiondb_checkpoint_once(self):
        plugin = load_hermes_plugin()
        sess_id = "sess-turn-recovery-1"
        summary = {
            "status": "ok", "context": ["Recovered turn"],
            "important_conversations": [], "decisions": ["Recover safely"],
            "learnings": [], "open_items": [], "evidence": [],
        }
        with mock.patch.dict(os.environ, {
            "PZ_MEMORY_BASE_DIR": str(self.outbox_root),
            "PZ_MEMORY_VAULT_DAILY": str(self.vault / "daily" / "2026-08-31"),
        }):
            plugin._IN_MEMORY_COMPLETED.clear()
            plugin._IN_MEMORY_EXECUTING.clear()
            with mock.patch.object(
                plugin, "_get_session_transcript",
                return_value=("USER: recover\nASSISTANT: pending", "gpt-5.4-mini", "task-1", 0),
            ):
                self.assertTrue(plugin._stage_turn_checkpoint(sess_id))
            with mock.patch.object(plugin, "_summarize_with_hermes", return_value=(summary, "custom", "gpt-5.4-mini")) as summarize:
                plugin._recover_pending_turn_checkpoints()
                self.assertEqual(1, summarize.call_count)
            self.assertFalse(plugin._checkpoint_paths(sess_id))
            self.assertTrue(plugin._is_session_completed(sess_id, locks_dir=str(self.outbox_root / "state" / "locks")))
            event_paths = list((self.outbox_root / "outbox" / "events").glob("*.md"))
            self.assertEqual(1, len(event_paths))
            self.assertEqual("checkpoint_recovery", parse_event_artifact(event_paths[0].read_text())["event"])

    def test_memory_plugin_does_not_read_codex_native_memory(self):
        plugin_source = (Path(__file__).resolve().parent.parent.parent / "hermes_plugins" / "pz-memory-v1" / "__init__.py").read_text(encoding="utf-8")
        adapter_source = (Path(__file__).resolve().parent.parent.parent / "memory_v1" / "adapters.py").read_text(encoding="utf-8")
        self.assertNotIn("memories_1.sqlite", plugin_source)
        self.assertNotIn("memories_1.sqlite", adapter_source)

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

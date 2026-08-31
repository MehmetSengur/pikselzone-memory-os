"""Tests for runtime-native subscription memory (M1.2)."""
from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from memory_v1.adapters import checkpoint_hook, drain_checkpoint
from memory_v1.core import (
    MemoryConfig, NoMemory, ProviderBlocked, SchemaError, discover_codex_binary,
    normalize_transcript, session_key, summary_json_schema, validate_summary,
)
from memory_v1.doctor import run_doctor
from memory_v1.events import EventWriter, parse_event_artifact
from memory_v1.provider import (
    RuntimeNativeProvider, create_provider,
    summarize_with_claude, summarize_with_codex, summarize_with_hermes,
)
import memory_v1.cli as memory_cli
import memory_v1.hook_runner as hook_runner


SAMPLE_SUMMARY = {
    "status": "memory",
    "context": ["Session investigating infrastructure deployment."],
    "important_conversations": ["User decided to proceed with slice-a."],
    "decisions": ["Deploy slice-a today."],
    "learnings": ["Pro subscription handles non-interactive CLI summaries."],
    "open_items": ["Verify backup status tomorrow."],
    "evidence": ["test-runtime-native-1"],
}


class RuntimeNativeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="pz-native-test-")
        self.root = Path(self.temp.name).resolve()
        self.vault = self.root / "vault"
        self.state = self.root / "runtime-state"
        self.vault.mkdir()
        self.state.mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def _make_config(self, provider_dict: dict | None = None, role: str = "workstation") -> MemoryConfig:
        runtimes = ["hermes"] if role == "memory-engine" else ["codex", "claude"]
        raw = {
            "role": role,
            "vault_path": str(self.vault),
            "state_path": str(self.state),
            "runtimes": runtimes,
            "transcript_roots": {r: [str(self.root)] for r in runtimes},
            "can_write_event_memory": True,
            "can_run_compiler": role == "memory-engine",
            "models": {"flush": "gpt-5.6-luna", "compiler": "gpt-5.6-terra"},
            "provider": provider_dict or {},
        }
        return MemoryConfig.from_dict(raw)

    # 1. runtime-native mode default
    def test_runtime_native_mode_default(self):
        cfg = self._make_config({})
        self.assertEqual("runtime-native", cfg.provider_mode)

    # 2. Claude command construction
    def test_claude_command_construction(self):
        captured_cmd = []
        captured_kwargs = {}

        def mock_runner(cmd, **kwargs):
            captured_cmd.extend(cmd)
            captured_kwargs.update(kwargs)
            res_obj = {
                "is_error": False,
                "result": json.dumps(SAMPLE_SUMMARY),
                "modelUsage": {"claude-haiku-4-5-20251001": {"tokens": 100}},
            }
            return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(res_obj), stderr="")

        schema = summary_json_schema()
        summary, model_used, provider_name = summarize_with_claude(
            instruction="Summarize session.",
            untrusted_input="User: hi\nAssistant: hello",
            schema=schema,
            runner=mock_runner,
        )
        self.assertEqual(SAMPLE_SUMMARY, summary)
        self.assertEqual("claude-haiku-4-5-20251001", model_used)
        self.assertEqual("claude-subscription", provider_name)
        self.assertEqual(["claude", "-p"], captured_cmd[:2])
        self.assertIn("--output-format", captured_cmd)
        self.assertIn("json", captured_cmd)
        self.assertIn("--no-session-persistence", captured_cmd)
        self.assertIn("--safe-mode", captured_cmd)
        self.assertIn("--tools", captured_cmd)
        self.assertEqual(subprocess.DEVNULL, captured_kwargs.get("stdin"))
        self.assertEqual("memory-v1", captured_kwargs.get("env", {}).get("PZ_MEMORY_INVOKED_BY"))

    # 3. Claude recursion guard
    def test_claude_recursion_guard(self):
        with mock.patch.dict(os.environ, {"PZ_MEMORY_INVOKED_BY": "memory-v1"}):
            with self.assertRaises(ProviderBlocked) as ctx:
                summarize_with_claude(
                    instruction="instr", untrusted_input="input", schema={},
                )
            self.assertIn("claude-recursion-detected", str(ctx.exception))

            ret = hook_runner.main(["--config", "/nonexistent", "--runtime", "claude", "--event", "SessionEnd"])
            self.assertEqual(0, ret)

            cli_ret = memory_cli.main(["--config", "/nonexistent", "flush", "--runtime", "claude"])
            self.assertEqual(0, cli_ret)

    # 4. Claude invalid output
    def test_claude_invalid_output(self):
        def bad_json_runner(cmd, **kwargs):
            return subprocess.CompletedProcess(cmd, 0, stdout="not-json", stderr="")

        with self.assertRaises(ProviderBlocked) as ctx:
            summarize_with_claude(instruction="i", untrusted_input="u", schema={}, runner=bad_json_runner)
        self.assertIn("claude-output-not-json", str(ctx.exception))

        def error_response_runner(cmd, **kwargs):
            return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps({"is_error": True}), stderr="")

        with self.assertRaises(ProviderBlocked) as ctx:
            summarize_with_claude(instruction="i", untrusted_input="u", schema={}, runner=error_response_runner)
        self.assertIn("claude-error-response", str(ctx.exception))

        def bad_inner_json_runner(cmd, **kwargs):
            return subprocess.CompletedProcess(
                cmd, 0, stdout=json.dumps({"is_error": False, "result": "corrupt"}), stderr=""
            )

        with self.assertRaises(SchemaError) as ctx:
            summarize_with_claude(instruction="i", untrusted_input="u", schema={}, runner=bad_inner_json_runner)
        self.assertIn("claude-result-not-json", str(ctx.exception))

    # 5. Claude timeout
    def test_claude_timeout(self):
        def timeout_runner(cmd, **kwargs):
            raise subprocess.TimeoutExpired(cmd, timeout=10)

        with self.assertRaises(ProviderBlocked) as ctx:
            summarize_with_claude(instruction="i", untrusted_input="u", schema={}, runner=timeout_runner)
        self.assertIn("claude-timeout", str(ctx.exception))

    # 6. Codex bundled discovery
    def test_codex_bundled_discovery(self):
        cfg = self._make_config()
        discovered = discover_codex_binary(cfg)
        if (
            Path("/Applications/ChatGPT.app/Contents/Resources/codex").is_file()
            and not shutil.which("codex")
        ):
            self.assertEqual("/Applications/ChatGPT.app/Contents/Resources/codex", discovered)

    # 7. Codex exec command construction
    def test_codex_exec_command_construction(self):
        captured_cmd = []
        captured_kwargs = {}

        def mock_runner(cmd, **kwargs):
            captured_cmd.extend(cmd)
            captured_kwargs.update(kwargs)
            jsonl_lines = [
                json.dumps({"type": "session.start"}),
                json.dumps({
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": json.dumps(SAMPLE_SUMMARY)},
                }),
                json.dumps({"type": "turn.completed"}),
            ]
            return subprocess.CompletedProcess(cmd, 0, stdout="\n".join(jsonl_lines), stderr="")

        cfg = self._make_config()
        schema = summary_json_schema()
        summary, model_used, provider_name = summarize_with_codex(
            config=cfg,
            instruction="Summarize session.",
            untrusted_input="User: hi\nAssistant: hello",
            schema=schema,
            runner=mock_runner,
        )
        self.assertEqual(SAMPLE_SUMMARY, summary)
        self.assertEqual("gpt-5.6-luna", model_used)
        self.assertEqual("chatgpt-subscription", provider_name)
        self.assertIn("exec", captured_cmd)
        self.assertIn("--ephemeral", captured_cmd)
        self.assertIn("-s", captured_cmd)
        self.assertIn("read-only", captured_cmd)
        self.assertIn("--skip-git-repo-check", captured_cmd)
        self.assertIn("--ignore-rules", captured_cmd)
        self.assertIn("--json", captured_cmd)
        self.assertIn("--output-schema", captured_cmd)
        self.assertEqual(subprocess.DEVNULL, captured_kwargs.get("stdin"))
        self.assertEqual("memory-v1", captured_kwargs.get("env", {}).get("PZ_MEMORY_INVOKED_BY"))

    # 8. Codex explicit stdin close/control
    def test_codex_explicit_stdin_close(self):
        captured_kwargs = {}

        def mock_runner(cmd, **kwargs):
            captured_kwargs.update(kwargs)
            jsonl = json.dumps({
                "type": "item.completed",
                "item": {"type": "agent_message", "text": json.dumps(SAMPLE_SUMMARY)},
            })
            return subprocess.CompletedProcess(cmd, 0, stdout=jsonl, stderr="")

        summarize_with_codex(
            config=self._make_config(), instruction="i", untrusted_input="u", schema={},
            runner=mock_runner,
        )
        self.assertEqual(subprocess.DEVNULL, captured_kwargs.get("stdin"))

    # 9. Codex timeout/hang fail-closed
    def test_codex_timeout_fail_closed(self):
        def timeout_runner(cmd, **kwargs):
            raise subprocess.TimeoutExpired(cmd, timeout=10)

        with self.assertRaises(ProviderBlocked) as ctx:
            summarize_with_codex(
                config=self._make_config(), instruction="i", untrusted_input="u", schema={},
                runner=timeout_runner,
            )
        self.assertIn("codex-timeout", str(ctx.exception))

    # 10. Codex recursion guard
    def test_codex_recursion_guard(self):
        with mock.patch.dict(os.environ, {"PZ_MEMORY_INVOKED_BY": "memory-v1"}):
            with self.assertRaises(ProviderBlocked) as ctx:
                summarize_with_codex(
                    config=self._make_config(), instruction="i", untrusted_input="u", schema={},
                )
            self.assertIn("codex-recursion-detected", str(ctx.exception))

    # 11. Codex invalid output
    def test_codex_invalid_output(self):
        def no_message_runner(cmd, **kwargs):
            return subprocess.CompletedProcess(
                cmd, 0, stdout=json.dumps({"type": "turn.failed", "error": "rate limit"}), stderr=""
            )

        with self.assertRaises(ProviderBlocked) as ctx:
            summarize_with_codex(
                config=self._make_config(), instruction="i", untrusted_input="u", schema={},
                runner=no_message_runner,
            )
        self.assertIn("codex-turn-failed", str(ctx.exception))

    # 12. Runtime outputs normalize to same schema
    def test_runtime_outputs_normalize_to_same_schema(self):
        claude_val = validate_summary(SAMPLE_SUMMARY)
        codex_val = validate_summary(SAMPLE_SUMMARY)
        self.assertEqual(claude_val, codex_val)
        self.assertEqual("memory", claude_val["status"])
        for field in ("context", "important_conversations", "decisions", "learnings", "open_items", "evidence"):
            self.assertIsInstance(claude_val[field], list)

    # 13. External API mode remains optional only
    def test_external_api_mode_remains_optional(self):
        cfg = self._make_config({
            "mode": "external-openai-api",
            "api_base": "https://api.openai.com/v1",
            "key_env": "TEST_EXTERNAL_KEY",
        })
        self.assertEqual("external-openai-api", cfg.provider_mode)
        self.assertEqual("TEST_EXTERNAL_KEY", cfg.provider_key_env)

    # 14. Missing API key in runtime-native mode is NOT blocker
    def test_missing_api_key_in_runtime_native_mode_not_blocker(self):
        cfg = self._make_config()
        with mock.patch.dict(os.environ, {}, clear=True):
            result = run_doctor(cfg)
        rows = {r["check"]: r for r in result["checks"]}
        self.assertEqual("pass", rows["memory_provider"]["status"])
        self.assertEqual("runtime-native", rows["memory_provider"]["detail"])

    # 15. No silent runtime-native -> API fallback
    def test_no_silent_fallback(self):
        def failing_claude_runner(cmd, **kwargs):
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="usage limit")

        provider = RuntimeNativeProvider(
            self._make_config(), claude_runner=failing_claude_runner
        )
        with self.assertRaises(ProviderBlocked) as ctx:
            provider.request(
                runtime="claude",
                model="haiku",
                instruction="instr",
                untrusted_input="input",
                schema_name="schema",
                schema={},
            )
        self.assertIn("claude-process-failed:1", str(ctx.exception))

    # 16. Doctor mode-aware checks
    def test_doctor_mode_aware_checks(self):
        native_cfg = self._make_config({"mode": "runtime-native"})
        with mock.patch.dict(os.environ, {}, clear=True):
            native_res = run_doctor(native_cfg)
        native_rows = {r["check"]: r for r in native_res["checks"]}
        self.assertEqual("pass", native_rows["memory_provider"]["status"])

        external_cfg = self._make_config({
            "mode": "external-openai-api",
            "key_env": "DEFINITELY_UNSET_KEY_FOR_TEST",
        })
        with mock.patch.dict(os.environ, {}, clear=True):
            external_res = run_doctor(external_cfg)
        external_rows = {r["check"]: r for r in external_res["checks"]}
        self.assertEqual("blocked", external_rows["memory_provider"]["status"])

    # 17. Secrets never exposed
    def test_secrets_never_exposed(self):
        fake_secret = "sk-live-should-never-leak-xyz123"
        def error_runner(cmd, **kwargs):
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr=f"error with {fake_secret}")

        with self.assertRaises(ProviderBlocked) as ctx:
            summarize_with_claude(
                instruction="instr",
                untrusted_input=f"User: token is {fake_secret}",
                schema={},
                runner=error_runner,
            )
        self.assertNotIn(fake_secret, str(ctx.exception))

    # 18. End-to-end event flush with native provider
    def test_e2e_event_flush_with_native_provider(self):
        def mock_claude_runner(cmd, **kw):
            obj = {
                "is_error": False,
                "result": json.dumps(SAMPLE_SUMMARY),
                "modelUsage": {"claude-haiku-4-5-20251001": {"tokens": 100}},
            }
            return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(obj), stderr="")

        cfg = self._make_config()
        provider = RuntimeNativeProvider(cfg, claude_runner=mock_claude_runner)
        writer = EventWriter(cfg, provider)
        transcript_file = self.root / "session.jsonl"
        transcript_file.write_text(json.dumps({"role": "user", "content": "Deploy slice-a."}) + "\n")

        path = writer.flush(
            runtime="claude",
            agent_id="claude-agent",
            session_id="session-e2e-123",
            event="session_end",
            transcript=transcript_file,
        )
        self.assertTrue(path.is_file())
        content = path.read_text(encoding="utf-8")
        parsed = parse_event_artifact(content)
        self.assertEqual("claude", parsed["runtime"])
        self.assertEqual("claude-haiku-4-5-20251001", parsed["source_model"])
        self.assertEqual("claude-subscription", parsed.get("source_provider"))
        self.assertEqual(["Deploy slice-a today."], parsed["sections"]["decisions"])

    # 19. Drain unlinks pending checkpoint on NoMemory (empty summary)
    def test_drain_checkpoint_unlinks_on_no_memory(self):
        def empty_summary_runner(cmd, **kw):
            obj = {
                "is_error": False,
                "result": json.dumps({"status": "empty", "context": [], "important_conversations": [], "decisions": [], "learnings": [], "open_items": [], "evidence": []}),
            }
            return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(obj), stderr="")

        cfg = self._make_config()
        provider = RuntimeNativeProvider(cfg, claude_runner=empty_summary_runner)
        transcript_file = self.root / "empty-session.jsonl"
        transcript_file.write_text(json.dumps({"role": "user", "content": "Just saying hello."}) + "\n")

        qpath = checkpoint_hook(
            cfg, runtime="claude",
            payload={"session_id": "sess-empty-1", "transcript_path": str(transcript_file), "event": "session_end"}
        )
        self.assertTrue(qpath.is_file())
        with self.assertRaises(NoMemory):
            drain_checkpoint(cfg, qpath, provider=provider)
        # Checkpoint MUST be unlinked so it doesn't leak in pending queue
        self.assertFalse(qpath.is_file())

    # 20. Source model falls back cleanly when payload passes "unknown"
    def test_drain_checkpoint_model_provenance_fallback(self):
        def mock_claude_runner(cmd, **kw):
            obj = {
                "is_error": False,
                "result": json.dumps(SAMPLE_SUMMARY),
                "modelUsage": {"claude-haiku-4-5-20251001": {"tokens": 50}},
            }
            return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(obj), stderr="")

        cfg = self._make_config()
        provider = RuntimeNativeProvider(cfg, claude_runner=mock_claude_runner)
        transcript_file = self.root / "fallback-session.jsonl"
        transcript_file.write_text(json.dumps({"role": "user", "content": "Architecture decision."}) + "\n")

        qpath = checkpoint_hook(
            cfg, runtime="claude",
            payload={"session_id": "sess-fallback-1", "transcript_path": str(transcript_file), "event": "session_end"}
        )
        event_path = drain_checkpoint(cfg, qpath, provider=provider)
        parsed = parse_event_artifact(event_path.read_text(encoding="utf-8"))
        self.assertEqual("claude-haiku-4-5-20251001", parsed["source_model"])
        self.assertEqual("claude-subscription", parsed["source_provider"])

    # 21. Deduplication: second drain of exact same session is idempotent
    def test_deduplication_and_idempotent_drain(self):
        call_count = 0
        def counting_runner(cmd, **kw):
            nonlocal call_count
            call_count += 1
            obj = {
                "is_error": False,
                "result": json.dumps(SAMPLE_SUMMARY),
                "modelUsage": {"claude-haiku-4-5-20251001": {"tokens": 50}},
            }
            return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(obj), stderr="")

        cfg = self._make_config()
        provider = RuntimeNativeProvider(cfg, claude_runner=counting_runner)
        transcript_file = self.root / "dedup-session.jsonl"
        transcript_file.write_text(json.dumps({"role": "user", "content": "Identical work."}) + "\n")

        qpath1 = checkpoint_hook(
            cfg, runtime="claude",
            payload={"session_id": "sess-dedup-1", "transcript_path": str(transcript_file), "event": "session_end"}
        )
        event_path1 = drain_checkpoint(cfg, qpath1, provider=provider)
        self.assertEqual(1, call_count)

        # Re-trigger hook with exact same session and transcript
        qpath2 = checkpoint_hook(
            cfg, runtime="claude",
            payload={"session_id": "sess-dedup-1", "transcript_path": str(transcript_file), "event": "session_end"}
        )
        event_path2 = drain_checkpoint(cfg, qpath2, provider=provider)
        # Second drain must return the exact same event path without re-calling LLM
        self.assertEqual(event_path1, event_path2)
        self.assertEqual(1, call_count)
        self.assertFalse(qpath2.is_file())

    # 22. Transcript normalization handles dict with messages array
    def test_hermes_messages_dict_transcript_normalized(self):
        sample = {
            "id": "20260827_231953_8cefef",
            "source": "cli",
            "messages": [
                {"id": 1, "role": "user", "content": "Hello Hermes agent."},
                {"id": 2, "role": "tool", "content": "tool result"},
                {"id": 3, "role": "assistant", "content": "Hello! I am ready."},
            ]
        }
        f = self.root / "hermes-session.jsonl"
        f.write_text(json.dumps(sample) + "\n")
        rendered, turns, digest = normalize_transcript(f, allowed_roots=(self.root,))
        self.assertEqual(2, turns)
        self.assertIn("USER: Hello Hermes agent.", rendered)
        self.assertIn("ASSISTANT: Hello! I am ready.", rendered)
        self.assertNotIn("tool result", rendered)

    # 23. MemoryConfig allows verified canonical Hermes models
    def test_hermes_models_allowed_in_config(self):
        raw = {
            "role": "memory-engine",
            "vault_path": str(self.vault),
            "state_path": str(self.state),
            "runtimes": ["hermes"],
            "transcript_roots": {"hermes": [str(self.root)]},
            "models": {
                "flush": "gpt-5.4-mini-2026-03-17",
                "compiler": "gpt-5.6-terra"
            },
            "provider": {"mode": "runtime-native"}
        }
        cfg = MemoryConfig.from_dict(raw)
        self.assertEqual("gpt-5.4-mini-2026-03-17", cfg.flush_model)
        self.assertEqual("gpt-5.6-terra", cfg.compiler_model)

    # 24. RuntimeNativeProvider defaults to Hermes on memory-engine role
    def test_runtime_native_provider_hermes_default_for_memory_engine(self):
        called = False
        def mock_hermes_runner(*, instruction, untrusted_input, schema, **kw):
            nonlocal called
            called = True
            return {"status": "changes", "writes": []}, "gpt-5.6-terra", "pz-openai-serial"

        raw = {
            "role": "memory-engine",
            "vault_path": str(self.vault),
            "state_path": str(self.state),
            "runtimes": ["hermes"],
            "transcript_roots": {"hermes": [str(self.root)]},
            "can_run_compiler": True,
            "provider": {"mode": "runtime-native"}
        }
        cfg = MemoryConfig.from_dict(raw)
        provider = RuntimeNativeProvider(cfg, hermes_runner=mock_hermes_runner)
        res = provider.request(
            model="gpt-5.6-terra",
            instruction="test",
            untrusted_input="input",
            schema_name="test_schema",
            schema={"type": "object"}
        )
        self.assertTrue(called)
        self.assertEqual("changes", res["status"])
        self.assertEqual("gpt-5.6-terra", provider.last_source_model)
        self.assertEqual("pz-openai-serial", provider.last_source_provider)

    # 25. Doctor rejects manual/incomplete activation evidence lacking causal provenance
    def test_doctor_rejects_manual_activation_evidence(self):
        cfg = self._make_config(role="memory-engine")
        from memory_v1.doctor import _activation_evidence_valid
        evidence_dir = self.state / "evidence"
        evidence_dir.mkdir(parents=True, exist_ok=True)
        evidence_file = evidence_dir / "hermes-lifecycle-smoke.json"

        # 1. Manual evidence lacking checkpoint_id, provenance, source_provider
        manual_evidence = {
            "schema": "pikselzone-memory-activation-evidence-v1",
            "runtime": "hermes",
            "status": "pass",
            "runtime_version": "0.19.0",
            "hook_config_sha256": "0" * 64,
            "smoke_session_key": "a" * 32,
            "checkpoint_mode": "0600",
            "event_path": str(self.vault / "daily" / "2026-08-28" / "hermes-fake.md"),
            "event_sha256": "b" * 64,
            "duplicate_files": 0,
            "observed_at": "2026-08-28T02:21:00+03:00",
        }
        evidence_file.write_text(json.dumps(manual_evidence))
        self.assertFalse(_activation_evidence_valid(cfg, "hermes", evidence_file, None))

        # 2. Evidence with provenance="manual" explicitly rejected
        manual_evidence.update({
            "checkpoint_id": "queue-123.json",
            "provenance": "manual",
            "source_provider": "pz-openai-serial",
        })
        evidence_file.write_text(json.dumps(manual_evidence))
        self.assertFalse(_activation_evidence_valid(cfg, "hermes", evidence_file, None))

        # 3. Valid causal automatic evidence with matching event artifact
        event_dir = self.vault / "daily" / "2026-08-28"
        event_dir.mkdir(parents=True, exist_ok=True)
        event_file = event_dir / "hermes-test-event.md"
        from memory_v1.events import EventWriter
        event_content = EventWriter._render(
            runtime="hermes",
            agent_id="hermes-main",
            session_id="sess-test-hermes",
            event="session_end",
            events_seen=["pre_compact", "session_end"],
            created_at="2026-08-28T02:21:00+03:00",
            source_model="gpt-5.4-mini-2026-03-17",
            source_provider="pz-openai-serial",
            root_task_id="t-123",
            kanban_ids=[],
            source_digest="0" * 64,
            summary={
                "context": ["Test bağlamı."],
                "important_conversations": ["Test konuşması."],
                "decisions": ["Test kararı."],
                "learnings": ["Test öğrenimi."],
                "open_items": ["Test açık madde."],
                "evidence": ["test-ev-1"],
            },
            redaction_count=0,
        )
        event_file.write_text(event_content, encoding="utf-8")
        import hashlib
        from memory_v1.core import session_key
        event_sha = hashlib.sha256(event_file.read_bytes()).hexdigest()

        valid_evidence = {
            "schema": "pikselzone-memory-activation-evidence-v1",
            "runtime": "hermes",
            "status": "pass",
            "runtime_version": "0.19.0",
            "hook_config_sha256": "0" * 64,
            "smoke_session_key": session_key("sess-test-hermes"),
            "checkpoint_id": "queue-123.json",
            "provenance": "automatic-lifecycle-drain",
            "source_provider": "pz-openai-serial",
            "checkpoint_mode": "0600",
            "event_path": str(event_file),
            "event_sha256": event_sha,
            "duplicate_files": 0,
            "observed_at": "2026-08-28T02:21:00+03:00",
        }
        evidence_file.write_text(json.dumps(valid_evidence))
        self.assertTrue(_activation_evidence_valid(cfg, "hermes", evidence_file, None))

        # 4. Mismatched source_provider rejected
        valid_evidence["source_provider"] = "different-provider"
        evidence_file.write_text(json.dumps(valid_evidence))
        self.assertFalse(_activation_evidence_valid(cfg, "hermes", evidence_file, None))

    # 32. Detached worker invocation construction
    def test_detached_worker_invocation_construction(self):
        from memory_v1.hook_runner import build_drain_command

        cfg_path = self.root / "config.json"
        q_path = self.root / "checkpoint.json"
        cfg_path.touch()
        q_path.touch()

        cmd = build_drain_command(cfg_path, q_path, python_bin="/opt/custom/python3")
        self.assertEqual("/opt/custom/python3", cmd[0])
        self.assertEqual(["-m", "memory_v1.cli"], cmd[1:3])
        self.assertIn("--config", cmd)
        self.assertEqual(str(cfg_path.resolve()), cmd[cmd.index("--config") + 1])
        self.assertIn("drain", cmd)
        self.assertIn("--queue", cmd)
        self.assertEqual(str(q_path.resolve()), cmd[cmd.index("--queue") + 1])

    def test_codex_stop_hook_checkpoints_without_spawning_provider_worker(self):
        cfg = self._make_config()
        transcript_file = self.root / "stop-hook.jsonl"
        transcript_file.write_text(
            json.dumps({"role": "user", "content": "Keep this."}) + "\n"
            + json.dumps({"role": "assistant", "content": "Checkpointed."}) + "\n",
            encoding="utf-8",
        )
        payload = json.dumps({
            "session_id": "sess-stop-hook-1", "turn_id": "turn-001",
            "transcript_path": str(transcript_file),
        })
        with mock.patch.object(hook_runner.MemoryConfig, "load", return_value=cfg), \
             mock.patch.object(hook_runner, "_spawn_drain") as spawn, \
             mock.patch("sys.stdin", io.StringIO(payload)):
            rc = hook_runner.main([
                "--config", str(self.root / "config.json"),
                "--runtime", "codex", "--event", "Stop",
            ])
        self.assertEqual(0, rc)
        spawn.assert_not_called()
        pending = list((self.state / "queue" / "pending").glob("*.json"))
        self.assertEqual(1, len(pending))

    # 33. Automatic worker receipt and evidence generation
    def test_automatic_worker_receipt_and_evidence_generation(self):
        from memory_v1.doctor import _activation_evidence_valid

        mock_codex_out = json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": json.dumps(SAMPLE_SUMMARY)}}) + "\n"
        def mock_codex_runner(cmd, **kw):
            return subprocess.CompletedProcess(cmd, 0, stdout=mock_codex_out, stderr="")

        evidence_path = self.state / "evidence" / "codex-smoke.json"
        hooks_path = self.root / "hooks.json"
        hooks_path.write_text(json.dumps({"hooks": {}}))
        raw_cfg = {
            "role": "workstation",
            "vault_path": str(self.vault),
            "state_path": str(self.state),
            "runtimes": ["codex", "claude"],
            "transcript_roots": {"codex": [str(self.root)], "claude": [str(self.root)]},
            "can_write_event_memory": True,
            "can_run_compiler": False,
            "models": {"flush": "gpt-5.6-luna", "compiler": "gpt-5.6-terra"},
            "provider": {},
            "activation": {
                "codex_hooks_path": str(hooks_path),
                "codex_smoke_evidence_path": str(evidence_path),
            },
        }
        cfg = MemoryConfig.from_dict(raw_cfg)
        provider = RuntimeNativeProvider(cfg, codex_runner=mock_codex_runner)

        transcript_file = self.root / "codex-session.jsonl"
        transcript_file.write_text(json.dumps({"role": "user", "content": "Automatic drain test."}) + "\n")

        qpath = checkpoint_hook(
            cfg, runtime="codex",
            payload={"session_id": "sess-auto-1", "transcript_path": str(transcript_file), "event": "session_end"}
        )
        self.assertTrue(qpath.is_file())

        event_path = drain_checkpoint(cfg, qpath, provider=provider)
        self.assertTrue(event_path.is_file())
        self.assertFalse(qpath.is_file())

        # Evidence must be created automatically by the worker
        self.assertTrue(evidence_path.is_file())
        evidence_data = json.loads(evidence_path.read_text(encoding="utf-8"))
        self.assertIn("worker_receipt", evidence_data)
        receipt = evidence_data["worker_receipt"]
        self.assertEqual("codex", receipt["runtime"])
        self.assertEqual("chatgpt-subscription", receipt["source_provider"])
        self.assertEqual(str(event_path), receipt["event_path"])
        self.assertEqual(evidence_data["event_sha256"], receipt["event_sha256"])

        # Doctor activation evidence check must PASS
        self.assertTrue(_activation_evidence_valid(cfg, "codex", evidence_path, hooks_path))

    # 34. Fabricated smoke evidence rejected by doctor
    def test_fabricated_smoke_evidence_rejected(self):
        from memory_v1.doctor import _activation_evidence_valid

        evidence_path = self.state / "evidence" / "codex-smoke.json"
        hooks_path = self.root / "hooks.json"
        hooks_path.write_text(json.dumps({"hooks": {}}))
        hooks_sha = hashlib.sha256(hooks_path.read_bytes()).hexdigest()
        raw_cfg = {
            "role": "workstation",
            "vault_path": str(self.vault),
            "state_path": str(self.state),
            "runtimes": ["codex", "claude"],
            "transcript_roots": {"codex": [str(self.root)], "claude": [str(self.root)]},
            "can_write_event_memory": True,
            "can_run_compiler": False,
            "models": {"flush": "gpt-5.6-luna", "compiler": "gpt-5.6-terra"},
            "provider": {},
            "activation": {
                "codex_hooks_path": str(hooks_path),
                "codex_smoke_evidence_path": str(evidence_path),
            },
        }
        cfg = MemoryConfig.from_dict(raw_cfg)

        # Create a valid daily event
        daily_dir = self.vault / "daily" / "2026-08-29"
        daily_dir.mkdir(parents=True, exist_ok=True)
        event_file = daily_dir / "codex-test-sess.md"
        event_file.write_text(
            "---\nschema: \"pikselzone-memory-event-v1\"\nruntime: \"codex\"\nagent_id: \"codex-main\"\n"
            "session_id: \"sess-fake-test\"\nevent: \"session_end\"\nevents_seen: [\"session_end\"]\n"
            "created_at: \"2026-08-29T12:00:00+03:00\"\nsource_runtime: \"codex\"\nsource_model: \"gpt-5.6-luna\"\n"
            "source_provider: \"chatgpt-subscription\"\nroot_task_id: \"none\"\nkanban_ids: []\n"
            "source_sha256: \"" + "a" * 64 + "\"\nsecret_redactions: 0\ngenerated_by: \"pikselzone-memory-v1\"\n"
            "authority: \"derived-session-memory-not-operational-truth\"\n---\n\n## Bağlam\n- Test\n"
        )
        event_sha = hashlib.sha256(event_file.read_bytes()).hexdigest()

        # 1. Missing worker_receipt rejected
        fake_evidence = {
            "schema": "pikselzone-memory-activation-evidence-v1",
            "runtime": "codex",
            "status": "pass",
            "runtime_version": "codex-cli 0.150.0",
            "hook_config_sha256": hooks_sha,
            "smoke_session_key": session_key("sess-fake-test"),
            "checkpoint_mode": "0600",
            "event_path": str(event_file),
            "event_sha256": event_sha,
            "duplicate_files": 0,
            "observed_at": "2026-08-29T12:01:00+03:00",
            "checkpoint_id": "codex-fake.json",
            "provenance": "automatic-hook-drain",
            "source_provider": "chatgpt-subscription",
        }
        evidence_path.parent.mkdir(parents=True, exist_ok=True)
        evidence_path.write_text(json.dumps(fake_evidence))
        self.assertFalse(_activation_evidence_valid(cfg, "codex", evidence_path, hooks_path))

        # 2. Receipt with mismatched event_sha256 rejected
        fake_evidence["worker_receipt"] = {
            "runtime": "codex",
            "session_key": session_key("sess-fake-test"),
            "checkpoint_id": "codex-fake.json",
            "checkpoint_sha256": "f" * 64,
            "hook_observed_at": "2026-08-29T12:00:00+03:00",
            "worker_started_at": "2026-08-29T12:00:01+03:00",
            "worker_completed_at": "2026-08-29T12:00:05+03:00",
            "event_path": str(event_file),
            "event_sha256": "e" * 64,  # mismatched
            "source_provider": "chatgpt-subscription",
            "source_model": "gpt-5.6-luna",
            "worker_pid": 12345,
        }
        evidence_path.write_text(json.dumps(fake_evidence))
        self.assertFalse(_activation_evidence_valid(cfg, "codex", evidence_path, hooks_path))

        # 3. Receipt with out-of-order timestamps rejected
        fake_evidence["worker_receipt"]["event_sha256"] = event_sha
        fake_evidence["worker_receipt"]["worker_started_at"] = "2026-08-29T12:00:10+03:00"
        fake_evidence["worker_receipt"]["worker_completed_at"] = "2026-08-29T12:00:05+03:00"  # before start
        evidence_path.write_text(json.dumps(fake_evidence))
        self.assertFalse(_activation_evidence_valid(cfg, "codex", evidence_path, hooks_path))

    # 35. Provider failure leaves checkpoint retryable
    def test_provider_failure_leaves_checkpoint_retryable(self):
        def failing_runner(cmd, **kw):
            raise subprocess.TimeoutExpired(cmd, timeout=5)

        cfg = self._make_config()
        provider = RuntimeNativeProvider(cfg, codex_runner=failing_runner)
        transcript_file = self.root / "timeout-session.jsonl"
        transcript_file.write_text(json.dumps({"role": "user", "content": "Timeout task."}) + "\n")

        qpath = checkpoint_hook(
            cfg, runtime="codex",
            payload={"session_id": "sess-timeout-1", "transcript_path": str(transcript_file), "event": "session_end"}
        )
        self.assertTrue(qpath.is_file())
        with self.assertRaises(ProviderBlocked):
            drain_checkpoint(cfg, qpath, provider=provider)
        # Checkpoint MUST remain for retry
        self.assertTrue(qpath.is_file())

    # 36. Successful worker removes checkpoint
    def test_successful_worker_removes_checkpoint(self):
        mock_out = json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": json.dumps(SAMPLE_SUMMARY)}}) + "\n"
        def mock_runner(cmd, **kw):
            return subprocess.CompletedProcess(cmd, 0, stdout=mock_out, stderr="")

        cfg = self._make_config()
        provider = RuntimeNativeProvider(cfg, codex_runner=mock_runner)
        transcript_file = self.root / "success-session.jsonl"
        transcript_file.write_text(json.dumps({"role": "user", "content": "Success task."}) + "\n")

        qpath = checkpoint_hook(
            cfg, runtime="codex",
            payload={"session_id": "sess-success-1", "transcript_path": str(transcript_file), "event": "session_end"}
        )
        self.assertTrue(qpath.is_file())
        event_path = drain_checkpoint(cfg, qpath, provider=provider)
        self.assertTrue(event_path.is_file())
        # Checkpoint must be unlinked
        self.assertFalse(qpath.is_file())

    # 37. Duplicate session does not call model again
    def test_codex_duplicate_session_does_not_call_model_again(self):
        call_count = 0
        mock_out = json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": json.dumps(SAMPLE_SUMMARY)}}) + "\n"
        def counting_runner(cmd, **kw):
            nonlocal call_count
            call_count += 1
            return subprocess.CompletedProcess(cmd, 0, stdout=mock_out, stderr="")

        cfg = self._make_config()
        provider = RuntimeNativeProvider(cfg, codex_runner=counting_runner)
        transcript_file = self.root / "dup-session.jsonl"
        transcript_file.write_text(json.dumps({"role": "user", "content": "Dedup check."}) + "\n")

        qpath1 = checkpoint_hook(
            cfg, runtime="codex",
            payload={"session_id": "sess-dup-1", "transcript_path": str(transcript_file), "event": "session_end"}
        )
        event_path1 = drain_checkpoint(cfg, qpath1, provider=provider)
        self.assertEqual(1, call_count)

        # Duplicate checkpoint with identical content
        qpath2 = checkpoint_hook(
            cfg, runtime="codex",
            payload={"session_id": "sess-dup-1", "transcript_path": str(transcript_file), "event": "session_end"}
        )
        event_path2 = drain_checkpoint(cfg, qpath2, provider=provider)
        self.assertEqual(1, call_count)
        self.assertEqual(event_path1, event_path2)
        self.assertFalse(qpath2.is_file())

    def test_claude_hook_registration_rejects_empty_matcher(self):
        from memory_v1.doctor import _hook_registration_row
        settings_file = self.root / "claude-settings-test.json"

        # 1. Valid hook with no matcher
        valid_hooks = {
            "hooks": {
                "SessionStart": [{"hooks": [{"type": "command", "command": "python3 -m memory_v1.hook_runner --runtime claude --event SessionStart"}]}],
                "PreCompact": [{"hooks": [{"type": "command", "command": "python3 -m memory_v1.hook_runner --runtime claude --event PreCompact"}]}],
                "SessionEnd": [{"hooks": [{"type": "command", "command": "python3 -m memory_v1.hook_runner --runtime claude --event SessionEnd"}]}],
            }
        }
        settings_file.write_text(json.dumps(valid_hooks), encoding="utf-8")
        row = _hook_registration_row("claude_hook_registration", settings_file, "claude")
        self.assertEqual("pass", row["status"])

        # 2. Hook with invalid empty matcher
        invalid_hooks = {
            "hooks": {
                "SessionStart": [{
                    "matcher": "",
                    "hooks": [{"type": "command", "command": "python3 -m memory_v1.hook_runner --runtime claude --event SessionStart"}]
                }],
                "PreCompact": [{"hooks": [{"type": "command", "command": "python3 -m memory_v1.hook_runner --runtime claude --event PreCompact"}]}],
                "SessionEnd": [{"hooks": [{"type": "command", "command": "python3 -m memory_v1.hook_runner --runtime claude --event SessionEnd"}]}],
            }
        }
        settings_file.write_text(json.dumps(invalid_hooks), encoding="utf-8")
        row_invalid = _hook_registration_row("claude_hook_registration", settings_file, "claude")
        self.assertEqual("fail", row_invalid["status"])
        self.assertIn("invalid-empty-matcher", row_invalid["detail"])


if __name__ == "__main__":
    unittest.main()

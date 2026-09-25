"""``health/hook-<runtime>.json`` must describe the last lifecycle hook.

The component only ever moved to ``blocked``.  Nothing wrote it back to ``ok``,
so a failure that was fixed days earlier kept showing red to an operator
reading the file directly -- while doctor, drain and flush all reported a
healthy system.  These tests pin the recovery semantics and, just as
importantly, pin what must *not* be painted green.
"""
from __future__ import annotations

import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from memory_v1.core import MemoryConfig, ProviderBlocked, session_key
from memory_v1.provider import RuntimeNativeProvider
import memory_v1.cli as memory_cli
import memory_v1.hook_runner as hook_runner


SAMPLE_SUMMARY = {
    "status": "memory",
    "context": ["Hook health regression session."],
    "important_conversations": ["Operator watched health/hook-claude.json."],
    "decisions": ["Write ok after a successful capture."],
    "learnings": ["A health component that only records failure never recovers."],
    "open_items": ["None."],
    "evidence": ["test-hook-health-1"],
}


def _codex_ok(cmd, **kwargs):
    out = json.dumps({
        "type": "item.completed",
        "item": {"type": "agent_message", "text": json.dumps(SAMPLE_SUMMARY)},
    }) + "\n"
    return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr="")


def _codex_usage_limit(cmd, **kwargs):
    out = json.dumps({"type": "error", "message": "You've hit your usage limit."}) + "\n"
    return subprocess.CompletedProcess(cmd, 1, stdout=out, stderr="")


class HookHealthTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="pz-hookhealth-test-")
        self.root = Path(self.temp.name).resolve()
        self.vault = self.root / "vault"
        self.state = self.root / "state"
        self.transcripts = self.root / "transcripts"
        self.repo = self.root / "repo"
        for path in (self.vault, self.state, self.transcripts, self.repo):
            path.mkdir()
        self.raw_config = {
            "role": "workstation",
            "vault_path": str(self.vault),
            "state_path": str(self.state),
            "runtimes": ["codex", "claude"],
            "transcript_roots": {
                "codex": [str(self.transcripts)], "claude": [str(self.transcripts)],
            },
            "can_write_event_memory": True,
            "can_run_compiler": False,
            "models": {"flush": "gpt-5.6-luna", "compiler": "gpt-5.6-terra"},
            "provider": {},
        }
        self.config_path = self.root / "config.json"
        self.config_path.write_text(json.dumps(self.raw_config), encoding="utf-8")
        self.config = MemoryConfig.from_dict(self.raw_config)
        from memory_v1 import project_registry
        project_registry.register(self.state, self.repo, "demo")

    def tearDown(self) -> None:
        self.temp.cleanup()

    # --- helpers --------------------------------------------------------

    def _transcript(self, name: str) -> Path:
        path = self.transcripts / name
        path.write_text(
            json.dumps({"role": "user", "content": "Do the thing."}) + "\n"
            + json.dumps({"role": "assistant", "content": "Done."}) + "\n",
            encoding="utf-8",
        )
        return path

    def _seed_blocked(self, runtime: str, detail: str) -> None:
        from memory_v1.core import write_health
        write_health(self.state, f"hook-{runtime}", "blocked", detail)
        self.assertEqual("blocked", self._health(f"hook-{runtime}")["status"])

    def _health(self, component: str) -> dict:
        return json.loads(
            (self.state / "health" / f"{component}.json").read_text(encoding="utf-8")
        )

    def _health_exists(self, component: str) -> bool:
        return (self.state / "health" / f"{component}.json").is_file()

    def _run_hook(self, *, runtime: str, event: str, payload: dict) -> int:
        with mock.patch.object(hook_runner.MemoryConfig, "load", return_value=self.config), \
             mock.patch.object(hook_runner, "_spawn_drain"), \
             mock.patch("sys.stdin", io.StringIO(json.dumps(payload))):
            return hook_runner.main([
                "--config", str(self.config_path),
                "--runtime", runtime, "--event", event,
                "--project", "demo", "--project-root", str(self.repo),
            ])

    # --- the defect -----------------------------------------------------

    def test_successful_claude_stop_clears_stale_blocked(self):
        self._seed_blocked("claude", "secure-read-open:FileNotFoundError")
        transcript = self._transcript("claude-stop.jsonl")

        rc = self._run_hook(runtime="claude", event="Stop", payload={
            "session_id": "sess-claude-heal", "transcript_path": str(transcript),
            "cwd": str(self.repo), "hook_event_name": "Stop",
        })

        self.assertEqual(0, rc)
        health = self._health("hook-claude")
        self.assertEqual("ok", health["status"])
        self.assertEqual("lifecycle-ok:Stop", health["detail"])

    def test_successful_codex_session_end_clears_stale_blocked(self):
        self._seed_blocked("codex", "checkpoint-transcript-empty")
        transcript = self._transcript("codex-end.jsonl")

        rc = self._run_hook(runtime="codex", event="SessionEnd", payload={
            "session_id": "sess-codex-heal", "transcript_path": str(transcript),
            "cwd": str(self.repo), "hook_event_name": "SessionEnd",
        })

        self.assertEqual(0, rc)
        health = self._health("hook-codex")
        self.assertEqual("ok", health["status"])
        self.assertEqual("lifecycle-ok:SessionEnd", health["detail"])

    def test_precompact_records_its_own_event_name(self):
        transcript = self._transcript("claude-precompact.jsonl")
        rc = self._run_hook(runtime="claude", event="PreCompact", payload={
            "session_id": "sess-claude-pc", "transcript_path": str(transcript),
            "cwd": str(self.repo), "hook_event_name": "PreCompact",
        })
        self.assertEqual(0, rc)
        self.assertEqual("lifecycle-ok:PreCompact", self._health("hook-claude")["detail"])

    # --- what must NOT be painted green ---------------------------------

    def test_failed_lifecycle_still_blocks(self):
        outside = self.root / "elsewhere" / "leak.jsonl"
        outside.parent.mkdir()
        outside.write_text("[]", encoding="utf-8")

        rc = self._run_hook(runtime="claude", event="SessionEnd", payload={
            "session_id": "sess-claude-fail", "transcript_path": str(outside),
            "cwd": str(self.repo), "hook_event_name": "SessionEnd",
        })

        self.assertEqual(0, rc, "a capture failure never steers the runtime (exit-code contract)")
        health = self._health("hook-claude")
        self.assertEqual("blocked", health["status"])
        self.assertIn("transcript-path-outside-allowed-roots", health["detail"])

    def test_failed_lifecycle_after_success_returns_to_blocked(self):
        transcript = self._transcript("claude-ok-then-fail.jsonl")
        self._run_hook(runtime="claude", event="Stop", payload={
            "session_id": "sess-claude-seq", "transcript_path": str(transcript),
            "cwd": str(self.repo), "hook_event_name": "Stop",
        })
        self.assertEqual("ok", self._health("hook-claude")["status"])

        rc = self._run_hook(runtime="claude", event="SessionEnd", payload={
            "session_id": "sess-claude-seq", "transcript_path": "relative/path.jsonl",
            "cwd": str(self.repo), "hook_event_name": "SessionEnd",
        })
        self.assertEqual(0, rc, "a capture failure never steers the runtime (exit-code contract)")
        self.assertEqual("blocked", self._health("hook-claude")["status"])

    def test_zero_turn_no_op_keeps_its_own_ok_detail(self):
        session_id = "a849ebe4-ce15-4d80-ae60-b7d0deb7fac4"
        project_dir = self.transcripts / "-Users-someone-zero"
        project_dir.mkdir()
        missing = project_dir / f"{session_id}.jsonl"

        rc = self._run_hook(runtime="claude", event="SessionEnd", payload={
            "session_id": session_id, "transcript_path": str(missing),
            "cwd": str(self.repo), "hook_event_name": "SessionEnd",
        })

        self.assertEqual(0, rc)
        health = self._health("hook-claude")
        self.assertEqual("ok", health["status"])
        self.assertEqual("lifecycle-empty:transcript-never-created", health["detail"])

    def test_capture_off_does_not_touch_hook_health(self):
        self._seed_blocked("claude", "secure-read-open:FileNotFoundError")
        transcript = self._transcript("unregistered.jsonl")
        with mock.patch.object(hook_runner.MemoryConfig, "load", return_value=self.config), \
             mock.patch.object(hook_runner, "_spawn_drain"), \
             mock.patch("sys.stdin", io.StringIO(json.dumps({
                 "session_id": "sess-off", "transcript_path": str(transcript),
             }))):
            rc = hook_runner.main([
                "--config", str(self.config_path),
                "--runtime", "claude", "--event", "Stop",
            ])
        self.assertEqual(0, rc)
        self.assertEqual("blocked", self._health("hook-claude")["status"])
        self.assertEqual("off", self._health("capture-claude")["status"])

    def test_session_start_does_not_write_hook_health(self):
        """Startup reports under recall-<runtime>; the components stay separate."""
        self._seed_blocked("claude", "secure-read-open:FileNotFoundError")
        before = self._health("hook-claude")
        transcript = self._transcript("startup.jsonl")

        rc = self._run_hook(runtime="claude", event="SessionStart", payload={
            "session_id": "b1b1b1b1-0000-4000-8000-000000000001",
            "transcript_path": str(transcript), "cwd": str(self.repo),
        })

        self.assertEqual(0, rc)
        self.assertEqual(before, self._health("hook-claude"))
        self.assertEqual("ok", self._health("recall-claude")["status"])

    # --- separation from the detached drain -----------------------------

    def test_hook_stays_ok_when_the_later_drain_fails(self):
        transcript = self._transcript("codex-drain-fail.jsonl")
        rc = self._run_hook(runtime="codex", event="SessionEnd", payload={
            "session_id": "sess-codex-drainfail", "transcript_path": str(transcript),
            "cwd": str(self.repo), "hook_event_name": "SessionEnd",
        })
        self.assertEqual(0, rc)
        self.assertEqual("ok", self._health("hook-codex")["status"])
        self.assertFalse(self._health_exists("drain"))

        pending = sorted((self.state / "queue" / "pending").glob("codex-*.json"))
        self.assertEqual(1, len(pending))
        failing = RuntimeNativeProvider(self.config, codex_runner=_codex_usage_limit)
        with mock.patch.object(memory_cli, "create_provider", return_value=failing), \
             mock.patch.object(memory_cli.MemoryConfig, "load", return_value=self.config):
            drain_rc = memory_cli.main([
                "--config", str(self.config_path), "drain", "--queue", str(pending[0]),
            ])

        self.assertEqual(2, drain_rc)
        # The drain owns its own failure...
        self.assertEqual("blocked", self._health("drain")["status"])
        self.assertEqual("blocked", self._health("flush-codex")["status"])
        # ...and the capture that genuinely succeeded stays green.
        hook_health = self._health("hook-codex")
        self.assertEqual("ok", hook_health["status"])
        self.assertEqual("lifecycle-ok:SessionEnd", hook_health["detail"])
        self.assertTrue(pending[0].is_file(), "raw checkpoint still preserved")

    def test_successful_drain_after_successful_hook_keeps_everything_ok(self):
        transcript = self._transcript("codex-drain-ok.jsonl")
        self._seed_blocked("codex", "checkpoint-transcript-empty")
        self._run_hook(runtime="codex", event="SessionEnd", payload={
            "session_id": "sess-codex-drainok", "transcript_path": str(transcript),
            "cwd": str(self.repo), "hook_event_name": "SessionEnd",
        })
        pending = sorted((self.state / "queue" / "pending").glob("codex-*.json"))
        working = RuntimeNativeProvider(self.config, codex_runner=_codex_ok)
        with mock.patch.object(memory_cli, "create_provider", return_value=working), \
             mock.patch.object(memory_cli.MemoryConfig, "load", return_value=self.config):
            drain_rc = memory_cli.main([
                "--config", str(self.config_path), "drain", "--queue", str(pending[0]),
            ])
        self.assertEqual(0, drain_rc)
        self.assertEqual("ok", self._health("hook-codex")["status"])
        self.assertEqual("ok", self._health("drain")["status"])
        self.assertEqual("ok", self._health("flush-codex")["status"])
        state_file = (
            self.state / "sessions" / "codex"
            / f"{session_key('sess-codex-drainok')}.json"
        )
        self.assertTrue(state_file.is_file())

    def test_health_payload_keeps_the_existing_schema(self):
        transcript = self._transcript("schema.jsonl")
        self._run_hook(runtime="claude", event="Stop", payload={
            "session_id": "sess-schema", "transcript_path": str(transcript),
            "cwd": str(self.repo), "hook_event_name": "Stop",
        })
        health = self._health("hook-claude")
        self.assertEqual("pikselzone-memory-health-v1", health["schema"])
        self.assertEqual(
            {"schema", "component", "status", "updated_at", "detail"}, set(health)
        )


if __name__ == "__main__":
    unittest.main()

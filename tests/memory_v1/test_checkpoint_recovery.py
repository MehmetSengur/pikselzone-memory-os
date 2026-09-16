"""Bounded recovery of stale checkpoints and zero-turn lifecycle boundaries.

Two production defects are pinned here:

* BUG-2 -- a ``session_end`` checkpoint whose drain hit a transient provider
  usage limit stayed pending for days because nothing ever retried it.
* BUG-1 -- a Claude ``SessionEnd`` for a session that produced zero turns (its
  transcript was never created) was reported as ``health/hook-claude=blocked``,
  a monitoring false positive with no data loss behind it.
"""
from __future__ import annotations

import datetime as dt
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from memory_v1.adapters import (
    checkpoint_hook, drain_checkpoint, empty_lifecycle_reason,
)
from memory_v1.core import MemoryConfig, NoMemory, ProviderBlocked, SchemaError, session_key
from memory_v1.events import parse_event_artifact
from memory_v1.provider import RuntimeNativeProvider
from memory_v1.retry import (
    MAX_DRAIN_ATTEMPTS, STALE_CHECKPOINT_MIN_AGE_SECONDS, classify_drain_failure,
    find_stale_recoverable_checkpoints, load_retry_state, retry_summary,
)
import memory_v1.hook_runner as hook_runner


SAMPLE_SUMMARY = {
    "status": "memory",
    "context": ["Stale checkpoint recovery session."],
    "important_conversations": ["Operator asked for the pending drain to be retried."],
    "decisions": ["Retry the transient failure with bounded backoff."],
    "learnings": ["A preserved checkpoint is worthless if nothing ever reads it again."],
    "open_items": ["Confirm the merged daily artifact."],
    "evidence": ["COMMIT_SHA=ceb977c40d649e593ee1157f6e0b9f8059467901"],
}

USAGE_LIMIT_STDOUT = (
    json.dumps({"type": "thread.started", "thread_id": "t-1"}) + "\n"
    + json.dumps({"type": "error", "message": "You've hit your usage limit."}) + "\n"
)


def _codex_ok(summary: dict | None = None):
    payload = json.dumps({
        "type": "item.completed",
        "item": {"type": "agent_message", "text": json.dumps(summary or SAMPLE_SUMMARY)},
    }) + "\n"

    def runner(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 0, stdout=payload, stderr="")
    return runner


def _codex_usage_limit(cmd, **kwargs):
    """Reproduce the exact real failure: exit 1 with a usage-limit error event."""
    return subprocess.CompletedProcess(cmd, 1, stdout=USAGE_LIMIT_STDOUT, stderr="")


class CheckpointRetryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="pz-recovery-test-")
        self.root = Path(self.temp.name).resolve()
        self.vault = self.root / "vault"
        self.state = self.root / "state"
        self.vault.mkdir()
        self.state.mkdir()
        self.config = self._make_config()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _make_config(self) -> MemoryConfig:
        return MemoryConfig.from_dict({
            "role": "workstation",
            "vault_path": str(self.vault),
            "state_path": str(self.state),
            "runtimes": ["codex", "claude"],
            "transcript_roots": {"codex": [str(self.root)], "claude": [str(self.root)]},
            "can_write_event_memory": True,
            "can_run_compiler": False,
            "models": {"flush": "gpt-5.6-luna", "compiler": "gpt-5.6-terra"},
            "provider": {},
        })

    def _transcript(self, name: str, *turns: tuple[str, str]) -> Path:
        path = self.root / name
        path.write_text(
            "".join(
                json.dumps({"role": role, "content": text}) + "\n" for role, text in turns
            ),
            encoding="utf-8",
        )
        return path

    def _checkpoint(self, *, session_id: str, event: str, transcript: Path,
                    runtime: str = "codex") -> Path:
        return checkpoint_hook(
            self.config, runtime=runtime,
            payload={
                "session_id": session_id,
                "transcript_path": str(transcript),
                "event": event,
            },
        )

    @staticmethod
    def _backdate(path: Path, seconds: int) -> None:
        stamp = path.stat().st_mtime - seconds
        os.utime(path, (stamp, stamp))

    # --- classification -------------------------------------------------

    def test_transient_provider_failures_are_retryable(self):
        for reason in (
            "codex-process-failed:1: err=You've hit your usage limit",
            "codex-timeout",
            "claude-timeout",
            "codex-exec-error:OSError",
            "provider-transport:URLError",
            "provider-http-429",
            "codex-turn-failed",
        ):
            with self.subTest(reason=reason):
                self.assertEqual(
                    "retryable", classify_drain_failure(ProviderBlocked(reason))
                )

    def test_permanent_failures_are_not_retryable(self):
        for exc in (
            SchemaError("checkpoint-schema-invalid"),
            SchemaError("checkpoint-source-digest-mismatch"),
            ProviderBlocked("credential-missing:PZ_MEMORY_KEY"),
            ProviderBlocked("codex-recursion-detected"),
            ProviderBlocked("unsupported-runtime:banana"),
            ProviderBlocked("provider-unclassified-novel-failure"),
        ):
            with self.subTest(reason=str(exc)):
                self.assertEqual("permanent", classify_drain_failure(exc))

    # --- BUG-2: transient failure keeps the checkpoint and schedules retry --

    def test_transient_failure_leaves_checkpoint_pending_and_scheduled(self):
        transcript = self._transcript(
            "transient.jsonl", ("user", "Push the branch."), ("assistant", "Pushed."),
        )
        queue_path = self._checkpoint(
            session_id="sess-transient", event="session_end", transcript=transcript
        )
        provider = RuntimeNativeProvider(self.config, codex_runner=_codex_usage_limit)
        with self.assertRaises(ProviderBlocked):
            drain_checkpoint(self.config, queue_path, provider=provider)

        self.assertTrue(queue_path.is_file(), "raw checkpoint must survive")
        state = load_retry_state(self.config, queue_path)
        self.assertEqual("retryable", state["classification"])
        self.assertEqual("retry-scheduled", state["status"])
        self.assertEqual(1, state["attempts"])
        self.assertIn("usage limit", state["last_reason"])
        self.assertTrue(state["next_attempt_after"])
        self.assertEqual({"scheduled": 1, "exhausted": 0, "permanent": 0},
                         retry_summary(self.config))

    def test_retryable_checkpoint_later_succeeds(self):
        transcript = self._transcript(
            "later-ok.jsonl", ("user", "Retry me."), ("assistant", "Done."),
        )
        queue_path = self._checkpoint(
            session_id="sess-later-ok", event="session_end", transcript=transcript
        )
        failing = RuntimeNativeProvider(self.config, codex_runner=_codex_usage_limit)
        with self.assertRaises(ProviderBlocked):
            drain_checkpoint(self.config, queue_path, provider=failing)
        self.assertTrue(queue_path.is_file())

        working = RuntimeNativeProvider(self.config, codex_runner=_codex_ok())
        event_path = drain_checkpoint(self.config, queue_path, provider=working)

        self.assertTrue(event_path.is_file())
        self.assertFalse(queue_path.is_file(), "settled checkpoint is removed")
        self.assertEqual({}, load_retry_state(self.config, queue_path))
        self.assertEqual({"scheduled": 0, "exhausted": 0, "permanent": 0},
                         retry_summary(self.config))

    def test_retry_is_bounded(self):
        transcript = self._transcript(
            "bounded.jsonl", ("user", "Fail forever."), ("assistant", "Ok."),
        )
        queue_path = self._checkpoint(
            session_id="sess-bounded", event="session_end", transcript=transcript
        )
        provider = RuntimeNativeProvider(self.config, codex_runner=_codex_usage_limit)
        for _ in range(MAX_DRAIN_ATTEMPTS):
            with self.assertRaises(ProviderBlocked):
                drain_checkpoint(self.config, queue_path, provider=provider)

        state = load_retry_state(self.config, queue_path)
        self.assertEqual(MAX_DRAIN_ATTEMPTS, state["attempts"])
        self.assertEqual("retry-exhausted", state["status"])
        self.assertIsNone(state["next_attempt_after"])
        self.assertTrue(queue_path.is_file(), "raw data is still preserved")

        self._backdate(queue_path, STALE_CHECKPOINT_MIN_AGE_SECONDS + 60)
        self.assertEqual(
            [], find_stale_recoverable_checkpoints(self.config, runtime="codex")
        )

    def test_permanent_failure_is_not_retried(self):
        transcript = self._transcript(
            "permanent.jsonl", ("user", "Corrupt me."), ("assistant", "Ok."),
        )
        queue_path = self._checkpoint(
            session_id="sess-permanent", event="session_end", transcript=transcript
        )
        provider = RuntimeNativeProvider(self.config, codex_runner=_codex_ok())
        with mock.patch(
            "memory_v1.events.EventWriter.flush",
            side_effect=SchemaError("checkpoint-schema-invalid"),
        ):
            with self.assertRaises(SchemaError):
                drain_checkpoint(self.config, queue_path, provider=provider)

        state = load_retry_state(self.config, queue_path)
        self.assertEqual("permanent", state["classification"])
        self.assertEqual("permanent", state["status"])
        self.assertIsNone(state["next_attempt_after"])
        self.assertTrue(queue_path.is_file())

        self._backdate(queue_path, STALE_CHECKPOINT_MIN_AGE_SECONDS + 60)
        self.assertEqual(
            [], find_stale_recoverable_checkpoints(self.config, runtime="codex")
        )

    def test_backoff_defers_the_next_attempt(self):
        transcript = self._transcript(
            "backoff.jsonl", ("user", "Wait."), ("assistant", "Ok."),
        )
        queue_path = self._checkpoint(
            session_id="sess-backoff", event="session_end", transcript=transcript
        )
        provider = RuntimeNativeProvider(self.config, codex_runner=_codex_usage_limit)
        with self.assertRaises(ProviderBlocked):
            drain_checkpoint(self.config, queue_path, provider=provider)
        self._backdate(queue_path, STALE_CHECKPOINT_MIN_AGE_SECONDS + 60)

        now = dt.datetime.now().astimezone()
        self.assertEqual(
            [],
            find_stale_recoverable_checkpoints(self.config, runtime="codex", now=now),
        )
        later = now + dt.timedelta(days=1)
        self.assertEqual(
            [queue_path],
            find_stale_recoverable_checkpoints(self.config, runtime="codex", now=later),
        )

    # --- stale selection is bounded and never touches turn checkpoints ---

    def test_stale_selection_skips_turn_checkpoints_and_fresh_files(self):
        turn_transcript = self._transcript(
            "canary.jsonl", ("user", "PZ-CANARY test noise."), ("assistant", "Ack."),
        )
        turn_path = self._checkpoint(
            session_id="sess-canary", event="Stop", transcript=turn_transcript
        )
        self._backdate(turn_path, STALE_CHECKPOINT_MIN_AGE_SECONDS + 600)

        fresh_transcript = self._transcript(
            "fresh.jsonl", ("user", "Just now."), ("assistant", "Ok."),
        )
        fresh_path = self._checkpoint(
            session_id="sess-fresh", event="session_end", transcript=fresh_transcript
        )

        stale_transcript = self._transcript(
            "stale.jsonl", ("user", "Days old."), ("assistant", "Ok."),
        )
        stale_path = self._checkpoint(
            session_id="sess-stale", event="session_end", transcript=stale_transcript
        )
        self._backdate(stale_path, STALE_CHECKPOINT_MIN_AGE_SECONDS + 600)

        selected = find_stale_recoverable_checkpoints(self.config, runtime="codex")
        self.assertEqual([stale_path], selected)
        self.assertNotIn(turn_path, selected)
        self.assertNotIn(fresh_path, selected)

    def test_stale_selection_is_capped(self):
        paths = []
        for index in range(4):
            transcript = self._transcript(
                f"many-{index}.jsonl", ("user", f"n{index}"), ("assistant", "ok"),
            )
            path = self._checkpoint(
                session_id=f"sess-many-{index}", event="session_end", transcript=transcript
            )
            self._backdate(path, STALE_CHECKPOINT_MIN_AGE_SECONDS + 600 + index)
            paths.append(path)
        selected = find_stale_recoverable_checkpoints(self.config, runtime="codex")
        self.assertLessEqual(len(selected), 2)
        self.assertTrue(set(selected).issubset(set(paths)))

    def test_session_start_spawns_stale_recovery(self):
        from memory_v1 import project_registry

        repo = self.root / "repo"
        repo.mkdir()
        project_registry.register(self.state, repo, "demo")
        transcript = self._transcript(
            "startup-stale.jsonl", ("user", "Old."), ("assistant", "Ok."),
        )
        stale_path = self._checkpoint(
            session_id="sess-startup-stale", event="session_end", transcript=transcript
        )
        self._backdate(stale_path, STALE_CHECKPOINT_MIN_AGE_SECONDS + 600)

        payload = json.dumps({
            "session_id": "01a05981-9068-79f3-8385-0df778aba176",
            "transcript_path": str(transcript), "cwd": str(repo),
        })
        with mock.patch.object(hook_runner.MemoryConfig, "load", return_value=self.config), \
             mock.patch.object(hook_runner, "_spawn_drain") as spawn, \
             mock.patch("sys.stdin", io.StringIO(payload)):
            rc = hook_runner.main([
                "--config", str(self.root / "config.json"),
                "--runtime", "codex", "--event", "SessionStart",
                "--project", "demo", "--project-root", str(repo),
            ])
        self.assertEqual(0, rc)
        spawned = [call.args[1] for call in spawn.call_args_list]
        self.assertIn(stale_path, spawned)

    # --- BUG-2 acceptance shape: merge into the existing daily artifact ---

    def test_precompact_daily_plus_session_end_retry_yields_one_merged_daily(self):
        session_id = "01a05981-9068-79f3-8385-0df778aba176"
        early = self._transcript(
            "merge-pre.jsonl",
            ("user", "Implement the fix."), ("assistant", "Implemented, push unverified."),
        )
        pre_path = self._checkpoint(
            session_id=session_id, event="PreCompact", transcript=early
        )
        drain_checkpoint(
            self.config, pre_path,
            provider=RuntimeNativeProvider(self.config, codex_runner=_codex_ok()),
        )

        full = self._transcript(
            "merge-end.jsonl",
            ("user", "Implement the fix."), ("assistant", "Implemented, push unverified."),
            ("user", "Push it."), ("assistant", "COMMIT_SHA=ceb977c4 pushed, 255/255 pass."),
        )
        end_path = self._checkpoint(
            session_id=session_id, event="SessionEnd", transcript=full
        )
        with self.assertRaises(ProviderBlocked):
            drain_checkpoint(
                self.config, end_path,
                provider=RuntimeNativeProvider(self.config, codex_runner=_codex_usage_limit),
            )
        self.assertTrue(end_path.is_file())

        recovered = {**SAMPLE_SUMMARY, "evidence": ["COMMIT_SHA=ceb977c4", "255/255 PASS"]}
        event_path = drain_checkpoint(
            self.config, end_path,
            provider=RuntimeNativeProvider(self.config, codex_runner=_codex_ok(recovered)),
        )

        daily_files = sorted(self.vault.glob("daily/*/*.md"))
        self.assertEqual(1, len(daily_files), "no duplicate daily artifact")
        self.assertEqual(event_path, daily_files[0])
        artifact = parse_event_artifact(event_path.read_text(encoding="utf-8"))
        self.assertEqual(["pre_compact", "session_end"], artifact["events_seen"])
        self.assertEqual("session_end", artifact["event"])
        self.assertIn("COMMIT_SHA=ceb977c4", artifact["sections"]["evidence"])

        state_file = self.state / "sessions" / "codex" / f"{session_key(session_id)}.json"
        session_state = json.loads(state_file.read_text(encoding="utf-8"))
        self.assertEqual(["pre_compact", "session_end"], session_state["events_seen"])

    def test_stale_hook_config_does_not_overwrite_activation_evidence(self):
        """A checkpoint captured under a superseded hook config must not
        replace activation evidence that attests to the current one."""
        hooks = self.root / "hooks.json"
        hooks.write_text(json.dumps({"hooks": {"SessionEnd": []}}), encoding="utf-8")
        evidence = self.state / "evidence" / "codex-smoke.json"
        config = MemoryConfig.from_dict({
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
                "codex_hooks_path": str(hooks),
                "codex_smoke_evidence_path": str(evidence),
            },
        })
        transcript = self._transcript(
            "stale-cfg.jsonl", ("user", "Old config."), ("assistant", "Ok."),
        )
        queue_path = checkpoint_hook(
            config, runtime="codex",
            payload={
                "session_id": "sess-stale-cfg", "transcript_path": str(transcript),
                "event": "session_end",
            },
        )
        # The operator edits the hook configuration after the checkpoint was
        # written, then the stale checkpoint is finally recovered.
        hooks.write_text(json.dumps({"hooks": {"SessionEnd": [], "Stop": []}}), encoding="utf-8")
        drain_checkpoint(
            config, queue_path,
            provider=RuntimeNativeProvider(config, codex_runner=_codex_ok()),
        )
        self.assertFalse(evidence.exists(), "stale recovery must not write evidence")

    def test_current_hook_config_still_writes_activation_evidence(self):
        hooks = self.root / "hooks-current.json"
        hooks.write_text(json.dumps({"hooks": {"SessionEnd": []}}), encoding="utf-8")
        evidence = self.state / "evidence" / "codex-smoke.json"
        config = MemoryConfig.from_dict({
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
                "codex_hooks_path": str(hooks),
                "codex_smoke_evidence_path": str(evidence),
            },
        })
        transcript = self._transcript(
            "current-cfg.jsonl", ("user", "Same config."), ("assistant", "Ok."),
        )
        queue_path = checkpoint_hook(
            config, runtime="codex",
            payload={
                "session_id": "sess-current-cfg", "transcript_path": str(transcript),
                "event": "session_end",
            },
        )
        drain_checkpoint(
            config, queue_path,
            provider=RuntimeNativeProvider(config, codex_runner=_codex_ok()),
        )
        self.assertTrue(evidence.is_file())
        written = json.loads(evidence.read_text(encoding="utf-8"))
        self.assertEqual("pass", written["status"])

    def test_repeated_recovery_is_idempotent(self):
        transcript = self._transcript(
            "idempotent.jsonl", ("user", "Once."), ("assistant", "Ok."),
        )
        queue_path = self._checkpoint(
            session_id="sess-idempotent", event="session_end", transcript=transcript
        )
        provider = RuntimeNativeProvider(self.config, codex_runner=_codex_ok())
        first = drain_checkpoint(self.config, queue_path, provider=provider)
        before = first.read_text(encoding="utf-8")

        with self.assertRaises(NoMemory) as caught:
            drain_checkpoint(self.config, queue_path, provider=provider)
        self.assertEqual("checkpoint-already-settled", str(caught.exception))

        self.assertEqual(1, len(sorted(self.vault.glob("daily/*/*.md"))))
        self.assertEqual(before, first.read_text(encoding="utf-8"))

    def test_second_drain_of_identical_content_does_not_duplicate(self):
        transcript = self._transcript(
            "dup.jsonl", ("user", "Same."), ("assistant", "Content."),
        )
        first_path = self._checkpoint(
            session_id="sess-dup", event="session_end", transcript=transcript
        )
        provider = RuntimeNativeProvider(self.config, codex_runner=_codex_ok())
        event_path = drain_checkpoint(self.config, first_path, provider=provider)

        second_path = self._checkpoint(
            session_id="sess-dup", event="session_end", transcript=transcript
        )
        again = drain_checkpoint(self.config, second_path, provider=provider)
        self.assertEqual(event_path, again)
        self.assertEqual(1, len(sorted(self.vault.glob("daily/*/*.md"))))


class ZeroTurnLifecycleTests(unittest.TestCase):
    """BUG-1: a session that produced zero turns is empty, not blocked."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="pz-zeroturn-test-")
        self.root = Path(self.temp.name).resolve()
        self.vault = self.root / "vault"
        self.state = self.root / "state"
        self.projects = self.root / "projects"
        self.vault.mkdir()
        self.state.mkdir()
        self.projects.mkdir()
        self.config = MemoryConfig.from_dict({
            "role": "workstation",
            "vault_path": str(self.vault),
            "state_path": str(self.state),
            "runtimes": ["codex", "claude"],
            "transcript_roots": {
                "codex": [str(self.projects)], "claude": [str(self.projects)],
            },
            "can_write_event_memory": True,
            "can_run_compiler": False,
            "models": {"flush": "gpt-5.6-luna", "compiler": "gpt-5.6-terra"},
            "provider": {},
        })
        self.repo = self.root / "repo"
        self.repo.mkdir()
        from memory_v1 import project_registry
        project_registry.register(self.state, self.repo, "demo")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _run_session_end(self, payload: dict, runtime: str = "claude") -> int:
        with mock.patch.object(hook_runner.MemoryConfig, "load", return_value=self.config), \
             mock.patch.object(hook_runner, "_spawn_drain") as spawn, \
             mock.patch("sys.stdin", io.StringIO(json.dumps(payload))):
            rc = hook_runner.main([
                "--config", str(self.root / "config.json"),
                "--runtime", runtime, "--event", "SessionEnd",
                "--project", "demo", "--project-root", str(self.repo),
            ])
            self.spawn_calls = spawn.call_args_list
        return rc

    def _health(self, component: str) -> dict:
        path = self.state / "health" / f"{component}.json"
        return json.loads(path.read_text(encoding="utf-8"))

    def test_zero_turn_missing_transcript_is_empty_not_blocked(self):
        session_id = "a849ebe4-ce15-4d80-ae60-b7d0deb7fac4"
        project_dir = self.projects / "-Users-someone-Documents-antigravity"
        project_dir.mkdir()
        transcript = project_dir / f"{session_id}.jsonl"
        self.assertFalse(transcript.exists())

        rc = self._run_session_end({
            "session_id": session_id, "transcript_path": str(transcript),
            "cwd": str(self.repo), "hook_event_name": "SessionEnd", "reason": "other",
        })

        self.assertEqual(0, rc)
        health = self._health("hook-claude")
        self.assertEqual("ok", health["status"])
        self.assertEqual("lifecycle-empty:transcript-never-created", health["detail"])
        self.assertFalse((self.state / "queue" / "pending").exists())
        self.assertEqual([], self.spawn_calls)

    def test_zero_turn_empty_transcript_is_empty_not_blocked(self):
        session_id = "b0b0b0b0-0000-4000-8000-000000000001"
        project_dir = self.projects / "-Users-someone-Documents-empty"
        project_dir.mkdir()
        transcript = project_dir / f"{session_id}.jsonl"
        transcript.write_text("", encoding="utf-8")

        rc = self._run_session_end({
            "session_id": session_id, "transcript_path": str(transcript),
            "cwd": str(self.repo), "hook_event_name": "SessionEnd",
        }, runtime="codex")

        self.assertEqual(0, rc)
        health = self._health("hook-codex")
        self.assertEqual("ok", health["status"])
        self.assertEqual("lifecycle-empty:transcript-zero-turns", health["detail"])

    def test_missing_transcript_for_a_session_with_memory_stays_blocked(self):
        session_id = "c0ffee00-0000-4000-8000-000000000002"
        project_dir = self.projects / "-Users-someone-Documents-real"
        project_dir.mkdir()
        transcript = project_dir / f"{session_id}.jsonl"
        state_file = (
            self.state / "sessions" / "claude" / f"{session_key(session_id)}.json"
        )
        state_file.parent.mkdir(parents=True, exist_ok=True)
        state_file.write_text(json.dumps({
            "runtime": "claude", "session_key": session_key(session_id),
            "source_digest": "0" * 64, "events_seen": ["pre_compact"],
            "event_path": str(self.vault / "daily" / "x.md"), "status": "ok",
        }), encoding="utf-8")

        rc = self._run_session_end({
            "session_id": session_id, "transcript_path": str(transcript),
            "cwd": str(self.repo), "hook_event_name": "SessionEnd",
        })

        self.assertEqual(0, rc, "a capture failure never steers the runtime (exit-code contract)")
        health = self._health("hook-claude")
        self.assertEqual("blocked", health["status"])
        self.assertIn("secure-read-open", health["detail"])

    def test_missing_transcript_with_pending_checkpoint_stays_blocked(self):
        session_id = "d0d0d0d0-0000-4000-8000-000000000003"
        project_dir = self.projects / "-Users-someone-Documents-pending"
        project_dir.mkdir()
        real = project_dir / "turns.jsonl"
        real.write_text(
            json.dumps({"role": "user", "content": "a"}) + "\n"
            + json.dumps({"role": "assistant", "content": "b"}) + "\n",
            encoding="utf-8",
        )
        checkpoint_hook(
            self.config, runtime="claude",
            payload={
                "session_id": session_id, "transcript_path": str(real), "event": "Stop",
            },
        )
        vanished = project_dir / f"{session_id}.jsonl"

        rc = self._run_session_end({
            "session_id": session_id, "transcript_path": str(vanished),
            "cwd": str(self.repo), "hook_event_name": "SessionEnd",
        })

        self.assertEqual(0, rc, "a capture failure never steers the runtime (exit-code contract)")
        self.assertEqual("blocked", self._health("hook-claude")["status"])

    def test_transcript_outside_allowed_roots_stays_blocked(self):
        session_id = "e0e0e0e0-0000-4000-8000-000000000004"
        outside = self.root / "elsewhere" / f"{session_id}.jsonl"
        outside.parent.mkdir()

        rc = self._run_session_end({
            "session_id": session_id, "transcript_path": str(outside),
            "cwd": str(self.repo), "hook_event_name": "SessionEnd",
        })

        self.assertEqual(0, rc, "a capture failure never steers the runtime (exit-code contract)")
        health = self._health("hook-claude")
        self.assertEqual("blocked", health["status"])
        self.assertIn("transcript-path-outside-allowed-roots", health["detail"])

    def test_classifier_rejects_a_missing_parent_directory(self):
        session_id = "f0f0f0f0-0000-4000-8000-000000000005"
        transcript = self.projects / "never-made" / f"{session_id}.jsonl"
        from memory_v1.core import PolicyError

        self.assertIsNone(empty_lifecycle_reason(
            self.config, runtime="claude",
            payload={"session_id": session_id, "transcript_path": str(transcript)},
            exc=PolicyError("secure-read-open:FileNotFoundError"),
        ))

    def test_classifier_rejects_unrelated_failures(self):
        from memory_v1.core import PolicyError

        session_id = "aaaaaaaa-0000-4000-8000-000000000006"
        project_dir = self.projects / "-Users-someone-Documents-perm"
        project_dir.mkdir()
        transcript = project_dir / f"{session_id}.jsonl"
        self.assertIsNone(empty_lifecycle_reason(
            self.config, runtime="claude",
            payload={"session_id": session_id, "transcript_path": str(transcript)},
            exc=PolicyError("secure-read-open:PermissionError"),
        ))


if __name__ == "__main__":
    unittest.main()

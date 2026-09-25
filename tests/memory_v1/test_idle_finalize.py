"""Idle finalize for workstation threads that never send SessionEnd.

Measured on 2026-09-15: Codex Desktop/App (codex 0.153.4) sent no SessionEnd
for 43 user threads in ten days, so their raw ``turn_complete`` checkpoints
stayed pending forever.  Pinned here:

* a quiet thread's pending turns promote as one batch from a SessionStart;
* settlement is scoped to turn digests, so a thread the user returns to keeps
  its new turns eligible and a re-captured turn never promotes twice;
* batches and a later real SessionEnd share one daily artifact;
* provider failure settles nothing and follows the retry backoff.
"""
from __future__ import annotations

import datetime as dt
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from memory_v1.adapters import (
    checkpoint_hook, drain_checkpoint, settled_turn_digests,
)
from memory_v1.core import (
    ConfigError, MemoryConfig, NoMemory, ProviderBlocked, SchemaError, session_key,
)
from memory_v1.events import parse_event_artifact
from memory_v1.retry import (
    MAX_IDLE_FINALIZE_SPAWNS, find_idle_turn_batches, load_retry_state,
)
import memory_v1.hook_runner as hook_runner


IDLE = 45 * 60


def _summary(**sections: list[str]) -> dict:
    value = {
        "status": "memory", "context": [], "important_conversations": [],
        "decisions": [], "learnings": [], "open_items": [], "evidence": [],
    }
    value.update(sections)
    return value


EMPTY = {
    "status": "empty", "context": [], "important_conversations": [],
    "decisions": [], "learnings": [], "open_items": [], "evidence": [],
}


class FakeProvider:
    """Returns queued summaries, records every call, fails when told to."""

    last_source_model = "fake-summarizer"
    last_source_provider = "fake"

    def __init__(self, *responses: object) -> None:
        self.responses = list(responses)
        self.inputs: list[str] = []

    def request(self, **kwargs):
        self.inputs.append(kwargs["untrusted_input"])
        if not self.responses:
            raise AssertionError("provider must not be called")
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


class IdleFinalizeBase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="pz-idle-test-")
        self.root = Path(self.temp.name).resolve()
        self.vault = self.root / "vault"
        self.state = self.root / "state"
        self.vault.mkdir()
        self.state.mkdir()
        self.config = self._config()
        self.pending = self.state / "queue" / "pending"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _config(self, **extra) -> MemoryConfig:
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
            **extra,
        })

    def _turns(self, session_id: str, runtime: str, turns: list[tuple[str, str]]) -> list[Path]:
        """Append completed turns to one transcript, one Stop per turn."""
        transcript = self.root / f"{runtime}-{session_key(session_id)[:8]}.jsonl"
        paths = []
        for user, assistant in turns:
            with transcript.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps({"role": "user", "content": user}) + "\n")
                handle.write(json.dumps({"role": "assistant", "content": assistant}) + "\n")
            paths.append(self._stop(session_id, runtime, transcript))
        return paths

    def _stop(self, session_id: str, runtime: str, transcript: Path) -> Path:
        return checkpoint_hook(
            self.config, runtime=runtime,
            payload={
                "session_id": session_id, "transcript_path": str(transcript),
                "hook_event_name": "Stop",
            },
        )

    @staticmethod
    def _age(paths, seconds: int) -> None:
        for path in paths:
            stamp = path.stat().st_mtime - seconds
            os.utime(path, (stamp, stamp))

    def _dailies(self) -> list[Path]:
        return sorted(self.vault.glob("daily/*/*.md"))

    def _state(self, runtime: str, session_id: str) -> dict:
        path = self.state / "sessions" / runtime / f"{session_key(session_id)}.json"
        return json.loads(path.read_text(encoding="utf-8"))


class IdleFinalizeConfigTests(IdleFinalizeBase):
    def test_default_window_is_forty_five_minutes(self):
        self.assertEqual(IDLE, self.config.idle_finalize_seconds)

    def test_window_is_configurable_and_zero_disables(self):
        self.assertEqual(30 * 60, self._config(idle_finalize_minutes=30).idle_finalize_seconds)
        disabled = self._config(idle_finalize_minutes=0)
        paths = self._turns("sess-off", "codex", [("Hi.", "Hello.")])
        self._age(paths, 10 * IDLE)
        self.assertEqual([], find_idle_turn_batches(disabled))

    def test_invalid_windows_are_rejected(self):
        for value in (5, 2000, "30", True, 12.5):
            with self.subTest(value=value), self.assertRaises(ConfigError):
                self._config(idle_finalize_minutes=value)


class IdleSelectionTests(IdleFinalizeBase):
    def test_quiet_session_selects_its_oldest_turn(self):
        paths = self._turns("sess-quiet", "codex", [("One.", "A."), ("Two.", "B.")])
        self._age(paths[:1], IDLE + 120)
        self._age(paths[1:], IDLE + 60)
        self.assertEqual([paths[0]], find_idle_turn_batches(self.config))

    def test_a_recent_turn_keeps_the_whole_session_open(self):
        paths = self._turns("sess-active", "claude", [("One.", "A."), ("Two.", "B.")])
        self._age(paths[:1], 10 * IDLE)
        self.assertEqual([], find_idle_turn_batches(self.config))

    def test_pending_terminal_checkpoint_owns_the_session(self):
        paths = self._turns("sess-terminal", "codex", [("One.", "A.")])
        transcript = self.root / f"codex-{session_key('sess-terminal')[:8]}.jsonl"
        terminal = checkpoint_hook(
            self.config, runtime="codex",
            payload={"session_id": "sess-terminal", "transcript_path": str(transcript),
                     "hook_event_name": "SessionEnd"},
        )
        self._age([*paths, terminal], 10 * IDLE)
        self.assertEqual([], find_idle_turn_batches(self.config))

    def test_hermes_checkpoints_are_never_swept(self):
        self.pending.mkdir(parents=True, exist_ok=True)
        hermes = self.pending / f"hermes-{'a' * 32}-turn_complete-{'b' * 16}.json"
        hermes.write_text("{}", encoding="utf-8")
        self._age([hermes], 10 * IDLE)
        self.assertEqual([], find_idle_turn_batches(self.config))

    def test_excluded_session_and_cap(self):
        chosen = []
        for index in range(MAX_IDLE_FINALIZE_SPAWNS + 2):
            paths = self._turns(f"sess-cap-{index}", "codex", [(f"Q{index}.", "A.")])
            self._age(paths, IDLE + 60 + index)
            chosen.extend(paths)
        selected = find_idle_turn_batches(self.config)
        self.assertEqual(MAX_IDLE_FINALIZE_SPAWNS, len(selected))
        excluded = frozenset({("codex", session_key("sess-cap-0"))})
        self.assertNotIn(chosen[0], find_idle_turn_batches(self.config, exclude=excluded, limit=10))


class IdleDrainTests(IdleFinalizeBase):
    def test_batch_return_batch_then_session_end_share_one_artifact(self):
        session = "019a0000-0000-7000-8000-00000000c0de"
        first = self._turns(session, "codex", [("Plan the fix.", "Planned."), ("Apply it.", "Applied.")])
        provider = FakeProvider(_summary(decisions=["Idle batch one decision."]))
        event_path = drain_checkpoint(self.config, first[0], provider=provider)
        self.assertEqual(1, len(provider.inputs), "one provider call for the whole batch")
        self.assertIn("Plan the fix.", provider.inputs[0])
        self.assertIn("Apply it.", provider.inputs[0])
        self.assertEqual([], sorted(self.pending.glob("*.json")))
        artifact = parse_event_artifact(event_path.read_text(encoding="utf-8"))
        self.assertEqual(["checkpoint_recovery"], artifact["events_seen"])

        # The user comes back to the thread: new turns must stay eligible.
        later = self._turns(session, "codex", [("Now test it.", "Tests pass.")])
        self.assertEqual(1, len(later))
        provider = FakeProvider(_summary(evidence=["Idle batch two evidence."]))
        again = drain_checkpoint(self.config, later[0], provider=provider)
        self.assertEqual(event_path, again)
        self.assertNotIn("Plan the fix.", provider.inputs[0])
        merged = parse_event_artifact(again.read_text(encoding="utf-8"))
        self.assertEqual(["Idle batch one decision."], merged["sections"]["decisions"])
        self.assertEqual(["Idle batch two evidence."], merged["sections"]["evidence"])
        self.assertEqual(artifact["created_at"], merged["created_at"])

        # A real SessionEnd arriving afterwards merges into the same artifact.
        transcript = self.root / f"codex-{session_key(session)[:8]}.jsonl"
        end = checkpoint_hook(
            self.config, runtime="codex",
            payload={"session_id": session, "transcript_path": str(transcript),
                     "hook_event_name": "SessionEnd"},
        )
        final = drain_checkpoint(
            self.config, end, provider=FakeProvider(_summary(decisions=["Full session."]))
        )
        self.assertEqual(event_path, final)
        self.assertEqual([event_path], self._dailies())
        closed = parse_event_artifact(final.read_text(encoding="utf-8"))
        self.assertEqual(["checkpoint_recovery", "session_end"], closed["events_seen"])
        self.assertEqual(3, len(settled_turn_digests(
            self.config, runtime="codex", state_key=session_key(session)
        )))

    def test_recaptured_settled_turn_is_settled_without_a_provider_call(self):
        paths = self._turns("sess-recapture", "claude", [("Keep this.", "Kept.")])
        drain_checkpoint(self.config, paths[0], provider=FakeProvider(
            _summary(learnings=["Only once."])
        ))
        transcript = self.root / f"claude-{session_key('sess-recapture')[:8]}.jsonl"
        recaptured = self._stop("sess-recapture", "claude", transcript)
        self.assertTrue(recaptured.is_file())
        same = drain_checkpoint(self.config, recaptured, provider=FakeProvider())
        self.assertEqual(self._dailies(), [same])
        self.assertFalse(recaptured.exists())
        artifact = parse_event_artifact(same.read_text(encoding="utf-8"))
        self.assertEqual(["Only once."], artifact["sections"]["learnings"])

    def test_turns_absorbed_by_session_end_do_not_promote_again(self):
        session = "sess-absorbed"
        self._turns(session, "codex", [("Decide.", "Decided.")])
        transcript = self.root / f"codex-{session_key(session)[:8]}.jsonl"
        end = checkpoint_hook(
            self.config, runtime="codex",
            payload={"session_id": session, "transcript_path": str(transcript),
                     "hook_event_name": "SessionEnd"},
        )
        event_path = drain_checkpoint(
            self.config, end, provider=FakeProvider(_summary(context=["End."]))
        )
        recaptured = self._stop(session, "codex", transcript)
        self.assertEqual(
            event_path, drain_checkpoint(self.config, recaptured, provider=FakeProvider())
        )
        self.assertFalse(recaptured.exists())
        self.assertEqual([event_path], self._dailies())

    def test_recaptured_turn_without_an_artifact_is_no_memory(self):
        session = "sess-empty-recapture"
        paths = self._turns(session, "codex", [("Thanks.", "Welcome.")])
        with self.assertRaises(NoMemory):
            drain_checkpoint(self.config, paths[0], provider=FakeProvider(EMPTY))
        transcript = self.root / f"codex-{session_key(session)[:8]}.jsonl"
        recaptured = self._stop(session, "codex", transcript)
        with self.assertRaises(NoMemory) as caught:
            drain_checkpoint(self.config, recaptured, provider=FakeProvider())
        self.assertEqual("turn-batch-already-settled", str(caught.exception))
        self.assertEqual([], self._dailies())

    def test_provider_failure_settles_nothing_and_backs_off(self):
        paths = self._turns("sess-limit", "codex", [("One.", "A."), ("Two.", "B.")])
        self._age(paths, IDLE + 60)
        failure = ProviderBlocked("codex-process-failed:1: err=You've hit your usage limit")
        with self.assertRaises(ProviderBlocked):
            drain_checkpoint(self.config, paths[0], provider=FakeProvider(failure))
        self.assertTrue(all(path.is_file() for path in paths))
        self.assertEqual([], self._dailies())
        self.assertEqual([], settled_turn_digests(
            self.config, runtime="codex", state_key=session_key("sess-limit")
        ))
        state = load_retry_state(self.config, paths[0])
        self.assertEqual("retry-scheduled", state["status"])
        now = dt.datetime.now().astimezone()
        self.assertEqual([], find_idle_turn_batches(self.config, now=now))
        later = now + dt.timedelta(hours=1)
        self.assertEqual([paths[0]], find_idle_turn_batches(self.config, now=later))

    def test_empty_batch_keeps_the_artifact_for_the_next_batch(self):
        session = "sess-empty-between"
        first = self._turns(session, "codex", [("Real work.", "Done.")])
        event_path = drain_checkpoint(
            self.config, first[0], provider=FakeProvider(_summary(decisions=["Real."]))
        )
        second = self._turns(session, "codex", [("Thanks.", "You're welcome.")])
        with self.assertRaises(NoMemory):
            drain_checkpoint(self.config, second[0], provider=FakeProvider(EMPTY))
        self.assertEqual(str(event_path), self._state("codex", session)["event_path"])
        third = self._turns(session, "codex", [("More work.", "Done too.")])
        drain_checkpoint(
            self.config, third[0], provider=FakeProvider(_summary(decisions=["Also real."]))
        )
        self.assertEqual([event_path], self._dailies())
        artifact = parse_event_artifact(event_path.read_text(encoding="utf-8"))
        self.assertEqual(["Real.", "Also real."], artifact["sections"]["decisions"])

    def test_unmergeable_artifact_fails_before_the_provider(self):
        session = "sess-corrupt"
        first = self._turns(session, "codex", [("Work.", "Done.")])
        event_path = drain_checkpoint(
            self.config, first[0], provider=FakeProvider(_summary(context=["Kept."]))
        )
        event_path.write_text("not an event artifact\n", encoding="utf-8")
        second = self._turns(session, "codex", [("More.", "Ok.")])
        with self.assertRaises(SchemaError):
            drain_checkpoint(self.config, second[0], provider=FakeProvider())
        self.assertTrue(second[0].is_file())
        self.assertEqual("permanent", load_retry_state(self.config, second[0])["status"])

    def test_second_worker_for_a_settled_batch_is_a_no_op(self):
        paths = self._turns("sess-race", "claude", [("One.", "A."), ("Two.", "B.")])
        drain_checkpoint(self.config, paths[0], provider=FakeProvider(_summary(context=["Once."])))
        with self.assertRaises(NoMemory) as caught:
            drain_checkpoint(self.config, paths[1], provider=FakeProvider())
        self.assertEqual("checkpoint-already-settled", str(caught.exception))
        self.assertEqual(1, len(self._dailies()))


class IdleSessionStartTests(IdleFinalizeBase):
    def setUp(self) -> None:
        super().setUp()
        from memory_v1 import project_registry

        self.repo = self.root / "repo"
        self.repo.mkdir()
        project_registry.register(self.state, self.repo, "demo")

    def _session_start(self, *, runtime: str, session_id: str) -> list[Path]:
        transcript = self.root / "startup.jsonl"
        transcript.write_text(
            json.dumps({"role": "user", "content": "Hi."}) + "\n"
            + json.dumps({"role": "assistant", "content": "Hello."}) + "\n",
            encoding="utf-8",
        )
        payload = json.dumps({
            "session_id": session_id, "transcript_path": str(transcript), "cwd": str(self.repo),
        })
        with mock.patch.object(hook_runner.MemoryConfig, "load", return_value=self.config), \
             mock.patch.object(hook_runner, "_spawn_drain") as spawn, \
             mock.patch("sys.stdin", io.StringIO(payload)):
            rc = hook_runner.main([
                "--config", str(self.root / "config.json"),
                "--runtime", runtime, "--event", "SessionStart",
                "--project", "demo", "--project-root", str(self.repo),
            ])
        self.assertEqual(0, rc)
        return [call.args[1] for call in spawn.call_args_list]

    def test_any_workstation_start_promotes_another_runtimes_idle_thread(self):
        desktop = self._turns("019a0000-0000-7000-8000-00000000d35c", "codex", [("Q.", "A.")])
        fresh = self._turns("019a0000-0000-7000-8000-00000000f7e5", "codex", [("Q.", "A.")])
        self._age(desktop, IDLE + 60)
        spawned = self._session_start(
            runtime="claude", session_id="11111111-2222-4333-8444-555555555555"
        )
        self.assertIn(desktop[0], spawned)
        self.assertNotIn(fresh[0], spawned)

    def test_starting_thread_is_left_to_its_own_resume_path(self):
        session = "019a0000-0000-7000-8000-0000000e5e1f"
        own = self._turns(session, "codex", [("Q.", "A.")])
        self._age(own, IDLE + 60)
        spawned = self._session_start(runtime="codex", session_id=session)
        self.assertEqual(1, spawned.count(own[0]), "resume path spawns once, idle sweep not again")


if __name__ == "__main__":
    unittest.main()

"""Hardening of idle finalize: nothing unprocessed is ever counted as settled.

Pinned here:

* a batch larger than the summarizer ceiling promotes whole turns only and
  leaves the rest pending, instead of clamping away the oldest turns;
* a checkpoint rewritten or added during a drain stays pending;
* crashes between the artifact write, the state write and the unlinks never
  mark an unpromoted turn as settled;
* a terminal boundary absorbs only the raw turns its transcript contains;
* a Codex lifecycle payload with ``transcript_path: null`` resolves its rollout
  by thread id, and is an empty boundary only when no rollout and no memory
  exist;
* memory finalized after a session started reaches it once, at its next prompt.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import io
import json
import time
from pathlib import Path
from unittest import mock

import test_idle_finalize as base

from memory_v1.adapters import (
    checkpoint_hook, drain_checkpoint, resolve_codex_rollout, settled_turn_digests,
)
from memory_v1.core import TRANSCRIPT_MAX_CHARS, session_key
from memory_v1.events import parse_event_artifact
from memory_v1.late_recall import deliver_late_recall
import memory_v1.adapters as adapters_module
import memory_v1.events as events_module
import memory_v1.hook_runner as hook_runner


def _uuid7(offset_ms: int = 0) -> str:
    stamp = f"{int(time.time() * 1000) + offset_ms:012x}"
    return f"{stamp[:8]}-{stamp[8:12]}-7abc-8def-0123456789ab"


class CallbackProvider(base.FakeProvider):
    def __init__(self, *responses, on_request=None):
        super().__init__(*responses)
        self.on_request = on_request

    def request(self, **kwargs):
        if self.on_request is not None:
            callback, self.on_request = self.on_request, None
            callback()
        return super().request(**kwargs)


class HardeningBase(base.IdleFinalizeBase):
    def _transcript_path(self, session_id: str, runtime: str = "codex") -> Path:
        return self.root / f"{runtime}-{session_key(session_id)[:8]}.jsonl"

    def _append(self, session_id: str, *records: tuple[str, str], runtime: str = "codex") -> Path:
        transcript = self._transcript_path(session_id, runtime)
        with transcript.open("a", encoding="utf-8") as handle:
            for role, text in records:
                handle.write(json.dumps({"role": role, "content": text}) + "\n")
        return transcript

    def _stop_turn(self, session_id: str, turn_id: str, runtime: str = "codex",
                   project: str | None = None) -> Path:
        return checkpoint_hook(
            self.config, runtime=runtime, project=project,
            payload={
                "session_id": session_id, "turn_id": turn_id,
                "transcript_path": str(self._transcript_path(session_id, runtime)),
                "hook_event_name": "Stop",
            },
        )


class BatchBoundaryTests(HardeningBase):
    def test_oversized_batch_promotes_whole_turns_and_keeps_the_rest(self):
        session = "sess-big"
        filler = "x" * 45_000
        paths = []
        for index in range(3):
            self._append(session, ("user", f"BIG-{index} question"), ("assistant", filler))
            paths.append(self._stop_turn(session, f"turn-{index}"))
        provider = base.FakeProvider(base._summary(context=["First two big turns."]))
        drain_checkpoint(self.config, paths[0], provider=provider)
        self.assertIn("BIG-0", provider.inputs[0])
        self.assertIn("BIG-1", provider.inputs[0])
        self.assertNotIn("BIG-2", provider.inputs[0], "the third turn exceeds the ceiling")
        self.assertLessEqual(len(provider.inputs[0]), TRANSCRIPT_MAX_CHARS + 200)
        self.assertFalse(paths[0].exists())
        self.assertFalse(paths[1].exists())
        self.assertTrue(paths[2].is_file(), "an unprocessed turn is not settled")
        key = session_key(session)
        third_digest = json.loads(paths[2].read_text(encoding="utf-8"))["source_digest"]
        self.assertNotIn(third_digest, settled_turn_digests(self.config, runtime="codex", state_key=key))

        follow_up = base.FakeProvider(base._summary(context=["Third big turn."]))
        event_path = drain_checkpoint(self.config, paths[2], provider=follow_up)
        self.assertIn("BIG-2", follow_up.inputs[0])
        self.assertEqual(
            ["First two big turns.", "Third big turn."],
            parse_event_artifact(event_path.read_text(encoding="utf-8"))["sections"]["context"],
        )
        self.assertEqual([], sorted(self.pending.glob("*.json")))

    def test_rewritten_and_new_checkpoints_during_a_drain_stay_pending(self):
        session = "sess-moving"
        self._append(session, ("user", "One."), ("assistant", "A."))
        first = self._stop_turn(session, "t1")
        self._append(session, ("user", "Two."), ("assistant", "B."))
        second = self._stop_turn(session, "t2")
        old_second_digest = json.loads(second.read_text(encoding="utf-8"))["source_digest"]

        def runtime_keeps_working():
            # The same turn is re-captured with more text, and a new turn lands.
            self._append(session, ("assistant", "B continued."))
            self._stop_turn(session, "t2")
            self._append(session, ("user", "Three."), ("assistant", "C."))
            self._stop_turn(session, "t3")

        provider = CallbackProvider(
            base._summary(context=["One and two."]), on_request=runtime_keeps_working
        )
        drain_checkpoint(self.config, first, provider=provider)
        self.assertFalse(first.exists())
        self.assertTrue(second.is_file(), "rewritten content was not processed")
        rewritten = json.loads(second.read_text(encoding="utf-8"))
        self.assertIn("B continued.", rewritten["normalized_transcript"])
        remaining = sorted(path.name for path in self.pending.glob("*.json"))
        self.assertEqual(2, len(remaining))
        settled = settled_turn_digests(self.config, runtime="codex", state_key=session_key(session))
        self.assertIn(old_second_digest, settled)
        self.assertNotIn(rewritten["source_digest"], settled)


class CrashWindowTests(HardeningBase):
    def test_crash_before_unlink_is_settled_without_re_promotion(self):
        session = "sess-crash-unlink"
        turns = self._turns(session, "codex", [("Alpha work.", "Done."), ("Beta work.", "Done.")])
        with mock.patch.object(adapters_module, "safe_unlink", side_effect=OSError("crash")):
            with self.assertRaises(OSError):
                drain_checkpoint(self.config, turns[0], provider=base.FakeProvider(
                    base._summary(decisions=["Alpha and beta."])
                ))
        self.assertTrue(all(path.is_file() for path in turns))
        self.assertEqual(2, len(settled_turn_digests(
            self.config, runtime="codex", state_key=session_key(session)
        )))
        later = self._turns(session, "codex", [("Gamma work.", "Done.")])
        provider = base.FakeProvider(base._summary(decisions=["Gamma."]))
        event_path = drain_checkpoint(self.config, turns[0], provider=provider)
        self.assertNotIn("Alpha work.", provider.inputs[0])
        self.assertIn("Gamma work.", provider.inputs[0])
        self.assertEqual(
            ["Alpha and beta.", "Gamma."],
            parse_event_artifact(event_path.read_text(encoding="utf-8"))["sections"]["decisions"],
        )
        self.assertFalse(later[0].exists())
        self.assertEqual([], sorted(self.pending.glob("*.json")))

    def test_crash_before_state_write_counts_nothing_as_settled(self):
        session = "sess-crash-state"
        turns = self._turns(session, "codex", [("Delta work.", "Done.")])
        real_atomic_json = events_module.atomic_json

        def failing_state_write(path, value, *args, **kwargs):
            if "sessions" in Path(path).parts:
                raise OSError("crash")
            return real_atomic_json(path, value, *args, **kwargs)

        with mock.patch.object(events_module, "atomic_json", side_effect=failing_state_write):
            with self.assertRaises(OSError):
                drain_checkpoint(self.config, turns[0], provider=base.FakeProvider(
                    base._summary(decisions=["Delta."])
                ))
        self.assertTrue(turns[0].is_file())
        self.assertEqual([], settled_turn_digests(
            self.config, runtime="codex", state_key=session_key(session)
        ))
        event_path = drain_checkpoint(
            self.config, turns[0], provider=base.FakeProvider(base._summary(decisions=["Delta."]))
        )
        self.assertEqual([event_path], self._dailies())
        self.assertEqual(
            ["Delta."],
            parse_event_artifact(event_path.read_text(encoding="utf-8"))["sections"]["decisions"],
        )
        self.assertFalse(turns[0].exists())


class TerminalAbsorbTests(HardeningBase):
    def test_terminal_absorbs_only_turns_its_transcript_contains(self):
        session = "sess-partial-terminal"
        foreign = self.root / "foreign.jsonl"
        foreign.write_text(
            json.dumps({"role": "user", "content": "Only in an earlier transcript."}) + "\n"
            + json.dumps({"role": "assistant", "content": "Old."}) + "\n",
            encoding="utf-8",
        )
        orphan = checkpoint_hook(self.config, runtime="codex", payload={
            "session_id": session, "transcript_path": str(foreign), "hook_event_name": "Stop",
        })
        self._append(session, ("user", "Current."), ("assistant", "Now."))
        contained = self._stop_turn(session, "current")
        end = checkpoint_hook(self.config, runtime="codex", payload={
            "session_id": session, "transcript_path": str(self._transcript_path(session)),
            "hook_event_name": "SessionEnd",
        })
        drain_checkpoint(self.config, end, provider=base.FakeProvider(base._summary(context=["End."])))
        self.assertFalse(end.exists())
        self.assertFalse(contained.exists())
        self.assertTrue(orphan.is_file(), "a turn the terminal transcript lacks stays pending")


class NullTranscriptTests(HardeningBase):
    def setUp(self) -> None:
        super().setUp()
        from memory_v1 import project_registry

        self.repo = self.root / "repo"
        self.repo.mkdir()
        project_registry.register(self.state, self.repo, "demo")

    def _rollout(self, thread_id: str) -> Path:
        created = dt.datetime.fromtimestamp(int(thread_id.replace("-", "")[:12], 16) / 1000)
        directory = self.root / f"{created:%Y}" / f"{created:%m}" / f"{created:%d}"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"rollout-{created:%Y-%m-%dT%H-%M-%S}-{thread_id}.jsonl"
        path.write_text(
            json.dumps({"role": "user", "content": "Desktop decision."}) + "\n"
            + json.dumps({"role": "assistant", "content": "Recorded."}) + "\n",
            encoding="utf-8",
        )
        return path

    def _hook(self, event: str, payload: dict) -> int:
        with mock.patch.object(hook_runner.MemoryConfig, "load", return_value=self.config), \
             mock.patch.object(hook_runner, "_spawn_drain"), \
             mock.patch("sys.stdin", io.StringIO(json.dumps(payload))), \
             contextlib.redirect_stdout(io.StringIO()):
            return hook_runner.main([
                "--config", str(self.root / "config.json"), "--runtime", "codex",
                "--event", event, "--project", "demo", "--project-root", str(self.repo),
            ])

    def _health(self) -> dict:
        return json.loads((self.state / "health" / "hook-codex.json").read_text(encoding="utf-8"))

    def test_rollout_is_resolved_exactly_by_thread_id(self):
        thread = _uuid7()
        rollout = self._rollout(thread)
        self.assertEqual(rollout, resolve_codex_rollout(self.config, thread))
        self.assertIsNone(resolve_codex_rollout(self.config, _uuid7(offset_ms=5)))
        self.assertIsNone(resolve_codex_rollout(self.config, "not-a-thread-id"))

    def test_session_end_without_transcript_path_checkpoints_the_rollout(self):
        thread = _uuid7()
        self._rollout(thread)
        rc = self._hook("SessionEnd", {
            "session_id": thread, "transcript_path": None, "cwd": str(self.repo),
            "hook_event_name": "SessionEnd", "reason": "other",
        })
        self.assertEqual(0, rc)
        created = sorted(self.pending.glob(f"codex-{session_key(thread)}-session_end-*.json"))
        self.assertEqual(1, len(created))
        self.assertIn("Desktop decision.", json.loads(created[0].read_text())["normalized_transcript"])

    def test_unresolvable_boundary_is_empty_only_without_memory(self):
        thread = _uuid7()
        rc = self._hook("SessionEnd", {
            "session_id": thread, "transcript_path": None, "cwd": str(self.repo),
            "hook_event_name": "SessionEnd", "reason": "other",
        })
        self.assertEqual(0, rc)
        self.assertEqual("lifecycle-empty:transcript-not-supplied", self._health()["detail"])

        with_memory = _uuid7(offset_ms=7)
        rollout = self._rollout(with_memory)
        checkpoint_hook(self.config, runtime="codex", payload={
            "session_id": with_memory, "transcript_path": str(rollout), "hook_event_name": "Stop",
        })
        rollout.unlink()
        rc = self._hook("SessionEnd", {
            "session_id": with_memory, "transcript_path": None, "cwd": str(self.repo),
            "hook_event_name": "SessionEnd", "reason": "other",
        })
        self.assertEqual(2, rc)
        self.assertEqual("blocked", self._health()["status"])


class LateRecallTests(HardeningBase):
    def setUp(self) -> None:
        super().setUp()
        from memory_v1 import project_registry

        self.repo = self.root / "repo"
        self.repo.mkdir()
        project_registry.register(self.state, self.repo, "demo")
        self.session = "22222222-3333-4444-8555-666666666666"

    def _run(self, event: str, payload: dict) -> str:
        out = io.StringIO()
        with mock.patch.object(hook_runner.MemoryConfig, "load", return_value=self.config), \
             mock.patch.object(hook_runner, "_spawn_drain"), \
             mock.patch("sys.stdin", io.StringIO(json.dumps(payload))), \
             contextlib.redirect_stdout(out):
            rc = hook_runner.main([
                "--config", str(self.root / "config.json"), "--runtime", "claude",
                "--event", event, "--project", "demo", "--project-root", str(self.repo),
            ])
        self.assertEqual(0, rc)
        return out.getvalue()

    def _start(self) -> None:
        transcript = self.root / "late-start.jsonl"
        transcript.write_text(
            json.dumps({"role": "user", "content": "Hi."}) + "\n"
            + json.dumps({"role": "assistant", "content": "Hello."}) + "\n",
            encoding="utf-8",
        )
        self._run("SessionStart", {
            "session_id": self.session, "transcript_path": str(transcript), "cwd": str(self.repo),
        })

    def _prompt(self) -> str:
        return self._run("UserPromptSubmit", {
            "session_id": self.session, "cwd": str(self.repo),
            "prompt": "Bu projede en son hangi karar alındı?",
        })

    def _idle_thread(self, session_id: str, project: str, text: str) -> Path:
        self._append(session_id, ("user", text), ("assistant", "Ok."))
        path = self._stop_turn(session_id, "only", project=project)
        self._age([path], base.IDLE + 60)
        return path

    def test_finalized_memory_reaches_the_session_once(self):
        same = self._idle_thread("sess-late-demo", "demo", "Demo decision.")
        other = self._idle_thread("sess-late-other", "other", "Other decision.")
        self._start()
        marker = self.state / "recall" / "late" / f"claude-{session_key(self.session)}.json"
        targets = json.loads(marker.read_text(encoding="utf-8"))["targets"]
        self.assertEqual([same.name], [target["checkpoint"] for target in targets])

        first = self._prompt()
        self.assertIn("henüz hafızaya işleniyor", first)
        self.assertNotIn("henüz hafızaya işleniyor", self._prompt(), "notice is shown once")

        drain_checkpoint(self.config, same, provider=base.FakeProvider(
            base._summary(decisions=["LATE-CANARY-7f3a demo decision."])
        ))
        delivered = self._prompt()
        self.assertIn("LATE-CANARY-7f3a", delivered)
        self.assertIn("DERIVED MEMORY", delivered)
        self.assertFalse(marker.exists())
        evidence = json.loads(
            (self.state / "evidence" / "late-recall-claude.json").read_text(encoding="utf-8")
        )
        self.assertEqual(session_key(self.session), evidence["session_key"])
        self.assertIn("LATE-CANARY-7f3a", evidence["text"])
        self.assertNotIn("LATE-CANARY-7f3a", self._prompt(), "delivered once")
        self.assertTrue(other.is_file())

    def test_expired_marker_delivers_nothing(self):
        self._idle_thread("sess-late-expired", "demo", "Expired decision.")
        self._start()
        later = dt.datetime.now().astimezone() + dt.timedelta(hours=3)
        self.assertEqual("", deliver_late_recall(
            self.config, runtime="claude", session_id=self.session, now=later
        ))
        self.assertFalse(
            (self.state / "recall" / "late" / f"claude-{session_key(self.session)}.json").exists()
        )

    def test_failed_finalize_is_announced(self):
        path = self._idle_thread("sess-late-failed", "demo", "Failing decision.")
        self._start()
        from memory_v1.core import ProviderBlocked
        with self.assertRaises(ProviderBlocked):
            drain_checkpoint(self.config, path, provider=base.FakeProvider(
                ProviderBlocked("claude-timeout")
            ))
        # A timeout is retryable: the thread stays tracked, announced once.
        self.assertIn("yeniden denenecek", self._prompt())


if __name__ == "__main__":
    import unittest

    unittest.main()

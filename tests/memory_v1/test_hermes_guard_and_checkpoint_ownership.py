"""Two defects found re-reviewing add4810, each pinned before it was fixed.

1. ``PZ_MEMORY_INTERNAL_CALL`` is a single process-wide flag saved and restored
   per call.  Two overlapping summaries interleave their save/restore, so the
   flag survives both of them and every later lifecycle event in that process
   is treated as a recursive internal call forever.  A deferral record is not a
   fix: nothing would ever run the live path again.
2. Turn checkpoints are named by session id alone.  Two profiles holding the
   same session id each stage their own checkpoint, and settling one of them
   deletes both -- the other profile's raw turn is destroyed before it was ever
   summarized.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import types
import unittest
from pathlib import Path
from unittest import mock

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from tests.memory_v1.test_hermes_finalize_retry import HermesPluginFixture


class InternalCallGuardLifetimeTests(HermesPluginFixture, unittest.TestCase):
    """The guard must cover exactly the calls it protects, and not outlive them."""

    def _blocking_llm(self, entered: threading.Semaphore, release: threading.Event):
        facade = mock.Mock()

        def complete_structured(**kwargs):
            entered.release()
            if not release.wait(5):
                raise TimeoutError("test release never arrived")
            return types.SimpleNamespace(
                parsed={"status": "empty"}, provider="openai-codex", model="gpt-5.6-luna",
            )

        facade.complete_structured.side_effect = complete_structured
        module = types.ModuleType("agent.plugin_llm")
        module.PluginLlm = mock.Mock(return_value=facade)
        module.PluginLlmTextInput = lambda **kw: kw
        return module

    def test_overlapping_summaries_do_not_strand_the_guard(self) -> None:
        entered = threading.Semaphore(0)
        release = threading.Event()
        module = self._blocking_llm(entered, release)
        results: list[tuple] = []

        def worker():
            results.append(self.plugin._summarize_with_hermes("transcript"))

        with mock.patch.dict(sys.modules, {"agent.plugin_llm": module}), \
             mock.patch.dict(os.environ, self.env):
            os.environ.pop("PZ_MEMORY_INTERNAL_CALL", None)
            threads = [threading.Thread(target=worker) for _ in range(2)]
            for thread in threads:
                thread.start()
            try:
                for _ in threads:
                    self.assertTrue(entered.acquire(timeout=5), "summarizer never started")
                # A different native thread remains able to capture while both summaries run.
                self.assertFalse(self.plugin._is_internal_call())
            finally:
                release.set()
                for thread in threads:
                    thread.join(5)

            self.assertEqual(len(results), 2)
            self.assertFalse(
                self.plugin._is_internal_call(),
                "the guard outlived both summaries and now blocks every live event",
            )
            self.assertNotIn("PZ_MEMORY_INTERNAL_CALL", os.environ)

    def test_a_pre_existing_guard_value_is_restored_not_cleared(self) -> None:
        module = self._blocking_llm(threading.Semaphore(0), threading.Event())
        module.PluginLlm.return_value.complete_structured.side_effect = None
        module.PluginLlm.return_value.complete_structured.return_value = types.SimpleNamespace(
            parsed={"status": "empty"}, provider="p", model="m",
        )
        env = dict(self.env, PZ_MEMORY_INTERNAL_CALL="1")
        with mock.patch.dict(sys.modules, {"agent.plugin_llm": module}), \
             mock.patch.dict(os.environ, env):
            self.plugin._summarize_with_hermes("transcript")
            self.assertEqual(os.environ.get("PZ_MEMORY_INTERNAL_CALL"), "1")

    def test_a_live_event_after_a_summary_is_handled_normally(self) -> None:
        module = self._blocking_llm(threading.Semaphore(0), threading.Event())
        module.PluginLlm.return_value.complete_structured.side_effect = None
        module.PluginLlm.return_value.complete_structured.return_value = types.SimpleNamespace(
            parsed={"status": "empty"}, provider="p", model="m",
        )
        with mock.patch.dict(sys.modules, {"agent.plugin_llm": module}), \
             mock.patch.dict(os.environ, self.env):
            os.environ.pop("PZ_MEMORY_INTERNAL_CALL", None)
            threads = [threading.Thread(target=self.plugin._summarize_with_hermes, args=("t",))
                       for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(5)

        with self._runtime(), mock.patch.dict(os.environ, self.env):
            self.plugin.on_session_end(session_id=self.session_id)
        checkpoints = list((self.state / "checkpoints").glob("*.json"))
        self.assertEqual(len(checkpoints), 1, "the live session was still treated as internal")
        self.assertEqual(self._retry_records(), [])


class CheckpointOwnershipTests(HermesPluginFixture, unittest.TestCase):
    """Raw turns belong to the database they came from."""

    def setUp(self) -> None:
        super().setUp()
        self.other_messages = [
            {"role": "user", "content": "başka profilin sorusu"},
            {"role": "assistant", "content": "başka profilin cevabı"},
        ]
        self.other_db = self._add_session(
            "pz-engineering", self.session_id, self.other_messages, ended=True,
        )

    def _checkpoints(self) -> list[dict]:
        return [
            json.loads(path.read_text(encoding="utf-8"))
            for path in sorted((self.state / "checkpoints").glob("*.json"))
        ]

    def _stage_both(self) -> None:
        with self._runtime(), mock.patch.dict(os.environ, self.env):
            self.plugin.on_session_end(session_id=self.session_id)
        with self._runtime(active_profile="pz-engineering"), \
             mock.patch.dict(os.environ, self.env):
            self.plugin.on_session_end(session_id=self.session_id)

    def test_each_profile_stages_its_own_turn(self) -> None:
        self._stage_both()
        staged = self._checkpoints()
        self.assertEqual(len(staged), 2)
        self.assertEqual(
            {"başka profilin sorusu" in item["normalized_transcript"] for item in staged},
            {True, False},
        )

    def test_settling_one_profile_keeps_the_other_profiles_turn(self) -> None:
        self._stage_both()
        with self._runtime(), mock.patch.dict(os.environ, self.env):
            self.plugin.on_session_finalize(session_id=self.session_id)

        remaining = self._checkpoints()
        self.assertEqual(
            len(remaining), 1,
            "settling one profile destroyed the other profile's unsummarized turn",
        )
        self.assertIn("başka profilin", remaining[0]["normalized_transcript"])

    def test_each_profile_produces_its_own_artifact(self) -> None:
        self._stage_both()
        with self._runtime(), mock.patch.dict(os.environ, self.env):
            self.plugin.on_session_finalize(session_id=self.session_id)
        with self._runtime(active_profile="pz-engineering"), \
             mock.patch.dict(os.environ, self.env):
            self.plugin.on_session_finalize(session_id=self.session_id)

        events = self._events()
        self.assertEqual(len(events), 2, "two distinct sessions collapsed into one artifact")
        bodies = [path.read_text(encoding="utf-8") for path in events]
        self.assertEqual(len({text.split("source_sha256:")[1][:70] for text in bodies}), 2)

    def test_a_later_turn_survives_an_earlier_settlement(self) -> None:
        """Even within one profile, settling turn 1 must not delete turn 2."""
        with self._runtime(), mock.patch.dict(os.environ, self.env):
            self.plugin.on_session_end(session_id=self.session_id)
        key = str((self.profiles / "pz-orchestrator" / "state.db").resolve())
        settled_transcript_messages = list(self.messages)
        self.databases[key][0]["messages"] = settled_transcript_messages + [
            {"role": "user", "content": "sonraki tur sorusu"},
            {"role": "assistant", "content": "sonraki tur cevabı"},
        ]
        with self._runtime(), mock.patch.dict(os.environ, self.env):
            # A second completed turn is staged before the first one settles.
            self.plugin.on_session_end(session_id=self.session_id)
        self.assertEqual(len(self._checkpoints()), 2)

        # Settle only the first turn's source.
        with self._runtime(), mock.patch.dict(os.environ, self.env):
            self.plugin._settle_source(
                session_id=self.session_id,
                transcript=self._transcript(settled_transcript_messages),
                source_sha=self._source_sha(settled_transcript_messages),
                model="gpt-6-astra", task_id="task-1", redactions=0,
                hook_event="session_finalize",
                database=key,
                profile="pz-orchestrator",
            )

        remaining = self._checkpoints()
        self.assertEqual(len(remaining), 1, "an unsettled later turn was deleted")
        self.assertIn("sonraki tur", remaining[0]["normalized_transcript"])


if __name__ == "__main__":
    unittest.main()

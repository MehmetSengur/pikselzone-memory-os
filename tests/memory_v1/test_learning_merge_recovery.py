"""A learning merge interrupted at any point is completed once, never twice.

Each case stops the real merge at one point (fault injection), then runs the
normal merge again, and checks the shared files and the ledger.
"""
from __future__ import annotations

import json
import multiprocessing
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from memory_v1 import learning_inbox as li
from memory_v1 import provenance as pv
from memory_v1.companion import CompanionManager
from memory_v1.core import MemoryConfig
from memory_v1.rule_learner import RuleLearner

PREFERENCE = "Çünkü artık terminal değilde uygulamalara dönmek istiyorum."


class Crash(Exception):
    pass


def _configs(root: Path):
    vault = root / "vault"
    (vault / "companion").mkdir(parents=True, exist_ok=True)
    mac = MemoryConfig.from_dict({
        "role": "workstation", "vault_path": str(vault), "state_path": str(root / "mac-state"),
        "runtimes": ["claude", "codex"], "transcript_roots": {"claude": [str(root)], "codex": [str(root)]},
        "can_write_event_memory": True, "can_run_compiler": False,
        "models": {"flush": "gpt-5.6-luna", "compiler": "gpt-5.6-terra"}, "provider": {"mode": "runtime-native"},
    })
    vps = MemoryConfig.from_dict({
        "role": "memory-engine", "vault_path": str(vault), "state_path": str(root / "vps-state"),
        "runtimes": ["hermes"], "transcript_roots": {"hermes": [str(root)]},
        "can_write_event_memory": True, "can_run_compiler": True, "provider": {"mode": "runtime-native"},
    })
    return vault, mac, vps


def _merge_in_process(root: str, barrier_path: str) -> None:
    """Runs in a separate process: wait for the start signal, then merge."""
    _, _, vps = _configs(Path(root))
    while not Path(barrier_path).exists():
        time.sleep(0.01)
    li.merge_learning_inbox(vps)


class MergeRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        self.vault, self.mac, self.vps = _configs(self.root)
        CompanionManager(self.vault).ensure_companion_files()
        self.journal = self.vault / "companion" / "Journal.md"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _journal(self, text="Harness saat kanıtı düzeltildi.", session="claude-a"):
        li.record_journal(self.mac, CompanionManager(self.vault), title="Session End Özeti", narrative=text,
                          runtime="claude", source_session=session)

    def _learn(self, text, session):
        RuleLearner(CompanionManager(self.vault), sink=li.learning_sink(self.mac, "claude")).learn_from_transcript(
            [("user", text)], source_session=session)

    def _ledger(self):
        path = li.ledger_path(self.vps)
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.is_file() else []

    def _count(self, needle):
        return self.journal.read_text(encoding="utf-8").count(needle)

    # 1. stopped before anything was applied
    def test_crash_before_apply_is_applied_on_the_next_run(self):
        self._journal()
        with mock.patch.object(li, "_apply", side_effect=Crash):
            with self.assertRaises(Crash):
                li.merge_learning_inbox(self.vps)
        self.assertEqual(0, self._count("Harness saat kanıtı düzeltildi."))
        self.assertEqual(["intent"], [e["phase"] for e in self._ledger()])
        self.assertEqual(1, len(li.pending_observations(self.vps)))  # the file is still there

        li.merge_learning_inbox(self.vps)
        self.assertEqual(1, self._count("Harness saat kanıtı düzeltildi."))
        self.assertEqual("recovered:journal-appended", self._ledger()[-1]["outcome"])
        self.assertEqual([], li.pending_observations(self.vps))

    # 2. stopped after the effect was written, before the commit record
    def _crash_on_commit(self):
        real = li._append_ledger

        def append(config, obs, outcome, *, phase="committed"):
            if phase == "committed":
                raise Crash
            return real(config, obs, outcome, phase=phase)
        return mock.patch.object(li, "_append_ledger", side_effect=append)

    def test_journal_written_but_not_committed_is_not_appended_twice(self):
        self._journal()
        with self._crash_on_commit(), self.assertRaises(Crash):
            li.merge_learning_inbox(self.vps)
        self.assertEqual(1, self._count("Harness saat kanıtı düzeltildi."))
        li.merge_learning_inbox(self.vps)
        self.assertEqual(1, self._count("Harness saat kanıtı düzeltildi."))
        self.assertEqual("recovered:journal-already-present", self._ledger()[-1]["outcome"])

    def test_candidate_written_but_not_committed_does_not_gain_a_second_session(self):
        self._learn(PREFERENCE, "claude-a")
        with self._crash_on_commit(), self.assertRaises(Crash):
            li.merge_learning_inbox(self.vps)
        li.merge_learning_inbox(self.vps)
        [candidate] = CompanionManager(self.vault).read_rule_candidates()
        self.assertEqual(["claude-a"], candidate.sources)
        self.assertEqual("recovered:candidate-same-session", self._ledger()[-1]["outcome"])

    def test_promotion_written_but_not_committed_is_not_repeated(self):
        self._learn(PREFERENCE, "claude-a")
        li.merge_learning_inbox(self.vps)
        self._learn(PREFERENCE, "codex-b")
        with self._crash_on_commit(), self.assertRaises(Crash):
            li.merge_learning_inbox(self.vps)
        companion = CompanionManager(self.vault)
        self.assertEqual(1, [r.text for r in companion.read_rules()].count(PREFERENCE))
        li.merge_learning_inbox(self.vps)
        self.assertEqual(1, [r.text for r in companion.read_rules()].count(PREFERENCE))
        self.assertEqual([], companion.read_rule_candidates())
        self.assertTrue(self._ledger()[-1]["outcome"].startswith("recovered:"))

    def test_promotion_is_a_single_write(self):
        # Two writes (remove candidate, then add rule) could lose both on a crash between them.
        self._learn(PREFERENCE, "claude-a")
        li.merge_learning_inbox(self.vps)
        self._learn(PREFERENCE, "codex-b")
        from memory_v1 import companion as companion_module
        with mock.patch.object(companion_module, "atomic_write", wraps=companion_module.atomic_write) as write:
            li.merge_learning_inbox(self.vps)
        rules_writes = [c for c in write.call_args_list if str(c.args[0]).endswith("Kurallar.md")]
        self.assertEqual(1, len(rules_writes))

    def test_replayed_replacement_does_not_replace_a_second_rule(self):
        companion = CompanionManager(self.vault)
        learner = RuleLearner(companion)
        learner.learn_from_transcript([("user", "Bundan sonra testleri unittest framework'ü ile yaz.")], source_session="s1")
        learner.learn_from_transcript([("user", "Bundan sonra testleri unittest yerine pytest ile yaz.")], source_session="s2")
        before = (self.vault / "companion" / "Kurallar.md").read_text(encoding="utf-8")
        from memory_v1.rule_learner import ExtractedRule
        item = ExtractedRule(rule_text="Bundan sonra testleri unittest yerine pytest ile yaz.", reason="r",
                             is_explicit=True, confidence=0.95, source_turn="", intent=pv.DURABLE_DIRECTIVE)
        self.assertEqual("duplicate-active", learner.apply_rule(item, "s2"))
        self.assertEqual(before, (self.vault / "companion" / "Kurallar.md").read_text(encoding="utf-8"))

    # 3. committed, then stopped before the inbox file was removed
    def test_crash_after_commit_before_inbox_cleanup(self):
        self._journal()
        with mock.patch.object(li, "safe_unlink", side_effect=Crash), self.assertRaises(Crash):
            li.merge_learning_inbox(self.vps)
        self.assertEqual(1, len(li.pending_observations(self.vps)))
        result = li.merge_learning_inbox(self.vps)
        self.assertEqual({"duplicate-delivery": 1}, result["counts"])
        self.assertEqual(1, self._count("Harness saat kanıtı düzeltildi."))
        self.assertEqual([], li.pending_observations(self.vps))

    # 4. the same observation delivered again later
    def test_redelivery_after_commit(self):
        self._journal()
        [path] = li.pending_observations(self.mac)
        payload = path.read_text(encoding="utf-8")
        li.merge_learning_inbox(self.vps)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(payload, encoding="utf-8")
        self.assertEqual({"duplicate-delivery": 1}, li.merge_learning_inbox(self.vps)["counts"])
        self.assertEqual(1, self._count("Harness saat kanıtı düzeltildi."))

    def test_two_real_sessions_promote_and_redelivery_does_not_count(self):
        self._learn(PREFERENCE, "claude-a")
        [first] = li.pending_observations(self.mac)
        payload = first.read_text(encoding="utf-8")
        li.merge_learning_inbox(self.vps)
        first.parent.mkdir(parents=True, exist_ok=True)
        first.write_text(payload, encoding="utf-8")   # same session again
        li.merge_learning_inbox(self.vps)
        self.assertEqual(["claude-a"], CompanionManager(self.vault).read_rule_candidates()[0].sources)
        self._learn(PREFERENCE, "codex-b")          # a genuinely different session
        li.merge_learning_inbox(self.vps)
        self.assertIn(PREFERENCE, [r.text for r in CompanionManager(self.vault).read_rule_candidates()] +
                      [r.text for r in CompanionManager(self.vault).read_rules()])
        self.assertIn(PREFERENCE, [r.text for r in CompanionManager(self.vault).read_rules()])

    # 5. two merges at the same time
    def test_concurrent_merges_apply_each_observation_once(self):
        self._journal("Birinci eşzamanlı gözlem.", "claude-a")
        self._journal("İkinci eşzamanlı gözlem.", "codex-b")
        barrier = self.root / "go"
        ctx = multiprocessing.get_context("spawn")
        workers = [ctx.Process(target=_merge_in_process, args=(str(self.root), str(barrier))) for _ in range(2)]
        for worker in workers:
            worker.start()
        time.sleep(0.5)
        barrier.write_text("go", encoding="utf-8")
        for worker in workers:
            worker.join(60)
            self.assertEqual(0, worker.exitcode)
        self.assertEqual(1, self._count("Birinci eşzamanlı gözlem."))
        self.assertEqual(1, self._count("İkinci eşzamanlı gözlem."))
        committed = [e for e in self._ledger() if e["phase"] == "committed" and not e["outcome"].startswith("duplicate")]
        self.assertEqual(2, len(committed))

    def test_maintenance_waits_for_a_running_merge(self):
        from memory_v1.memory_repair import retire_rule_candidate
        self._learn(PREFERENCE, "claude-a")
        li.merge_learning_inbox(self.vps)
        done = threading.Event()

        def retire():
            retire_rule_candidate(self.vps, PREFERENCE, classification="c", evidence="e")
            done.set()

        with li.merge_lock(self.vps):
            thread = threading.Thread(target=retire)
            thread.start()
            time.sleep(0.5)
            self.assertFalse(done.is_set())  # blocked behind the merge lock
        thread.join(10)
        self.assertTrue(done.is_set())

    def test_existing_ledger_entries_without_phase_stay_committed(self):
        path = li.ledger_path(self.vps)
        path.parent.mkdir(parents=True, exist_ok=True)
        obs = li.Observation(kind="journal", text="Eski format kayıt.", source_session="claude-old", runtime="claude",
                             origin_host="h", observed_at="t", reason="x")
        path.write_text(json.dumps({"schema": li.LEDGER_SCHEMA, "obs_id": obs.obs_id, "outcome": "journal-appended"}) + "\n",
                        encoding="utf-8")
        li.write_observation(self.mac, obs)
        self.assertEqual({"duplicate-delivery": 1}, li.merge_learning_inbox(self.vps)["counts"])


if __name__ == "__main__":
    unittest.main()

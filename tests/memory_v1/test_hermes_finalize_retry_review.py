"""Regressions for the seven defects found reviewing 108e351.

Each test pins one way the first finalize-retry implementation could still lose
work, spend the provider twice, or report a backlog it could not actually
account for:

1. the recovered artifact used an event name the event contract rejects, so the
   publisher could never promote it;
2. the summarizer's process-wide recursion guard made a concurrent live session
   drop its own raw turn and its own finalize;
3. session identity ignored the owning database, so two profiles holding the
   same session id shared one settlement record and one lock;
4. a run spent its whole budget on records it only skipped, and never
   re-checked state after taking the lock;
5. the doctor counted a session settled even when a newer checkpoint proved the
   settlement did not cover it;
6. when the publisher had already taken the staged artifact, a failed
   settlement made recovery summarize and publish the same session again;
7. a subscription quota outlives the generic backoff horizon, and nothing moved
   a due record forward when no session event ever arrived.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from memory_v1.doctor import _hermes_finalize_rows
from memory_v1.events import parse_event_artifact
from memory_v1.publisher import publish_outbox
from tests.memory_v1.test_hermes_finalize_retry import (
    USAGE_LIMIT_ERROR, HermesPluginFixture, load_retry,
)


def _later() -> dt.datetime:
    return dt.datetime.now().astimezone() + dt.timedelta(hours=2)


class RecoveredArtifactContractTests(HermesPluginFixture, unittest.TestCase):
    """Finding 1: a recovered artifact must satisfy the real event contract."""

    def _fail_then_recover(self) -> None:
        with self._runtime(raises=USAGE_LIMIT_ERROR), mock.patch.dict(os.environ, self.env):
            self.plugin.on_session_end(session_id=self.session_id)
            self.plugin.on_session_finalize(session_id=self.session_id)
        with self._runtime(), mock.patch.dict(os.environ, self.env):
            outcome = self.plugin.run_due_finalize_retries(now=_later())
        self.assertEqual(outcome["outcomes"], {"settled": 1})

    def test_recovered_artifact_parses_under_the_event_contract(self) -> None:
        self._fail_then_recover()
        text = self._events()[0].read_text(encoding="utf-8")
        parsed = parse_event_artifact(text)
        self.assertEqual(parsed["event"], "checkpoint_recovery")
        self.assertIn("checkpoint_recovery", parsed["events_seen"])

    def test_publisher_promotes_the_recovered_artifact(self) -> None:
        self._fail_then_recover()
        results = publish_outbox(self._config())
        self.assertEqual([item["status"] for item in results], ["published"])
        published = Path(results[0]["target"])
        self.assertTrue(published.is_file())
        self.assertEqual(self._events(), [])

    def test_recovery_provenance_survives_the_contract_fix(self) -> None:
        self._fail_then_recover()
        evidence = json.loads(
            sorted((self.base / "outbox" / "evidence").glob("hermes-*.json"))[0]
            .read_text(encoding="utf-8")
        )
        self.assertEqual(evidence["provenance"], "hermes-retry-recovery")
        self.assertEqual(evidence["status"], "unverified")


class RecursionGuardIsolationTests(HermesPluginFixture, unittest.TestCase):
    """Finding 2: a background summarizer must not mute live session hooks."""

    def test_live_turn_is_still_captured_during_a_background_summary(self) -> None:
        # The recovery worker sets this process-wide while it calls the model.
        env = dict(self.env, PZ_MEMORY_INTERNAL_CALL="1")
        with self._runtime(), mock.patch.dict(os.environ, env):
            self.plugin.on_session_end(session_id=self.session_id)
        self.assertTrue(
            list((self.state / "checkpoints").glob("*.json")),
            "a live completed turn was dropped because recovery held the guard",
        )

    def test_finalize_suppressed_by_the_guard_is_recorded_not_lost(self) -> None:
        env = dict(self.env, PZ_MEMORY_INTERNAL_CALL="1")
        with self._runtime(), mock.patch.dict(os.environ, env):
            self.plugin.on_session_finalize(session_id=self.session_id)
        records = self._retry_records()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["reason_code"], "guard-deferred")
        self.assertEqual(records[0]["status"], "retry-scheduled")

    def test_guard_still_blocks_a_recursive_summarizer_call(self) -> None:
        env = dict(self.env, PZ_MEMORY_INTERNAL_CALL="1")
        with self._runtime(), mock.patch.dict(os.environ, env):
            self.plugin.on_session_finalize(session_id=self.session_id)
        self.assertEqual(self.llm_calls, [], "the recursion guard must still hold")


class ProfileIdentityTests(HermesPluginFixture, unittest.TestCase):
    """Finding 3: identity is (database, session), not session alone."""

    def setUp(self) -> None:
        super().setUp()
        self.other_messages = [
            {"role": "user", "content": "başka profil sorusu"},
            {"role": "assistant", "content": "başka profil cevabı"},
        ]
        self.other_db = self._add_session(
            "pz-engineering", self.session_id, self.other_messages, ended=True,
        )

    def test_each_profile_keeps_its_own_settlement(self) -> None:
        with self._runtime(raises=USAGE_LIMIT_ERROR), mock.patch.dict(os.environ, self.env):
            self.plugin.on_session_end(session_id=self.session_id)
            self.plugin.on_session_finalize(session_id=self.session_id)
        with self._runtime(active_profile="pz-engineering"), \
             mock.patch.dict(os.environ, self.env):
            self.plugin.on_session_finalize(session_id=self.session_id)
        other_sha = self._source_sha(self.other_messages)
        with mock.patch.dict(os.environ, self.env):
            self.assertTrue(self.plugin._is_source_settled(self.session_id, other_sha))

        with self._runtime(), mock.patch.dict(os.environ, self.env):
            self.plugin.run_due_finalize_retries(now=_later())

        # Recovering the orchestrator session must not erase the engineering
        # session's settlement; losing it would let that session be summarized
        # a second time.
        self.plugin._IN_MEMORY_SETTLED.clear()
        with mock.patch.dict(os.environ, self.env):
            self.assertTrue(
                self.plugin._is_source_settled(self.session_id, other_sha),
                "the other profile's settlement was overwritten",
            )
            self.assertTrue(
                self.plugin._is_source_settled(self.session_id, self._source_sha()),
            )

    def test_a_lock_in_one_profile_does_not_block_the_other(self) -> None:
        with mock.patch.dict(os.environ, self.env):
            self.assertTrue(
                self.plugin._acquire_execution_lock(
                    self.session_id, database=str(self.other_db.resolve()),
                )
            )
            try:
                with self._runtime(raises=USAGE_LIMIT_ERROR):
                    self.plugin.on_session_end(session_id=self.session_id)
                    self.plugin.on_session_finalize(session_id=self.session_id)
            finally:
                self.plugin._release_execution_lock(
                    self.session_id, database=str(self.other_db.resolve()),
                )
        # The orchestrator finalize ran and recorded its own failure.
        self.assertEqual(len(self._retry_records()), 1)


class SelectionProgressTests(HermesPluginFixture, unittest.TestCase):
    """Finding 4: skipped work must not consume the run, and state is
    re-validated once the lock is held."""

    def test_skipped_records_do_not_starve_an_eligible_one(self) -> None:
        blocked = []
        for index in range(2):
            session_id = f"20260915_11000{index}_blocked{index}"
            self._add_session(
                "pz-orchestrator", session_id,
                [{"role": "user", "content": f"soru {index}"},
                 {"role": "assistant", "content": f"cevap {index}"}],
                ended=True,
            )
            blocked.append(session_id)
        with self._runtime(raises=USAGE_LIMIT_ERROR), mock.patch.dict(os.environ, self.env):
            for session_id in blocked:
                self.plugin.on_session_finalize(session_id=session_id)
            self.plugin.on_session_finalize(session_id=self.session_id)
        self.assertEqual(len(self._retry_records()), 3)

        # The two older ones are no longer eligible: the user reopened them.
        key = str((self.profiles / "pz-orchestrator" / "state.db").resolve())
        for row in self.databases[key]:
            if row["id"] in blocked:
                row["ended_at"] = None

        with self._runtime(), mock.patch.dict(os.environ, self.env):
            result = self.plugin.run_due_finalize_retries(now=_later())

        self.assertEqual(result["outcomes"].get("settled"), 1, result["outcomes"])
        self.assertEqual(result["outcomes"].get("session-active"), 2, result["outcomes"])

    def test_state_is_revalidated_after_the_lock_is_taken(self) -> None:
        with self._runtime(raises=USAGE_LIMIT_ERROR), mock.patch.dict(os.environ, self.env):
            self.plugin.on_session_end(session_id=self.session_id)
            self.plugin.on_session_finalize(session_id=self.session_id)

        original = self.plugin._acquire_execution_lock
        source_sha = self._source_sha()

        def racing_lock(session_id, locks_dir=None, database=""):
            acquired = original(session_id, locks_dir, database=database)
            # A live finalize settled this exact source while we queued.
            self.plugin._mark_durable_settlement(
                session_id, source_sha, status="staged-event",
                event_path="/tmp/already-there.md", database=database,
            )
            return acquired

        calls_before = len(self.llm_calls)
        with self._runtime(), mock.patch.dict(os.environ, self.env), \
             mock.patch.object(self.plugin, "_acquire_execution_lock", racing_lock):
            result = self.plugin.run_due_finalize_retries(now=_later())

        self.assertEqual(result["outcomes"], {"already-settled": 1})
        self.assertEqual(len(self.llm_calls), calls_before)
        self.assertEqual(self._retry_records(), [])


class DoctorCoverageTests(HermesPluginFixture, unittest.TestCase):
    """Finding 5: an older settlement does not account for a newer checkpoint."""

    def _write_checkpoint(self, session_id: str, observed_at: str, digest: str) -> None:
        session_hash = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:32]
        (self.state / "checkpoints" / f"hermes-{session_hash}-{digest[:16]}.json").write_text(
            json.dumps({
                "schema": "pikselzone-memory-turn-checkpoint-v2",
                "runtime": "hermes", "session_id": session_id,
                "turn_digest": digest, "normalized_transcript": "USER: x\nASSISTANT: y",
                "observed_at": observed_at,
            }), encoding="utf-8",
        )

    def _write_settlement(self, session_id: str, settled_at: str) -> None:
        session_hash = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:32]
        directory = self.state / "settlements"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"hermes-{session_hash}.json").write_text(
            json.dumps({
                "schema": "pikselzone-memory-hermes-settlement-v1",
                "session_id": session_id, "source_sha256": "c" * 64,
                "status": "staged-event", "settled_at": settled_at,
                "event_path": "/srv/pz-hermes/vault/daily/2026-09-14/x.md",
            }), encoding="utf-8",
        )

    def test_checkpoint_newer_than_its_settlement_is_not_counted_as_settled(self) -> None:
        session_id = "20260914_195907_5a80e1"
        self._write_settlement(session_id, "2026-09-14T20:03:00+03:00")
        self._write_checkpoint(session_id, "2026-09-14T20:07:00+03:00", "d" * 64)

        rows = {row["check"]: row for row in _hermes_finalize_rows(self._config())}
        detail = rows["hermes_finalize_backlog"]["detail"]
        self.assertEqual(rows["hermes_finalize_backlog"]["status"], "warn", detail)
        self.assertIn("stale_after_settlement=1", detail)

    def test_checkpoint_older_than_its_settlement_stays_quiet(self) -> None:
        session_id = "20260914_210923_cb56da"
        self._write_checkpoint(session_id, "2026-09-14T21:10:00+03:00", "e" * 64)
        self._write_settlement(session_id, "2026-09-14T21:14:00+03:00")

        rows = {row["check"]: row for row in _hermes_finalize_rows(self._config())}
        self.assertEqual(rows["hermes_finalize_backlog"]["status"], "pass",
                         rows["hermes_finalize_backlog"]["detail"])


class PublishedArtifactTests(HermesPluginFixture, unittest.TestCase):
    """Finding 6: the publisher may take the artifact before settlement lands."""

    def test_settlement_retry_after_publication_neither_resummarizes_nor_republishes(self) -> None:
        with self._runtime(), mock.patch.dict(os.environ, self.env), \
             mock.patch.object(self.plugin, "_mark_durable_settlement", return_value=False):
            self.plugin.on_session_end(session_id=self.session_id)
            self.plugin.on_session_finalize(session_id=self.session_id)

        staged = self._events()
        self.assertEqual(len(staged), 1)
        record = self._retry_records()[0]
        self.assertEqual(record["reason_code"], "settlement-write")
        self.assertTrue(record.get("artifact_produced"), "the staged artifact was not recorded")

        # The publisher promoted it to the vault and removed the outbox copy.
        staged[0].unlink()

        calls_before = len(self.llm_calls)
        with self._runtime(), mock.patch.dict(os.environ, self.env):
            result = self.plugin.run_due_finalize_retries(now=_later())

        self.assertEqual(result["outcomes"], {"settled": 1})
        self.assertEqual(len(self.llm_calls), calls_before, "the session was summarized twice")
        self.assertEqual(self._events(), [], "a second artifact was staged for one source")
        settlement = json.loads(
            self._settlement_path(self.session_id).read_text(encoding="utf-8")
        )
        self.assertEqual(settlement["status"], "staged-event")
        self.assertEqual(self._retry_records(), [])


class QuotaHorizonAndProgressTests(HermesPluginFixture, unittest.TestCase):
    """Finding 7: quota waits outlive the generic bound, and a due record must
    move even when no session event ever arrives."""

    def test_quota_waits_longer_than_a_generic_transient_failure(self) -> None:
        retry = load_retry()
        base = str(self.base)
        now = dt.datetime(2026, 9, 16, 12, 0, 0).astimezone()

        quota = None
        for _ in range(5):
            quota = retry.record_failure(
                base, session_id="quota", source_sha="a" * 64,
                exc=USAGE_LIMIT_ERROR, now=now,
            )
        self.assertNotEqual(
            quota["status"], "retry-exhausted",
            "a daily quota window outlives five short attempts",
        )
        scheduled = retry.parse_iso(quota["next_attempt_after"])
        self.assertIsNotNone(scheduled)
        self.assertGreaterEqual((scheduled - now).total_seconds(), 3600)

        generic = None
        for _ in range(5):
            generic = retry.record_failure(
                base, session_id="slow", source_sha="b" * 64,
                exc=TimeoutError("request timed out"), now=now,
            )
        self.assertEqual(generic["status"], "retry-exhausted")

    def test_watchdog_runs_due_work_without_any_session_event(self) -> None:
        started: list[str] = []

        class FakeThread:
            def __init__(self, target=None, args=(), name="", daemon=False):
                started.append(name)
                self.target, self.args = target, args

            def start(self):
                return None

        env = dict(self.env, PZ_MEMORY_FINALIZE_RETRY="1", PZ_MEMORY_RETRY_WATCHDOG="1")
        with mock.patch.dict(os.environ, env), \
             mock.patch.object(self.plugin, "_discover_final_turn_checkpoints"), \
             mock.patch.object(self.plugin.threading, "Thread", FakeThread):
            self.plugin.register(types.SimpleNamespace(register_hook=lambda *a, **k: None))
        self.assertIn("pz-memory-retry-watchdog", started)

        # The loop body itself: one wait, one bounded run, then stop.
        waits: list[float] = []

        class FakeStop:
            def __init__(self):
                self.calls = 0

            def wait(self, timeout=None):
                waits.append(timeout)
                self.calls += 1
                return self.calls > 1

            def is_set(self):
                return self.calls > 1

        runs: list[str] = []
        with mock.patch.dict(os.environ, env), \
             mock.patch.object(self.plugin, "_RETRY_WATCHDOG_STOP", FakeStop()), \
             mock.patch.object(
                 self.plugin, "run_due_finalize_retries",
                 lambda **kwargs: runs.append("ran") or {"status": "ok", "outcomes": {}},
             ):
            self.plugin._retry_watchdog_loop()
        self.assertEqual(runs, ["ran"])
        self.assertTrue(all(value and value > 0 for value in waits))

    def test_watchdog_stays_out_of_a_short_lived_cli_process(self) -> None:
        started: list[str] = []

        class FakeThread:
            def __init__(self, target=None, args=(), name="", daemon=False):
                started.append(name)

            def start(self):
                return None

        env = {key: value for key, value in self.env.items()}
        env["PZ_MEMORY_FINALIZE_RETRY"] = "1"
        env.pop("PZ_MEMORY_RETRY_WATCHDOG", None)
        with mock.patch.dict(os.environ, env, clear=False), \
             mock.patch.dict(os.environ, {"PZ_HERMES_USER_SURFACE": ""}), \
             mock.patch.object(self.plugin, "_discover_final_turn_checkpoints"), \
             mock.patch.object(self.plugin.threading, "Thread", FakeThread):
            self.plugin.register(types.SimpleNamespace(register_hook=lambda *a, **k: None))
        self.assertEqual(started, [])


if __name__ == "__main__":
    unittest.main()

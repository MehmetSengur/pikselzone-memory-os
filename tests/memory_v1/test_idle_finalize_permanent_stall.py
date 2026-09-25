"""A permanent drain verdict must not silence a thread forever.

Observed live on 2026-09-16: ``classify_drain_failure`` called every
non-``ProviderBlocked`` failure permanent, ``record_drain_failure`` wrote that
verdict on the *oldest pending checkpoint* of a turn batch, and ``retry_due``
returned ``False`` for it for good.  Two Claude sessions were stranded that way
on ``summary-...-directive-shaped`` -- a rejection of the summarizer's own
output, not of anything stored.  The thread then kept capturing turns it could
never drain until it hit ``MAX_TURN_CHECKPOINTS_PER_SESSION``, after which its
Stop hook stopped capturing at all.

Pinned here:

* a rejected *summary* is retryable; a malformed *checkpoint* stays permanent;
* a permanent verdict is bound to the batch content it was reached on, so new
  turns make the batch eligible again;
* content that already failed is not re-sent to the provider on its own;
* a session at the retention limit is reported, and quarantine unblocks it
  without deleting a single raw checkpoint.
"""
from __future__ import annotations

import json

from memory_v1.adapters import (
    MAX_TURN_CHECKPOINTS_PER_SESSION, drain_checkpoint,
)
from memory_v1.core import PolicyError, ProviderBlocked, SchemaError, session_key
from memory_v1.doctor import run_doctor
from memory_v1.retry import (
    classify_drain_failure, find_idle_turn_batches, load_retry_state,
    quarantine_checkpoint, quarantined_checkpoints, restore_quarantined_checkpoint,
    retry_due, stalled_turn_sessions, turn_batch_key,
)

from test_idle_finalize import IDLE, FakeProvider, IdleFinalizeBase, _summary


class ClassificationTests(IdleFinalizeBase):
    def test_rejected_summary_output_is_retryable(self):
        for reason in (
            "summary-learnings-directive-shaped",
            "summary-important_conversations-directive-shaped",
            "summary-fields-invalid",
            "memory-summary-empty",
            "empty-summary-has-content",
        ):
            with self.subTest(reason=reason):
                self.assertEqual("retryable", classify_drain_failure(SchemaError(reason)))

    def test_malformed_stored_checkpoint_stays_permanent(self):
        for reason in (
            "checkpoint-corrupt",
            "checkpoint-schema-invalid",
            "checkpoint-runtime-invalid",
            "checkpoint-not-session-member",
        ):
            with self.subTest(reason=reason):
                self.assertEqual("permanent", classify_drain_failure(SchemaError(reason)))

    def test_configuration_failures_outrank_transport(self):
        self.assertEqual(
            "permanent", classify_drain_failure(ProviderBlocked("credential-missing")),
        )


class BatchContentBindingTests(IdleFinalizeBase):
    def _stick(self, session: str, runtime: str = "claude") -> list:
        """Drive one batch drain into a permanent verdict."""
        paths = self._turns(session, runtime, [("One.", "A."), ("Two.", "B.")])
        self._age(paths, IDLE + 60)
        selected = find_idle_turn_batches(self.config)
        self.assertEqual([paths[0]], selected)
        provider = FakeProvider(PolicyError("checkpoint-not-session-member"))
        with self.assertRaises(PolicyError):
            drain_checkpoint(self.config, selected[0], provider=provider)
        return paths

    def test_permanent_verdict_records_the_batch_it_judged(self):
        paths = self._stick("sess-bound")
        state = load_retry_state(self.config, paths[0])
        self.assertEqual("permanent", state["status"])
        self.assertEqual(turn_batch_key(p.name for p in paths), state["batch_key"])

    def test_same_content_is_not_retried(self):
        paths = self._stick("sess-same")
        self.assertEqual([], find_idle_turn_batches(self.config))
        # And a direct re-selection would not reach the provider either.
        self.assertFalse(
            retry_due(
                load_retry_state(self.config, paths[0]),
                batch_key=turn_batch_key(p.name for p in paths),
            )
        )

    def test_a_new_turn_makes_the_batch_eligible_again(self):
        session = "sess-returns"
        paths = self._stick(session)
        self.assertEqual([], find_idle_turn_batches(self.config))
        # The user comes back to the thread and produces one more turn.
        transcript = self.root / f"claude-{session_key(session)[:8]}.jsonl"
        with transcript.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"role": "user", "content": "Three."}) + "\n")
            handle.write(json.dumps({"role": "assistant", "content": "C."}) + "\n")
        fresh = self._stop(session, "claude", transcript)
        self._age([*paths, fresh], IDLE + 30)
        self.assertEqual([paths[0]], find_idle_turn_batches(self.config))

    def test_the_retried_batch_promotes_every_turn_once(self):
        session = "sess-recovers"
        paths = self._stick(session)
        transcript = self.root / f"claude-{session_key(session)[:8]}.jsonl"
        with transcript.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"role": "user", "content": "Three."}) + "\n")
            handle.write(json.dumps({"role": "assistant", "content": "C."}) + "\n")
        fresh = self._stop(session, "claude", transcript)
        self._age([*paths, fresh], IDLE + 30)
        provider = FakeProvider(_summary(decisions=["Recovered after a permanent verdict."]))
        drain_checkpoint(
            self.config, find_idle_turn_batches(self.config)[0], provider=provider,
        )
        self.assertEqual(1, len(provider.inputs))
        for text in ("One.", "Two.", "Three."):
            self.assertIn(text, provider.inputs[0])
        self.assertEqual([], list(self.pending.glob("claude-*.json")))
        self.assertEqual({}, load_retry_state(self.config, paths[0]))

    def test_a_fresh_batch_starts_its_own_attempt_budget(self):
        session = "sess-budget"
        paths = self._stick(session)
        transcript = self.root / f"claude-{session_key(session)[:8]}.jsonl"
        with transcript.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"role": "user", "content": "Three."}) + "\n")
            handle.write(json.dumps({"role": "assistant", "content": "C."}) + "\n")
        fresh = self._stop(session, "claude", transcript)
        self._age([*paths, fresh], IDLE + 30)
        provider = FakeProvider(SchemaError("summary-learnings-directive-shaped"))
        with self.assertRaises(SchemaError):
            drain_checkpoint(self.config, paths[0], provider=provider)
        state = load_retry_state(self.config, paths[0])
        self.assertEqual(1, state["attempts"])
        self.assertEqual("retry-scheduled", state["status"])


class LegacyVerdictTests(IdleFinalizeBase):
    """Verdicts written before content scoping must not silence a thread."""

    def test_a_verdict_with_no_batch_key_does_not_apply(self):
        paths = self._turns("sess-legacy", "claude", [("One.", "A."), ("Two.", "B.")])
        self._age(paths, IDLE + 60)
        provider = FakeProvider(PolicyError("checkpoint-not-session-member"))
        with self.assertRaises(PolicyError):
            drain_checkpoint(self.config, paths[0], provider=provider)
        # Rewrite the sidecar the way the pre-fix code wrote it.
        sidecar = self.state / "queue" / "retry" / paths[0].name
        state = json.loads(sidecar.read_text(encoding="utf-8"))
        state.pop("batch_key")
        sidecar.write_text(json.dumps(state), encoding="utf-8")
        self.assertEqual("permanent", load_retry_state(self.config, paths[0])["status"])
        self.assertEqual([paths[0]], find_idle_turn_batches(self.config))

    def test_a_terminal_verdict_is_still_honoured(self):
        """Stale recovery asks without a batch key; its verdict still holds."""
        state = {"status": "permanent", "attempts": 1}
        self.assertFalse(retry_due(state))
        self.assertTrue(retry_due(state, batch_key="whatever"))


class StallReportingTests(IdleFinalizeBase):
    def _stalled_session(self, session: str = "sess-stalled") -> list:
        paths = self._turns(session, "claude", [("One.", "A."), ("Two.", "B.")])
        self._age(paths, IDLE + 60)
        provider = FakeProvider(PolicyError("checkpoint-not-session-member"))
        with self.assertRaises(PolicyError):
            drain_checkpoint(self.config, paths[0], provider=provider)
        return paths

    def test_doctor_warns_about_a_stalled_session(self):
        self._stalled_session()
        stalled = stalled_turn_sessions(self.config)
        self.assertEqual(1, len(stalled))
        self.assertEqual(2, stalled[0]["pending_turns"])
        self.assertEqual(
            MAX_TURN_CHECKPOINTS_PER_SESSION, stalled[0]["retention_limit"],
        )
        row = self._doctor_row("turn_batch_stalled")
        self.assertEqual("warn", row["status"])
        self.assertIn("sessions=1", row["detail"])

    def test_a_healthy_queue_reports_no_stall(self):
        self._turns("sess-clean", "claude", [("One.", "A.")])
        self.assertEqual([], stalled_turn_sessions(self.config))
        self.assertEqual("pass", self._doctor_row("turn_batch_stalled")["status"])

    def test_a_session_that_moved_on_is_no_longer_stalled(self):
        session = "sess-moved"
        self._stalled_session(session)
        transcript = self.root / f"claude-{session_key(session)[:8]}.jsonl"
        with transcript.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"role": "user", "content": "Three."}) + "\n")
            handle.write(json.dumps({"role": "assistant", "content": "C."}) + "\n")
        self._stop(session, "claude", transcript)
        self.assertEqual([], stalled_turn_sessions(self.config))

    def _doctor_row(self, name: str) -> dict:
        report = run_doctor(self.config)
        rows = [row for row in report["checks"] if row["check"] == name]
        self.assertEqual(1, len(rows), f"missing doctor row {name}")
        return rows[0]


class RetentionLimitTests(IdleFinalizeBase):
    """What a session that already hit the retention limit does.

    Reaching the limit is the end state of the stall this module fixes: the
    Stop hook refuses to capture anything further, so the thread goes dark.
    The turns it has are still drainable, and draining them re-opens capture.
    """

    def _fill(self, session: str) -> list:
        transcript = self.root / f"claude-{session_key(session)[:8]}.jsonl"
        paths = []
        for index in range(MAX_TURN_CHECKPOINTS_PER_SESSION):
            with transcript.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps({"role": "user", "content": f"Q{index}."}) + "\n")
                handle.write(json.dumps({"role": "assistant", "content": f"A{index}."}) + "\n")
            paths.append(self._stop(session, "claude", transcript))
        return paths

    def test_a_full_session_refuses_new_turns_but_still_drains(self):
        session = "sess-full"
        paths = self._fill(session)
        self.assertEqual(MAX_TURN_CHECKPOINTS_PER_SESSION, len(paths))
        transcript = self.root / f"claude-{session_key(session)[:8]}.jsonl"
        with transcript.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"role": "user", "content": "One too many."}) + "\n")
            handle.write(json.dumps({"role": "assistant", "content": "Refused."}) + "\n")
        with self.assertRaises(PolicyError) as caught:
            self._stop(session, "claude", transcript)
        self.assertIn("retention-limit", str(caught.exception))

        # The backlog is still eligible, and draining it re-opens capture.
        self._age(paths, IDLE + 60)
        self.assertEqual([paths[0]], find_idle_turn_batches(self.config))
        drain_checkpoint(
            self.config, paths[0],
            provider=FakeProvider(_summary(decisions=["Drained a full session."])),
        )
        self.assertEqual([], list(self.pending.glob("claude-*.json")))
        self.assertTrue(self._stop(session, "claude", transcript).is_file())

    def test_doctor_shows_how_close_a_stalled_session_is_to_the_limit(self):
        session = "sess-full-stalled"
        paths = self._fill(session)
        with self.assertRaises(PolicyError):
            drain_checkpoint(
                self.config, paths[0],
                provider=FakeProvider(PolicyError("checkpoint-not-session-member")),
            )
        stalled = stalled_turn_sessions(self.config)
        self.assertEqual(1, len(stalled))
        self.assertEqual(
            MAX_TURN_CHECKPOINTS_PER_SESSION, stalled[0]["pending_turns"],
        )
        report = run_doctor(self.config)
        row = [r for r in report["checks"] if r["check"] == "turn_batch_stalled"][0]
        self.assertEqual("warn", row["status"])
        self.assertIn(
            f"{MAX_TURN_CHECKPOINTS_PER_SESSION}/{MAX_TURN_CHECKPOINTS_PER_SESSION}",
            row["detail"],
        )


class QuarantineTests(IdleFinalizeBase):
    def test_quarantine_preserves_bytes_and_unblocks_the_session(self):
        session = "sess-poison"
        paths = self._turns(session, "claude", [("Poison.", "A."), ("Good.", "B.")])
        self._age(paths, IDLE + 60)
        provider = FakeProvider(PolicyError("checkpoint-not-session-member"))
        with self.assertRaises(PolicyError):
            drain_checkpoint(self.config, paths[0], provider=provider)
        self.assertEqual([], find_idle_turn_batches(self.config))

        original = paths[0].read_bytes()
        moved = quarantine_checkpoint(self.config, paths[0], reason="poisoned-turn")
        self.assertFalse(paths[0].exists())
        self.assertEqual(original, moved.read_bytes())

        records = quarantined_checkpoints(self.config)
        self.assertEqual(1, len(records))
        self.assertEqual("poisoned-turn", records[0]["reason"])
        self.assertEqual(paths[0].name, records[0]["checkpoint_id"])

        # The session's surviving turn is a different batch, and drains.
        self._age([paths[1]], IDLE + 60)
        self.assertEqual([paths[1]], find_idle_turn_batches(self.config))
        good = FakeProvider(_summary(decisions=["Survived the poisoned turn."]))
        drain_checkpoint(self.config, paths[1], provider=good)
        self.assertIn("Good.", good.inputs[0])
        self.assertNotIn("Poison.", good.inputs[0])

    def test_doctor_reports_quarantined_checkpoints(self):
        paths = self._turns("sess-q-doctor", "claude", [("One.", "A.")])
        quarantine_checkpoint(self.config, paths[0])
        report = run_doctor(self.config)
        row = [r for r in report["checks"] if r["check"] == "checkpoint_quarantine"][0]
        self.assertEqual("warn", row["status"])
        self.assertIn("quarantined=1", row["detail"])

    def test_restore_returns_the_checkpoint_untouched(self):
        paths = self._turns("sess-restore", "claude", [("One.", "A.")])
        original = paths[0].read_bytes()
        quarantine_checkpoint(self.config, paths[0])
        restored = restore_quarantined_checkpoint(self.config, paths[0].name)
        self.assertEqual(paths[0], restored)
        self.assertEqual(original, restored.read_bytes())
        self.assertEqual([], quarantined_checkpoints(self.config))

    def test_quarantine_refuses_a_path_outside_the_queue(self):
        stray = self.root / "not-a-checkpoint.json"
        stray.write_text("{}", encoding="utf-8")
        with self.assertRaises(Exception):
            quarantine_checkpoint(self.config, stray)
        self.assertTrue(stray.exists())

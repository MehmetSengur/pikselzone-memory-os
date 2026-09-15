"""Regressions for the four review findings on idle finalize (commit 9fe1a6e).

1. A partial (clamped) terminal transcript must not replace the summary of
   turns an earlier idle batch promoted.
2. A late-recall target whose drain is waiting on a scheduled retry must stay
   tracked and still be delivered when the retry succeeds.
3. A late-recall block that does not fit the character budget must stay
   pending and whole; it must never be cut and counted as delivered.
4. Acceptance must require the idle path, late recall and startup recall
   separately.
"""
from __future__ import annotations

import datetime as dt
import importlib.util
import json
from pathlib import Path

import test_idle_finalize as base
import test_idle_finalize_hardening as hardening

from memory_v1.adapters import checkpoint_hook, drain_checkpoint
from memory_v1.core import ProviderBlocked, SchemaError, session_key
from memory_v1.events import parse_event_artifact
from memory_v1.late_recall import (
    LATE_RECALL_BUDGET_CHARS, LATE_RECALL_HEADER, LATE_RECALL_INTRO, condensed_block,
    deliver_late_recall, record_pending_finalize,
)

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "idle-finalize-evidence.py"
_spec = importlib.util.spec_from_file_location("idle_finalize_evidence", _SCRIPT)
evidence = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(evidence)


class TerminalReplaceTests(hardening.HardeningBase):
    def _end(self, session: str) -> Path:
        return checkpoint_hook(self.config, runtime="codex", payload={
            "session_id": session, "transcript_path": str(self._transcript_path(session)),
            "hook_event_name": "SessionEnd",
        })

    def test_clamped_terminal_merges_instead_of_erasing_the_idle_summary(self):
        session = "sess-clamped-terminal"
        filler = "y" * 45_000
        turns = []
        for index in range(2):
            self._append(session, ("user", f"EARLY-{index} decision"), ("assistant", filler))
            turns.append(self._stop_turn(session, f"early-{index}"))
        drain_checkpoint(self.config, turns[0], provider=base.FakeProvider(
            base._summary(decisions=["EARLY-0 was decided in the first idle batch."])
        ))
        self._append(session, ("user", "LATE-2 decision"), ("assistant", filler))
        late = self._stop_turn(session, "late-2")
        end = self._end(session)
        terminal_text = json.loads(end.read_text(encoding="utf-8"))["normalized_transcript"]
        self.assertNotIn("EARLY-0", terminal_text, "precondition: the terminal transcript is clamped")

        event_path = drain_checkpoint(self.config, end, provider=base.FakeProvider(
            base._summary(decisions=["LATE-2 was decided before the session ended."])
        ))
        artifact = parse_event_artifact(event_path.read_text(encoding="utf-8"))
        self.assertEqual(
            ["EARLY-0 was decided in the first idle batch.",
             "LATE-2 was decided before the session ended."],
            artifact["sections"]["decisions"],
        )
        self.assertIn("session_end", artifact["events_seen"])
        self.assertEqual([event_path], self._dailies())
        self.assertFalse(late.exists(), "the turn the terminal transcript contains is absorbed")

    def test_complete_terminal_still_replaces_the_batch_summary(self):
        session = "sess-complete-terminal"
        turns = self._turns(session, "codex", [("Short one.", "Ok."), ("Short two.", "Ok.")])
        drain_checkpoint(self.config, turns[0], provider=base.FakeProvider(
            base._summary(decisions=["Batch wording of the decision."])
        ))
        # A later turn makes the terminal transcript differ from the batch
        # (an identical one is only an events_seen merge), while it still
        # contains every promoted turn.
        self._turns(session, "codex", [("Short three.", "Ok.")])
        event_path = drain_checkpoint(self.config, self._end(session), provider=base.FakeProvider(
            base._summary(decisions=["Whole-session wording of the decision."])
        ))
        self.assertEqual(
            ["Whole-session wording of the decision."],
            parse_event_artifact(event_path.read_text(encoding="utf-8"))["sections"]["decisions"],
        )


class LateRecallDirectBase(hardening.HardeningBase):
    viewer = "33333333-4444-4555-8666-777777777777"

    def _idle(self, session_id: str, text: str) -> Path:
        self._append(session_id, ("user", text), ("assistant", "Ok."))
        return self._stop_turn(session_id, "only", project="demo")

    def _marker(self) -> dict:
        path = self.state / "recall" / "late" / f"claude-{session_key(self.viewer)}.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}

    def _deliver(self) -> str:
        return deliver_late_recall(self.config, runtime="claude", session_id=self.viewer)


class RetryTrackingTests(LateRecallDirectBase):
    def test_scheduled_retry_stays_tracked_and_is_delivered_after_success(self):
        target = self._idle("sess-retry-late", "Retry decision.")
        record_pending_finalize(
            self.config, runtime="claude", session_id=self.viewer, project="demo",
            checkpoints=[target],
        )
        with self.assertRaises(ProviderBlocked):
            drain_checkpoint(self.config, target, provider=base.FakeProvider(
                ProviderBlocked("codex-process-failed:1: err=You've hit your usage limit")
            ))
        first = self._deliver()
        self.assertIn("yeniden denenecek", first)
        self.assertEqual("pending", self._marker()["targets"][0]["status"])
        self.assertNotIn("yeniden denenecek", self._deliver(), "retry notice is shown once")

        drain_checkpoint(self.config, target, provider=base.FakeProvider(
            base._summary(decisions=["RETRY-CANARY-51c2 decision after the retry."])
        ))
        self.assertIn("RETRY-CANARY-51c2", self._deliver())
        self.assertEqual({}, self._marker(), "tracking ends once delivered")

    def test_permanent_failure_ends_tracking(self):
        target = self._idle("sess-permanent-late", "Permanent decision.")
        record_pending_finalize(
            self.config, runtime="claude", session_id=self.viewer, project="demo",
            checkpoints=[target],
        )
        with self.assertRaises(SchemaError):
            drain_checkpoint(self.config, target, provider=base.FakeProvider(
                SchemaError("summary-decisions-directive-shaped")
            ))
        self.assertIn("kalıcı hata", self._deliver())
        self.assertEqual({}, self._marker())
        self.assertTrue(target.is_file(), "the raw checkpoint and its retry record are kept")


class BudgetTests(LateRecallDirectBase):
    LONG = "Z" * 400

    def _long_summary(self, tag: str) -> dict:
        return base._summary(
            context=[f"{tag} context {self.LONG}"],
            decisions=[f"{tag} decision one {self.LONG}", f"{tag} decision two {self.LONG}"],
            open_items=[f"{tag} open {self.LONG}"],
            evidence=[f"{tag} evidence {self.LONG}"],
        )

    def test_largest_block_always_fits_with_header_and_intro(self):
        artifact = {"sections": {
            field: [self.LONG] * 5 for field in
            ("context", "decisions", "open_items", "evidence", "learnings", "important_conversations")
        }}
        block = condensed_block(artifact, "daily/2026-09-16/claude-" + "f" * 32 + ".md")
        self.assertLessEqual(
            len("\n".join([LATE_RECALL_HEADER, f"{LATE_RECALL_INTRO}\n{block}"])),
            LATE_RECALL_BUDGET_CHARS,
        )

    def test_block_over_budget_stays_pending_whole_and_is_delivered_next(self):
        first = self._idle("sess-budget-a", "Budget A.")
        second = self._idle("sess-budget-b", "Budget B.")
        record_pending_finalize(
            self.config, runtime="claude", session_id=self.viewer, project="demo",
            checkpoints=[first, second],
        )
        drain_checkpoint(self.config, first, provider=base.FakeProvider(self._long_summary("BLOCK-A")))
        drain_checkpoint(self.config, second, provider=base.FakeProvider(self._long_summary("BLOCK-B")))

        def whole_block(event_path: str) -> str:
            path = Path(event_path)
            artifact = parse_event_artifact(path.read_text(encoding="utf-8"))
            return condensed_block(artifact, str(path.relative_to(self.vault)))

        def receipt() -> dict:
            return json.loads(
                (self.state / "evidence" / "late-recall-claude.json").read_text(encoding="utf-8")
            )

        text = self._deliver()
        self.assertLessEqual(len(text), LATE_RECALL_BUDGET_CHARS)
        first_receipt = receipt()
        self.assertEqual(1, len(first_receipt["delivered"]), "only one block fits the budget")
        delivered_path = first_receipt["delivered"][0]["event_path"]
        self.assertIn(whole_block(delivered_path), text, "the delivered block is whole")
        self.assertEqual(
            ["delivered", "pending"], sorted(t["status"] for t in self._marker()["targets"]),
            "the block that did not fit is not counted delivered",
        )
        withheld_path = next(
            str(path) for path in self._dailies() if str(path) != delivered_path
        )
        self.assertNotIn(whole_block(withheld_path).splitlines()[0], text)

        following = self._deliver()
        self.assertIn(whole_block(withheld_path), following, "delivered whole on the next prompt")
        self.assertEqual(withheld_path, receipt()["delivered"][0]["event_path"])
        self.assertEqual({}, self._marker())


class AcceptanceGateTests(hardening.HardeningBase):
    canary = "PZ-IDLE-CANARY-0a1b2c3d"

    def _captured_and_promoted(self, *, via_session_end: bool = False) -> tuple[dict, str]:
        session = "sess-acceptance"
        self._append(session, ("user", f"Decision {self.canary}."), ("assistant", "Recorded."))
        turn = self._stop_turn(session, "canary")
        item = json.loads(turn.read_text(encoding="utf-8"))
        record = {"capture": {
            "pass": True, "session_key": session_key(session), "canary": self.canary,
            "checkpoints": [{
                "checkpoint": turn.name, "source_digest": item["source_digest"],
                "event": "turn_complete",
            }],
        }}
        summary = base._summary(decisions=[f"The user recorded {self.canary}."])
        if via_session_end:
            end = checkpoint_hook(self.config, runtime="codex", payload={
                "session_id": session, "transcript_path": str(self._transcript_path(session)),
                "hook_event_name": "SessionEnd",
            })
            drain_checkpoint(self.config, end, provider=base.FakeProvider(summary))
        else:
            drain_checkpoint(self.config, turn, provider=base.FakeProvider(summary))
        return record, session

    def _session_transcript(self, session_id: str, *, assistant: str, user: str) -> None:
        directory = self.root / "claude-project"
        directory.mkdir(exist_ok=True)
        (directory / f"{session_id}.jsonl").write_text(
            json.dumps({"role": "user", "content": user}) + "\n"
            + json.dumps({"role": "assistant", "content": assistant}) + "\n",
            encoding="utf-8",
        )

    def test_promote_rejects_a_session_end_promotion(self):
        record, _ = self._captured_and_promoted(via_session_end=True)
        self.assertFalse(evidence.promote(self.config, record))
        self.assertIn("idle-finalize-did-not-promote", record["promote"]["reasons"])

    def test_recall_late_requires_promotion_after_the_session_started(self):
        record, _ = self._captured_and_promoted()
        self.assertTrue(evidence.promote(self.config, record))
        late_session = "44444444-5555-4666-8777-888888888888"
        self._session_transcript(late_session, user="En son karar neydi?",
                                 assistant=f"Karar {self.canary} idi.")
        promoted_at = dt.datetime.fromisoformat(record["promote"]["state_updated_at"])
        receipt = {
            "schema": "pikselzone-memory-late-recall-evidence-v1",
            "session_key": session_key(late_session),
            "session_started_at": (promoted_at + dt.timedelta(minutes=5)).isoformat(),
            "delivered": [{
                "event_path": record["promote"]["event_path"],
                "state_updated_at": record["promote"]["state_updated_at"],
            }],
            "text": f"- Karar: The user recorded {self.canary}.",
        }
        evidence_dir = self.state / "evidence"
        evidence_dir.mkdir(parents=True, exist_ok=True)
        (evidence_dir / "late-recall-claude.json").write_text(json.dumps(receipt), encoding="utf-8")
        self.assertFalse(evidence.recall_late(self.config, record, runtime="claude", session=late_session))
        self.assertIn("promotion-not-after-session-start", record["recall_late"]["reasons"])

        receipt["session_started_at"] = (promoted_at - dt.timedelta(minutes=5)).isoformat()
        (evidence_dir / "late-recall-claude.json").write_text(json.dumps(receipt), encoding="utf-8")
        self.assertTrue(evidence.recall_late(self.config, record, runtime="claude", session=late_session))

    def test_verdict_requires_both_recall_paths_in_distinct_sessions(self):
        passed = {"pass": True}
        record = {"capture": passed, "promote": passed, "recall_late": {**passed, "session": "a"}}
        self.assertFalse(evidence.verdict(record))
        self.assertIn("recall_startup", record["verdict"]["failed_or_missing"])
        record["recall_startup"] = {**passed, "session": "a"}
        self.assertFalse(evidence.verdict(record))
        self.assertIn("recall-sessions-not-distinct", record["verdict"]["failed_or_missing"])
        record["recall_startup"] = {**passed, "session": "b"}
        self.assertTrue(evidence.verdict(record))
        del record["recall_late"]
        self.assertFalse(evidence.verdict(record))


if __name__ == "__main__":
    import unittest

    unittest.main()

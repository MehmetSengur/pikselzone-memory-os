"""Targeting, canary and session-identity rules of the cross-runtime harness.

These cover the parts that decide *which* host and *which* session a run binds
its evidence to. They run without touching a live host: every case here comes
from a way the harness previously produced, or could have produced, evidence
about the wrong thing.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from memory_v1.core import directive_shaped
from memory_v1.harness import (
    CONTABO_TARGET,
    HarnessTarget,
    _extract_codex_session_id,
    build_canary,
    load_target,
    preflight,
    stage_canary_artifact,
)


class CanaryArtifactTests(unittest.TestCase):
    def test_check_value_originates_from_a_file_on_disk(self):
        # The value must be something a session reads, not something a prompt
        # asserts: an asserted token that a later session is asked to repeat is
        # the shape of an exfiltration attempt, and capture flagged it as one.
        with tempfile.TemporaryDirectory() as tmp:
            artifact = stage_canary_artifact(
                Path(tmp), "PZ-M4-CANARY-abcd1234", "PZV-0123456789ab", "harness-test"
            )
            self.assertTrue(artifact.is_file())
            data = json.loads(artifact.read_text(encoding="utf-8"))
            self.assertEqual("PZV-0123456789ab", data["check_value"])
            self.assertEqual("PZ-M4-CANARY-abcd1234", data["marker"])

    def test_artifact_declares_itself_non_authoritative(self):
        with tempfile.TemporaryDirectory() as tmp:
            artifact = stage_canary_artifact(
                Path(tmp), "PZ-M4-CANARY-abcd1234", "PZV-0123456789ab", "harness-test"
            )
            data = json.loads(artifact.read_text(encoding="utf-8"))
            self.assertEqual("test-artifact-not-operational-policy", data["authority"])


class CanaryShapeTests(unittest.TestCase):
    def test_canary_is_labelled_test_data_and_not_a_policy_claim(self):
        canary = build_canary("PZ-M4-CANARY-abcd1234", "PZV-0123456789ab")
        self.assertIn("test data", canary.casefold())
        self.assertIn("not an operational policy", canary.casefold())
        self.assertIn("PZ-M4-CANARY-abcd1234", canary)
        self.assertIn("PZV-0123456789ab", canary)

    def test_canary_does_not_trip_the_directive_shaped_guard(self):
        # A canary that trips the injection defense blocks the very drain the
        # harness waits on, which is how an earlier revision failed.
        canary = build_canary("PZ-M4-CANARY-abcd1234", "PZV-0123456789ab")
        self.assertFalse(directive_shaped(canary))

    def test_canary_avoids_the_words_the_guard_matches(self):
        canary = build_canary("PZ-M4-CANARY-abcd1234", "PZV-0123456789ab").casefold()
        for forbidden in ("tool call", "system prompt", "execute this", "run this command"):
            self.assertNotIn(forbidden, canary)


class HermesInvocationTests(unittest.TestCase):
    def test_hermes_leg_runs_as_the_service_user(self):
        # SSH lands as root; a root-run session writes outbox recall evidence
        # root-owned, and the publisher (running as the service user) then
        # cannot read it -- one such run blocked every later promotion.
        from memory_v1.harness import hermes_cmd

        command = hermes_cmd(CONTABO_TARGET, "sessions list")
        self.assertIn("sudo -n -u pzhermes", command)
        self.assertIn(CONTABO_TARGET.hermes_cli, command)
        self.assertIn(CONTABO_TARGET.hermes_profile, command)

    def test_no_user_switch_when_target_declares_none(self):
        from memory_v1.harness import hermes_cmd

        target = dataclasses_replace(CONTABO_TARGET, hermes_run_user="")
        command = hermes_cmd(target, "sessions list")
        self.assertNotIn("sudo", command)


def dataclasses_replace(instance, **changes):
    import dataclasses

    return dataclasses.replace(instance, **changes)


class CodexSessionIdTests(unittest.TestCase):
    def test_reads_session_id_from_event_stream(self):
        stdout = "\n".join([
            '{"type":"thread.started","session":{"session_id":"11111111-2222-3333-4444-555555555555"}}',
            '{"type":"item.completed"}',
        ])
        self.assertEqual(
            "11111111-2222-3333-4444-555555555555",
            _extract_codex_session_id(stdout, ""),
        )

    def test_falls_back_to_plain_text_session_line(self):
        self.assertEqual(
            "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            _extract_codex_session_id("session id: aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", ""),
        )

    def test_returns_empty_rather_than_inventing_an_id(self):
        # The caller turns this into a hard failure. Guessing here is what put
        # an unrelated concurrent session's id into signed evidence before.
        self.assertEqual("", _extract_codex_session_id("no identifiers here", ""))

    def test_rejects_non_uuid_session_values(self):
        stdout = '{"type":"thread.started","session_id":"latest"}'
        self.assertEqual("", _extract_codex_session_id(stdout, ""))


class TargetResolutionTests(unittest.TestCase):
    def test_default_target_is_the_live_host_not_the_retired_alias(self):
        self.assertEqual("pz-contabo", CONTABO_TARGET.ssh_alias)
        self.assertEqual("vmi3566230", CONTABO_TARGET.expected_hostname)
        self.assertNotEqual("pz-hermes", CONTABO_TARGET.ssh_alias)

    def test_target_file_rejects_unknown_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "target.json"
            path.write_text(json.dumps({"name": "x", "sshAlias": "y"}), encoding="utf-8")
            with self.assertRaises(ValueError):
                HarnessTarget.from_file(path)

    def test_load_target_without_spec_returns_the_default(self):
        with mock.patch.dict("os.environ", {}, clear=False):
            self.assertIs(CONTABO_TARGET, load_target(None))


class PreflightTests(unittest.TestCase):
    def _run_preflight_with(self, hostname: str, probe_output: str):
        def fake_ssh(target, command, check=False):
            if command == "hostname":
                return mock.Mock(returncode=0, stdout=hostname + "\n", stderr="")
            if command.endswith("id -un"):
                return mock.Mock(returncode=0, stdout=target.hermes_run_user + "\n", stderr="")
            return mock.Mock(returncode=0, stdout=probe_output, stderr="")

        with mock.patch("memory_v1.harness.ssh", side_effect=fake_ssh):
            return preflight(CONTABO_TARGET)

    def _all_paths_present(self) -> str:
        keys = [
            "vault_path", "hermes_home", "startup_bundle_path", "profile_state_db",
            "receipts_dir", "evidence_dir", "hermes_cli", "memory_cli",
            "memory_config_path", "memory_python", "memory_pythonpath",
        ]
        return "\n".join(f"OK {k}" for k in keys)

    def test_refuses_a_host_that_is_not_the_expected_one(self):
        # The whole point: an alias left pointing at the old server must abort
        # the run rather than quietly test the wrong machine.
        with self.assertRaisesRegex(RuntimeError, "Refusing to run"):
            self._run_preflight_with("some-other-host", self._all_paths_present())

    def test_refuses_when_required_paths_are_missing(self):
        probe = self._all_paths_present().replace("OK memory_cli", "MISS memory_cli")
        with self.assertRaisesRegex(RuntimeError, "missing required paths"):
            self._run_preflight_with("vmi3566230", probe)

    def test_reports_policy_guard_as_unavailable_when_not_configured(self):
        info = self._run_preflight_with("vmi3566230", self._all_paths_present())
        self.assertEqual("vmi3566230", info["hostname"])
        self.assertFalse(info["policy_guard_available"])

    def test_refuses_when_the_service_user_cannot_be_assumed(self):
        def fake_ssh(target, command, check=False):
            if command == "hostname":
                return mock.Mock(returncode=0, stdout="vmi3566230\n", stderr="")
            if command.endswith("id -un"):
                return mock.Mock(returncode=1, stdout="", stderr="sudo: a password is required")
            return mock.Mock(returncode=0, stdout=self._all_paths_present(), stderr="")

        with mock.patch("memory_v1.harness.ssh", side_effect=fake_ssh):
            with self.assertRaisesRegex(RuntimeError, "Cannot run as service user"):
                preflight(CONTABO_TARGET)


if __name__ == "__main__":
    unittest.main()


class NegativeResultRejectionTests(unittest.TestCase):
    """Each case is a failure the harness used to score as a success."""

    def _target(self):
        return CONTABO_TARGET

    def test_failed_probe_is_not_read_as_all_paths_present(self):
        # An SSH hiccup returns empty stdout; with no MISS line the old check
        # concluded every required path existed.
        def fake_ssh(target, command, check=False):
            if command == "hostname":
                return mock.Mock(returncode=0, stdout="vmi3566230\n", stderr="")
            return mock.Mock(returncode=0, stdout="", stderr="")

        with mock.patch("memory_v1.harness.ssh", side_effect=fake_ssh):
            with self.assertRaisesRegex(RuntimeError, "no result for"):
                preflight(self._target())

    def test_ssh_error_during_probe_aborts(self):
        def fake_ssh(target, command, check=False):
            if command == "hostname":
                return mock.Mock(returncode=0, stdout="vmi3566230\n", stderr="")
            return mock.Mock(returncode=255, stdout="", stderr="connection reset")

        with mock.patch("memory_v1.harness.ssh", side_effect=fake_ssh):
            with self.assertRaisesRegex(RuntimeError, "Path probe failed"):
                preflight(self._target())

    def test_preexisting_hermes_session_is_rejected(self):
        # Membership in the store is not evidence: the id has to be one this
        # run created, or the model could name any session it likes.
        from memory_v1.harness import _HERMES_SESSION_MARK, run_hermes_retrieval

        old_id = "20260909_120000_aaaaaa"
        new_id = "20260910_130000_bbbbbb"
        # A session really was created by this run, but the runtime names an
        # older one. The old code accepted it because it existed in the store.
        listings = [
            f"Title Workspace Last ID\n{old_id}\n",
            f"Title Workspace Last ID\n{old_id}\n{new_id}\n",
        ]
        reply = f"value\n{_HERMES_SESSION_MARK} {old_id}\n".encode()

        with mock.patch("memory_v1.harness.ssh",
                        side_effect=[mock.Mock(returncode=0, stdout=item, stderr="")
                                     for item in listings]), \
             mock.patch("memory_v1.harness.subprocess.run",
                        return_value=mock.Mock(returncode=0, stdout=reply, stderr=b"")):
            with self.assertRaisesRegex(RuntimeError, "Cannot bind evidence to a Hermes session"):
                run_hermes_retrieval(self._target(), "PZ-M4-CANARY-abcd1234")

    def test_reported_and_new_hermes_session_is_accepted(self):
        from memory_v1.harness import _HERMES_SESSION_MARK, run_hermes_retrieval

        new_id = "20260910_130000_bbbbbb"
        listings = [
            "Title Workspace Last ID\n20260909_120000_aaaaaa\n",
            f"Title Workspace Last ID\n20260909_120000_aaaaaa\n{new_id}\n",
        ]
        reply = f"value\n{_HERMES_SESSION_MARK} {new_id}\n".encode()

        with mock.patch("memory_v1.harness.ssh",
                        side_effect=[mock.Mock(returncode=0, stdout=item, stderr="")
                                     for item in listings]), \
             mock.patch("memory_v1.harness.subprocess.run",
                        return_value=mock.Mock(returncode=0, stdout=reply, stderr=b"")):
            session_id, _, _, obs = run_hermes_retrieval(self._target(), "PZ-M4-CANARY-abcd1234")
        self.assertEqual(new_id, session_id)
        self.assertEqual("runtime-reported-and-new-during-run", obs["identification_basis"])

    def test_codex_tool_output_does_not_stand_in_for_the_answer(self):
        # A grep that printed the value must not satisfy the check when the
        # model's final answer says it found nothing.
        from memory_v1.core import codex_final_agent_message

        stream = "\n".join([
            json.dumps({"type": "item.completed", "item": {
                "item_type": "command_execution",
                "aggregated_output": "check_value: PZ-HARNESS-TESTVALUE-deadbeef00",
            }}),
            json.dumps({"type": "item.completed", "item": {
                "item_type": "agent_message",
                "text": "I could not find any recorded check value for that marker.",
            }}),
        ])
        answer = codex_final_agent_message(stream)
        self.assertIn("could not find", answer)
        self.assertNotIn("PZ-HARNESS-TESTVALUE-deadbeef00", answer)

    def test_codex_final_answer_is_the_last_agent_message(self):
        from memory_v1.core import codex_final_agent_message

        stream = "\n".join([
            json.dumps({"type": "item.completed", "item": {"item_type": "agent_message", "text": "thinking"}}),
            json.dumps({"type": "item.completed", "item": {"item_type": "agent_message", "text": "final answer"}}),
        ])
        self.assertEqual("final answer", codex_final_agent_message(stream))


class CaptureDiagnosticsTests(unittest.TestCase):
    def _config(self, tmp, health):
        import types
        state = Path(tmp)
        (state / "health").mkdir(parents=True, exist_ok=True)
        for name, payload in health.items():
            (state / "health" / f"{name}.json").write_text(json.dumps(payload), encoding="utf-8")
        return types.SimpleNamespace(state_path=state, vault_path=state / "vault")

    def test_stale_health_is_not_blamed_for_this_run(self):
        # A capture-claude entry from days earlier was once reported as the
        # cause of a timeout it had nothing to do with.
        from memory_v1.harness import _capture_blockers

        with tempfile.TemporaryDirectory() as tmp:
            cfg = self._config(tmp, {"capture-claude": {
                "status": "off", "detail": "no-project-arg", "updated_at": "2026-09-05T15:05:53+03:00",
            }})
            self.assertEqual([], _capture_blockers(cfg, "2026-09-10T15:00:00+03:00"))

    def test_failure_observed_during_the_run_is_reported(self):
        from memory_v1.harness import _capture_blockers

        with tempfile.TemporaryDirectory() as tmp:
            cfg = self._config(tmp, {"drain": {
                "status": "blocked", "detail": "directive-shaped", "updated_at": "2026-09-10T15:30:00+03:00",
            }})
            blockers = _capture_blockers(cfg, "2026-09-10T15:00:00+03:00")
            self.assertEqual(1, len(blockers))
            self.assertIn("drain=blocked", blockers[0])


def _bundle(text, *, items, source_shas, generated_at="2026-09-14T17:00:00+03:00", audit=None):
    import hashlib
    return {
        "text": text,
        "bundle_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "selected_item_ids": items,
        "source_shas": source_shas,
        "generated_at": generated_at,
        "selection_audit": audit or {},
    }


EXPECTED = {
    "item_id": "tier-d-claude-abc",
    "event_rel": "daily/2026-09-14/claude-abc.md",
    "source_line": "(Source: daily/2026-09-14/claude-abc.md)",
    "content_lines": ["- Harness artifact verified for marker PZ-M4-CANARY-1"],
}
EVENT_SHA = "e" * 64
EVENT_TEXT = "### Session claude (Source: daily/2026-09-14/claude-abc.md)\n- Harness artifact verified for marker PZ-M4-CANARY-1\n"


class StartupInjectionCheckTests(unittest.TestCase):
    """B must be proven from content, not from a hash that changed."""

    def test_bundle_with_event_content_passes(self):
        from memory_v1.harness import check_bundle_injection
        bundle = _bundle("head\n" + EVENT_TEXT, items=[EXPECTED["item_id"]], source_shas={EXPECTED["event_rel"]: EVENT_SHA})
        self.assertEqual((True, "event-content-in-startup-bundle"),
                         check_bundle_injection(bundle, EXPECTED, EVENT_SHA, not_before_iso="2026-09-14T16:59:00+03:00"))

    def test_metadata_only_rebuild_is_rejected(self):
        # Same memory, new timestamp: the hash changes but the event is absent.
        from memory_v1.harness import check_bundle_injection
        bundle = _bundle("Observed At: 2026-09-14T17:05:00+03:00\nsame memory", items=["tier-a-companion/Core.md"],
                         source_shas={}, generated_at="2026-09-14T17:05:00+03:00")
        ok, detail = check_bundle_injection(bundle, EXPECTED, EVENT_SHA, not_before_iso="2026-09-14T16:59:00+03:00")
        self.assertFalse(ok)
        self.assertTrue(detail.startswith("event-not-selected"))

    def test_selected_but_content_missing_is_rejected(self):
        from memory_v1.harness import check_bundle_injection
        bundle = _bundle("no content here", items=[EXPECTED["item_id"]], source_shas={EXPECTED["event_rel"]: EVENT_SHA})
        ok, _ = check_bundle_injection(bundle, EXPECTED, EVENT_SHA, not_before_iso="2026-09-14T16:59:00+03:00")
        self.assertFalse(ok)

    def test_other_version_of_the_event_is_rejected(self):
        from memory_v1.harness import check_bundle_injection
        bundle = _bundle(EVENT_TEXT, items=[EXPECTED["item_id"]], source_shas={EXPECTED["event_rel"]: "f" * 64})
        self.assertEqual((False, "event-source-sha-mismatch"),
                         check_bundle_injection(bundle, EXPECTED, EVENT_SHA, not_before_iso="2026-09-14T16:59:00+03:00"))

    def test_bundle_built_before_arrival_is_rejected(self):
        from memory_v1.harness import check_bundle_injection
        bundle = _bundle(EVENT_TEXT, items=[EXPECTED["item_id"]], source_shas={EXPECTED["event_rel"]: EVENT_SHA},
                         generated_at="2026-09-14T16:00:00+03:00")
        self.assertEqual((False, "bundle-built-before-event-arrived"),
                         check_bundle_injection(bundle, EXPECTED, EVENT_SHA, not_before_iso="2026-09-14T16:59:00+03:00"))

    def _evidence(self, session_key="20260914_170100_aaaaaa", snapshot=EVENT_TEXT, items=None, observed="2026-09-14T17:01:05+03:00"):
        import hashlib
        from memory_v1.recall import compute_lifecycle_receipt
        sha = hashlib.sha256(snapshot.encode("utf-8")).hexdigest()
        items = items if items is not None else [EXPECTED["item_id"]]
        receipt = compute_lifecycle_receipt(
            runtime="hermes", lifecycle_event="pre_llm_call", session_key=session_key,
            bundle_generated_at=observed, bundle_sha256=sha, bundle_chars=len(snapshot),
            selected_item_ids=items, provenance="native-lifecycle-startup", session_artifact_sha256="a" * 64,
        )
        return {
            "schema": "pikselzone-memory-recall-evidence-v1", "runtime": "hermes", "session_key": session_key,
            "bundle_snapshot": snapshot, "bundle_sha256": sha, "selected_item_ids": items,
            "lifecycle_receipt": receipt, "observed_at": observed,
        }

    def test_session_evidence_bound_to_this_session_passes(self):
        from memory_v1.harness import check_session_injection
        ok, detail = check_session_injection(self._evidence(), "20260914_170100_aaaaaa", EXPECTED,
                                             not_before_iso="2026-09-14T17:01:00+03:00")
        self.assertTrue(ok, detail)

    def test_another_sessions_receipt_is_rejected(self):
        from memory_v1.harness import check_session_injection
        ok, detail = check_session_injection(self._evidence(session_key="20260914_165900_bbbbbb"),
                                             "20260914_170100_aaaaaa", EXPECTED, not_before_iso="2026-09-14T17:01:00+03:00")
        self.assertFalse(ok)
        self.assertTrue(detail.startswith("evidence-for-another-session"))

    def test_injected_context_without_the_event_is_rejected(self):
        from memory_v1.harness import check_session_injection
        ok, _ = check_session_injection(self._evidence(snapshot="unrelated context", items=["tier-a-companion/Core.md"]),
                                        "20260914_170100_aaaaaa", EXPECTED, not_before_iso="2026-09-14T17:01:00+03:00")
        self.assertFalse(ok)

    def test_tampered_receipt_is_rejected(self):
        from memory_v1.harness import check_session_injection
        evidence = self._evidence()
        evidence["lifecycle_receipt"]["receipt_digest"] = "0" * 64
        self.assertEqual((False, "lifecycle-receipt-digest-mismatch"),
                         check_session_injection(evidence, "20260914_170100_aaaaaa", EXPECTED,
                                                 not_before_iso="2026-09-14T17:01:00+03:00"))

    def test_stale_evidence_from_before_the_run_is_rejected(self):
        from memory_v1.harness import check_session_injection
        ok, detail = check_session_injection(self._evidence(observed="2026-09-14T16:00:00+03:00"),
                                             "20260914_170100_aaaaaa", EXPECTED, not_before_iso="2026-09-14T17:01:00+03:00")
        self.assertEqual((False, "evidence-older-than-this-run"), (ok, detail))


class PublisherCycleCheckTests(unittest.TestCase):
    """C needs a complete successful run that started after the event arrived."""

    def _records(self, invocation, start, ok=True, result="done"):
        payload = '{"status": "ok", "results": []}' if ok else '{"status": "error"}'
        return [
            {"__REALTIME_TIMESTAMP": str(int(start * 1e6)), "INVOCATION_ID": invocation, "JOB_TYPE": "start",
             "MESSAGE": "Starting pz-memory-publisher.service - Publish"},
            {"__REALTIME_TIMESTAMP": str(int((start + 1) * 1e6)), "_SYSTEMD_INVOCATION_ID": invocation, "MESSAGE": payload},
            {"__REALTIME_TIMESTAMP": str(int((start + 2) * 1e6)), "INVOCATION_ID": invocation, "JOB_TYPE": "start",
             "JOB_RESULT": result, "MESSAGE": "Finished pz-memory-publisher.service"},
        ]

    def test_run_started_after_arrival_is_selected(self):
        from memory_v1.harness import parse_publisher_runs, select_publisher_run_after
        runs = parse_publisher_runs(self._records("old", 900) + self._records("new", 1010))
        self.assertEqual("new", select_publisher_run_after(runs, 1000)["invocation_id"])

    def test_run_that_started_before_arrival_is_not_accepted(self):
        from memory_v1.harness import parse_publisher_runs, select_publisher_run_after
        self.assertIsNone(select_publisher_run_after(parse_publisher_runs(self._records("old", 990)), 1000))

    def test_failed_run_is_not_accepted(self):
        from memory_v1.harness import parse_publisher_runs, select_publisher_run_after
        runs = parse_publisher_runs(self._records("bad", 1010, ok=False) + self._records("crash", 1020, result="failed"))
        self.assertIsNone(select_publisher_run_after(runs, 1000))

    def test_journal_saved_without_a_matching_run_is_not_evidence(self):
        from memory_v1.harness import parse_publisher_runs, select_publisher_run_after
        orphan_payload = [{"__REALTIME_TIMESTAMP": "1010000000", "_SYSTEMD_INVOCATION_ID": "x",
                           "MESSAGE": '{"status": "ok"}'}]
        self.assertIsNone(select_publisher_run_after(parse_publisher_runs(orphan_payload), 1000))


if __name__ == "__main__":
    unittest.main()



class HostClockObservationTests(unittest.TestCase):
    """Arrival is when the host was seen holding the content, on the host's clock."""

    def test_stale_mtime_does_not_move_the_observation_back(self):
        # Obsidian Sync keeps the source mtime: a file synced now can carry an
        # mtime from hours ago. The observation must come from the host clock
        # read together with the hash, never from stat.
        from memory_v1.harness import wait_for_vps_obsidian_sync
        sha = "a" * 64
        commands = []

        def fake_ssh(target, command, check=False):
            commands.append(command)
            if command.startswith("stat"):
                return mock.Mock(returncode=0, stdout="1000\n", stderr="")
            return mock.Mock(returncode=0, stdout=f"{sha} 5000.25\n", stderr="")

        with mock.patch("memory_v1.harness.ssh", side_effect=fake_ssh), mock.patch("memory_v1.harness.time.sleep"):
            observation = wait_for_vps_obsidian_sync(CONTABO_TARGET, "daily/x.md", sha, timeout=5)
        self.assertEqual(5000.25, observation["observed_epoch"])
        self.assertFalse(any(c.startswith("stat") for c in commands))
        self.assertTrue(all("sha256sum" in c and "date +%s.%N" in c for c in commands))

    def test_other_content_is_not_an_arrival(self):
        from memory_v1.harness import wait_for_vps_obsidian_sync
        fake = mock.Mock(returncode=0, stdout=f"{'b' * 64} 5000.0\n", stderr="")
        with mock.patch("memory_v1.harness.ssh", return_value=fake), mock.patch("memory_v1.harness.time.sleep"), \
                mock.patch("memory_v1.harness.time.time", side_effect=[0, 0, 10]):
            with self.assertRaises(TimeoutError):
                wait_for_vps_obsidian_sync(CONTABO_TARGET, "daily/x.md", "a" * 64, timeout=5)

    def test_failed_or_partial_remote_output_is_not_an_observation(self):
        from memory_v1.harness import observe_remote_file_sha, parse_remote_sha_observation
        self.assertIsNone(parse_remote_sha_observation(""))
        self.assertIsNone(parse_remote_sha_observation("a" * 64))  # hash without a clock reading
        self.assertIsNone(parse_remote_sha_observation("nothex 5000"))
        with mock.patch("memory_v1.harness.ssh", return_value=mock.Mock(returncode=255, stdout=f"{'a' * 64} 1.0", stderr="reset")):
            self.assertIsNone(observe_remote_file_sha(CONTABO_TARGET, "/x"))

    def test_unreadable_host_clock_aborts(self):
        from memory_v1.harness import remote_clock_epoch
        with mock.patch("memory_v1.harness.ssh", return_value=mock.Mock(returncode=255, stdout="", stderr="reset")):
            with self.assertRaises(RuntimeError):
                remote_clock_epoch(CONTABO_TARGET)


class PublisherJournalReadbackTests(unittest.TestCase):
    def _records(self, invocation, start):
        return [
            {"__REALTIME_TIMESTAMP": str(int(start * 1e6)), "INVOCATION_ID": invocation, "JOB_TYPE": "start",
             "MESSAGE": "Starting pz-memory-publisher.service - Publish"},
            {"__REALTIME_TIMESTAMP": str(int((start + 1) * 1e6)), "_SYSTEMD_INVOCATION_ID": invocation,
             "MESSAGE": '{"status": "ok", "results": []}'},
            {"__REALTIME_TIMESTAMP": str(int((start + 2) * 1e6)), "INVOCATION_ID": invocation, "JOB_TYPE": "start",
             "JOB_RESULT": "done", "MESSAGE": "Finished pz-memory-publisher.service"},
        ]

    def _run(self, readback_code, listing_code=0):
        from memory_v1.harness import wait_for_publisher_run_after
        listing = "\n".join(json.dumps(r) for r in self._records("inv1", 1010))

        def fake_ssh(target, command, check=False):
            if "-o json" in command:
                return mock.Mock(returncode=listing_code, stdout=listing if listing_code == 0 else "", stderr="")
            return mock.Mock(returncode=readback_code, stdout="journal line\n" if readback_code == 0 else "", stderr="")

        with mock.patch("memory_v1.harness.ssh", side_effect=fake_ssh), mock.patch("memory_v1.harness.time.sleep"), \
                mock.patch("memory_v1.harness.time.time", side_effect=[0, 0, 1, 1, 99, 99]):
            return wait_for_publisher_run_after(CONTABO_TARGET, 1000, timeout=5)

    def test_run_with_readable_journal_is_evidence(self):
        self.assertEqual("inv1", self._run(0)["invocation_id"])

    def test_run_whose_journal_cannot_be_read_back_is_not_evidence(self):
        with self.assertRaises(TimeoutError):
            self._run(255)

    def test_failed_journal_listing_is_not_evidence(self):
        with self.assertRaises(TimeoutError):
            self._run(0, listing_code=255)

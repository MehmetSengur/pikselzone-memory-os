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

    def test_absent_marker_is_not_read_as_present(self):
        # "NOT_FOUND" contains "FOUND": a substring test on the negative answer
        # reported the startup bundle as already refreshed.
        from memory_v1.harness import wait_for_vps_publisher_refresh

        calls = []

        def fake_ssh(target, command, check=False):
            calls.append(command)
            return mock.Mock(returncode=0, stdout="MARKER_ABSENT\n", stderr="")

        with mock.patch("memory_v1.harness.ssh", side_effect=fake_ssh):
            with self.assertRaises(TimeoutError):
                wait_for_vps_publisher_refresh(self._target(), "PZ-M4-CANARY-abcd1234", timeout=1)

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

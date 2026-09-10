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

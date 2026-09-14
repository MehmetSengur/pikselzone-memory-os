"""Sync freshness is judged by sequence acknowledgement, each side on its own clock."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from memory_v1 import sync_heartbeat as hb
from memory_v1.core import MemoryConfig


class SyncHeartbeatTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name).resolve()
        self.vault = root / "vault"
        self.vault.mkdir()
        self.mac = MemoryConfig.from_dict({
            "role": "workstation", "vault_path": str(self.vault), "state_path": str(root / "mac-state"),
            "runtimes": ["claude", "codex"], "transcript_roots": {"claude": [str(root)], "codex": [str(root)]},
            "can_write_event_memory": True, "can_run_compiler": False,
            "models": {"flush": "gpt-5.6-luna", "compiler": "gpt-5.6-terra"}, "provider": {"mode": "runtime-native"},
        })
        self.vps = MemoryConfig.from_dict({
            "role": "memory-engine", "vault_path": str(self.vault), "state_path": str(root / "vps-state"),
            "runtimes": ["hermes"], "transcript_roots": {"hermes": [str(root)]},
            "can_write_event_memory": True, "can_run_compiler": True, "provider": {"mode": "runtime-native"},
        })
        patcher = mock.patch("memory_v1.learning_inbox.host_label", side_effect=self._host)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.current_host = "mac-studio"
        app = mock.patch.object(hb, "obsidian_app_running", return_value=False)
        app.start()
        self.addCleanup(app.stop)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _host(self, *_args):
        return self.current_host

    def _ack(self, now):
        self.current_host = "vmi3566230"
        try:
            return hb.acknowledge_heartbeats(self.vps, now=now)
        finally:
            self.current_host = "mac-studio"

    def test_heartbeat_is_rate_limited_and_sequenced(self):
        self.assertEqual(1, hb.write_heartbeat(self.mac, now=1000)["seq"])
        self.assertIsNone(hb.write_heartbeat(self.mac, now=1100))
        self.assertEqual(2, hb.write_heartbeat(self.mac, now=1000 + hb.MIN_INTERVAL_SECONDS)["seq"])

    def test_engine_does_not_heartbeat(self):
        self.assertIsNone(hb.write_heartbeat(self.vps, now=1000))

    def test_acknowledged_heartbeat_passes(self):
        hb.write_heartbeat(self.mac, now=1000)
        self._ack(now=5_000_000)  # engine clock may differ wildly; only sequence matters
        row = hb.sync_roundtrip_row(self.mac, now=1200)
        self.assertEqual("pass", row["status"])
        self.assertIn("seq=1 acknowledged", row["detail"])

    def test_old_acknowledgement_does_not_cover_newer_local_changes(self):
        hb.write_heartbeat(self.mac, now=1000)
        self._ack(now=1010)
        hb.write_heartbeat(self.mac, now=1000 + hb.MIN_INTERVAL_SECONDS)  # Obsidian closed from here on
        row = hb.sync_roundtrip_row(self.mac, now=1000 + hb.MIN_INTERVAL_SECONDS + hb.UNACKED_WARN_SECONDS + 60)
        self.assertEqual("warn", row["status"])
        self.assertIn("acked_seq=1", row["detail"])
        self.assertIn("obsidian_app=not-running", row["detail"])

    def test_recent_unacknowledged_heartbeat_is_in_flight(self):
        hb.write_heartbeat(self.mac, now=1000)
        row = hb.sync_roundtrip_row(self.mac, now=1060)
        self.assertEqual("pass", row["status"])
        self.assertIn("in-flight", row["detail"])

    def test_engine_ack_file_is_rewritten_only_for_new_sequences(self):
        hb.write_heartbeat(self.mac, now=1000)
        self.assertEqual(["mac-studio"], self._ack(now=2000)["new"])
        ack = next(hb.heartbeat_dir(self.vps).glob(f"{hb.ACK_PREFIX}*.md"))
        before = ack.read_bytes()
        self.assertEqual([], self._ack(now=3000)["new"])
        self.assertEqual(before, ack.read_bytes())

    def test_quiet_workstation_is_a_warning_on_the_engine_not_a_block(self):
        hb.write_heartbeat(self.mac, now=1000)
        self._ack(now=2000)
        row = hb.sync_roundtrip_row(self.vps, now=2000 + hb.FOREIGN_QUIET_WARN_SECONDS + 1)
        self.assertEqual("warn", row["status"])
        self.assertIn("engine-memory-still-usable", row["detail"])
        self.assertEqual("pass", hb.sync_roundtrip_row(self.vps, now=2100)["status"])


if __name__ == "__main__":
    unittest.main()

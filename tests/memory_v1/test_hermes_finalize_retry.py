"""Durable retry and bounded recovery for failed Hermes session finalization.

The production defect pinned here: ``on_session_finalize`` hit a provider 429,
logged that the source "remains retryable" and returned.  Nothing ever retried
it -- startup discovery never calls the provider and
``_recover_pending_turn_checkpoints`` has no caller -- and because the session's
``ended_at`` was already stamped it never received another finalize callback.
The session stayed unsettled indefinitely while the health row, being
last-write-wins, went green as soon as an unrelated session settled.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import importlib.util
import json
import os
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest import mock

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from memory_v1.core import MemoryConfig
from memory_v1.doctor import _hermes_finalize_rows

PLUGIN_PATH = _REPO_ROOT / "hermes_plugins" / "pz-memory-v1" / "__init__.py"
RETRY_MODULE_PATH = _REPO_ROOT / "hermes_plugins" / "pz-memory-v1" / "finalize_retry.py"


def load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_plugin():
    return load_module(PLUGIN_PATH, "pz_memory_v1_under_test")


def load_retry():
    return load_module(RETRY_MODULE_PATH, "pz_memory_v1_retry_under_test")


SUMMARY_WITH_CONTENT = {
    "status": "ok",
    "context": ["Finalize retry work."],
    "important_conversations": ["Operator asked why a finalized session never settled."],
    "decisions": ["Record every finalize failure durably."],
    "learnings": ["A preserved checkpoint nothing ever reads again is not durability."],
    "open_items": ["Confirm recall in a fresh session."],
    "evidence": ["COMMIT_SHA=70bb5ade89d3e4bf09ac7f89d87b882f8b209d11"],
}

USAGE_LIMIT_ERROR = RuntimeError(
    "Error code: 429 - {'error': {'type': 'usage_limit_reached', "
    "'message': 'The usage limit has been reached', 'plan_type': 'plus'}}"
)


class FinalizeRetryStateTests(unittest.TestCase):
    """The sidecar record itself: classification, bounds, and what it stores."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="pz-finalize-retry-")
        self.base = str(Path(self._tmp.name).resolve())
        self.retry = load_retry()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_provider_quota_is_retryable_and_scheduled(self) -> None:
        record = self.retry.record_failure(
            self.base, session_id="s1", source_sha="a" * 64, exc=USAGE_LIMIT_ERROR,
        )
        self.assertEqual(record["classification"], "retryable")
        self.assertEqual(record["reason_code"], "usage-limit")
        self.assertEqual(record["status"], "retry-scheduled")
        self.assertEqual(record["attempts"], 1)
        self.assertTrue(record["next_attempt_after"])

    def test_trust_and_schema_failures_are_permanent(self) -> None:
        for exc, expected in (
            (PermissionError("model-override-denied"), "trust-denied"),
            (ValueError("json schema validation failed"), "schema"),
        ):
            record = self.retry.record_failure(
                self.base, session_id=f"s-{expected}", source_sha="b" * 64, exc=exc,
            )
            self.assertEqual(record["classification"], "permanent", expected)
            self.assertEqual(record["reason_code"], expected)
            self.assertIsNone(record["next_attempt_after"])
            self.assertFalse(self.retry.is_due(record))

    def test_unknown_failure_is_held_not_retried_forever(self) -> None:
        record = self.retry.record_failure(
            self.base, session_id="s2", source_sha="c" * 64,
            exc=RuntimeError("something nobody has classified yet"),
        )
        self.assertEqual(record["classification"], "unknown")
        self.assertEqual(record["status"], "hold-unclassified")
        self.assertFalse(self.retry.is_due(record))

    def test_record_never_stores_the_raw_provider_message(self) -> None:
        secret = RuntimeError("usage_limit_reached for account bearer sk-live-SHOULD-NOT-PERSIST")
        self.retry.record_failure(
            self.base, session_id="s3", source_sha="d" * 64, exc=secret,
        )
        raw = Path(self.retry.record_path(self.base, "s3", "d" * 64)).read_text(encoding="utf-8")
        self.assertNotIn("SHOULD-NOT-PERSIST", raw)
        self.assertIn("usage-limit", raw)
        self.assertIn("RuntimeError", raw)

    def test_backoff_grows_and_attempts_are_capped(self) -> None:
        now = dt.datetime(2026, 9, 16, 12, 0, 0).astimezone()
        waits = []
        for attempt in range(1, self.retry.MAX_ATTEMPTS + 1):
            record = self.retry.record_failure(
                self.base, session_id="s4", source_sha="e" * 64,
                exc=TimeoutError("request timed out"), now=now,
            )
            self.assertEqual(record["attempts"], attempt)
            scheduled = self.retry.parse_iso(record["next_attempt_after"])
            if scheduled is not None:
                waits.append((scheduled - now).total_seconds())
        self.assertEqual(waits, sorted(waits))
        self.assertLess(waits[0], waits[-1])
        self.assertEqual(record["status"], "retry-exhausted")
        self.assertFalse(self.retry.is_due(record))

    def test_record_survives_a_restart_and_stays_due_only_after_backoff(self) -> None:
        now = dt.datetime(2026, 9, 16, 12, 0, 0).astimezone()
        self.retry.record_failure(
            self.base, session_id="s5", source_sha="f" * 64, exc=USAGE_LIMIT_ERROR, now=now,
        )
        # A restart is a fresh import reading the same directory.
        reloaded = load_retry()
        record = reloaded.load_record(self.base, "s5", "f" * 64)
        self.assertEqual(record["attempts"], 1)
        self.assertFalse(reloaded.is_due(record, now=now))
        self.assertTrue(reloaded.is_due(record, now=now + dt.timedelta(hours=1)))
        self.assertEqual(
            [item["session_id"] for item in reloaded.due_records(
                self.base, now=now + dt.timedelta(hours=1))],
            ["s5"],
        )

    def test_due_selection_is_bounded(self) -> None:
        now = dt.datetime(2026, 9, 16, 12, 0, 0).astimezone()
        for index in range(5):
            self.retry.record_failure(
                self.base, session_id=f"many-{index}", source_sha=f"{index}" * 64,
                exc=USAGE_LIMIT_ERROR, now=now,
            )
        due = self.retry.due_records(self.base, now=now + dt.timedelta(hours=2), limit=2)
        self.assertEqual(len(due), 2)


class HermesPluginFixture:
    """Disposable Hermes runtime: fake SessionDB, profiles, PluginLlm and config.

    Shared with the review-findings regressions so both suites drive the same
    plugin surface instead of two drifting copies of it.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="pz-finalize-recovery-")
        self.root = Path(self._tmp.name).resolve()
        self.hermes_data = self.root / "hermes-data"
        self.base = self.hermes_data / "memory-v1"
        self.state = self.base / "state"
        self.profiles = self.hermes_data / "profiles"
        (self.base / "outbox" / "events").mkdir(parents=True)
        (self.base / "outbox" / "evidence").mkdir(parents=True)
        self.state.mkdir(parents=True)
        (self.state / "checkpoints").mkdir()
        self.vault = self.root / "vault"
        (self.vault / "daily").mkdir(parents=True)
        (self.vault / "knowledge").mkdir(parents=True)
        self.engine_state = self.root / "engine-state"
        self.engine_state.mkdir()

        self.plugin = load_plugin()
        self.retry = load_retry()
        self.session_id = "20260915_105210_e3e7ad"
        self.messages = [
            {"role": "user", "content": "Kayıt akışında ne bozuldu?"},
            {"role": "assistant", "content": "Finalize hatası kalıcı olarak kaydedilmiyordu."},
        ]
        self.databases: dict[str, list[dict]] = {}
        self.opened: list[str] = []
        self.llm_calls: list[str] = []
        self._add_session("pz-orchestrator", self.session_id, self.messages, ended=True)
        self.env = {
            "PZ_MEMORY_BASE_DIR": str(self.base),
            "PZ_MEMORY_TEST_MODE": "1",
            # Keep the background trigger out of the way; it has its own test.
            "PZ_MEMORY_FINALIZE_RETRY": "0",
        }

    def tearDown(self) -> None:
        self._tmp.cleanup()

    # -- fixture helpers --------------------------------------------------
    def _add_session(self, profile: str, session_id: str, messages, *, ended: bool) -> Path:
        home = self.profiles / profile
        home.mkdir(parents=True, exist_ok=True)
        db_path = home / "state.db"
        db_path.touch()
        self.databases.setdefault(str(db_path.resolve()), []).append({
            "id": session_id,
            "model": "gpt-6-astra",
            "handoff_state": "task-1",
            "ended_at": 1789462159.38 if ended else None,
            "end_reason": "ws_orphan_reap" if ended else None,
            "messages": messages,
        })
        return db_path

    def _runtime(self, *, active_profile: str = "pz-orchestrator", summary=None, raises=None):
        """Install the supported Hermes interfaces, nothing more."""
        databases = self.databases
        opened = self.opened
        llm_calls = self.llm_calls
        homes = {path.name: path for path in self.profiles.iterdir()}

        class FakeSessionDB:
            def __init__(inner, db_path=None, read_only=False):
                key = str(Path(db_path).resolve())
                opened.append(key)
                if key not in databases:
                    raise RuntimeError("unknown-database")
                inner.sessions = databases[key]

            def get_session(inner, session_id):
                for item in inner.sessions:
                    if item["id"] == session_id:
                        return {k: v for k, v in item.items() if k != "messages"}
                return None

            def export_session(inner, session_id):
                for item in inner.sessions:
                    if item["id"] == session_id:
                        return dict(item)
                return None

            def close(inner):
                return None

        state_module = types.ModuleType("hermes_state")
        state_module.SessionDB = FakeSessionDB

        constants = types.ModuleType("hermes_constants")
        home_box = {"home": homes[active_profile]}
        constants.get_hermes_home = lambda: home_box["home"]

        def set_override(value):
            previous = home_box["home"]
            home_box["home"] = Path(value)
            return previous

        constants.set_hermes_home_override = set_override
        constants.reset_hermes_home_override = lambda token: home_box.update(home=token)

        facade = mock.Mock()

        def complete_structured(**kwargs):
            llm_calls.append(str(home_box["home"]))
            if raises is not None:
                raise raises
            return types.SimpleNamespace(
                parsed=summary if summary is not None else SUMMARY_WITH_CONTENT,
                provider="openai-codex", model="gpt-5.6-luna",
            )

        facade.complete_structured.side_effect = complete_structured
        llm_module = types.ModuleType("agent.plugin_llm")
        llm_module.PluginLlm = mock.Mock(return_value=facade)
        llm_module.PluginLlmTextInput = lambda **kw: kw

        return mock.patch.dict(sys.modules, {
            "hermes_state": state_module,
            "hermes_constants": constants,
            "agent.plugin_llm": llm_module,
        })

    def _settlement_path(self, session_id: str) -> Path:
        """The settlement for a session, whichever owner wrote it."""
        digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:32]
        directory = self.state / "settlements"
        matches = sorted(directory.glob(f"hermes-{digest}*.json"))
        return matches[0] if matches else directory / f"hermes-{digest}.json"

    def _transcript(self, messages=None) -> str:
        items = messages if messages is not None else self.messages
        return "\n".join(
            ("USER: " if item["role"] == "user" else "ASSISTANT: ") + item["content"]
            for item in items
        )

    def _source_sha(self, messages=None) -> str:
        return hashlib.sha256(self._transcript(messages).encode("utf-8")).hexdigest()

    def _retry_records(self) -> list[dict]:
        return self.retry.iter_records(str(self.base))

    def _flush_health(self) -> dict:
        path = self.base / "outbox" / "evidence" / "flush-hermes.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}

    def _events(self) -> list[Path]:
        return sorted((self.base / "outbox" / "events").glob("*.md"))

    def _config(self) -> MemoryConfig:
        return MemoryConfig.from_dict({
            "role": "memory-engine",
            "vault_path": str(self.vault),
            "state_path": str(self.engine_state),
            "runtimes": ["hermes"],
            "transcript_roots": {"hermes": [str(self.hermes_data)]},
            "can_write_event_memory": True,
            "can_run_compiler": True,
            "provider": {"mode": "runtime-native"},
        })


class HermesFinalizeRecoveryTests(HermesPluginFixture, unittest.TestCase):
    """The live plugin path: failure is recorded, and recovery settles it once."""

    # -- tests ------------------------------------------------------------
    def test_transient_failure_is_recorded_then_recovered_when_due(self) -> None:
        with self._runtime(raises=USAGE_LIMIT_ERROR), mock.patch.dict(os.environ, self.env):
            self.plugin.on_session_end(session_id=self.session_id)
            self.plugin.on_session_finalize(session_id=self.session_id)

        self.assertFalse(self._settlement_path(self.session_id).exists())
        self.assertEqual(self._events(), [])
        records = self._retry_records()
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(record["session_id"], self.session_id)
        self.assertEqual(record["source_sha256"], self._source_sha())
        self.assertEqual(record["status"], "retry-scheduled")
        self.assertEqual(record["reason_code"], "usage-limit")
        self.assertEqual(record["profile"], "pz-orchestrator")
        self.assertEqual(self._flush_health()["status"], "blocked")
        # The raw turn is still preserved; nothing was consumed by the failure.
        self.assertTrue(list((self.state / "checkpoints").glob("*.json")))

        later = dt.datetime.now().astimezone() + dt.timedelta(hours=2)
        with self._runtime(), mock.patch.dict(os.environ, self.env):
            result = self.plugin.run_due_finalize_retries(now=later)

        self.assertEqual(result["outcomes"], {"settled": 1})
        settlement = json.loads(self._settlement_path(self.session_id).read_text(encoding="utf-8"))
        self.assertEqual(settlement["status"], "staged-event")
        self.assertEqual(settlement["source_sha256"], self._source_sha())
        self.assertEqual(len(self._events()), 1)
        self.assertEqual(self._retry_records(), [])
        self.assertEqual(self._flush_health()["status"], "ok")

    def test_recovery_evidence_is_not_presented_as_a_native_finalize(self) -> None:
        with self._runtime(raises=USAGE_LIMIT_ERROR), mock.patch.dict(os.environ, self.env):
            self.plugin.on_session_finalize(session_id=self.session_id)
        later = dt.datetime.now().astimezone() + dt.timedelta(hours=2)
        with self._runtime(), mock.patch.dict(os.environ, self.env):
            self.plugin.run_due_finalize_retries(now=later)

        evidence_files = sorted((self.base / "outbox" / "evidence").glob("hermes-*.json"))
        self.assertEqual(len(evidence_files), 1)
        evidence = json.loads(evidence_files[0].read_text(encoding="utf-8"))
        self.assertEqual(evidence["provenance"], "hermes-retry-recovery")
        self.assertEqual(evidence["status"], "unverified")
        self.assertNotIn("lifecycle_receipt", evidence)
        event_text = self._events()[0].read_text(encoding="utf-8")
        self.assertIn('event: "checkpoint_recovery"', event_text)

    def test_reopened_session_is_left_to_its_own_boundary(self) -> None:
        with self._runtime(raises=USAGE_LIMIT_ERROR), mock.patch.dict(os.environ, self.env):
            self.plugin.on_session_finalize(session_id=self.session_id)
        # The user resumed it: Hermes clears ended_at.
        for row in self.databases[str((self.profiles / "pz-orchestrator" / "state.db").resolve())]:
            row["ended_at"] = None

        later = dt.datetime.now().astimezone() + dt.timedelta(hours=2)
        calls_before = len(self.llm_calls)
        with self._runtime(), mock.patch.dict(os.environ, self.env):
            result = self.plugin.run_due_finalize_retries(now=later)

        self.assertEqual(result["outcomes"], {"session-active": 1})
        self.assertEqual(len(self.llm_calls), calls_before)
        self.assertFalse(self._settlement_path(self.session_id).exists())
        self.assertEqual(len(self._retry_records()), 1)

    def test_superseded_source_is_dropped_without_a_provider_call(self) -> None:
        with self._runtime(raises=USAGE_LIMIT_ERROR), mock.patch.dict(os.environ, self.env):
            self.plugin.on_session_finalize(session_id=self.session_id)
        # A later turn landed: the recorded digest is no longer the source.
        key = str((self.profiles / "pz-orchestrator" / "state.db").resolve())
        self.databases[key][0]["messages"] = self.messages + [
            {"role": "user", "content": "Bir soru daha"},
            {"role": "assistant", "content": "Bir cevap daha"},
        ]
        self.databases[key][0]["ended_at"] = 1789462999.0

        later = dt.datetime.now().astimezone() + dt.timedelta(hours=2)
        calls_before = len(self.llm_calls)
        with self._runtime(), mock.patch.dict(os.environ, self.env):
            result = self.plugin.run_due_finalize_retries(now=later)

        self.assertEqual(result["outcomes"], {"superseded": 1})
        self.assertEqual(len(self.llm_calls), calls_before)
        self.assertEqual(self._retry_records(), [])

    def test_recovery_summarizes_inside_the_owning_profile(self) -> None:
        # A second profile holds a row with the SAME session id and different
        # content.  Recovery must never read it.
        other_db = self._add_session(
            "pz-engineering", self.session_id,
            [{"role": "user", "content": "başka profil"},
             {"role": "assistant", "content": "başka içerik"}],
            ended=True,
        )
        with self._runtime(raises=USAGE_LIMIT_ERROR), mock.patch.dict(os.environ, self.env):
            self.plugin.on_session_finalize(session_id=self.session_id)
        record = self._retry_records()[0]
        self.assertEqual(Path(record["database"]).parent.name, "pz-orchestrator")

        self.opened.clear()
        later = dt.datetime.now().astimezone() + dt.timedelta(hours=2)
        with self._runtime(active_profile="pz-engineering"), mock.patch.dict(os.environ, self.env):
            result = self.plugin.run_due_finalize_retries(now=later)

        self.assertEqual(result["outcomes"], {"settled": 1})
        self.assertNotIn(str(other_db.resolve()), self.opened)
        # The summarizer ran under the owning profile's home, not the active one.
        self.assertEqual(Path(self.llm_calls[-1]).name, "pz-orchestrator")

    def test_a_concurrent_attempt_does_not_double_process(self) -> None:
        with self._runtime(raises=USAGE_LIMIT_ERROR), mock.patch.dict(os.environ, self.env):
            self.plugin.on_session_finalize(session_id=self.session_id)

        later = dt.datetime.now().astimezone() + dt.timedelta(hours=2)
        owning_db = self._retry_records()[0]["database"]
        with mock.patch.dict(os.environ, self.env):
            self.assertTrue(
                self.plugin._acquire_execution_lock(self.session_id, database=owning_db)
            )
            try:
                with self._runtime():
                    result = self.plugin.run_due_finalize_retries(now=later)
            finally:
                self.plugin._release_execution_lock(self.session_id, database=owning_db)

        self.assertEqual(result["outcomes"], {"locked": 1})
        self.assertEqual(self._events(), [])
        self.assertEqual(len(self._retry_records()), 1)

    def test_content_artifact_is_produced_exactly_once(self) -> None:
        # Settlement write fails after the artifact is staged.
        with self._runtime(), mock.patch.dict(os.environ, self.env), \
             mock.patch.object(self.plugin, "_mark_durable_settlement", return_value=False):
            self.plugin.on_session_finalize(session_id=self.session_id)

        self.assertEqual(len(self._events()), 1)
        record = self._retry_records()[0]
        self.assertEqual(record["reason_code"], "settlement-write")
        self.assertEqual(record["classification"], "retryable")
        self.assertEqual(self._flush_health()["status"], "blocked")

        calls_before = len(self.llm_calls)
        later = dt.datetime.now().astimezone() + dt.timedelta(hours=2)
        with self._runtime(), mock.patch.dict(os.environ, self.env):
            result = self.plugin.run_due_finalize_retries(now=later)

        self.assertEqual(result["outcomes"], {"settled": 1})
        # The staged artifact was reused: no second provider call, one file.
        self.assertEqual(len(self.llm_calls), calls_before)
        self.assertEqual(len(self._events()), 1)
        self.assertEqual(self._retry_records(), [])

    def test_validated_empty_settles_and_clears_the_record(self) -> None:
        with self._runtime(raises=USAGE_LIMIT_ERROR), mock.patch.dict(os.environ, self.env):
            self.plugin.on_session_finalize(session_id=self.session_id)
        later = dt.datetime.now().astimezone() + dt.timedelta(hours=2)
        with self._runtime(summary={"status": "empty"}), mock.patch.dict(os.environ, self.env):
            result = self.plugin.run_due_finalize_retries(now=later)

        self.assertEqual(result["outcomes"], {"settled": 1})
        settlement = json.loads(self._settlement_path(self.session_id).read_text(encoding="utf-8"))
        self.assertEqual(settlement["status"], "validated-empty")
        self.assertEqual(self._retry_records(), [])
        self.assertEqual(self._flush_health()["detail"], "no-memory")

    def test_validated_empty_that_cannot_be_persisted_is_not_a_success(self) -> None:
        with self._runtime(summary={"status": "empty"}), mock.patch.dict(os.environ, self.env), \
             mock.patch.object(self.plugin, "_mark_durable_settlement", return_value=False):
            self.plugin.on_session_end(session_id=self.session_id)
            self.plugin.on_session_finalize(session_id=self.session_id)

        self.assertFalse(self._settlement_path(self.session_id).exists())
        record = self._retry_records()[0]
        self.assertEqual(record["reason_code"], "settlement-write")
        self.assertEqual(self._flush_health()["status"], "blocked")
        # The raw checkpoint must survive a settlement that was never written.
        self.assertTrue(list((self.state / "checkpoints").glob("*.json")))

    def test_permanent_failure_is_never_auto_retried(self) -> None:
        with self._runtime(raises=PermissionError("model-override-denied")), \
             mock.patch.dict(os.environ, self.env):
            self.plugin.on_session_finalize(session_id=self.session_id)

        record = self._retry_records()[0]
        self.assertEqual(record["status"], "permanent")
        later = dt.datetime.now().astimezone() + dt.timedelta(days=7)
        calls_before = len(self.llm_calls)
        with self._runtime(), mock.patch.dict(os.environ, self.env):
            result = self.plugin.run_due_finalize_retries(now=later)
        self.assertEqual(result["outcomes"], {})
        self.assertEqual(len(self.llm_calls), calls_before)

    def test_trigger_is_bounded_and_never_runs_at_registration(self) -> None:
        with self._runtime(raises=USAGE_LIMIT_ERROR), mock.patch.dict(os.environ, self.env):
            self.plugin.on_session_finalize(session_id=self.session_id)

        enabled = dict(self.env, PZ_MEMORY_FINALIZE_RETRY="1")
        started: list[str] = []

        class FakeThread:
            def __init__(self, target=None, args=(), name="", daemon=False):
                started.append(name)
                self.target, self.args = target, args

            def start(self):
                return None

        # Registration stays provider-free: raw discovery only, no retry run.
        with mock.patch.dict(os.environ, enabled), \
             mock.patch.object(self.plugin, "_discover_final_turn_checkpoints") as discover, \
             mock.patch.object(self.plugin.threading, "Thread", FakeThread):
            self.plugin.register(types.SimpleNamespace(register_hook=lambda *a, **k: None))
        self.assertTrue(discover.called)
        self.assertEqual(started, [])

        # A lifecycle event does trigger it, and only once inside the interval.
        self.plugin._LAST_RETRY_RUN_AT = 0.0
        with mock.patch.dict(os.environ, enabled), \
             mock.patch.object(self.plugin.threading, "Thread", FakeThread), \
             mock.patch.object(
                 self.plugin, "_finalize_retry_module",
                 return_value=types.SimpleNamespace(due_records=lambda *a, **k: [{"x": 1}]),
             ):
            self.plugin._maybe_trigger_finalize_retries("test")
            self.plugin._maybe_trigger_finalize_retries("test")
        self.assertEqual(started, ["pz-memory-finalize-retry"])

    def test_disabled_trigger_still_records_failures(self) -> None:
        with self._runtime(raises=USAGE_LIMIT_ERROR), mock.patch.dict(os.environ, self.env):
            self.plugin.on_session_end(session_id=self.session_id)
            self.plugin.on_session_finalize(session_id=self.session_id)
        self.assertEqual(len(self._retry_records()), 1)

    # -- doctor visibility -------------------------------------------------
    def test_doctor_reports_retry_state_and_legacy_backlog_separately(self) -> None:
        # One failed session under the new contract.
        with self._runtime(raises=USAGE_LIMIT_ERROR), mock.patch.dict(os.environ, self.env):
            self.plugin.on_session_finalize(session_id=self.session_id)
        # Two legacy sessions: raw checkpoints, no settlement, no retry record.
        for index in range(2):
            legacy = f"2026090{index}_120000_legacy{index}"
            digest = hashlib.sha256(legacy.encode("utf-8")).hexdigest()[:32]
            (self.state / "checkpoints" / f"hermes-{digest}-{'a' * 16}.json").write_text(
                json.dumps({
                    "schema": "pikselzone-memory-turn-checkpoint-v2",
                    "runtime": "hermes", "session_id": legacy,
                    "turn_digest": "a" * 64, "normalized_transcript": "USER: x\nASSISTANT: y",
                    "observed_at": "2026-09-09T15:35:00+03:00",
                }), encoding="utf-8",
            )

        rows = {row["check"]: row for row in _hermes_finalize_rows(self._config())}
        self.assertEqual(rows["hermes_finalize_retry"]["status"], "warn")
        self.assertIn("scheduled=1", rows["hermes_finalize_retry"]["detail"])
        self.assertEqual(rows["hermes_finalize_backlog"]["status"], "warn")
        self.assertIn("unresolved_sessions=2", rows["hermes_finalize_backlog"]["detail"])

    def test_a_healthy_latest_flush_does_not_hide_the_backlog(self) -> None:
        with self._runtime(raises=USAGE_LIMIT_ERROR), mock.patch.dict(os.environ, self.env):
            self.plugin.on_session_finalize(session_id=self.session_id)
        # An unrelated session settles normally and overwrites the health row.
        self._add_session("pz-orchestrator", "20260915_131929_c4a942", [
            {"role": "user", "content": "ikinci oturum"},
            {"role": "assistant", "content": "tamam"},
        ], ended=True)
        with self._runtime(summary={"status": "empty"}), mock.patch.dict(os.environ, self.env):
            self.plugin.on_session_finalize(session_id="20260915_131929_c4a942")

        self.assertEqual(self._flush_health()["status"], "ok")
        rows = {row["check"]: row for row in _hermes_finalize_rows(self._config())}
        self.assertEqual(rows["hermes_finalize_retry"]["status"], "warn")
        self.assertIn("scheduled=1", rows["hermes_finalize_retry"]["detail"])

    def test_legacy_sessions_are_visible_but_never_processed(self) -> None:
        legacy = "20260909_154619_d3dda2"
        digest = hashlib.sha256(legacy.encode("utf-8")).hexdigest()[:32]
        (self.state / "checkpoints" / f"hermes-{digest}-{'b' * 16}.json").write_text(
            json.dumps({
                "schema": "pikselzone-memory-turn-checkpoint-v2",
                "runtime": "hermes", "session_id": legacy, "turn_digest": "b" * 64,
                "normalized_transcript": "USER: eski\nASSISTANT: kayıt",
                "observed_at": "2026-09-09T15:46:00+03:00",
            }), encoding="utf-8",
        )
        later = dt.datetime.now().astimezone() + dt.timedelta(days=7)
        with self._runtime(), mock.patch.dict(os.environ, self.env):
            result = self.plugin.run_due_finalize_retries(now=later)

        self.assertEqual(result["outcomes"], {})
        self.assertEqual(self.llm_calls, [])
        rows = {row["check"]: row for row in _hermes_finalize_rows(self._config())}
        self.assertIn(legacy, rows["hermes_finalize_backlog"]["detail"])

    def test_backlog_row_is_not_applicable_without_hermes_state(self) -> None:
        config = MemoryConfig.from_dict({
            "role": "workstation",
            "vault_path": str(self.vault),
            "state_path": str(self.engine_state),
            "runtimes": ["codex", "claude"],
            "transcript_roots": {"codex": [str(self.root)], "claude": [str(self.root)]},
            "can_write_event_memory": True,
            "can_run_compiler": False,
            "provider": {"mode": "runtime-native"},
        })
        rows = {row["check"]: row for row in _hermes_finalize_rows(config)}
        self.assertEqual(rows["hermes_finalize_retry"]["status"], "not-applicable")


class DiscoveryCursorWarningTests(unittest.TestCase):
    """A valid record for another profile's database is not a broken record."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="pz-cursor-")
        self.root = Path(self._tmp.name).resolve()
        self.plugin = load_plugin()
        self.db_a = self.root / "profiles" / "a" / "state.db"
        self.db_b = self.root / "profiles" / "b" / "state.db"
        for path in (self.db_a, self.db_b):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _entry(self, db_path: Path, session_id: str) -> tuple[str, dict]:
        return self.plugin._discovery_entry(
            profile=db_path.parent.name, database=db_path, session_id=session_id,
            digest=None, observed_at="2026-09-15T13:20:52+03:00",
        )

    def test_other_database_entries_are_skipped_silently(self) -> None:
        sessions = dict([self._entry(self.db_a, "s-a"), self._entry(self.db_b, "s-b")])
        logger = self.plugin.logger
        with mock.patch.object(logger, "warning") as warn:
            tracked = self.plugin._tracked_session_ids_for_database(sessions, self.db_a)
        self.assertEqual(tracked, ["s-a"])
        warn.assert_not_called()

    def test_structurally_broken_entries_still_warn(self) -> None:
        identity, entry = self._entry(self.db_a, "s-a")
        sessions = {
            identity: entry,
            "not-a-matching-identity": {"session_id": "s-a", "database": str(self.db_a.resolve())},
            "broken": {"session_id": 17, "database": str(self.db_a.resolve())},
        }
        with mock.patch.object(self.plugin.logger, "warning") as warn:
            tracked = self.plugin._tracked_session_ids_for_database(sessions, self.db_a)
        self.assertEqual(tracked, ["s-a"])
        self.assertEqual(warn.call_count, 2)


if __name__ == "__main__":
    unittest.main()

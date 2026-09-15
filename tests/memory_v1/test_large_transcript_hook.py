"""Lifecycle hooks on transcripts larger than the 20 MiB read ceiling.

Live on 2026-09-16: a resumed Codex thread kept appending to its original
rollout until it reached 22.8 MB.  The capture read raised
``secure-read-too-large`` and the hook exited 2, so Codex treated every Stop as
a continuation request ("Stop hook exited with code 2 but did not write a
continuation prompt to stderr") and no Stop, PreCompact or SessionEnd of that
session was captured again.

Pinned here:

* a >20 MiB Codex rollout or Claude JSONL still yields Stop, PreCompact and
  SessionEnd checkpoints from its newest window, with exit 0;
* the tail window drops only a leading partial line, keeps a window that
  starts on a line boundary, and keeps every secure-read guarantee;
* exit-code contract: every hook event exits 0 on failure and records blocked
  health, because exit 2 is a runtime decision, not an error code.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import memory_v1.core as core
import memory_v1.hook_runner as hook_runner
from memory_v1.core import MemoryConfig, PolicyError, secure_read_file, session_key, transcript_turns


MIB = 1024 * 1024
FILLER_LINES = 21  # each ~1 MiB, so the file exceeds the 20 MiB ceiling


def _codex_lines(head_marker: str, user: str, assistant: str) -> list[str]:
    lines = [
        json.dumps({"type": "session_meta", "payload": {"originator": "codex-tui"}}),
        json.dumps({"type": "event_msg", "payload": {"type": "user_message", "message": head_marker}}),
        json.dumps({"type": "event_msg", "payload": {"type": "agent_message", "message": "Head answer."}}),
    ]
    filler = "x" * (MIB - 200)
    lines += [
        json.dumps({"type": "response_item", "payload": {"type": "function_call_output", "output": filler}})
        for _ in range(FILLER_LINES)
    ]
    lines += [
        json.dumps({"type": "event_msg", "payload": {"type": "user_message", "message": user}}),
        json.dumps({"type": "event_msg", "payload": {"type": "agent_message", "message": assistant}}),
    ]
    return lines


def _claude_lines(head_marker: str, user: str, assistant: str) -> list[str]:
    lines = [
        json.dumps({"type": "user", "message": {"role": "user", "content": head_marker}}),
        json.dumps({"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "Head answer."}]}}),
    ]
    filler = "y" * (MIB - 200)
    lines += [json.dumps({"type": "progress", "data": filler}) for _ in range(FILLER_LINES)]
    lines += [
        json.dumps({"type": "user", "message": {"role": "user", "content": user}}),
        json.dumps({"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": assistant}]}}),
    ]
    return lines


class LargeTranscriptBase(unittest.TestCase):
    def setUp(self) -> None:
        from memory_v1 import project_registry

        self.temp = tempfile.TemporaryDirectory(prefix="pz-large-test-")
        self.root = Path(self.temp.name).resolve()
        self.vault = self.root / "vault"
        self.state = self.root / "state"
        self.transcripts = self.root / "transcripts"
        self.repo = self.root / "repo"
        for directory in (self.vault, self.state, self.transcripts, self.repo):
            directory.mkdir()
        self.config = MemoryConfig.from_dict({
            "role": "workstation",
            "vault_path": str(self.vault),
            "state_path": str(self.state),
            "runtimes": ["codex", "claude"],
            "transcript_roots": {"codex": [str(self.transcripts)], "claude": [str(self.transcripts)]},
            "can_write_event_memory": True,
            "can_run_compiler": False,
            "models": {"flush": "gpt-5.6-luna", "compiler": "gpt-5.6-terra"},
            "provider": {},
        })
        project_registry.register(self.state, self.repo, "demo")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _write(self, name: str, lines: list[str]) -> Path:
        path = self.transcripts / name
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path

    def _hook(self, runtime: str, event: str, payload: dict) -> tuple[int, str]:
        out = io.StringIO()
        with mock.patch.object(hook_runner.MemoryConfig, "load", return_value=self.config), \
             mock.patch.object(hook_runner, "_spawn_drain"), \
             mock.patch("sys.stdin", io.StringIO(json.dumps(payload))), \
             contextlib.redirect_stdout(out):
            rc = hook_runner.main([
                "--config", str(self.root / "config.json"), "--runtime", runtime,
                "--event", event, "--project", "demo", "--project-root", str(self.repo),
            ])
        return rc, out.getvalue()

    def _health(self, runtime: str) -> dict:
        return json.loads((self.state / "health" / f"hook-{runtime}.json").read_text(encoding="utf-8"))

    def _pending(self, runtime: str, session_id: str, event: str) -> list[dict]:
        pattern = f"{runtime}-{session_key(session_id)}-{event}-*.json"
        return [
            json.loads(path.read_text(encoding="utf-8"))
            for path in sorted((self.state / "queue" / "pending").glob(pattern))
        ]


class LargeTranscriptCaptureTests(LargeTranscriptBase):
    def test_codex_stop_on_rollout_over_20_mib_writes_checkpoint_and_exits_0(self):
        rollout = self._write("rollout-big.jsonl", _codex_lines(
            "HEAD-ONLY-MARKER", "Latest question on a resumed thread.", "Latest answer.",
        ))
        self.assertGreater(rollout.stat().st_size, core.TRANSCRIPT_READ_MAX_BYTES)
        rc, stdout = self._hook("codex", "Stop", {
            "session_id": "sess-big-stop", "turn_id": "turn-latest",
            "transcript_path": str(rollout), "cwd": str(self.repo), "hook_event_name": "Stop",
        })
        self.assertEqual(0, rc)
        self.assertEqual("", stdout, "no continuation or block decision is emitted")
        checkpoints = self._pending("codex", "sess-big-stop", "turn_complete")
        self.assertEqual(1, len(checkpoints))
        self.assertEqual(
            "USER: Latest question on a resumed thread.\nASSISTANT: Latest answer.",
            checkpoints[0]["normalized_transcript"],
        )
        self.assertEqual({"status": "ok", "detail": "lifecycle-ok:Stop"},
                         {k: self._health("codex")[k] for k in ("status", "detail")})

    def test_codex_precompact_and_session_end_on_rollout_over_20_mib(self):
        rollout = self._write("rollout-big-terminal.jsonl", _codex_lines(
            "HEAD-ONLY-MARKER", "Wrap up the resumed work.", "Wrapped up.",
        ))
        for event, name in (("PreCompact", "pre_compact"), ("SessionEnd", "session_end")):
            with self.subTest(event=event):
                rc, stdout = self._hook("codex", event, {
                    "session_id": "sess-big-terminal", "transcript_path": str(rollout),
                    "cwd": str(self.repo), "hook_event_name": event,
                })
                self.assertEqual(0, rc)
                self.assertEqual("", stdout)
                checkpoints = self._pending("codex", "sess-big-terminal", name)
                self.assertEqual(1, len(checkpoints))
                text = checkpoints[0]["normalized_transcript"]
                self.assertTrue(text.endswith("USER: Wrap up the resumed work.\nASSISTANT: Wrapped up."))
                self.assertNotIn("HEAD-ONLY-MARKER", text, "only the newest window is read")
                self.assertEqual(f"lifecycle-ok:{event}", self._health("codex")["detail"])

    def test_claude_session_end_on_jsonl_over_20_mib(self):
        transcript = self._write("claude-big.jsonl", _claude_lines(
            "HEAD-ONLY-MARKER", "Claude long session question.", "Claude long session answer.",
        ))
        self.assertGreater(transcript.stat().st_size, core.TRANSCRIPT_READ_MAX_BYTES)
        rc, stdout = self._hook("claude", "SessionEnd", {
            "session_id": "sess-claude-big", "transcript_path": str(transcript),
            "cwd": str(self.repo), "hook_event_name": "SessionEnd",
        })
        self.assertEqual(0, rc)
        self.assertEqual("", stdout)
        checkpoints = self._pending("claude", "sess-claude-big", "session_end")
        self.assertEqual(1, len(checkpoints))
        self.assertTrue(checkpoints[0]["normalized_transcript"].endswith(
            "USER: Claude long session question.\nASSISTANT: Claude long session answer."
        ))


class TailWindowTests(LargeTranscriptBase):
    def _file(self, name: str, data: bytes) -> Path:
        path = self.transcripts / name
        path.write_bytes(data)
        return path

    def test_mid_line_window_drops_only_the_partial_line(self):
        data = b"first line\nsecond line\nthird line\n"
        path = self._file("mid.jsonl", data)
        window = len(data) - 5  # starts inside "first line"
        read, _ = secure_read_file(path, root=self.transcripts, max_bytes=window, tail=True)
        self.assertEqual(b"second line\nthird line\n", read)

    def test_window_starting_on_a_line_boundary_keeps_that_line(self):
        data = b"first line\nsecond line\nthird line\n"
        path = self._file("boundary.jsonl", data)
        window = len(data) - len(b"first line\n")
        read, _ = secure_read_file(path, root=self.transcripts, max_bytes=window, tail=True)
        self.assertEqual(b"second line\nthird line\n", read)
        window = len(data) - len(b"first line\nsecond line\n")
        read, _ = secure_read_file(path, root=self.transcripts, max_bytes=window, tail=True)
        self.assertEqual(b"third line\n", read)

    def test_window_without_a_line_break_fails_closed(self):
        path = self._file("one-line.jsonl", b"x" * 100)
        with self.assertRaises(PolicyError) as caught:
            secure_read_file(path, root=self.transcripts, max_bytes=40, tail=True)
        self.assertEqual("secure-read-tail-without-line-boundary", str(caught.exception))

    def test_window_whose_only_line_break_is_the_last_byte_fails_closed(self):
        path = self._file("trailing-newline.jsonl", b"x" * 100 + b"\n")
        with self.assertRaises(PolicyError) as caught:
            secure_read_file(path, root=self.transcripts, max_bytes=40, tail=True)
        self.assertEqual("secure-read-tail-without-line-boundary", str(caught.exception))

    def test_small_file_and_non_tail_callers_are_unchanged(self):
        data = b"a\nb\n"
        path = self._file("small.jsonl", data)
        self.assertEqual(data, secure_read_file(path, root=self.transcripts, max_bytes=100, tail=True)[0])
        with self.assertRaises(PolicyError) as caught:
            secure_read_file(path, root=self.transcripts, max_bytes=2)
        self.assertEqual("secure-read-too-large", str(caught.exception))

    def test_tail_mode_still_refuses_symlinks(self):
        target = self._file("real.jsonl", b"line one\nline two\n")
        link = self.transcripts / "link.jsonl"
        os.symlink(target, link)
        with self.assertRaises(PolicyError):
            secure_read_file(link, root=self.transcripts, max_bytes=5, tail=True)

    def test_transcript_window_cut_inside_a_json_record_stays_strict_and_readable(self):
        lines = [json.dumps({"role": "user", "content": f"message {index} " + "z" * 80})
                 for index in range(10)]
        lines += [json.dumps({"role": "assistant", "content": "final answer"})]
        path = self._write("strict.jsonl", lines)
        with mock.patch.object(core, "TRANSCRIPT_READ_MAX_BYTES", path.stat().st_size - 150):
            turns = transcript_turns(path, allowed_roots=(self.transcripts,), strict=True)
        self.assertEqual(("assistant", "final answer"), turns[-1])
        self.assertNotIn("message 0", " ".join(text for _, text in turns))


class ExitCodeContractTests(LargeTranscriptBase):
    EVENTS = ("Stop", "PreCompact", "SessionEnd", "UserPromptSubmit", "SessionStart")

    def test_every_event_exits_0_and_records_blocked_health_on_failure(self):
        for event in self.EVENTS:
            with self.subTest(event=event), \
                 mock.patch.object(hook_runner, "_resolve_scope", side_effect=RuntimeError("boom")):
                rc, stdout = self._hook("codex", event, {
                    "session_id": "sess-contract", "cwd": str(self.repo),
                    "hook_event_name": event, "prompt": "a prompt long enough to recall",
                })
                self.assertEqual(0, rc)
                self.assertNotIn("block", stdout)
                health = self._health("codex")
                self.assertEqual("blocked", health["status"])
                self.assertEqual("boom", health["detail"])

    def test_policy_failure_on_stop_exits_0_without_a_continuation(self):
        outside = self.root / "elsewhere.jsonl"
        outside.write_text("{}\n", encoding="utf-8")
        rc, stdout = self._hook("codex", "Stop", {
            "session_id": "sess-outside", "transcript_path": str(outside),
            "cwd": str(self.repo), "hook_event_name": "Stop",
        })
        self.assertEqual(0, rc)
        self.assertEqual("", stdout)
        self.assertIn("transcript-path-outside-allowed-roots", self._health("codex")["detail"])

    def test_window_without_turns_is_blocked_not_an_empty_lifecycle(self):
        lines = _codex_lines("HEAD-ONLY-MARKER", "unused", "unused")[:-2]  # no turn after the filler
        path = self._write("window-no-turns.jsonl", lines)
        self.assertGreater(path.stat().st_size, core.TRANSCRIPT_READ_MAX_BYTES)
        rc, stdout = self._hook("codex", "SessionEnd", {
            "session_id": "sess-window-no-turns", "transcript_path": str(path),
            "cwd": str(self.repo), "hook_event_name": "SessionEnd",
        })
        self.assertEqual(0, rc)
        self.assertEqual("", stdout)
        health = self._health("codex")
        self.assertEqual("blocked", health["status"], "a truncated read is never reported as empty")
        self.assertIn("transcript-window-without-turns", health["detail"])
        self.assertEqual([], self._pending("codex", "sess-window-no-turns", "session_end"))

    def test_oversized_single_record_fails_closed_but_exits_0(self):
        path = self._write("single-record.jsonl", [
            json.dumps({"type": "response_item", "payload": {"output": "q" * (FILLER_LINES * MIB)}}),
        ])
        rc, stdout = self._hook("codex", "Stop", {
            "session_id": "sess-single", "transcript_path": str(path),
            "cwd": str(self.repo), "hook_event_name": "Stop",
        })
        self.assertEqual(0, rc)
        self.assertEqual("", stdout)
        self.assertEqual("blocked", self._health("codex")["status"])
        self.assertIn("secure-read-tail-without-line-boundary", self._health("codex")["detail"])


if __name__ == "__main__":
    unittest.main()

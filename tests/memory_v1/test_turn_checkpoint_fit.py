"""A long Stop turn keeps its own prompt.

2026-09-28, Orchestrator SEO/GEO session: three of twelve turns were never
checkpointed. Each ran many tools; their results (one 150 KB) passed the
256 KiB ceiling, the session was clamped from the front before the turn was
cut out, and the turn's USER line fell off -- ``checkpoint-turn-incomplete``.
One of the lost turns carried a decision. A loaded skill's body, written into
the transcript as an ``isMeta`` user record, also became the turn's "prompt".
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from memory_v1.adapters import _fit_turn, checkpoint_hook, turn_segment_digests
from memory_v1.core import TRANSCRIPT_MAX_CHARS, MemoryConfig, transcript_turns

_PROMPT = "Shopify için mevcut geniş yetkili API anahtarını kullan, karar bu."


def _user(text: str, *, meta: bool = False) -> dict:
    record = {"type": "user", "message": {"role": "user", "content": text}}
    if meta:
        record["isMeta"] = True
    return record


def _assistant(text: str) -> dict:
    return {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}}


def _tool(call_id: str, size: int) -> dict:
    body = ("satır 42 durum ok " * (size // 18 + 1))[:size]
    return {"type": "user", "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": call_id, "content": body}]}}


class FitTurnTest(unittest.TestCase):
    def test_a_turn_under_the_ceiling_is_unchanged(self) -> None:
        lines = ["USER: a", "TOOL[01]: 1", "ASSISTANT: b"]
        self.assertEqual(_fit_turn(lines), "\n".join(lines))

    def test_tool_results_give_way_and_prose_is_kept_whole(self) -> None:
        prose = ["USER: " + "p" * 1000, "ASSISTANT: " + "a" * 1000]
        lines = [prose[0], "TOOL[01]: " + "x" * 200_000, "TOOL[02]: " + "y" * 150_000,
                 "TOOL[03]: small 7", prose[1]]
        text = _fit_turn(lines)
        self.assertLessEqual(len(text), TRANSCRIPT_MAX_CHARS)
        out = text.splitlines()
        self.assertEqual(out[0], prose[0])
        self.assertEqual(out[-1], prose[1])
        self.assertIn("TOOL[03]: small 7", out)
        self.assertTrue(all("kısaltıldı" in line for line in out if line.startswith(("TOOL[01]", "TOOL[02]"))))


class LongTurnCheckpointTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="pz-test-turnfit-")
        self.root = Path(self._tmp.name).resolve()
        self.transcripts = self.root / "transcripts"
        self.transcripts.mkdir()
        self.config = MemoryConfig.from_dict({
            "role": "workstation", "vault_path": str(self.root / "vault"),
            "state_path": str(self.root / "state"), "runtimes": ["codex", "claude"],
            "transcript_roots": {r: [str(self.transcripts)] for r in ("codex", "claude")},
            "can_write_event_memory": True, "can_run_compiler": False,
            "provider": {"mode": "runtime-native"},
        })

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _checkpoint(self, records: list[dict]) -> str:
        path = self.transcripts / "s.jsonl"
        path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n", encoding="utf-8")
        queued = checkpoint_hook(self.config, runtime="claude", payload={
            "hook_event_name": "Stop", "session_id": "s-long", "transcript_path": str(path)})
        return json.loads(queued.read_text(encoding="utf-8"))["normalized_transcript"]

    def test_a_turn_whose_tools_pass_the_ceiling_keeps_its_prompt(self) -> None:
        records = [_user("önceki soru"), _assistant("önceki cevap"), _user(_PROMPT)]
        records += [_tool(f"toolu_{i:02d}", 60_000) for i in range(6)]
        records += [_assistant("Tamam, geniş yetkili anahtarla ilerliyorum.")]
        text = self._checkpoint(records)
        self.assertTrue(text.startswith("USER: " + _PROMPT))
        self.assertIn("ASSISTANT: Tamam, geniş yetkili anahtarla ilerliyorum.", text)
        self.assertLessEqual(len(text), TRANSCRIPT_MAX_CHARS)

    def test_a_turn_of_more_than_200_records_keeps_its_prompt(self) -> None:
        records = [_user(_PROMPT)] + [_tool(f"toolu_{i:03d}", 50) for i in range(250)]
        records += [_assistant("Bitti.")]
        self.assertTrue(self._checkpoint(records).startswith("USER: " + _PROMPT))

    def test_a_loaded_skills_body_is_not_the_turns_prompt(self) -> None:
        records = [_user(_PROMPT), _assistant("Skill yüklüyorum."),
                   _user("Base directory for this skill: /x/SKILL.md ...", meta=True),
                   _assistant("Tamam.")]
        text = self._checkpoint(records)
        self.assertTrue(text.startswith("USER: " + _PROMPT))
        self.assertNotIn("Base directory for this skill", text)

    def test_the_terminal_digest_recognises_a_fitted_turn(self) -> None:
        records = [_user(_PROMPT)] + [_tool(f"toolu_{i:02d}", 60_000) for i in range(6)]
        records += [_assistant("Tamam.")]
        text = self._checkpoint(records)
        import hashlib
        rendered = "\n".join(
            f"{role.upper()}: {body}"
            for role, body in transcript_turns(records, include_tool_results=True, max_turns=10**6)
        )
        self.assertIn(hashlib.sha256(text.encode("utf-8")).hexdigest(), turn_segment_digests(rendered))


class PromptTooLongTest(unittest.TestCase):
    """2026-09-28: a 256 KiB turn of dense tool output passed Haiku's context;
    the CLI said so only in stdout, and every drain failed the same way."""

    def test_the_claude_provider_names_an_over_long_prompt(self) -> None:
        from types import SimpleNamespace
        from memory_v1.core import ProviderBlocked
        from memory_v1.provider import summarize_with_claude
        stdout = json.dumps({"is_error": True, "terminal_reason": "prompt_too_long"})
        runner = lambda *a, **k: SimpleNamespace(returncode=1, stdout=stdout, stderr="")
        with self.assertRaisesRegex(ProviderBlocked, "claude-prompt-too-long"):
            summarize_with_claude(instruction="i", untrusted_input="u", schema={}, runner=runner)

    def test_a_refused_flush_is_retried_with_tool_results_shrunk(self) -> None:
        from memory_v1.adapters import _flush_shrinking
        from memory_v1.core import NormalizedTranscript, ProviderBlocked
        import hashlib
        text = "\n".join(["USER: " + _PROMPT, "TOOL[01]: " + "x" * 100_000, "ASSISTANT: Tamam."])
        seen: list[str] = []

        def flush(transcript):
            seen.append(transcript.text)
            if len(transcript.text) > 30_000:
                raise ProviderBlocked("claude-prompt-too-long")
            return Path("/ok")

        first = NormalizedTranscript.from_checkpoint(text, hashlib.sha256(text.encode()).hexdigest())
        self.assertEqual(_flush_shrinking(flush, first), Path("/ok"))
        self.assertEqual(seen[0], text)
        self.assertLessEqual(len(seen[-1]), 30_000)
        self.assertTrue(seen[-1].startswith("USER: " + _PROMPT))
        self.assertTrue(seen[-1].endswith("ASSISTANT: Tamam."))

    def test_other_provider_failures_are_not_retried(self) -> None:
        from memory_v1.adapters import _flush_shrinking
        from memory_v1.core import NormalizedTranscript, ProviderBlocked
        import hashlib
        text = "USER: a\nASSISTANT: b"
        calls = []

        def flush(transcript):
            calls.append(1)
            raise ProviderBlocked("claude-timeout")

        first = NormalizedTranscript.from_checkpoint(text, hashlib.sha256(text.encode()).hexdigest())
        with self.assertRaisesRegex(ProviderBlocked, "claude-timeout"):
            _flush_shrinking(flush, first)
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()

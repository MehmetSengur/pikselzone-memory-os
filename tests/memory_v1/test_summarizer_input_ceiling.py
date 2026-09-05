"""The summarizer ceiling holds wherever a transcript is assembled.

Regression cover for the Claude lifecycle block observed 2026-09-05: a drain
handed the runtime an input larger than the design ceiling and waited on a flat
timeout sized for small turn checkpoints.  Two distinct blocked reasons came out
of that one cause -- ``claude-timeout`` for a large-but-valid transcript, and
``claude-process-failed:1`` with empty stderr for an over-context prompt.
"""
from __future__ import annotations

import subprocess
import unittest

from memory_v1.core import TRANSCRIPT_MAX_CHARS, clamp_transcript
from memory_v1.provider import (
    CLAUDE_TIMEOUT_BASE_SECONDS, CLAUDE_TIMEOUT_MAX_SECONDS,
    ProviderBlocked, claude_timeout_for, summarize_with_claude,
)


class TranscriptCeilingTests(unittest.TestCase):
    def test_short_transcript_is_untouched(self):
        text = "USER: hi\nASSISTANT: hello"
        self.assertEqual(text, clamp_transcript(text))

    def test_combined_recovery_transcript_is_capped(self):
        # Several already-capped checkpoints concatenated: the drain path.
        combined = "\n".join("USER: " + "x" * 90_000 for _ in range(4))
        self.assertGreater(len(combined), TRANSCRIPT_MAX_CHARS)
        clamped = clamp_transcript(combined)
        self.assertLessEqual(len(clamped), TRANSCRIPT_MAX_CHARS)

    def test_clamp_keeps_the_most_recent_turn_and_drops_a_partial_leader(self):
        clamped = clamp_transcript("USER: " + "a" * 200 + "\nASSISTANT: tail", max_chars=40)
        self.assertEqual("ASSISTANT: tail", clamped)


class ClaudeTimeoutScalingTests(unittest.TestCase):
    def test_small_prompt_keeps_the_base_timeout(self):
        self.assertEqual(CLAUDE_TIMEOUT_BASE_SECONDS, claude_timeout_for(0))
        self.assertEqual(CLAUDE_TIMEOUT_BASE_SECONDS, claude_timeout_for(-1))

    def test_ceiling_sized_prompt_gets_headroom_over_the_measured_duration(self):
        # 114,015 chars measured at 90.3s on haiku; a flat 60s cut it off.
        self.assertGreater(claude_timeout_for(114_015), 90)

    def test_timeout_is_bounded(self):
        self.assertEqual(CLAUDE_TIMEOUT_MAX_SECONDS, claude_timeout_for(10_000_000))

    def test_explicit_timeout_still_wins(self):
        seen = {}

        def runner(cmd, **kwargs):
            seen["timeout"] = kwargs["timeout"]
            raise subprocess.TimeoutExpired(cmd, timeout=kwargs["timeout"])

        with self.assertRaises(ProviderBlocked):
            summarize_with_claude(
                instruction="i", untrusted_input="u", schema={}, timeout=7, runner=runner,
            )
        self.assertEqual(7, seen["timeout"])

    def test_absent_timeout_is_derived_from_the_prompt(self):
        seen = {}

        def runner(cmd, **kwargs):
            seen["timeout"] = kwargs["timeout"]
            raise subprocess.TimeoutExpired(cmd, timeout=kwargs["timeout"])

        with self.assertRaises(ProviderBlocked):
            summarize_with_claude(
                instruction="i", untrusted_input="u" * 100_000, schema={}, runner=runner,
            )
        self.assertGreater(seen["timeout"], CLAUDE_TIMEOUT_BASE_SECONDS)


class NonZeroExitDiagnosabilityTests(unittest.TestCase):
    def test_stderr_tail_is_carried_into_the_blocked_reason(self):
        def runner(cmd, **kwargs):
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="usage limit reached")

        with self.assertRaises(ProviderBlocked) as ctx:
            summarize_with_claude(
                instruction="i", untrusted_input="u", schema={}, runner=runner,
            )
        self.assertIn("claude-process-failed:1", str(ctx.exception))
        self.assertIn("usage limit reached", str(ctx.exception))

    def test_empty_stderr_leaves_the_reason_unchanged(self):
        # An over-context prompt exits non-zero with empty stderr; the absent
        # hint is the signal to look at input size, not at the runtime.
        def runner(cmd, **kwargs):
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="")

        with self.assertRaises(ProviderBlocked) as ctx:
            summarize_with_claude(
                instruction="i", untrusted_input="u", schema={}, runner=runner,
            )
        self.assertEqual("claude-process-failed:1", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()

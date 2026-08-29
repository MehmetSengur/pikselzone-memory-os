"""Regression tests for Codex rollout format compatibility (old + new item_completed formats)."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from memory_v1.core import SchemaError, normalize_transcript


class TestCodexRolloutCompatibility(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name).resolve()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _write_jsonl(self, records: list[dict]) -> Path:
        p = self.root / "rollout.jsonl"
        with p.open("w", encoding="utf-8") as f:
            for r in records:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        return p

    # 1. Old Codex format: event_msg -> user_message / agent_message
    def test_old_format_user_and_agent_message(self):
        records = [
            {"type": "event_msg", "payload": {"type": "user_message", "message": "Can you analyze our SEO metrics?"}},
            {"type": "event_msg", "payload": {"type": "agent_message", "message": "I analyzed the metrics and found 12 opportunities."}},
        ]
        p = self._write_jsonl(records)
        rendered, turns, digest = normalize_transcript(p, allowed_roots=[self.root])
        self.assertEqual(turns, 2)
        self.assertIn("USER: Can you analyze our SEO metrics?", rendered)
        self.assertIn("ASSISTANT: I analyzed the metrics and found 12 opportunities.", rendered)

    # 2. New format: item_completed -> UserMessage
    def test_new_format_user_message(self):
        records = [
            {
                "type": "event_msg",
                "payload": {
                    "type": "item_completed",
                    "item": {
                        "type": "UserMessage",
                        "content": [{"type": "text", "text": "Please update the configuration."}],
                    },
                },
            },
            {
                "type": "event_msg",
                "payload": {
                    "type": "item_completed",
                    "item": {
                        "type": "AgentMessage",
                        "content": [{"type": "text", "text": "Configuration updated successfully."}],
                    },
                },
            },
        ]
        p = self._write_jsonl(records)
        rendered, turns, digest = normalize_transcript(p, allowed_roots=[self.root])
        self.assertEqual(turns, 2)
        self.assertIn("USER: Please update the configuration.", rendered)
        self.assertIn("ASSISTANT: Configuration updated successfully.", rendered)

    # 3. New format: item_completed -> AgentMessage with direct text string
    def test_new_format_agent_message_direct_text(self):
        records = [
            {
                "type": "event_msg",
                "payload": {
                    "type": "item_completed",
                    "item": {
                        "type": "UserMessage",
                        "text": "What is the status of task 42?",
                    },
                },
            },
            {
                "type": "event_msg",
                "payload": {
                    "type": "item_completed",
                    "item": {
                        "type": "AgentMessage",
                        "text": "Task 42 is completed and verified.",
                    },
                },
            },
        ]
        p = self._write_jsonl(records)
        rendered, turns, digest = normalize_transcript(p, allowed_roots=[self.root])
        self.assertEqual(turns, 2)
        self.assertIn("USER: What is the status of task 42?", rendered)
        self.assertIn("ASSISTANT: Task 42 is completed and verified.", rendered)

    # 4. Text block with lowercase "text"
    def test_lowercase_text_block(self):
        records = [
            {
                "type": "event_msg",
                "payload": {
                    "type": "item_completed",
                    "item": {
                        "type": "UserMessage",
                        "content": [{"type": "text", "text": "Lowercase text test"}],
                    },
                },
            }
        ]
        p = self._write_jsonl(records)
        rendered, turns, digest = normalize_transcript(p, allowed_roots=[self.root])
        self.assertEqual(turns, 1)
        self.assertIn("USER: Lowercase text test", rendered)

    # 5. Text block with capitalized "Text"
    def test_capitalized_text_block(self):
        records = [
            {
                "type": "event_msg",
                "payload": {
                    "type": "item_completed",
                    "item": {
                        "type": "AgentMessage",
                        "content": [{"type": "Text", "text": "Capitalized Text test response"}],
                    },
                },
            }
        ]
        p = self._write_jsonl(records)
        rendered, turns, digest = normalize_transcript(p, allowed_roots=[self.root])
        self.assertEqual(turns, 1)
        self.assertIn("ASSISTANT: Capitalized Text test response", rendered)

    # 6. Reasoning items ignored
    def test_reasoning_ignored(self):
        records = [
            {
                "type": "event_msg",
                "payload": {
                    "type": "item_completed",
                    "item": {
                        "type": "Reasoning",
                        "content": [{"type": "text", "text": "Internal thoughts: user wants X, we should check Y first."}],
                    },
                },
            },
            {
                "type": "event_msg",
                "payload": {
                    "type": "item_completed",
                    "item": {
                        "type": "UserMessage",
                        "content": [{"type": "text", "text": "Real user turn"}],
                    },
                },
            },
        ]
        p = self._write_jsonl(records)
        rendered, turns, digest = normalize_transcript(p, allowed_roots=[self.root])
        self.assertEqual(turns, 1)
        self.assertIn("USER: Real user turn", rendered)
        self.assertNotIn("Internal thoughts", rendered)

    # 7. Command execution ignored
    def test_command_execution_ignored(self):
        records = [
            {
                "type": "event_msg",
                "payload": {
                    "type": "item_completed",
                    "item": {
                        "type": "CommandExecution",
                        "command": "git status",
                        "output": "On branch main",
                    },
                },
            },
            {
                "type": "event_msg",
                "payload": {
                    "type": "item_completed",
                    "item": {
                        "type": "AgentMessage",
                        "text": "Repository is clean.",
                    },
                },
            },
        ]
        p = self._write_jsonl(records)
        rendered, turns, digest = normalize_transcript(p, allowed_roots=[self.root])
        self.assertEqual(turns, 1)
        self.assertIn("ASSISTANT: Repository is clean.", rendered)
        self.assertNotIn("git status", rendered)

    # 8. File change ignored
    def test_file_change_ignored(self):
        records = [
            {
                "type": "event_msg",
                "payload": {
                    "type": "item_completed",
                    "item": {
                        "type": "FileChange",
                        "file": "config.json",
                        "diff": "--- a\n+++ b\n",
                    },
                },
            },
            {
                "type": "event_msg",
                "payload": {
                    "type": "item_completed",
                    "item": {
                        "type": "UserMessage",
                        "text": "Did you apply the diff?",
                    },
                },
            },
        ]
        p = self._write_jsonl(records)
        rendered, turns, digest = normalize_transcript(p, allowed_roots=[self.root])
        self.assertEqual(turns, 1)
        self.assertIn("USER: Did you apply the diff?", rendered)
        self.assertNotIn("FileChange", rendered)

    # 9. Mixed old and new formats in same rollout transcript
    def test_mixed_old_and_new_rollout_formats(self):
        records = [
            # Turn 1: old format
            {"type": "event_msg", "payload": {"type": "user_message", "message": "Turn 1 user"}},
            {"type": "event_msg", "payload": {"type": "agent_message", "message": "Turn 1 assistant"}},
            # Internal noise in between
            {
                "type": "event_msg",
                "payload": {
                    "type": "item_completed",
                    "item": {"type": "Reasoning", "text": "Thinking..."},
                },
            },
            # Turn 2: new format with Text capitalization
            {
                "type": "event_msg",
                "payload": {
                    "type": "item_completed",
                    "item": {
                        "type": "UserMessage",
                        "content": [{"type": "Text", "text": "Turn 2 user"}],
                    },
                },
            },
            {
                "type": "event_msg",
                "payload": {
                    "type": "item_completed",
                    "item": {
                        "type": "AgentMessage",
                        "content": [{"type": "text", "text": "Turn 2 assistant"}],
                    },
                },
            },
        ]
        p = self._write_jsonl(records)
        rendered, turns, digest = normalize_transcript(p, allowed_roots=[self.root])
        self.assertEqual(turns, 4)
        self.assertIn("USER: Turn 1 user", rendered)
        self.assertIn("ASSISTANT: Turn 1 assistant", rendered)
        self.assertIn("USER: Turn 2 user", rendered)
        self.assertIn("ASSISTANT: Turn 2 assistant", rendered)
        self.assertNotIn("Thinking", rendered)

    # 10. Silent zero-turn regression prevention: conversation records present but 0 turns extracted
    def test_silent_zero_turn_regression_raises_schema_error(self):
        # A transcript with candidate conversation turns that fail extraction (e.g. empty whitespace text)
        records = [
            {
                "type": "event_msg",
                "payload": {
                    "type": "item_completed",
                    "item": {
                        "type": "UserMessage",
                        "content": [{"type": "text", "text": "   "}],
                    },
                },
            },
            {
                "type": "event_msg",
                "payload": {
                    "type": "item_completed",
                    "item": {
                        "type": "AgentMessage",
                        "content": [{"type": "Text", "text": "\n\t  \n"}],
                    },
                },
            },
        ]
        p = self._write_jsonl(records)
        with self.assertRaises(SchemaError) as ctx:
            normalize_transcript(p, allowed_roots=[self.root])
        self.assertIn("transcript-zero-turns-from-2-candidates", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()

"""Unit tests for Multi-Format History Import & Distillation Engine (SB2-11)."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from memory_v1.companion import CompanionManager
from memory_v1.core import MemoryConfig
from memory_v1.importers import HistoryImportEngine


class TestHistoryImportEngine(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name).resolve()
        self.vault = self.root / "vault"
        self.state = self.root / "state"
        self.vault.mkdir(parents=True)
        self.state.mkdir(parents=True)

        self.config = MemoryConfig.from_dict({
            "role": "workstation",
            "vault_path": str(self.vault),
            "state_path": str(self.state),
            "runtimes": ["claude", "codex"],
            "transcript_roots": {
                "claude": [str(self.root)],
                "codex": [str(self.root)],
            },
            "can_write_event_memory": True,
            "can_run_compiler": False,
            "models": {"flush": "gpt-5.6-luna", "compiler": "gpt-5.6-terra"},
            "provider": {"mode": "runtime-native"},
            "context_budget_chars": 16000,
        })
        self.importer = HistoryImportEngine(self.config)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    # 1. ChatGPT export JSON import
    def test_import_chatgpt_json(self):
        chatgpt_data = [
            {
                "id": "chatgpt-conv-1",
                "title": "Backend Architecture Discussion",
                "mapping": {
                    "node-1": {
                        "message": {
                            "author": {"role": "user"},
                            "content": {
                                "parts": [
                                    "Bundan sonra backend API endpoint'lerini daima kebab-case ile adlandır. Secret anahtarım: sk-proj-1234567890abcdefghijklmn"
                                ]
                            },
                        }
                    },
                    "node-2": {
                        "message": {
                            "author": {"role": "assistant"},
                            "content": {
                                "parts": [
                                    "Anlaşıldı! FastAPI ve PostgreSQL kullanarak tüm rotaları kebab-case yapacağız."
                                ]
                            },
                        }
                    },
                },
            }
        ]
        source_file = self.root / "conversations.json"
        source_file.write_text(json.dumps(chatgpt_data), encoding="utf-8")

        receipt = self.importer.import_file(source_file)
        self.assertEqual(receipt.source_format, "chatgpt")
        self.assertEqual(receipt.sessions_imported, 1)
        self.assertTrue(receipt.rules_extracted >= 1)
        self.assertTrue(receipt.concepts_extracted >= 1)
        self.assertTrue(len(receipt.receipt_sha256) == 64)

        # Verify rule was distilled to Kurallar.md
        rules_text = (self.vault / "companion" / "Kurallar.md").read_text(encoding="utf-8")
        self.assertIn("kebab-case", rules_text)
        # Verify secret was REDACTED
        self.assertNotIn("sk-proj-1234567890abcdefghijklmn", rules_text)

        # The shared knowledge graph has one canonical writer (the VPS
        # compiler); the importer distills rules into companion/ and no
        # longer writes knowledge/concepts directly.
        self.assertFalse((self.vault / "knowledge" / "concepts" / "fastapi.md").exists())
        self.assertFalse((self.vault / "knowledge" / "concepts" / "postgresql.md").exists())

    # 2. Claude export JSON import
    def test_import_claude_json(self):
        claude_data = [
            {
                "uuid": "claude-conv-1",
                "name": "Docker & Redis Setup",
                "chat_messages": [
                    {
                        "sender": "human",
                        "text": "Her zaman Docker Compose dosyalarında restart: always politikasını kullan.",
                    },
                    {
                        "sender": "assistant",
                        "text": "Harika, Redis ve Docker servisleri için restart: always kuralını uygulayalım.",
                    },
                ],
            }
        ]
        source_file = self.root / "claude_export.json"
        source_file.write_text(json.dumps(claude_data), encoding="utf-8")

        receipt = self.importer.import_file(source_file, source_format="claude")
        self.assertEqual(receipt.source_format, "claude")
        self.assertEqual(receipt.sessions_imported, 1)
        self.assertTrue(receipt.rules_extracted >= 1)

        rules_text = (self.vault / "companion" / "Kurallar.md").read_text(encoding="utf-8")
        self.assertIn("restart: always", rules_text)

        # No direct knowledge write from the importer (single-writer rule).
        self.assertFalse((self.vault / "knowledge" / "concepts" / "docker.md").exists())

    # 3. Markdown chat import
    def test_import_markdown_chat(self):
        md_text = (
            "## User:\n"
            "Asla production veritabanında drop table çalıştırma.\n\n"
            "## Assistant:\n"
            "Kesinlikle, PostgreSQL üretim tabloları için güvenlik korumalarını aktif ediyoruz.\n"
        )
        source_file = self.root / "meeting-notes.md"
        source_file.write_text(md_text, encoding="utf-8")

        receipt = self.importer.import_file(source_file, source_format="markdown")
        self.assertEqual(receipt.sessions_imported, 1)
        self.assertTrue(receipt.rules_extracted >= 1)

        rules_text = (self.vault / "companion" / "Kurallar.md").read_text(encoding="utf-8")
        self.assertIn("drop table", rules_text)

    # 4. Filter short / chit-chat sessions
    def test_filter_short_chit_chat(self):
        short_data = [
            {
                "id": "chat-trivial",
                "title": "Selamlaşma",
                "mapping": {
                    "node-1": {
                        "message": {
                            "author": {"role": "user"},
                            "content": {"parts": ["Selam!"]},
                        }
                    },
                    "node-2": {
                        "message": {
                            "author": {"role": "assistant"},
                            "content": {"parts": ["Merhaba!"]},
                        }
                    },
                },
            }
        ]
        source_file = self.root / "trivial.json"
        source_file.write_text(json.dumps(short_data), encoding="utf-8")

        receipt = self.importer.import_file(source_file, source_format="chatgpt")
        self.assertEqual(receipt.total_sessions_found, 1)
        self.assertEqual(receipt.sessions_imported, 0)
        self.assertEqual(receipt.rules_extracted, 0)


if __name__ == "__main__":
    unittest.main()

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

    def _merge_as_engine(self) -> None:
        # The workstation queues what it learned; only the memory-engine host
        # writes companion/Kurallar.md (memory_v1.learning_inbox).
        from memory_v1.learning_inbox import merge_learning_inbox
        engine = MemoryConfig.from_dict({
            "role": "memory-engine", "vault_path": str(self.vault), "state_path": str(self.root / "engine-state"),
            "runtimes": ["hermes"], "transcript_roots": {"hermes": [str(self.root)]},
            "can_write_event_memory": True, "can_run_compiler": True, "provider": {"mode": "runtime-native"},
        })
        merge_learning_inbox(engine)

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
        self._merge_as_engine()
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

        self._merge_as_engine()
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

        self._merge_as_engine()
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

    # 5. Claude Code transcript JSONL (regression: the codex path used to
    #    import a module that no longer exists, so every .jsonl import crashed)
    def test_import_claude_code_jsonl(self):
        lines = [
            {"parentUuid": None, "sessionId": "abc", "cwd": "/repo",
             "type": "user",
             "message": {"role": "user", "content": "Bundan sonra migration dosyalarini daima UTC ile adlandir."}},
            {"parentUuid": "1", "sessionId": "abc", "type": "assistant",
             "message": {"role": "assistant",
                         "content": [{"type": "text", "text": "Anlasildi, Alembic revizyonlarini UTC damgasiyla ureteceğim."}]}},
        ]
        source_file = self.root / "session.jsonl"
        source_file.write_text("\n".join(json.dumps(x) for x in lines), encoding="utf-8")

        self.assertEqual(self.importer._detect_format(source_file), "claude-code")
        receipt = self.importer.import_file(source_file)
        self.assertEqual(receipt.source_format, "claude-code")
        self.assertEqual(receipt.total_sessions_found, 1)
        self.assertEqual(receipt.sessions_imported, 1)

    # 6. Codex rollout JSONL still routes to the codex label
    def test_import_codex_rollout_jsonl(self):
        lines = [
            {"type": "item_completed",
             "item": {"type": "user_message", "text": "Deploy adimlarini her zaman staging ile basla."}},
            {"type": "item_completed",
             "item": {"type": "agent_message", "text": "Tamam, staging once calisacak sekilde plani guncelledim."}},
        ]
        source_file = self.root / "rollout-2026.jsonl"
        source_file.write_text("\n".join(json.dumps(x) for x in lines), encoding="utf-8")

        self.assertEqual(self.importer._detect_format(source_file), "codex")
        receipt = self.importer.import_file(source_file)
        self.assertEqual(receipt.source_format, "codex")
        self.assertEqual(receipt.sessions_imported, 1)

    # 7. A truncated trailing line is tolerated on import, not fatal
    def test_import_tolerates_truncated_trailing_line(self):
        good = json.dumps({
            "sessionId": "abc", "cwd": "/repo", "type": "user",
            "message": {"role": "user", "content": "Raporlarda once sonucu yaz, sonra kaniti."},
        })
        good2 = json.dumps({
            "sessionId": "abc", "type": "assistant",
            "message": {"role": "assistant", "content": "Anlasildi, sonuc ustte olacak."},
        })
        source_file = self.root / "truncated.jsonl"
        source_file.write_text(f"{good}\n{good2}\n{{\"type\": \"assis", encoding="utf-8")

        receipt = self.importer.import_file(source_file)
        self.assertEqual(receipt.sessions_imported, 1)

    # 8. project scoping reaches the receipt and the rule provenance
    def test_project_scoping_recorded(self):
        lines = [
            {"sessionId": "abc", "cwd": "/repo", "type": "user",
             "message": {"role": "user",
                         "content": "Bundan sonra release notlarinda breaking changes bolumunu en uste koy."}},
            {"sessionId": "abc", "type": "assistant",
             "message": {"role": "assistant", "content": "Tamam, breaking changes ilk bolum olacak."}},
        ]
        source_file = self.root / "scoped.jsonl"
        source_file.write_text("\n".join(json.dumps(x) for x in lines), encoding="utf-8")

        receipt = self.importer.import_file(source_file, project="demo-proje")
        self.assertEqual(receipt.project, "demo-proje")
        self._merge_as_engine()
        rules_text = (self.vault / "companion" / "Kurallar.md").read_text(encoding="utf-8")
        self.assertIn("demo-proje", rules_text)

    # 9. dry run reports without writing anything
    def test_dry_run_writes_nothing(self):
        lines = [
            {"sessionId": "abc", "cwd": "/repo", "type": "user",
             "message": {"role": "user",
                         "content": "Bundan sonra tum PR aciklamalarini Turkce yaz."}},
            {"sessionId": "abc", "type": "assistant",
             "message": {"role": "assistant", "content": "Anlasildi, aciklamalar Turkce olacak."}},
        ]
        source_file = self.root / "dry.jsonl"
        source_file.write_text("\n".join(json.dumps(x) for x in lines), encoding="utf-8")

        rules_file = self.vault / "companion" / "Kurallar.md"
        before = rules_file.read_text(encoding="utf-8") if rules_file.exists() else ""

        receipt = self.importer.import_file(source_file, project="demo-proje", dry_run=True)
        self.assertTrue(receipt.dry_run)
        self.assertEqual(receipt.sessions_imported, 1)

        after = rules_file.read_text(encoding="utf-8") if rules_file.exists() else ""
        self.assertEqual(before, after)
        self.assertFalse(list((self.state / "imports").glob("*.json")) if (self.state / "imports").exists() else [])


if __name__ == "__main__":
    unittest.main()

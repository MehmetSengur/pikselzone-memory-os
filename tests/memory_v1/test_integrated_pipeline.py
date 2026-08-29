"""Targeted tests proving full integration pipeline:
SessionEnd -> EventWriter.flush -> Companion -> Rule Learner -> Knowledge Graph -> Skill Engine.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from memory_v1.companion import CompanionManager
from memory_v1.core import MemoryConfig, NormalizedTranscript
from memory_v1.events import EventWriter
from memory_v1.provider import StructuredResponsesProvider


class MockSummaryProvider(StructuredResponsesProvider):
    def __init__(self, summary: dict):
        self._summary = summary
        self.last_source_model = "test-luna"
        self.last_source_provider = "mock"

    def request(self, *args, **kwargs) -> dict:
        return self._summary


class TestIntegratedPipeline(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name).resolve()
        self.vault = self.root / "vault"
        self.state = self.root / "state"
        self.vault.mkdir(parents=True)
        self.state.mkdir(parents=True)
        (self.vault / "daily").mkdir(parents=True)

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

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_full_pipeline_execution(self):
        # Setup mock provider with rich decisions, learnings, and workflows
        mock_summary = {
            "status": "memory",
            "context": ["Microservices caching and rate limiting setup"],
            "important_conversations": [
                "Deployment workflow: git checkout main, docker build -t app ., docker compose up -d"
            ],
            "decisions": [
                "FastAPI framework'ü ana backend API olarak seçildi.",
                "Redis servisi distributed token bucket ve cache katmanı olarak devreye alındı.",
            ],
            "learnings": [
                "PostgreSQL bağlantı havuzu boyutu 20 olarak optimize edildi.",
            ],
            "open_items": ["Monitoring alerting kuralları eklenecek"],
            "evidence": ["PR #42"],
        }
        provider = MockSummaryProvider(mock_summary)
        writer = EventWriter(self.config, provider)

        # Build transcript with explicit user directive
        raw_transcript = (
            "USER: Bundan sonra mikroservis log formatında JSON yapısını zorunlu kıl.\n"
            "ASSISTANT: Anlaşıldı, tüm servisler structured JSON log formatını kullanacak.\n"
        )
        import hashlib
        digest = hashlib.sha256(raw_transcript.encode("utf-8")).hexdigest()
        norm_t = NormalizedTranscript.from_checkpoint(raw_transcript, digest)

        # Run EventWriter.flush (Session 1)
        event_path = writer.flush(
            runtime="codex",
            agent_id="test-agent",
            session_id="session-pipeline-1",
            event="session_end",
            transcript=norm_t,
            source_model="gpt-5.6-luna",
            root_task_id="task-1",
        )
        self.assertTrue(event_path.is_file())

        # 1. Rule Learner check
        rules_text = (self.vault / "companion" / "Kurallar.md").read_text(encoding="utf-8")
        self.assertIn("JSON yapısını zorunlu kıl", rules_text)

        # 2. Companion Continuity check (Last-Session.md & Journal.md)
        last_session_text = (self.vault / "companion" / "Last-Session.md").read_text(encoding="utf-8")
        self.assertIn("FastAPI framework'ü", last_session_text)
        self.assertIn("Redis servisi", last_session_text)

        journal_text = (self.vault / "companion" / "Journal.md").read_text(encoding="utf-8")
        self.assertIn("Session end Özeti", journal_text)

        # 3. Knowledge Graph auto-growth check (Concepts & Connection)
        self.assertTrue((self.vault / "knowledge" / "concepts" / "fastapi.md").is_file())
        self.assertTrue((self.vault / "knowledge" / "concepts" / "redis.md").is_file())
        self.assertTrue((self.vault / "knowledge" / "concepts" / "postgresql.md").is_file())

        # Check connection file between co-occurring concepts
        connections = list((self.vault / "knowledge" / "connections").glob("*.md"))
        self.assertTrue(len(connections) >= 1)

        # Check knowledge/index.md and knowledge/log.md
        index_text = (self.vault / "knowledge" / "index.md").read_text(encoding="utf-8")
        self.assertIn("fastapi", index_text)
        log_text = (self.vault / "knowledge" / "log.md").read_text(encoding="utf-8")
        self.assertIn("CREATE_CONCEPT", log_text)

        # 4. Skill candidate observation (Repeat same workflow in Session 2 to trigger auto-synthesis)
        writer.flush(
            runtime="codex",
            agent_id="test-agent",
            session_id="session-pipeline-2",
            event="session_end",
            transcript=norm_t,
            source_model="gpt-5.6-luna",
            root_task_id="task-2",
        )
        # Verify Skill auto-synthesized!
        skill_file = self.vault / "skills" / "deployment-workflow" / "SKILL.md"
        self.assertTrue(skill_file.is_file())
        skill_content = skill_file.read_text(encoding="utf-8")
        self.assertIn("Deployment workflow", skill_content)


if __name__ == "__main__":
    unittest.main()

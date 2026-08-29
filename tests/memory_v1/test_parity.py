"""Unit tests for Claude / Codex / Hermes Shared-Brain Parity (SB2-09)."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from memory_v1.companion import CompanionManager
from memory_v1.core import MemoryConfig
from memory_v1.graph_engine import ConceptData, KnowledgeGraphEngine
from memory_v1.parity import SharedBrainParityManager


class TestSharedBrainParity(unittest.TestCase):
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
        self.manager = SharedBrainParityManager(self.vault)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    # 1. Parity alignment: router and skills sharing
    def test_align_shared_brain(self):
        report = self.manager.align_shared_brain()
        self.assertTrue(report.is_fully_aligned)
        self.assertTrue(report.claude_ready)
        self.assertTrue(report.codex_ready)
        self.assertTrue(report.hermes_ready)

        # Verify router files exist
        claude_md = self.vault / "CLAUDE.md"
        agents_md = self.vault / "AGENTS.md"
        self.assertTrue(claude_md.is_file())
        self.assertTrue(agents_md.exists())

        # Verify skills directory and built-ins exist
        skills_dir = self.vault / "skills"
        self.assertTrue((skills_dir / "beyin-doktor" / "SKILL.md").is_file())
        self.assertTrue((skills_dir / "gecmis-import" / "SKILL.md").is_file())

    # 2. Cross-runtime rule visibility (Claude learns -> Codex & Hermes see it)
    def test_cross_runtime_rule_visibility(self):
        self.manager.align_shared_brain()

        # Simulate Claude learning a rule
        companion = CompanionManager(self.vault)
        companion.add_or_update_rule(
            "Veritabanı migration işlemlerini daima test ortamında dene.",
            reason="Canlı veriyi koruma kuralı",
            source="claude-session-1",
        )

        recall_res = self.manager.test_cross_runtime_recall(self.config, "migration")
        self.assertTrue(recall_res["claude_startup"])
        self.assertTrue(recall_res["codex_startup"])
        self.assertTrue(recall_res["hermes_startup"])
        self.assertTrue(recall_res["targeted_recall"])
        self.assertTrue(recall_res["all_aligned"])

    # 3. Cross-runtime knowledge visibility (Hermes compiles -> Claude recalls it)
    def test_cross_runtime_knowledge_visibility(self):
        self.manager.align_shared_brain()

        # Simulate Hermes adding a concept to knowledge graph
        graph = KnowledgeGraphEngine(self.vault)
        graph.add_or_update_concept(ConceptData(
            title="Redis Cluster",
            summary="Yüksek erişilebilirlikli önbellekleme mimarisi.",
            details=["Sentinel düğümleri devrede"],
            sources=["hermes-compile-1"],
        ))

        recall_res = self.manager.test_cross_runtime_recall(self.config, "Redis Cluster")
        self.assertTrue(recall_res["all_aligned"])


if __name__ == "__main__":
    unittest.main()

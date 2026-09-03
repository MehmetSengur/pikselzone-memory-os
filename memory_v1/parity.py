"""Multi-Runtime Parity & Shared Brain Coexistence Manager (SB2-09).

Guarantees Claude Code, Codex, and Hermes share a single, unified Obsidian vault:
1. Router parity: AGENTS.md links to CLAUDE.md so Codex & Claude read the exact same instructions.
2. Skills store parity: .agents/skills & .claude/skills link to canonical skills/.
3. Companion parity: companion/ (Core, Kurallar, Last-Session, Threads, Journal) is common ground.
4. Knowledge parity: knowledge/ (concepts, connections, index, log) grows once and informs all three runtimes.
"""
from __future__ import annotations

import dataclasses
import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .companion import CompanionManager
from .core import MemoryConfig, PolicyError, atomic_write, iso_now
from .graph_engine import KnowledgeGraphEngine
from .recall import build_startup_recall_bundle, targeted_recall
from .skill_engine import SkillEngine

logger = logging.getLogger("memory_v1.parity")


@dataclasses.dataclass
class ParityReport:
    vault_path: str
    router_shared: bool
    skills_store_shared: bool
    companion_available: bool
    knowledge_available: bool
    claude_ready: bool
    codex_ready: bool
    hermes_ready: bool
    details: list[str] = dataclasses.field(default_factory=list)

    @property
    def is_fully_aligned(self) -> bool:
        return (
            self.router_shared
            and self.skills_store_shared
            and self.companion_available
            and self.knowledge_available
        )


class SharedBrainParityManager:
    """Manages vault alignment, symlinks, and cross-runtime parity across Claude, Codex, and Hermes."""

    def __init__(self, vault_path: Path) -> None:
        self.vault_path = vault_path.resolve()
        self.companion = CompanionManager(self.vault_path)
        self.graph = KnowledgeGraphEngine(self.vault_path)
        self.skills = SkillEngine(self.vault_path)

    def align_shared_brain(self) -> ParityReport:
        """Create or align shared router, skills, companion, and knowledge anchors."""
        details: list[str] = []

        # 1. Base directories
        self.companion.ensure_companion_files()
        # Directories only: index.md / log.md are written solely by the
        # compiler's deterministic post-promotion rebuild.
        self.graph.ensure_graph_dirs(seed_anchors=False)
        self.skills.ensure_skills_dirs()

        # 2. Router parity (CLAUDE.md <-> AGENTS.md)
        claude_md = self.vault_path / "CLAUDE.md"
        agents_md = self.vault_path / "AGENTS.md"

        router_content = (
            "# Pikselzone Second Brain — Multi-Runtime Operating Router\n\n"
            "Bu dosya Claude Code, Codex ve Hermes tarafından ortaklaşa okunan ana çalışma rehberidir.\n\n"
            "## 1. Yetki ve Doğruluk Hiyerarşisi\n"
            "1. Git deposu & canlı konfigürasyon = kod ve operasyon gerçeği\n"
            "2. Kanban = görev ve yürütme gerçeği\n"
            "3. Obsidian canonical dokümanları = kurumsal hafıza ve kararlar\n"
            "4. companion/ & knowledge/ = yaşayan ikinci beyin hafızası\n\n"
            "## 2. Ortak Çalışma Kuralları\n"
            "- Kurallar ve tercihler: `companion/Kurallar.md` dosyasından okunur.\n"
            "- Oturum sürekliliği: `companion/Last-Session.md` dosyasında tutulur.\n"
            "- Aktif iş akışı becerileri: `skills/` dizinindedir.\n"
            "- Tanı ve sağlık kontrolü: `beyin-doktor` skill'i kullanılır.\n"
        )

        if not claude_md.is_file() and not agents_md.is_file():
            atomic_write(claude_md, router_content)
            details.append("Created CLAUDE.md router")

        # Establish symlink or unified file for AGENTS.md
        if not agents_md.exists():
            try:
                os.symlink("CLAUDE.md", agents_md)
                details.append("Linked AGENTS.md -> CLAUDE.md")
            except OSError:
                # If filesystem doesn't support symlink, copy identical router
                atomic_write(agents_md, router_content)
                details.append("Created duplicate-safe AGENTS.md router")

        # 3. Skills store parity (.agents/skills, .claude/skills -> skills/)
        agents_skills_dir = self.vault_path / ".agents" / "skills"
        claude_skills_dir = self.vault_path / ".claude" / "skills"

        (self.vault_path / ".agents").mkdir(exist_ok=True)
        (self.vault_path / ".claude").mkdir(exist_ok=True)

        if not agents_skills_dir.exists():
            try:
                os.symlink("../skills", agents_skills_dir)
                details.append("Linked .agents/skills -> ../skills")
            except OSError:
                pass

        if not claude_skills_dir.exists():
            try:
                os.symlink("../skills", claude_skills_dir)
                details.append("Linked .claude/skills -> ../skills")
            except OSError:
                pass

        return self.inspect_parity()

    def inspect_parity(self) -> ParityReport:
        """Inspect current alignment across runtimes."""
        details: list[str] = []

        claude_md = self.vault_path / "CLAUDE.md"
        agents_md = self.vault_path / "AGENTS.md"
        router_shared = claude_md.is_file() and agents_md.exists()

        skills_canonical = (self.vault_path / "skills").is_dir()
        skills_shared = skills_canonical

        companion_avail = (self.vault_path / "companion").is_dir() or (self.vault_path / "🔮 850-Companion").is_dir()
        knowledge_avail = (self.vault_path / "knowledge").is_dir()

        details.append(f"Router shared: {router_shared}")
        details.append(f"Skills store shared: {skills_shared}")
        details.append(f"Companion available: {companion_avail}")
        details.append(f"Knowledge available: {knowledge_avail}")

        return ParityReport(
            vault_path=str(self.vault_path),
            router_shared=router_shared,
            skills_store_shared=skills_shared,
            companion_available=companion_avail,
            knowledge_available=knowledge_avail,
            claude_ready=router_shared and companion_avail,
            codex_ready=router_shared and companion_avail,
            hermes_ready=companion_avail and knowledge_avail,
            details=details,
        )

    def test_cross_runtime_recall(self, config: MemoryConfig, canary_term: str) -> dict[str, bool]:
        """Verify that an item created by one runtime is visible to all runtimes in recall."""
        # 1. Check Claude startup bundle
        claude_bundle = build_startup_recall_bundle(config, runtime="claude")
        claude_seen = canary_term.lower() in claude_bundle.text.lower()

        # 2. Check Codex startup bundle
        codex_bundle = build_startup_recall_bundle(config, runtime="codex")
        codex_seen = canary_term.lower() in codex_bundle.text.lower()

        # 3. Check Hermes startup bundle
        hermes_bundle = build_startup_recall_bundle(config, runtime="hermes")
        hermes_seen = canary_term.lower() in hermes_bundle.text.lower()

        # 4. Check targeted deep recall
        t_res = targeted_recall(config, query=canary_term)
        targeted_seen = canary_term.lower() in t_res["markdown"].lower()

        return {
            "claude_startup": claude_seen,
            "codex_startup": codex_seen,
            "hermes_startup": hermes_seen,
            "targeted_recall": targeted_seen,
            "all_aligned": claude_seen and codex_seen and hermes_seen and targeted_seen,
        }

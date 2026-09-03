"""knowledge/ has exactly one canonical writer: the VPS compiler.

Two hosts doing divergent whole-file rewrites of the same synced markdown is
what produced the `(Conflicted copy pz-hermes ...)` files and the collapsing
index.  These tests pin the resulting contract:

  * the model may propose only concepts/** and connections/**;
  * index.md and log.md are rebuilt deterministically after promotion;
  * the knowledge snapshot may still READ index.md / log.md;
  * a Claude / Codex / Hermes session flush never mutates knowledge/.
"""
from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from memory_v1.compiler import TerraCompiler
from memory_v1.core import (
    MemoryConfig, PolicyError, compiler_write_relative_path, knowledge_relative_path,
)
from memory_v1.events import EventWriter
from memory_v1.knowledge_index import (
    INDEX_TITLE, append_promotion_log, rebuild_after_promotion, render_index,
)

_CONCEPT = (
    '---\ntitle: "Meta Catalog"\naliases: []\ntags: ["#concept"]\n'
    'created: "2026-08-30"\nupdated: "2026-08-30"\nsources: ["twoberries:aa"]\n'
    "---\n\n# Meta Catalog\n\n## Özet\nFeed ve event identity hizalanmali.\n"
)

_SUMMARY = {
    "status": "memory",
    "context": ["FastAPI ve Redis konusuldu."],
    "important_conversations": ["Redis servisi eklendi."],
    "decisions": ["FastAPI kullanilacak."],
    "learnings": ["Redis baglantisi onceden dogrulanmali."],
    "open_items": ["PostgreSQL semasi netlesecek."],
    "evidence": ["fixture:single-writer"],
}


class _FakeProvider:
    last_source_model = "gpt-5.6-luna"
    last_source_provider = "chatgpt-subscription"

    def request(self, **kwargs):
        return json.loads(json.dumps(_SUMMARY))


class KnowledgeWritePolicyTest(unittest.TestCase):
    """Read allowlist vs. LLM-write allowlist."""

    def test_read_allowlist_still_includes_index_and_log(self) -> None:
        for rel in ("knowledge/index.md", "knowledge/log.md",
                    "knowledge/concepts/a.md", "knowledge/connections/a--b.md"):
            self.assertEqual(str(knowledge_relative_path(rel)), rel)

    def test_write_allowlist_rejects_index_and_log(self) -> None:
        for rel in ("knowledge/index.md", "knowledge/log.md"):
            with self.assertRaises(PolicyError) as cm:
                compiler_write_relative_path(rel)
            self.assertIn("not-model-writable", str(cm.exception))

    def test_write_allowlist_allows_concepts_and_connections(self) -> None:
        for rel in ("knowledge/concepts/meta-catalog.md",
                    "knowledge/connections/a--b.md"):
            self.assertEqual(str(compiler_write_relative_path(rel)), rel)

    def test_compiler_proposal_rejects_index(self) -> None:
        with self.assertRaises(PolicyError):
            TerraCompiler._validate_proposal({
                "status": "changes",
                "writes": [{"path": "knowledge/index.md", "content": "# Index\n"}],
            })

    def test_compiler_proposal_rejects_log(self) -> None:
        with self.assertRaises(PolicyError):
            TerraCompiler._validate_proposal({
                "status": "changes",
                "writes": [{"path": "knowledge/log.md", "content": "# Log\n"}],
            })

    def test_compiler_proposal_allows_concepts_and_connections(self) -> None:
        out = TerraCompiler._validate_proposal({
            "status": "changes",
            "writes": [
                {"path": "knowledge/concepts/meta-catalog.md", "content": _CONCEPT},
                {"path": "knowledge/connections/a--b.md", "content": "# İlişki\n"},
            ],
        })
        self.assertEqual(
            [w["path"] for w in out["writes"]],
            ["knowledge/concepts/meta-catalog.md", "knowledge/connections/a--b.md"],
        )

    def test_compiler_instruction_forbids_index_and_log(self) -> None:
        from memory_v1.compiler import COMPILER_INSTRUCTION
        self.assertIn("Never propose knowledge/index.md", COMPILER_INSTRUCTION)
        self.assertIn("knowledge/log.md", COMPILER_INSTRUCTION)
        self.assertIn("ONLY knowledge/concepts/", COMPILER_INSTRUCTION)


class DeterministicRebuildTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="pz-test-singlewriter-")
        self.root = Path(self._tmp.name).resolve()
        self.vault = self.root / "vault"
        (self.vault / "knowledge" / "concepts").mkdir(parents=True)
        (self.vault / "knowledge" / "connections").mkdir(parents=True)
        (self.vault / "knowledge" / "concepts" / "meta-catalog.md").write_text(
            _CONCEPT, encoding="utf-8")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_promotion_rebuilds_index_deterministically(self) -> None:
        result = rebuild_after_promotion(
            self.vault, batch_id="batch-1",
            promoted=["knowledge/concepts/meta-catalog.md"],
        )
        index_text = (self.vault / "knowledge" / "index.md").read_text(encoding="utf-8")
        self.assertTrue(index_text.startswith(INDEX_TITLE))
        self.assertIn("[Meta Catalog](concepts/meta-catalog.md)", index_text)
        self.assertEqual(result["index_rows"], 1)

    def test_promotion_writes_deterministic_log_audit(self) -> None:
        rebuild_after_promotion(
            self.vault, batch_id="batch-7",
            promoted=["knowledge/concepts/meta-catalog.md",
                      "knowledge/connections/a--b.md"],
        )
        log_text = (self.vault / "knowledge" / "log.md").read_text(encoding="utf-8")
        self.assertIn("PROMOTE batch-7", log_text)
        self.assertIn("1 concept, 1 connection", log_text)
        self.assertIn("concepts: meta-catalog", log_text)

    def test_rerun_on_same_concepts_is_byte_stable(self) -> None:
        first = render_index(self.vault)
        second = render_index(self.vault)
        self.assertEqual(first, second)
        rebuild_after_promotion(self.vault, batch_id="b1", promoted=[])
        after_first = (self.vault / "knowledge" / "index.md").read_text(encoding="utf-8")
        rebuild_after_promotion(self.vault, batch_id="b2", promoted=[])
        after_second = (self.vault / "knowledge" / "index.md").read_text(encoding="utf-8")
        self.assertEqual(after_first, after_second)

    def test_log_audit_is_append_only(self) -> None:
        append_promotion_log(self.vault, batch_id="b1", promoted=[], index_rows=1)
        first = (self.vault / "knowledge" / "log.md").read_text(encoding="utf-8")
        append_promotion_log(self.vault, batch_id="b2", promoted=[], index_rows=1)
        second = (self.vault / "knowledge" / "log.md").read_text(encoding="utf-8")
        self.assertTrue(second.startswith(first.rstrip()))
        self.assertIn("b1", second)
        self.assertIn("b2", second)


class FlushDoesNotTouchKnowledgeTest(unittest.TestCase):
    """A session flush produces daily + continuity evidence only."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="pz-test-flushknowledge-")
        self.root = Path(self._tmp.name).resolve()
        self.vault = self.root / "vault"
        self.vault.mkdir()
        self.transcript = self.root / "t.jsonl"
        self.transcript.write_text(
            "\n".join(json.dumps({"message": {"role": r, "content": c}}) for r, c in [
                ("user", "FastAPI ve Redis ile bir servis kuralim mi"),
                ("assistant", "Evet, Redis baglantisini once dogrulayalim."),
            ]) + "\n",
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _config(self, runtimes: list[str]) -> MemoryConfig:
        return MemoryConfig.from_dict({
            "role": "workstation" if set(runtimes) == {"codex", "claude"} else "memory-engine",
            "vault_path": str(self.vault),
            "state_path": str(self.root / "state"),
            "runtimes": runtimes,
            "transcript_roots": {rt: [str(self.root)] for rt in runtimes},
            "can_write_event_memory": True,
            "can_run_compiler": False,
            "provider": {"mode": "runtime-native"},
        })

    def _flush(self, runtime: str) -> Path:
        runtimes = ["codex", "claude"] if runtime in {"codex", "claude"} else ["hermes"]
        cfg = self._config(runtimes)
        return EventWriter(cfg, _FakeProvider()).flush(
            runtime=runtime, agent_id=f"{runtime}-main",
            session_id=f"sess-{runtime}", event="session_end",
            transcript=self.transcript, source_model="m",
            project="luvaa" if runtime != "hermes" else "unscoped",
            continuity_scope="luvaa" if runtime != "hermes" else "hermes",
        )

    def _assert_knowledge_untouched(self) -> None:
        knowledge = self.vault / "knowledge"
        self.assertFalse((knowledge / "index.md").exists(), "flush wrote index.md")
        self.assertFalse((knowledge / "log.md").exists(), "flush wrote log.md")
        self.assertEqual(list((knowledge / "concepts").glob("*.md")) if (knowledge / "concepts").is_dir() else [], [])
        self.assertEqual(list((knowledge / "connections").glob("*.md")) if (knowledge / "connections").is_dir() else [], [])

    def test_claude_flush_writes_daily_and_continuity_only(self) -> None:
        event_path = self._flush("claude")
        self.assertTrue(event_path.is_file())
        self.assertTrue((self.vault / "continuity" / "luvaa.md").is_file())
        self._assert_knowledge_untouched()

    def test_codex_flush_writes_daily_and_continuity_only(self) -> None:
        event_path = self._flush("codex")
        self.assertTrue(event_path.is_file())
        self.assertTrue((self.vault / "continuity" / "luvaa.md").is_file())
        self._assert_knowledge_untouched()

    def test_hermes_flush_writes_daily_and_continuity_only(self) -> None:
        event_path = self._flush("hermes")
        self.assertTrue(event_path.is_file())
        self.assertTrue((self.vault / "continuity" / "hermes.md").is_file())
        self._assert_knowledge_untouched()

    def test_parity_alignment_does_not_seed_shared_anchors(self) -> None:
        from memory_v1.parity import SharedBrainParityManager
        SharedBrainParityManager(self.vault).align_shared_brain()
        self.assertTrue((self.vault / "knowledge" / "concepts").is_dir())
        self.assertFalse((self.vault / "knowledge" / "index.md").exists())
        self.assertFalse((self.vault / "knowledge" / "log.md").exists())


if __name__ == "__main__":
    unittest.main()

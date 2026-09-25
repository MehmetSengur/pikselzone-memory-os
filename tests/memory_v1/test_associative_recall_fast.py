"""§6: bounded, index-first, synchronous cross-project associative recall."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from memory_v1 import recall as recall_mod
from memory_v1.core import MemoryConfig
from memory_v1.recall import associative_recall_fast

_INDEX = """# Knowledge Base Index

Living concept and connection index for Pikselzone Second Brain.

| Article | Summary | Source | Updated |
|---|---|---|---|
| [Meta Catalog Mismatch](concepts/meta-catalog-mismatch.md) | Meta feed parent id vs event variant id gave low catalog match rate content_ids alignment failed pattern | twoberries:a | 2026-08-30 |
| [Aura Cache Warmup](concepts/aura-cache-warmup.md) | Redis ping then flush then warmup by zone | claude:b | 2026-08-20 |
| [Deploy Rollback](concepts/deploy-rollback.md) | Rollback status goes right after results in ORION reports | hermes:c | 2026-08-19 |
"""

_META_CONCEPT = """---
title: "Meta Catalog Mismatch"
---

# Meta Catalog Mismatch

## Özet
Problem: Meta catalog content_ids match rate low at TwoBerries. Denenen: feed parent
id plus event variant id. Sonuc: basarisiz, AddToCart match ~0.1%. Neden: feed and
event identity strategy were misaligned.
"""


class AssociativeRecallFastTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="pz-test-assoc-")
        self.root = Path(self._tmp.name).resolve()
        self.vault = self.root / "vault"
        self.concepts = self.vault / "knowledge" / "concepts"
        self.concepts.mkdir(parents=True)
        (self.vault / "knowledge" / "connections").mkdir(parents=True)
        (self.vault / "knowledge" / "index.md").write_text(_INDEX, encoding="utf-8")
        (self.concepts / "meta-catalog-mismatch.md").write_text(_META_CONCEPT, encoding="utf-8")
        (self.concepts / "aura-cache-warmup.md").write_text(
            "---\ntitle: x\n---\n\n# x\n\n## Özet\nRedis warmup by zone.\n", encoding="utf-8"
        )
        (self.concepts / "deploy-rollback.md").write_text(
            "---\ntitle: y\n---\n\n# y\n\n## Özet\nRollback after results.\n", encoding="utf-8"
        )
        self.cfg = MemoryConfig.from_dict({
            "role": "workstation",
            "vault_path": str(self.vault),
            "state_path": str(self.root / "state"),
            "runtimes": ["codex", "claude"],
            "transcript_roots": {"codex": [str(self.root)], "claude": [str(self.root)]},
            "can_write_event_memory": True,
            "can_run_compiler": False,
            "provider": {"mode": "runtime-native"},
        })

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_trivial_prompt_gate(self) -> None:
        for p in ("tamam", "devam et", "ok", "commit et", "evet", "  ", "kısa"):
            self.assertEqual(associative_recall_fast(self.cfg, p), "", p)

    def test_high_score_prompt_surfaces_relevant_concept_only(self) -> None:
        out = associative_recall_fast(
            self.cfg,
            "Meta katalog content_ids event eslesme match rate low feed identity",
        )
        self.assertIn("meta-catalog-mismatch", out)
        self.assertIn("[DERIVED MEMORY", out)
        self.assertNotIn("aura-cache-warmup", out)
        self.assertNotIn("deploy-rollback", out)

    def test_low_score_prompt_injects_nothing(self) -> None:
        self.assertEqual(
            associative_recall_fast(
                self.cfg, "lütfen bu python fonksiyonunu biraz daha okunur yap"
            ),
            "",
        )

    def test_structural_bounds_index_first_and_capped_reads(self) -> None:
        real_secure_read = recall_mod.secure_read_text
        opened: list[str] = []

        def tracking_read(path, **kw):
            opened.append(str(path))
            return real_secure_read(path, **kw)

        with mock.patch.object(recall_mod, "secure_read_text", side_effect=tracking_read):
            associative_recall_fast(
                self.cfg,
                "Meta katalog content_ids event eslesme match rate low feed identity feed",
                max_items=3,
            )
        concept_reads = [p for p in opened if "/knowledge/concepts/" in p]
        self.assertLessEqual(len(concept_reads), 3)
        self.assertFalse(any("/knowledge/connections/" in p for p in opened))
        self.assertFalse(any(p.endswith("/daily") or "/daily/" in p for p in opened))

    def test_fail_open_on_missing_index(self) -> None:
        (self.vault / "knowledge" / "index.md").unlink()
        self.assertEqual(
            associative_recall_fast(self.cfg, "Meta katalog content_ids feed identity"),
            "",
        )

    def test_denylist_slug_is_downweighted(self) -> None:
        (self.vault / "knowledge" / "index.md").write_text(
            _INDEX + "| [API](concepts/api.md) | api api api feed identity content_ids |"
            " x | 2026-08-30 |\n",
            encoding="utf-8",
        )
        (self.concepts / "api.md").write_text(
            "---\ntitle: API\n---\n\n# API\n\n## Özet\napi feed identity content_ids api api\n",
            encoding="utf-8",
        )
        out = associative_recall_fast(self.cfg, "feed identity content_ids api mismatch pattern")
        self.assertNotIn("concepts/api.md", out)


if __name__ == "__main__":
    unittest.main()

"""Unit tests for scripts/rebuild_knowledge_index.py (deterministic index rebuild)."""
from __future__ import annotations

import io
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from memory_v1.core import MemoryConfig
from memory_v1.recall import _load_knowledge_index_entries
from scripts.rebuild_knowledge_index import (
    HEADER_ROW,
    INDEX_PROSE,
    INDEX_TITLE,
    SEPARATOR_ROW,
    main,
    rebuild_index,
)


CONCEPT_FULL = """---
title: "Alpha Service"
aliases: []
tags: ["#concept"]
created: "2026-08-01"
updated: "2026-08-29"
sources: ["claude:aaaa1111", "codex:bbbb2222"]
authority: derived-memory-not-canonical
---

# Alpha Service

## Özet
Alpha Service is the shared ingress tier that fronts every downstream worker.

## Detaylar
- extra detail line
"""

# Varied completeness: no frontmatter title (H1 fallback), only `created`, no sources.
CONCEPT_SPARSE = """---
created: "2026-07-15"
---

# Beta Thing

## Özet
Beta Thing   collapses      whitespace   across   lines
and keeps going on a second line until the blank.

## İlgili Bağlantılar
- [[concepts/alpha-service]]
"""

# No `## Özet` section at all.
CONCEPT_NO_OZET = """---
title: "Gamma"
updated: "2026-09-01"
---

# Gamma

## Detaylar
- Gamma has no summary section.
"""


class TestRebuildKnowledgeIndex(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(prefix="pz-test-index-rebuild-")
        self.root = Path(self.temp_dir.name).resolve()
        self.vault = self.root / "vault"
        self.state = self.root / "state"
        self.concepts_dir = self.vault / "knowledge" / "concepts"
        self.concepts_dir.mkdir(parents=True)
        (self.vault / "knowledge" / "connections").mkdir(parents=True)
        self.state.mkdir(parents=True)

        self._write("alpha-service.md", CONCEPT_FULL)
        self._write("beta-thing.md", CONCEPT_SPARSE)
        self._write("gamma.md", CONCEPT_NO_OZET)
        # Obsidian sync conflict copy -- must be ignored by the rebuild.
        self._write(
            "alpha-service (Conflicted copy pz-hermes 202608301608).md",
            CONCEPT_FULL,
        )

        self.config = MemoryConfig.from_dict(
            {
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
                "provider": {"mode": "runtime-native"},
            }
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _write(self, name: str, content: str) -> Path:
        path = self.concepts_dir / name
        path.write_text(content, encoding="utf-8")
        return path

    @property
    def index_path(self) -> Path:
        return self.vault / "knowledge" / "index.md"

    def _data_rows(self, text: str) -> list[str]:
        return [
            line
            for line in text.splitlines()
            if line.startswith("|")
            and not line.startswith("| Article")
            and "---" not in line
        ]

    # 1. Three concepts in, three rows out; conflicted copy excluded; sorted; anchor intact.
    def test_rebuild_produces_three_sorted_rows(self) -> None:
        text = rebuild_index(self.vault)

        lines = text.splitlines()
        self.assertEqual(lines[0], INDEX_TITLE)          # "# Knowledge Base Index"
        self.assertEqual(lines[1], "")
        self.assertEqual(lines[2], INDEX_PROSE)
        self.assertEqual(lines[3], "")
        self.assertEqual(lines[4], HEADER_ROW)
        self.assertEqual(lines[5], SEPARATOR_ROW)

        rows = self._data_rows(text)
        self.assertEqual(len(rows), 3)
        self.assertNotIn("Conflicted copy", text)

        # Rows sorted by article title (case-insensitive), then slug.
        # Article cell is a pipe-free markdown link relative to knowledge/index.md.
        self.assertIn("[Alpha Service](concepts/alpha-service.md)", rows[0])
        self.assertIn("[Beta Thing](concepts/beta-thing.md)", rows[1])
        self.assertIn("[Gamma](concepts/gamma.md)", rows[2])

        # File written to <vault>/knowledge/index.md and equal to the returned text.
        self.assertTrue(self.index_path.is_file())
        self.assertEqual(self.index_path.read_text(encoding="utf-8"), text)

    def test_emits_canonical_header_regardless_of_existing_content(self) -> None:
        self.index_path.write_text(
            "# Some Other Title\n\nstale body\n\n| junk |\n", encoding="utf-8"
        )
        lines = rebuild_index(self.vault).splitlines()
        self.assertEqual(
            lines[:6],
            [INDEX_TITLE, "", INDEX_PROSE, "", HEADER_ROW, SEPARATOR_ROW],
        )

    def test_row_fields_are_derived_from_frontmatter(self) -> None:
        rows = self._data_rows(rebuild_index(self.vault))
        alpha = rows[0]
        self.assertIn("[Alpha Service](concepts/alpha-service.md)", alpha)
        self.assertIn(
            "| Alpha Service is the shared ingress tier that fronts every downstream worker. |",
            alpha,
        )
        self.assertIn("| claude:aaaa1111; codex:bbbb2222 |", alpha)
        self.assertTrue(alpha.rstrip().endswith("| 2026-08-29 |"))

        # Sparse concept: H1 title fallback, whitespace-collapsed summary,
        # empty source, `created` used when `updated` is absent.
        beta = rows[1]
        self.assertIn("[Beta Thing](concepts/beta-thing.md)", beta)
        self.assertIn(
            "Beta Thing collapses whitespace across lines and keeps going on a second line until the blank.",
            beta,
        )
        self.assertIn("|  |", beta)  # empty Source cell
        self.assertTrue(beta.rstrip().endswith("| 2026-07-15 |"))

    # 2. Round-trip: the recall parser reads every rebuilt row column-clean.
    def test_recall_parser_round_trip(self) -> None:
        rebuild_index(self.vault)
        entries = _load_knowledge_index_entries(self.config)
        self.assertEqual(len(entries), 3)
        self.assertEqual({e.item_type for e in entries}, {"knowledge_index"})

        by_slug = {}
        for e in entries:
            for slug in ("alpha-service", "beta-thing", "gamma"):
                if slug in e.content:
                    by_slug[slug] = e
        self.assertEqual(set(by_slug), {"alpha-service", "beta-thing", "gamma"})

        # The pipe-free article cell means summary / updated land in the right
        # columns: the real Özet text is in `content`, the real date in `created_at`.
        alpha = by_slug["alpha-service"]
        self.assertIn("shared ingress tier", alpha.content)
        self.assertEqual(alpha.created_at, "2026-08-29")
        self.assertEqual(by_slug["beta-thing"].created_at, "2026-07-15")

    # 3. A concept with no `## Özet` still yields a row with an empty summary.
    def test_concept_without_ozet_still_emits_row(self) -> None:
        rows = self._data_rows(rebuild_index(self.vault))
        gamma = next(r for r in rows if "gamma" in r)
        self.assertIn("[Gamma](concepts/gamma.md)", gamma)
        # | article | (empty summary) | (empty source) | 2026-09-01 |
        cells = [c.strip() for c in gamma.split("|")[1:-1]]
        self.assertEqual(len(cells), 4)
        self.assertEqual(cells[1], "")   # summary
        self.assertEqual(cells[2], "")   # source
        self.assertEqual(cells[3], "2026-09-01")

    # 4. Dry run writes nothing and does not disturb an existing file.
    def test_dry_run_writes_nothing(self) -> None:
        self.assertFalse(self.index_path.exists())
        text = rebuild_index(self.vault, dry_run=True)
        self.assertIn(HEADER_ROW, text)
        self.assertFalse(self.index_path.exists())

        sentinel = "# Index\n\nSENTINEL - do not overwrite\n"
        self.index_path.write_text(sentinel, encoding="utf-8")
        rebuild_index(self.vault, dry_run=True)
        self.assertEqual(self.index_path.read_text(encoding="utf-8"), sentinel)

    def test_dry_run_via_out_target(self) -> None:
        out = self.root / "elsewhere" / "index.md"
        text = rebuild_index(self.vault, out=out, dry_run=True)
        self.assertFalse(out.exists())
        self.assertFalse(self.index_path.exists())
        self.assertIn(SEPARATOR_ROW, text)

    def test_out_target_is_written_when_not_dry_run(self) -> None:
        out = self.root / "elsewhere" / "index.md"
        text = rebuild_index(self.vault, out=out)
        self.assertTrue(out.is_file())
        self.assertEqual(out.read_text(encoding="utf-8"), text)
        self.assertFalse(self.index_path.exists())

    def test_cli_dry_run_prints_and_writes_nothing(self) -> None:
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            rc = main(["--vault", str(self.vault), "--dry-run"])
        self.assertEqual(rc, 0)
        self.assertIn(HEADER_ROW, buffer.getvalue())
        self.assertFalse(self.index_path.exists())

    def test_cli_writes_index(self) -> None:
        rc = main(["--vault", str(self.vault)])
        self.assertEqual(rc, 0)
        self.assertTrue(self.index_path.is_file())
        self.assertEqual(
            len(self._data_rows(self.index_path.read_text(encoding="utf-8"))), 3
        )


if __name__ == "__main__":
    unittest.main()

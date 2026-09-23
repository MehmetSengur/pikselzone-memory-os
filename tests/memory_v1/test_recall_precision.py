"""Recall precision: Turkish-aware tokens and a content-word injection gate.

Both defects below were measured on the live tokenizer, not assumed:

* ``[a-z0-9_-]+`` does not match Turkish letters, so a word is shredded at
  every non-ASCII letter: ``üzerinde`` -> ``zerinde``, ``aldığımız`` ->
  ``ald`` + ``ir``, ``gözden`` -> ``zden``, ``için`` -> ``in``.  Two costs
  follow.  A word stops matching itself across inflections (``kararları`` ->
  ``kararlar`` never meets ``karar``), and the leftover fragments (``ir``,
  ``in``, ``al``, ``ge``) are short junk tokens that match across unrelated
  documents.
* ``MIN_ASSOCIATIVE_SHARED_TOKENS`` counts *any* two shared tokens, so two
  shared stopwords satisfy the cross-project injection gate.  On the live
  vault the filler slug ``bunu`` reached a prompt this way, sharing only
  ``{bir, bunu}``.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from memory_v1 import recall as recall_mod
from memory_v1.core import MemoryConfig
from memory_v1.recall import associative_recall_fast


class TurkishTokenTest(unittest.TestCase):
    """The tokenizer must fold Turkish letters and fold inflections onto a stem."""

    def test_turkish_letters_do_not_shred_a_word(self) -> None:
        for word, forbidden in (
            ("üzerinde", "zerinde"),
            ("gözden", "zden"),
            ("aldığımız", "ald"),
            ("için", "in"),
        ):
            tokens = recall_mod._tokenize(word)
            self.assertNotIn(forbidden, tokens, f"{word} shredded into {sorted(tokens)}")

    def test_inflections_share_one_stem(self) -> None:
        for a, b in (
            ("kararları", "karar"),
            ("notlar", "not"),
            ("kütüphanesi", "kütüphane"),
            ("farkı", "fark"),
        ):
            self.assertTrue(
                recall_mod._tokenize(a) & recall_mod._tokenize(b),
                f"{a} and {b} share no stem: {sorted(recall_mod._tokenize(a))} "
                f"vs {sorted(recall_mod._tokenize(b))}",
            )

    def test_short_junk_fragments_are_not_content(self) -> None:
        """Whatever survives folding, function words are never content tokens."""
        content = recall_mod._content_tokens("bunu bir de şu için ve bu")
        self.assertEqual(content, set(), f"unexpected content tokens: {sorted(content)}")

    def test_real_words_survive_as_content(self) -> None:
        content = recall_mod._content_tokens("kanban kararları ve sahiplik devri")
        self.assertIn(recall_mod._stem("karar"), content)
        self.assertIn(recall_mod._stem("sahiplik"), content)


_INDEX = """# Knowledge Base Index

| Article | Summary | Source | Updated |
|---|---|---|---|
| [Bunu](concepts/bunu.md) | Bunu Pikselzone Memory OS ortak kalici hafizasina kaydet bu kayit bir islem icin gerekli oldu | codex:a | 2026-08-31 |
| [Kanban Karar Akisi](concepts/kanban-karar-akisi.md) | Kanban uzerinde alinan karar kaydi ve sahiplik devri | codex:c | 2026-08-29 |
"""

_BUNU = """---
title: "Bunu"
---

# Bunu

## Özet
Bunu Pikselzone Memory OS ortak kalici hafizasina ve shared Obsidian vault'a
kaydet. Bu kayit bir islem icin gerekli oldu.
"""

_KARAR = """---
title: "Kanban Karar Akisi"
---

# Kanban Karar Akisi

## Özet
Kanban üzerinde alınan karar kaydı sahiplik devri ile birlikte tutulur.
Her karar için sorumlu kişi ve tarih zorunludur.
"""


class AssociativeInjectionGateTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="pz-test-precision-")
        self.root = Path(self._tmp.name).resolve()
        self.vault = self.root / "vault"
        self.concepts = self.vault / "knowledge" / "concepts"
        self.concepts.mkdir(parents=True)
        (self.vault / "knowledge" / "connections").mkdir(parents=True)
        (self.vault / "knowledge" / "index.md").write_text(_INDEX, encoding="utf-8")
        (self.concepts / "bunu.md").write_text(_BUNU, encoding="utf-8")
        (self.concepts / "kanban-karar-akisi.md").write_text(_KARAR, encoding="utf-8")
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

    def test_filler_slug_is_not_injected_on_shared_stopwords(self) -> None:
        """`bunu` shares only {bir, bunu} with this prompt: both are stopwords."""
        out = associative_recall_fast(
            self.cfg,
            "bunu bir de su acidan dusunelim bir kargo etiketi hazirlayalim",
            min_score=0.0,
        )
        self.assertNotIn("concepts/bunu.md", out)

    def test_inflected_prompt_reaches_its_concept(self) -> None:
        """`kararları` must reach a concept that spells the word `karar`."""
        out = associative_recall_fast(
            self.cfg, "aldığımız kararları ve sahiplik devrini gözden geçir"
        )
        self.assertIn("concepts/kanban-karar-akisi.md", out)


if __name__ == "__main__":
    unittest.main()

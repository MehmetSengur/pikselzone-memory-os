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
from unittest import mock

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



class NoiseConceptSlugTest(unittest.TestCase):
    """An exact denylist only catches words someone already added to it."""

    def test_rejects_what_the_live_vault_actually_grew(self) -> None:
        from memory_v1.core import is_noise_concept_slug
        for slug in ("bunu", "current", "for", "yeni", "yyyy-mm-dd",
                     "pz-hc-20260830-3a172134f1", "pz-codex-canary-20260830-f3a9",
                     "sb2-claude-canary-83b54d", "a", ""):
            self.assertTrue(is_noise_concept_slug(slug), slug)

    def test_keeps_real_subjects_including_short_proper_nouns(self) -> None:
        from memory_v1.core import is_noise_concept_slug
        for slug in ("redis", "obsidian", "avenox", "ga4", "capi", "atlas",
                     "aura-cache-sync", "deploy-rollback", "hermes-vps-continuity"):
            self.assertFalse(is_noise_concept_slug(slug), slug)


class WeightedOverlapTest(unittest.TestCase):
    """A shared word is worth less the more of the vault carries it."""

    CORPUS = [
        "Aura Cache Sync redis warmup by zone",
        "Deploy Rollback sistem rollback after results",
        "Meta Catalog Mismatch sistem feed parent id",
        "Kanban Karar Akisi sistem karar kaydi",
    ]

    def test_a_rare_term_outweighs_a_common_one(self) -> None:
        from memory_v1.recall import document_frequencies, weighted_overlap
        frequencies, total = document_frequencies(self.CORPUS)
        rare = weighted_overlap("redis", self.CORPUS[0], frequencies, total)
        common = weighted_overlap("sistem", self.CORPUS[1], frequencies, total)
        self.assertGreater(rare, common)

    def test_no_shared_content_scores_zero(self) -> None:
        from memory_v1.recall import document_frequencies, weighted_overlap
        frequencies, total = document_frequencies(self.CORPUS)
        self.assertEqual(
            0.0, weighted_overlap("bunu bir de su", self.CORPUS[0], frequencies, total)
        )

    def test_length_does_not_penalise_a_relevant_document(self) -> None:
        """The failure avenoxai/avenoxbeyin#83 reports: long notes lose."""
        from memory_v1.recall import document_frequencies, weighted_overlap
        frequencies, total = document_frequencies(self.CORPUS)
        short = self.CORPUS[0]
        long = self.CORPUS[0] + " " + ("ek detay satiri " * 200)
        self.assertEqual(
            weighted_overlap("redis warmup", short, frequencies, total),
            weighted_overlap("redis warmup", long, frequencies, total),
        )


_LONG_A = """---
title: "Aura Cache Sync"
---

# Aura Cache Sync

## Özet
""" + ("Redis onbellek zonu isinma sirasi ve kanban karar kaydi ayrintisi. " * 40)

_LONG_B = """---
title: "Deploy Rollback Akisi"
---

# Deploy Rollback Akisi

## Özet
""" + ("Kanban karar kaydi sonrasi rollback sirasi ve sahiplik devri. " * 40)

_LONG_INDEX = """# Knowledge Base Index

| Article | Summary | Source | Updated |
|---|---|---|---|
| [Aura Cache Sync](concepts/aura-cache-sync.md) | Redis onbellek zonu isinma ve kanban karar kaydi | codex:a | 2026-08-31 |
| [Deploy Rollback Akisi](concepts/deploy-rollback-akisi.md) | Kanban karar kaydi sonrasi rollback ve sahiplik devri | codex:b | 2026-08-30 |
"""


class AssociativeBudgetTest(unittest.TestCase):
    """A budget must cut whole sections, never a word or a body away from its header.

    Both shapes below were observed in live hook output: a body severed
    mid-word ("...router/instruction deği") and a header whose body was cut to
    nothing, leaving a citation that pointed at text the reader never got.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="pz-test-budget-")
        self.root = Path(self._tmp.name).resolve()
        self.vault = self.root / "vault"
        concepts = self.vault / "knowledge" / "concepts"
        concepts.mkdir(parents=True)
        (self.vault / "knowledge" / "connections").mkdir(parents=True)
        (self.vault / "knowledge" / "index.md").write_text(_LONG_INDEX, encoding="utf-8")
        (concepts / "aura-cache-sync.md").write_text(_LONG_A, encoding="utf-8")
        (concepts / "deploy-rollback-akisi.md").write_text(_LONG_B, encoding="utf-8")
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
        self.query = "kanban karar kaydi ve rollback sirasi sahiplik devri"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_output_stays_within_budget(self) -> None:
        out = associative_recall_fast(self.cfg, self.query, budget_chars=2400)
        self.assertLessEqual(len(out), 2400)

    @staticmethod
    def _sections(out: str) -> list[tuple[str, str]]:
        """Concept sections only; the authority notice also starts with '### '."""
        import re as _re
        parts = _re.split(r"(?m)^### \[", out)[1:]
        return [(p.split("\n", 1)[0], p.split("\n", 1)[1] if "\n" in p else "") for p in parts]

    def test_no_header_is_left_without_a_body(self) -> None:
        out = associative_recall_fast(self.cfg, self.query, budget_chars=2400)
        sections = self._sections(out)
        self.assertTrue(sections, "expected at least one concept in the output")
        for header, body in sections:
            body = body.replace("[TRUNCATED_ASSOCIATIVE_RECALL]", "").strip()
            self.assertTrue(body, f"header with no body: {header}")

    def test_a_long_first_concept_does_not_starve_the_second(self) -> None:
        out = associative_recall_fast(self.cfg, self.query, budget_chars=2400, max_items=2)
        self.assertEqual(2, len(self._sections(out)),
                         f"one concept ate the budget:\n{out[:400]}")

    def test_a_body_is_not_cut_mid_word(self) -> None:
        """Every word emitted must be a whole word of the source vocabulary.

        A cut at a word boundary legitimately ends in a letter, so the last
        character says nothing. A cut inside a word produces a fragment that
        the source never contained.
        """
        vocabulary = set((_LONG_A + " " + _LONG_B).split())
        out = associative_recall_fast(self.cfg, self.query, budget_chars=2400)
        for header, body in self._sections(out):
            body = body.replace("[TRUNCATED_ASSOCIATIVE_RECALL]", "")
            for word in body.split():
                self.assertIn(word, vocabulary, f"fragment {word!r} in {header}")


_HASH_HEAVY = """---
title: "Hermes Vps Continuity"
aliases:
  - "Hermes Contabo deployment"
tags: ["#concept", hermes, vps]
sources:
""" + "".join(
    f'  - "codex-{i:02d}.md (sha256: {str(i) * 2}{"ab12cd34ef56" * 5})"\n' for i in range(9)
) + """created: "2026-09-14"
updated: "2026-09-14"
authority: derived-memory-not-canonical
---

# Hermes Vps Continuity

## Özet
Hermes vps uzerinde ortak hafiza senkronizasyonu Obsidian sync ile yurur ve
kanban karar kaydi ayrica tutulur. Bu cumle gercek icerik tasir.
"""

_HASH_INDEX = """# Knowledge Base Index

| Article | Summary | Source | Updated |
|---|---|---|---|
| [Hermes Vps Continuity](concepts/hermes-vps-continuity.md) | Hermes vps ortak hafiza senkronizasyonu ve kanban karar kaydi | codex:a | 2026-09-14 |
"""


class ConceptBodyTest(unittest.TestCase):
    """Frontmatter is metadata about the note, not the note.

    Measured on the live vault, two concepts carry a `sources:` list longer
    than the whole excerpt, so the injected text was 1400 characters of YAML
    hashes and not one word of the concept. The budget was spent to deliver
    nothing, under a header that cited the file.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="pz-test-body-")
        self.root = Path(self._tmp.name).resolve()
        self.vault = self.root / "vault"
        concepts = self.vault / "knowledge" / "concepts"
        concepts.mkdir(parents=True)
        (self.vault / "knowledge" / "connections").mkdir(parents=True)
        (self.vault / "knowledge" / "index.md").write_text(_HASH_INDEX, encoding="utf-8")
        (concepts / "hermes-vps-continuity.md").write_text(_HASH_HEAVY, encoding="utf-8")
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
        self.query = "hermes vps ortak hafiza senkronizasyonu ve kanban karar kaydi"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_the_injected_text_carries_the_concept_not_its_hashes(self) -> None:
        out = associative_recall_fast(self.cfg, self.query)
        self.assertIn("gercek icerik tasir", out)

    def test_source_hashes_do_not_reach_the_prompt(self) -> None:
        out = associative_recall_fast(self.cfg, self.query)
        self.assertNotIn("sha256:", out)
        self.assertNotIn("authority: derived-memory-not-canonical", out)

    def test_an_alias_still_finds_the_concept(self) -> None:
        """Frontmatter is dropped from the body, not from what is searched."""
        out = associative_recall_fast(self.cfg, "hermes contabo deployment surekliligi")
        self.assertIn("concepts/hermes-vps-continuity.md", out)


class RerankWiringTest(unittest.TestCase):
    """The recall path must honour the config, and be unchanged when it is off."""

    CONCEPT = """---
title: "%s"
---

# %s

## Özet
Kanban karar kaydi ve sahiplik devri %s icin tutulur ve gozden gecirilir.
"""
    INDEX = """# Knowledge Base Index

| Article | Summary | Source | Updated |
|---|---|---|---|
| [Alfa](concepts/alfa-kaydi.md) | Kanban karar kaydi ve sahiplik devri alfa | codex:a | 2026-09-01 |
| [Beta](concepts/beta-kaydi.md) | Kanban karar kaydi ve sahiplik devri beta | codex:b | 2026-09-02 |
"""

    def _config(self, rerank=None):
        raw = {
            "role": "workstation",
            "vault_path": str(self.vault),
            "state_path": str(self.root / "state"),
            "runtimes": ["codex", "claude"],
            "transcript_roots": {"codex": [str(self.root)], "claude": [str(self.root)]},
            "can_write_event_memory": True,
            "can_run_compiler": False,
            "provider": {"mode": "runtime-native"},
        }
        if rerank is not None:
            raw["rerank"] = rerank
        return MemoryConfig.from_dict(raw)

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="pz-test-rerank-wire-")
        self.root = Path(self._tmp.name).resolve()
        self.vault = self.root / "vault"
        concepts = self.vault / "knowledge" / "concepts"
        concepts.mkdir(parents=True)
        (self.vault / "knowledge" / "connections").mkdir(parents=True)
        (self.vault / "knowledge" / "index.md").write_text(self.INDEX, encoding="utf-8")
        for slug, name in (("alfa-kaydi", "Alfa"), ("beta-kaydi", "Beta")):
            (concepts / f"{slug}.md").write_text(
                self.CONCEPT % (name, name, name), encoding="utf-8"
            )
        self.query = "kanban karar kaydi ve sahiplik devri gozden gecir"
        self.enabled = {
            "mode": "on", "model": "gpt-5.4-nano-2026-03-17",
            "key_env": "PZ_TEST_RERANK_KEY", "min_score": 2,
        }

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_default_config_does_not_rerank(self) -> None:
        cfg = self._config()
        self.assertEqual("off", cfg.rerank["mode"])
        out = associative_recall_fast(cfg, self.query)
        self.assertIn("concepts/alfa-kaydi.md", out)
        self.assertIn("concepts/beta-kaydi.md", out)

    def test_an_enabled_rerank_drops_what_it_scores_low(self) -> None:
        def transport(payload, timeout):
            keep = [c["id"] for c in payload["state"]["candidates"] if "Beta" in c["title"]]
            return {c["id"]: (2 if c["id"] in keep else 0)
                    for c in payload["state"]["candidates"]}

        with mock.patch("memory_v1.reranker.build_transport", return_value=transport):
            out = associative_recall_fast(self._config(self.enabled), self.query)
        self.assertIn("concepts/beta-kaydi.md", out)
        self.assertNotIn("concepts/alfa-kaydi.md", out)

    def test_a_failing_rerank_leaves_the_lexical_result(self) -> None:
        def boom(payload, timeout):
            raise TimeoutError("slow")

        with mock.patch("memory_v1.reranker.build_transport", return_value=boom):
            out = associative_recall_fast(self._config(self.enabled), self.query)
        self.assertIn("concepts/alfa-kaydi.md", out)
        self.assertIn("concepts/beta-kaydi.md", out)

if __name__ == "__main__":
    unittest.main()

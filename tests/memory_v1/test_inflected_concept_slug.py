"""An inflected word grabbed mid-sentence is not a concept name.

The live vault grew `oturumun` ("of the session") and `tercihin` ("your
preference") as concepts: the compiler took an inflected word out of a sentence
and titled a concept with it. A real concept name is the base form, or several
words.

Detecting this by "the slug is not its own stem" was measured and rejected: the
suffix table is Turkish and English words end in those letters, so 13 of 15
single-word English concepts -- `claude`, `compiler`, `pikselzone` among them --
would have been deleted.

What separates the two is whether the base form is a word the vault actually
uses. Peeling exactly one suffix from `oturumun` gives `oturum`, which appears
in the corpus; peeling one from `claude` gives `clau` or `claud`, which appear
nowhere. Stemming to a fixpoint is not enough here either: it over-peels
`oturumun` past `oturum` to `otur`, which is unattested, and the case is missed.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from memory_v1.core import is_inflected_concept_slug

# What the live vault attests, with the counts measured there.
ATTESTED = {"oturum", "tercih", "karar", "hafiza", "kanban", "proje"}


class InflectedSlugTest(unittest.TestCase):
    def test_an_inflected_turkish_word_is_rejected(self) -> None:
        for slug in ("oturumun", "tercihin"):
            self.assertTrue(is_inflected_concept_slug(slug, ATTESTED), slug)

    def test_english_single_words_survive(self) -> None:
        """The measured false-positive set of the rejected stem-based rule."""
        for slug in ("claude", "closure", "compiler", "dockerfile", "evidence",
                     "native", "pikselzone", "preference", "pretooluse",
                     "probe", "service", "source"):
            self.assertFalse(is_inflected_concept_slug(slug, ATTESTED), slug)

    def test_a_base_form_is_not_an_inflection_of_itself(self) -> None:
        for slug in ("oturum", "tercih", "karar", "kanban"):
            self.assertFalse(is_inflected_concept_slug(slug, ATTESTED), slug)

    def test_multi_word_slugs_are_left_alone(self) -> None:
        """A real concept name may legitimately end in an inflected word."""
        for slug in ("kanban-karar-akisi", "hermes-vps-continuity",
                     "aura-cache-sync", "sengur-seo-oturumun"):
            self.assertFalse(is_inflected_concept_slug(slug, ATTESTED), slug)

    def test_an_unattested_base_is_not_evidence(self) -> None:
        """Without the base form in the corpus the split is a guess."""
        self.assertFalse(is_inflected_concept_slug("oturumun", set()))
        self.assertFalse(is_inflected_concept_slug("tercihin", {"baska"}))

    def test_empty_and_short_input_is_safe(self) -> None:
        for slug in ("", "a", "ga4", "capi"):
            self.assertFalse(is_inflected_concept_slug(slug, ATTESTED), slug)


if __name__ == "__main__":
    unittest.main()

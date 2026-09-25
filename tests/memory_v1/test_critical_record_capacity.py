"""A capacity limit must bound the output, never destroy the session.

Measured on the live workstation on 2026-09-23: thirteen retry records, all
`classification: permanent` after a single attempt with `next_attempt_after:
null`, every one of them `critical-record-capacity-exceeded-source-retained`.
Seven sessions were affected and twenty-seven of the thirty-three pending turn
checkpoints belonged to them, the oldest waiting since 18 September.

The cause is a soft limit raised as a hard error. A session that produces its
128th critical sentence, or one sentence longer than MAX_TEXT, loses its whole
memory: SchemaError is classified permanent, so the batch is never retried and
the thread can never be promoted again. Keeping 128 records and saying so is
strictly better than keeping none.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from memory_v1 import critical_records as cr
from memory_v1.core import SchemaError


def transcript(sentences: list[str]) -> str:
    return "".join(f"USER: {s}\n" for s in sentences)


def critical(n: int) -> list[str]:
    # CRITICAL matches a digit, so each of these qualifies on its own.
    return [f"Karar: {i} numarali paket icin fiyat {i} TL olarak belirlendi." for i in range(n)]


class ExtractionCapacityTest(unittest.TestCase):
    def test_a_session_over_the_limit_keeps_records_instead_of_losing_all(self) -> None:
        records = cr.extract_records(
            transcript(critical(cr.MAX_RECORDS + 40)),
            runtime="codex", session_id="s1",
        )
        self.assertEqual(cr.MAX_RECORDS, len(records))

    def test_the_result_still_validates(self) -> None:
        records = cr.extract_records(
            transcript(critical(cr.MAX_RECORDS + 40)),
            runtime="codex", session_id="s1",
        )
        cr.validate_records(records)  # must not raise

    def test_one_oversized_sentence_does_not_discard_the_others(self) -> None:
        sentences = critical(3)
        sentences.insert(1, "Karar: " + ("x" * (cr.MAX_TEXT + 10)) + " 5 TL.")
        records = cr.extract_records(
            transcript(sentences), runtime="codex", session_id="s2"
        )
        self.assertEqual(3, len(records), "the three normal decisions must survive")
        self.assertTrue(all(len(r["text"]) <= cr.MAX_TEXT for r in records))

    def test_a_normal_session_is_unchanged(self) -> None:
        records = cr.extract_records(
            transcript(critical(5)), runtime="codex", session_id="s3"
        )
        self.assertEqual(5, len(records))


class MergeCapacityTest(unittest.TestCase):
    def test_merging_over_the_limit_keeps_the_newest_instead_of_failing(self) -> None:
        first = cr.extract_records(
            transcript(critical(100)), runtime="codex", session_id="a"
        )
        second = cr.extract_records(
            transcript([f"Karar: ek madde {i} icin deger {i} TL." for i in range(80)]),
            runtime="codex", session_id="b",
        )
        merged = cr.merge_records(first, second)
        self.assertEqual(cr.MAX_RECORDS, len(merged))
        cr.validate_records(merged)
        newest = {r["id"] for r in second}
        self.assertTrue(
            newest & {r["id"] for r in merged},
            "the newer group must not be the one dropped wholesale",
        )

    def test_a_merge_within_the_limit_is_unchanged(self) -> None:
        first = cr.extract_records(transcript(critical(3)), runtime="codex", session_id="a")
        merged = cr.merge_records(first, [])
        self.assertEqual(3, len(merged))


class StoredArtefactValidationTest(unittest.TestCase):
    """Reading back a stored artefact stays strict; only the producers bound."""

    def test_an_oversized_stored_list_is_still_rejected(self) -> None:
        records = cr.extract_records(
            transcript(critical(cr.MAX_RECORDS)), runtime="codex", session_id="s"
        )
        with self.assertRaises(SchemaError):
            cr.validate_records(records + records[:1] * 1 + records[:1])


if __name__ == "__main__":
    unittest.main()

"""Canonical authority contract: authority is declared, never inherited.

Fix A. Living in ``canonical/`` grants a document nothing.  A document is
authoritative only while it says ``status: active`` in its own frontmatter;
everything else -- superseded, draft, unrecognised, absent -- is
non-authoritative, which is the safe default.

These tests deliberately do NOT encode any judgement about the real
``Pikselzone Agency Operating Context.md``.  That document's mixed content is
Fix C's problem, and is owner-governed.
"""
from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from memory_v1.core import MemoryConfig
from memory_v1.recall import (
    CANONICAL_AUTHORITY_BONUS,
    CANONICAL_STATUS_ACTIVE,
    CANONICAL_STATUS_DRAFT,
    CANONICAL_STATUS_SUPERSEDED,
    CANONICAL_STATUS_UNKNOWN,
    CANONICAL_STATUS_UNSPECIFIED,
    associative_recall_fast,
    build_startup_recall_bundle,
    read_canonical_authority,
    targeted_recall,
)

DERIVED_LABEL = "[DERIVED MEMORY — verify against operational truth]"
AUTHORITY_LABEL = "[AUTHORITATIVE SOURCE]"


class CanonicalAuthorityTestCase(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(prefix="pz-test-canon-auth-")
        self.root = Path(self.temp_dir.name).resolve()
        self.vault = self.root / "vault"
        self.state = self.root / "state"
        for sub in (
            "canonical",
            "companion",
            "daily",
            "knowledge/concepts",
            "knowledge/connections",
        ):
            (self.vault / sub).mkdir(parents=True, exist_ok=True)
        (self.state / "evidence").mkdir(parents=True, exist_ok=True)
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

    def tearDown(self):
        self.temp_dir.cleanup()

    # -- helpers ------------------------------------------------------------

    def _canonical(self, name: str, body: str, status: str | None = None,
                   superseded_by: str | None = None) -> Path:
        head = ""
        if status is not None or superseded_by is not None:
            lines = ["---"]
            if status is not None:
                lines.append(f"status: {status}")
            if superseded_by is not None:
                lines.append(f"superseded_by: {superseded_by}")
            lines.append("---")
            head = "\n".join(lines) + "\n"
        path = self.vault / "canonical" / f"{name}.md"
        path.write_text(head + body, encoding="utf-8")
        return path

    def _result_for(self, results: list[dict], source_stem: str) -> dict | None:
        for item in results:
            if item["source"].endswith(f"{source_stem}.md"):
                return item
        return None


class TestCanonicalMetadataContract(CanonicalAuthorityTestCase):
    """The frontmatter reader itself."""

    def test_absent_status_is_unspecified_and_non_authoritative(self):
        authority = read_canonical_authority("# Plain doc\nNo frontmatter here.\n")
        self.assertEqual(authority.status, CANONICAL_STATUS_UNSPECIFIED)
        self.assertFalse(authority.authoritative)
        self.assertTrue(authority.selectable)

    def test_active_is_authoritative(self):
        authority = read_canonical_authority("---\nstatus: active\n---\n# Doc\n")
        self.assertEqual(authority.status, CANONICAL_STATUS_ACTIVE)
        self.assertTrue(authority.authoritative)
        self.assertTrue(authority.selectable)

    def test_superseded_is_not_authoritative_and_not_selectable(self):
        authority = read_canonical_authority("---\nstatus: superseded\n---\n# Doc\n")
        self.assertEqual(authority.status, CANONICAL_STATUS_SUPERSEDED)
        self.assertFalse(authority.authoritative)
        self.assertFalse(authority.selectable)

    def test_draft_is_readable_but_not_authoritative(self):
        authority = read_canonical_authority("---\nstatus: draft\n---\n# Doc\n")
        self.assertEqual(authority.status, CANONICAL_STATUS_DRAFT)
        self.assertFalse(authority.authoritative)
        self.assertTrue(authority.selectable)

    def test_unknown_status_fails_safe(self):
        for raw in ("approved", "ACTIVE-ish", "canonical", "42", "yes"):
            with self.subTest(raw=raw):
                authority = read_canonical_authority(f"---\nstatus: {raw}\n---\n# Doc\n")
                self.assertEqual(authority.status, CANONICAL_STATUS_UNKNOWN)
                self.assertFalse(authority.authoritative)
                self.assertTrue(authority.selectable)

    def test_status_is_case_and_quote_insensitive(self):
        for raw in ("Active", "ACTIVE", '"active"', "'active'", "  active  "):
            with self.subTest(raw=raw):
                self.assertTrue(
                    read_canonical_authority(f"---\nstatus: {raw}\n---\n").authoritative
                )

    def test_superseded_by_is_captured(self):
        authority = read_canonical_authority(
            "---\nstatus: superseded\nsuperseded_by: canonical/New Truth.md\n---\n"
        )
        self.assertEqual(authority.superseded_by, "canonical/New Truth.md")

    def test_malformed_frontmatter_never_raises(self):
        for content in (
            "",
            "---\n",
            "---\nstatus:\n---\n",
            "---\n\tstatus: active\n",
            "---\nstatus: active",          # unterminated block
            "not frontmatter\n---\nstatus: active\n---\n",
            "---\n" + "x" * 10000 + "\n---\n",
        ):
            with self.subTest(content=content[:24]):
                authority = read_canonical_authority(content)
                self.assertFalse(authority.authoritative)

    def test_status_outside_frontmatter_confers_nothing(self):
        # A body line must never be mistaken for a declaration.
        authority = read_canonical_authority("# Doc\n\nstatus: active\n")
        self.assertFalse(authority.authoritative)
        self.assertEqual(authority.status, CANONICAL_STATUS_UNSPECIFIED)


class TestTargetedRecallAuthority(CanonicalAuthorityTestCase):
    """How the contract changes what targeted recall returns."""

    BODY = "# Ops Doc\nTwoBerries Meta Ads catalog campaign operations.\n"

    def test_1_no_status_gets_no_bonus_and_is_not_authoritative(self):
        self._canonical("Legacy Ops", self.BODY)
        res = targeted_recall(self.config, "TwoBerries Meta Ads catalog")
        hit = self._result_for(res["results"], "Legacy Ops")
        self.assertIsNotNone(hit, "an unspecified doc is still retrievable")
        self.assertNotIn("Canonical: ", hit["title"])
        self.assertIn("non-authoritative", hit["title"])
        self.assertIn(CANONICAL_STATUS_UNSPECIFIED, hit["title"])

    def test_2_active_earns_the_authority_bonus(self):
        self._canonical("Active Ops", self.BODY, status="active")
        active = targeted_recall(self.config, "TwoBerries Meta Ads catalog")
        active_hit = self._result_for(active["results"], "Active Ops")
        self.assertIsNotNone(active_hit)
        self.assertEqual(active_hit["title"], "Canonical: Active Ops")

        # Same bytes, no declaration -> exactly CANONICAL_AUTHORITY_BONUS lower.
        (self.vault / "canonical" / "Active Ops.md").unlink()
        self._canonical("Active Ops", self.BODY)
        plain = targeted_recall(self.config, "TwoBerries Meta Ads catalog")
        plain_hit = self._result_for(plain["results"], "Active Ops")
        self.assertAlmostEqual(
            active_hit["score"] - plain_hit["score"], CANONICAL_AUTHORITY_BONUS, places=4
        )

    def test_3_superseded_is_withheld_from_default_recall(self):
        self._canonical("Old Ops", self.BODY, status="superseded")
        res = targeted_recall(self.config, "TwoBerries Meta Ads catalog")
        self.assertIsNone(self._result_for(res["results"], "Old Ops"))

        # Still reachable for a deliberate historical lookup -- and still
        # non-authoritative when it comes back.
        opened = targeted_recall(
            self.config, "TwoBerries Meta Ads catalog", include_superseded=True
        )
        hit = self._result_for(opened["results"], "Old Ops")
        self.assertIsNotNone(hit)
        self.assertIn("non-authoritative", hit["title"])

    def test_3b_superseded_by_pointer_is_surfaced(self):
        self._canonical(
            "Old Ops", self.BODY, status="superseded",
            superseded_by="canonical/New Ops.md",
        )
        opened = targeted_recall(
            self.config, "TwoBerries Meta Ads catalog", include_superseded=True
        )
        self.assertIn("[SUPERSEDED BY: canonical/New Ops.md]", opened["markdown"])

    def test_4_draft_is_derived_and_non_authoritative(self):
        self._canonical("Draft Ops", self.BODY, status="draft")
        res = targeted_recall(self.config, "TwoBerries Meta Ads catalog")
        hit = self._result_for(res["results"], "Draft Ops")
        self.assertIsNotNone(hit)
        self.assertIn(CANONICAL_STATUS_DRAFT, hit["title"])
        self.assertIn("non-authoritative", hit["title"])

    def test_5_unknown_status_fails_safe_in_recall(self):
        self._canonical("Weird Ops", self.BODY, status="approved-canonical")
        res = targeted_recall(self.config, "TwoBerries Meta Ads catalog")
        hit = self._result_for(res["results"], "Weird Ops")
        self.assertIsNotNone(hit, "recall stays fail-open on unrecognised metadata")
        self.assertIn(CANONICAL_STATUS_UNKNOWN, hit["title"])
        self.assertIn("non-authoritative", hit["title"])

    def test_6_active_canonical_outranks_derived_knowledge_on_equal_text(self):
        shared = "TwoBerries Meta Ads catalog campaign operations.\n"
        self._canonical("Active Ops", f"# Ops\n{shared}", status="active")
        (self.vault / "knowledge" / "concepts" / "twoberries-catalog.md").write_text(
            f"# TwoBerries Catalog\n{shared}", encoding="utf-8"
        )
        res = targeted_recall(self.config, "TwoBerries Meta Ads catalog campaign")
        sources = [r["source"] for r in res["results"]]
        self.assertTrue(sources, "expected at least one match")
        self.assertTrue(
            sources[0].startswith("canonical/"),
            f"an active canonical doc should lead, got {sources}",
        )

    def test_7_stale_canonical_cannot_lead_on_folder_alone(self):
        """The regression this fix exists for.

        In production the legacy brand doc scored 6.0 against 4.0 for live
        concepts -- and that entire 2.0 gap was the folder bonus.  Here both
        documents carry the same body, so any surviving gap would be folder
        precedence.  It must be exactly zero, and declaring ``status: active``
        must be the only thing that opens one.
        """
        body = "TwoBerries Meta Ads catalog campaign operations.\n"
        (self.vault / "knowledge" / "concepts" / "twoberries-catalog.md").write_text(
            f"# TwoBerries Catalog\n{body}", encoding="utf-8"
        )
        query = "TwoBerries Meta Ads catalog campaign"

        def gap() -> float:
            scores = {r["source"]: r["score"] for r in targeted_recall(self.config, query)["results"]}
            canon = next(k for k in scores if k.startswith("canonical/"))
            concept = next(k for k in scores if k.startswith("knowledge/concepts/"))
            return scores[canon] - scores[concept]

        self._canonical("Legacy Agency Operating Context", f"# Legacy\n{body}")
        self.assertEqual(gap(), 0.0, "an undeclared canonical doc still carries a folder bonus")

        (self.vault / "canonical" / "Legacy Agency Operating Context.md").unlink()
        self._canonical(
            "Legacy Agency Operating Context", f"# Legacy\n{body}", status="active"
        )
        self.assertAlmostEqual(gap(), CANONICAL_AUTHORITY_BONUS, places=4)
        self.assertTrue(
            targeted_recall(self.config, query)["results"][0]["source"].startswith("canonical/"),
            "a declared-active canonical doc should lead",
        )

    def test_7b_ranking_does_not_depend_on_scan_order(self):
        """Equal scores must resolve by declared authority, then a stable key."""
        body = "TwoBerries Meta Ads catalog campaign operations.\n"
        (self.vault / "knowledge" / "concepts" / "twoberries-catalog.md").write_text(
            f"# TwoBerries Catalog\n{body}", encoding="utf-8"
        )
        self._canonical("Legacy Ops", f"# Legacy\n{body}")
        query = "TwoBerries Meta Ads catalog campaign"

        first = [r["source"] for r in targeted_recall(self.config, query)["results"]]
        for _ in range(4):
            self.assertEqual(
                [r["source"] for r in targeted_recall(self.config, query)["results"]],
                first,
                "targeted recall ordering is not reproducible",
            )

    def test_8_labels_follow_authority_not_position(self):
        self._canonical("Active Ops", "# Active\nTwoBerries catalog truth.\n", status="active")
        self._canonical("Draft Ops", "# Draft\nTwoBerries catalog draft.\n", status="draft")
        (self.vault / "knowledge" / "concepts" / "twoberries.md").write_text(
            "# TwoBerries\nTwoBerries catalog concept.\n", encoding="utf-8"
        )
        md = targeted_recall(self.config, "TwoBerries catalog")["markdown"]
        for line in md.splitlines():
            if not line.startswith("### ["):
                continue
            if "Canonical: Active Ops" in line:
                self.assertIn(AUTHORITY_LABEL, line)
                self.assertNotIn(DERIVED_LABEL, line)
            else:
                self.assertIn(DERIVED_LABEL, line)
                self.assertNotIn(AUTHORITY_LABEL, line)


class TestStartupIdentityFallbackAuthority(CanonicalAuthorityTestCase):
    """Tier A identity must also be earned, not inherited."""

    REL = "Pikselzone Agency Operating Context"

    def _bundle(self) -> str:
        return build_startup_recall_bundle(self.config, runtime="claude").text

    def test_undeclared_canonical_does_not_anchor_identity(self):
        self._canonical(self.REL, "# Ops\nAgency operating context body.\n")
        self.assertNotIn("1. Identity & Operating Context", self._bundle())

    def test_active_canonical_anchors_identity(self):
        self._canonical(self.REL, "# Ops\nAgency operating context body.\n", status="active")
        self.assertIn("1. Identity & Operating Context", self._bundle())

    def test_superseded_canonical_does_not_anchor_identity(self):
        self._canonical(self.REL, "# Ops\nAgency operating context body.\n", status="superseded")
        self.assertNotIn("1. Identity & Operating Context", self._bundle())

    def test_companion_core_still_wins_over_canonical(self):
        (self.vault / "companion" / "Core.md").write_text(
            "# Core\nSecond brain identity.\n", encoding="utf-8"
        )
        self._canonical(self.REL, "# Ops\nAgency operating context body.\n", status="active")
        bundle = self._bundle()
        self.assertIn("1. Identity & Operating Context", bundle)
        self.assertIn("Second brain identity.", bundle)
        self.assertNotIn("Agency operating context body.", bundle)


class TestSecurityAndRegression(CanonicalAuthorityTestCase):
    """The contract must not weaken existing guarantees."""

    def test_9a_symlinked_canonical_is_still_rejected(self):
        outside = self.root / "outside.md"
        outside.write_text("---\nstatus: active\n---\n# Outside\nTwoBerries secret.\n",
                           encoding="utf-8")
        link = self.vault / "canonical" / "Linked.md"
        try:
            link.symlink_to(outside)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unavailable on this platform")
        res = targeted_recall(self.config, "TwoBerries secret")
        self.assertIsNone(self._result_for(res["results"], "Linked"))

    def test_9b_traversal_shaped_superseded_by_is_inert(self):
        # The pointer is recorded and shown; it is never resolved or read.
        self._canonical(
            "Old Ops",
            "# Old\nTwoBerries catalog.\n",
            status="superseded",
            superseded_by="../../../../etc/passwd",
        )
        res = targeted_recall(self.config, "TwoBerries catalog", include_superseded=True)
        self.assertIn("[SUPERSEDED BY: ../../../../etc/passwd]", res["markdown"])
        self.assertNotIn("root:", res["markdown"])

    def test_9c_associative_recall_ignores_canonical_entirely(self):
        self._canonical(
            "Active Ops",
            "# Ops\nTwoBerries Meta Ads catalog campaign operations detail.\n",
            status="active",
        )
        injected = associative_recall_fast(
            self.config, "twoberries meta ads catalog campaign operations"
        )
        self.assertNotIn("canonical/", injected)

    def test_9d_project_scoped_continuity_is_unaffected(self):
        (self.vault / "continuity").mkdir(parents=True, exist_ok=True)
        (self.vault / "continuity" / "twoberries.md").write_text(
            "# Last Session\nTwoBerries catalog feed work in progress.\n", encoding="utf-8"
        )
        self._canonical("Active Ops", "# Ops\nTwoBerries catalog.\n", status="active")
        bundle = build_startup_recall_bundle(
            self.config, runtime="claude", continuity_scope="twoberries"
        )
        self.assertIn("TwoBerries catalog feed work in progress.", bundle.text)

    def test_9e_empty_canonical_folder_is_harmless(self):
        (self.vault / "knowledge" / "concepts" / "twoberries.md").write_text(
            "# TwoBerries\nTwoBerries catalog concept.\n", encoding="utf-8"
        )
        res = targeted_recall(self.config, "TwoBerries catalog")
        self.assertGreaterEqual(res["items_count"], 1)


if __name__ == "__main__":
    unittest.main()

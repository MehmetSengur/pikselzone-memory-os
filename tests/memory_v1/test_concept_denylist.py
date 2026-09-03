"""§7: code-enforced generic bare-concept denylist across the write paths."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from memory_v1.compiler import TerraCompiler
from memory_v1.core import BARE_CONCEPT_DENYLIST, MemoryConfig, PolicyError
from memory_v1.graph_engine import ConceptData, KnowledgeGraphEngine


def _concept_md(title: str) -> str:
    return (
        f'---\ntitle: "{title}"\naliases: []\ntags: ["#concept"]\n'
        f'created: "2026-09-03"\nupdated: "2026-09-03"\nsources: ["x:1"]\n---\n\n'
        f"# {title}\n\n## Özet\nbir sey.\n"
    )


class ConceptDenylistTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="pz-test-denylist-")
        self.root = Path(self._tmp.name).resolve()
        self.vault = self.root / "vault"
        self.vault.mkdir()
        self.engine = KnowledgeGraphEngine(self.vault)
        self.engine.ensure_graph_dirs()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    # --- graph engine (session-flush growth path) -----------------------
    def test_bare_generic_titles_are_rejected(self) -> None:
        for title in ("PASS", "api", "App", "Test", "error", "Status", "Done"):
            with self.assertRaises(PolicyError, msg=title):
                self.engine.add_or_update_concept(
                    ConceptData(title=title, summary="s", details=[], sources=["x:1"])
                )
            self.assertFalse((self.vault / "knowledge" / "concepts" / f"{title.lower()}.md").exists())

    def test_real_short_and_compound_names_are_accepted(self) -> None:
        for title in ("GA4", "CAPI", "PayTR", "Moka", "Shopify", "Trendyol-API", "Chronos-Gate"):
            path = self.engine.add_or_update_concept(
                ConceptData(title=title, summary="s", details=[], sources=["x:1"])
            )
            self.assertTrue(path.is_file(), title)

    def test_denylist_is_exact_match_not_substring(self) -> None:
        # "api" is denied; "trendyol-api" contains it but must pass.
        self.assertIn("api", BARE_CONCEPT_DENYLIST)
        self.assertNotIn("trendyol-api", BARE_CONCEPT_DENYLIST)

    # --- compiler proposal validation ---------------------------------
    def test_compiler_rejects_generic_concept_write(self) -> None:
        proposal = {
            "status": "changes",
            "writes": [{"path": "knowledge/concepts/pass.md", "content": _concept_md("PASS")}],
        }
        with self.assertRaises(PolicyError):
            TerraCompiler._validate_proposal(proposal)

    def test_compiler_accepts_specific_concept_write(self) -> None:
        proposal = {
            "status": "changes",
            "writes": [{"path": "knowledge/concepts/ga4.md", "content": _concept_md("GA4")}],
        }
        out = TerraCompiler._validate_proposal(proposal)
        self.assertEqual(out["writes"][0]["path"], "knowledge/concepts/ga4.md")

    # --- host-side promoter integrity gate ----------------------------
    def test_promoter_rejects_generic_candidate_concept(self) -> None:
        from memory_v1.knowledge_promoter import _validate_graph_candidate_integrity
        cfg = MemoryConfig.from_dict({
            "role": "workstation",
            "vault_path": str(self.vault),
            "state_path": str(self.root / "state"),
            "runtimes": ["codex", "claude"],
            "transcript_roots": {"codex": [str(self.root)], "claude": [str(self.root)]},
            "can_write_event_memory": True,
            "can_run_compiler": False,
            "provider": {"mode": "runtime-native"},
        })
        payloads = [("knowledge/concepts/error.md", Path("x"), _concept_md("error").encode())]
        with self.assertRaises(PolicyError):
            _validate_graph_candidate_integrity(cfg, payloads)


if __name__ == "__main__":
    unittest.main()

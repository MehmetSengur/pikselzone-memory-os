"""Unit tests for Knowledge Graph Auto-Growth, Reconciliation and Bidirectional Wikilinks (SB2-05)."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from memory_v1.core import PolicyError
from memory_v1.graph_engine import (
    ConceptData, KnowledgeGraphEngine, is_conflicted_copy_path,
    parse_frontmatter_aliases, slugify,
)


class TestKnowledgeGraphEngine(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.vault = Path(self.temp_dir.name).resolve()
        self.engine = KnowledgeGraphEngine(self.vault)
        self.engine.ensure_graph_dirs()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def write_concept(self, slug: str, title: str, aliases: str = "[]") -> Path:
        path = self.engine.concepts_dir / f"{slug}.md"
        path.write_text(
            f"---\ntitle: \"{title}\"\naliases: {aliases}\n---\n\n# {title}\n",
            encoding="utf-8",
        )
        return path

    # 1. Concept creation with frontmatter and wikilinks
    def test_create_concept(self):
        self.engine.add_or_update_concept(ConceptData(title="Redis", summary="Cache"))
        self.engine.add_or_update_concept(ConceptData(title="Meta Ads", summary="Ads"))
        data = ConceptData(
            title="TwoBerries CAPI",
            summary="Meta Conversion API entegrasyon servisi.",
            details=["FastAPI tabanlı mikroservis", "Redis kuyruğu kullanıyor"],
            aliases=["CAPI", "Conversion API"],
            tags=["#capi", "#marketing"],
            sources=["session-1"],
            related_concepts=["Redis", "Meta Ads"],
        )
        c_path = self.engine.add_or_update_concept(data)
        self.assertTrue(c_path.is_file())
        self.assertEqual(c_path.name, "twoberries-capi.md")

        content = c_path.read_text(encoding="utf-8")
        self.assertIn("title: \"TwoBerries CAPI\"", content)
        self.assertIn("[[concepts/redis|Redis]]", content)
        self.assertIn("[[concepts/meta-ads|Meta Ads]]", content)

        # Verify index.md updated
        index_content = self.engine.index_file.read_text(encoding="utf-8")
        self.assertIn("TwoBerries CAPI", index_content)

        # Verify log.md updated
        log_content = self.engine.log_file.read_text(encoding="utf-8")
        self.assertIn("CREATE_CONCEPT", log_content)

    # 2. In-place expansion without creating duplicate files
    def test_expand_existing_concept(self):
        data_1 = ConceptData(
            title="PostgreSQL",
            summary="İlişkisel veritabanı yönetim sistemi.",
            details=["Versiyon 16 kullanılıyor"],
            sources=["session-1"],
        )
        self.engine.add_or_update_concept(data_1)
        
        # Count concepts in directory
        initial_count = len(list(self.engine.concepts_dir.glob("*.md")))
        self.assertEqual(initial_count, 1)

        # Update with new detail
        data_2 = ConceptData(
            title="PostgreSQL",
            summary="İlişkisel veritabanı yönetim sistemi.",
            details=["Connection pooling için PgBouncer devrede"],
            sources=["session-2"],
        )
        self.engine.add_or_update_concept(data_2)

        # Directory must still have only 1 concept file (no duplicates)
        new_count = len(list(self.engine.concepts_dir.glob("*.md")))
        self.assertEqual(new_count, 1)

        updated_content = (self.engine.concepts_dir / "postgresql.md").read_text(encoding="utf-8")
        self.assertIn("PgBouncer devrede", updated_content)

    # 3. In-place contradiction reconciliation
    def test_reconciliation_in_concept(self):
        data_1 = ConceptData(
            title="CAPI Timeout",
            summary="CAPI istek zaman aşımı ayarı.",
            details=["Timeout değeri 30 saniye olarak belirlendi"],
            sources=["session-1"],
        )
        self.engine.add_or_update_concept(data_1)

        # New information contradicts old timeout
        data_2 = ConceptData(
            title="CAPI Timeout",
            summary="CAPI istek zaman aşımı ayarı.",
            details=["Timeout değeri 5 saniyeye düşürüldü"],
            contradictions=["Eski 30 saniye değeri yüksek gecikmeye neden olduğu için 5 saniye ile değiştirildi."],
            sources=["session-2"],
        )
        self.engine.add_or_update_concept(data_2)

        content = (self.engine.concepts_dir / "capi-timeout.md").read_text(encoding="utf-8")
        self.assertIn("Çelişki Çözümü", content)
        self.assertIn("5 saniye ile değiştirildi", content)

    # 4. Bidirectional connections with canonical naming
    def test_bidirectional_connections(self):
        # Create two concepts first
        self.engine.add_or_update_concept(ConceptData(title="Redis", summary="In-memory cache"))
        self.engine.add_or_update_concept(ConceptData(title="FastAPI", summary="Web framework"))

        # Connect them: "Redis" and "FastAPI" -> sorted slug: "fastapi--redis.md"
        conn_path = self.engine.add_or_update_connection(
            concept_a="Redis",
            concept_b="FastAPI",
            relationship="FastAPI oturum durumlarını Redis üzerinde saklar.",
            evidence=["Session 4"],
        )
        self.assertEqual(conn_path.name, "fastapi--redis.md")

        # Verify cross-linking in concept files
        fastapi_content = (self.engine.concepts_dir / "fastapi.md").read_text(encoding="utf-8")
        redis_content = (self.engine.concepts_dir / "redis.md").read_text(encoding="utf-8")
        self.assertIn("[[connections/fastapi--redis]]", fastapi_content)
        self.assertIn("[[connections/fastapi--redis]]", redis_content)

        # Opposite call must resolve to the same canonical connection file
        conn_path_opposite = self.engine.add_or_update_connection(
            concept_a="FastAPI",
            concept_b="Redis",
            relationship="FastAPI oturum durumlarını Redis üzerinde saklar.",
        )
        self.assertEqual(conn_path_opposite.name, "fastapi--redis.md")

    # 5. Graph growth from session summary
    def test_grow_from_session_summary(self):
        summary = {
            "decisions": ["TwoBerries CAPI ve Meta Webhook entegrasyonu tamamlandı."],
            "learnings": ["Meta CAPI sunucu tarafı olay doğrulaması gerektiriyor."],
            "important_conversations": [],
        }
        count = self.engine.grow_from_session_summary(summary, session_id="sess-test-summary")
        self.assertGreater(count, 0)
        
        # Verify concepts exist
        concepts = list(self.engine.concepts_dir.glob("*.md"))
        self.assertGreaterEqual(len(concepts), 1)

    def test_find_concept_resolves_canonical_slug_title_and_alias(self):
        path = self.write_concept(
            "runtime-native-subscription-memory",
            "Runtime-Native Subscription Memory",
            '["Native Subscription Architecture", "Subscription-Backed Memory V1"]',
        )
        self.assertEqual(path, self.engine.find_concept("runtime-native-subscription-memory"))
        self.assertEqual(path, self.engine.find_concept("Runtime-Native Subscription Memory"))
        self.assertEqual(path, self.engine.find_concept("Native Subscription Architecture"))
        self.assertEqual(path, self.engine.find_concept("subscription backed memory v1"))

    def test_alias_parser_supports_inline_block_quotes_and_duplicates(self):
        inline = "---\naliases: [Runtime Native Memory, 'Subscription Memory']\n---\n"
        block = "---\naliases:\n  - Runtime Native Memory\n  - \"Subscription Memory\"\n  - Runtime Native Memory\n---\n"
        self.assertEqual(
            ["Runtime Native Memory", "Subscription Memory"],
            parse_frontmatter_aliases(inline),
        )
        self.assertEqual(parse_frontmatter_aliases(inline), parse_frontmatter_aliases(block))

    def test_alias_parser_ignores_malformed_block(self):
        malformed = "---\naliases:\n  -\nnext: value\n---\n"
        self.assertEqual([], parse_frontmatter_aliases(malformed))

    def test_ambiguous_alias_fails_without_selecting_a_target(self):
        self.write_concept("first", "First", '["Shared Alias"]')
        self.write_concept("second", "Second", '["Shared Alias"]')
        with self.assertRaisesRegex(PolicyError, "ambiguous-concept-alias"):
            self.engine.find_concept("Shared Alias")

    def test_alias_input_reuses_existing_concept(self):
        existing = self.write_concept("runtime-native", "Runtime Native", '["Subscription Memory"]')
        result = self.engine.add_or_update_concept(
            ConceptData(title="Subscription Memory", summary="Updated summary", details=["New detail"])
        )
        self.assertEqual(existing, result)
        self.assertEqual(1, len(list(self.engine.concepts_dir.glob("*.md"))))
        self.assertIn("New detail", existing.read_text(encoding="utf-8"))

    def test_connection_resolves_title_and_alias_to_explicit_canonical_endpoints(self):
        first = self.write_concept("runtime-native", "Runtime Native", '["Subscription Memory"]')
        second = self.write_concept("automatic-drain-closure", "Automatic Drain Closure")
        connection = self.engine.add_or_update_connection(
            "Subscription Memory", "Automatic Drain Closure", "Depends on", ["fixture"],
        )
        self.assertEqual("automatic-drain-closure--runtime-native.md", connection.name)
        content = connection.read_text(encoding="utf-8")
        self.assertIn(f"[[concepts/{first.stem}|Runtime Native]]", content)
        self.assertIn(f"[[concepts/{second.stem}|Automatic Drain Closure]]", content)

    def test_connection_rejects_missing_self_and_reverse_duplicate(self):
        self.engine.add_or_update_concept(ConceptData(title="Alpha", summary="A"))
        self.engine.add_or_update_concept(ConceptData(title="Beta", summary="B"))
        with self.assertRaisesRegex(PolicyError, "connection-endpoint-not-found"):
            self.engine.add_or_update_connection("Alpha", "Missing", "Broken")
        with self.assertRaisesRegex(PolicyError, "cannot-connect-concept-to-itself"):
            self.engine.add_or_update_connection("Alpha", "Alpha", "Self")
        first = self.engine.add_or_update_connection("Alpha", "Beta", "Related")
        second = self.engine.add_or_update_connection("Beta", "Alpha", "Related")
        self.assertEqual(first, second)
        self.assertEqual(1, len(list(self.engine.connections_dir.glob("*.md"))))

    def test_related_concept_links_skip_missing_targets(self):
        created = self.engine.add_or_update_concept(
            ConceptData(title="Alpha", summary="A", related_concepts=["Missing"])
        )
        self.assertNotIn("[[concepts/missing", created.read_text(encoding="utf-8"))

    def test_conflicted_copy_pattern_is_narrow(self):
        self.assertTrue(is_conflicted_copy_path(Path("index (Conflicted copy pz-hermes 202608301608).md")))
        self.assertFalse(is_conflicted_copy_path(Path("important conflicted copy.md")))


if __name__ == "__main__":
    unittest.main()

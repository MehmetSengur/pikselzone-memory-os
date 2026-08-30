"""Knowledge Graph Auto-Growth, Reconciliation & Bidirectional Wikilink Engine (SB2-05).

Provides graph-aware lifecycle management for knowledge/:
- concepts/<slug>.md
- connections/<slug_a>--<slug_b>.md
- index.md
- log.md

Invariants:
1. True bidirectional graph linking with [[wikilinks]].
2. Canonical connection naming: sorted(slug_a, slug_b) prevents duplicate opposing files.
3. In-place expansion & contradiction reconciliation over duplicate file fragmentation.
4. Deterministic table maintenance in index.md and audit trail in log.md.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import csv
import logging
import re
import unicodedata
from pathlib import Path
from typing import Any, List, Optional, Set, Tuple

from .core import (
    MemoryConfig, PolicyError, atomic_write, iso_now,
    redact_sensitive_text, reject_symlink_chain, secure_read_text,
)

logger = logging.getLogger("memory_v1.graph_engine")


# This is deliberately limited to the conflict-copy filename format observed in
# the shared vault.  Do not turn this into a broad "conflict" match: ordinary
# user notes are valid graph material until there is evidence otherwise.
CONFLICTED_COPY_RE = re.compile(
    r"\s+\(Conflicted copy pz-hermes \d{12}\)\.md$", re.IGNORECASE
)


def is_conflicted_copy_path(path: Path) -> bool:
    """Return whether a file is an observed Obsidian sync conflict copy."""
    return bool(CONFLICTED_COPY_RE.search(path.name))


def slugify(text: str) -> str:
    """Generate safe lowercase ASCII/alphanumeric slug."""
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^\w\s-]", "", text.lower())
    slug = re.sub(r"[-\s]+", "-", text).strip("-_")
    return slug or "untitled-concept"


def _strip_optional_quotes(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1].strip()
    return value


def _frontmatter_lines(content: str) -> list[str]:
    """Return bounded YAML-like frontmatter lines without requiring PyYAML."""
    lines = content.splitlines()
    if not lines or lines[0].strip() != "---":
        return []
    for index, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            return lines[1:index]
    return []


def parse_frontmatter_aliases(content: str) -> list[str]:
    """Parse supported inline and block-style ``aliases`` frontmatter safely.

    This intentionally understands only the small subset written by the graph
    engine.  Invalid input is ignored so a malformed note cannot interrupt the
    compiler's degraded-first lifecycle.
    """
    lines = _frontmatter_lines(content)
    values: list[str] = []
    for index, line in enumerate(lines):
        match = re.match(r"^aliases\s*:\s*(.*)$", line)
        if not match:
            continue
        remainder = match.group(1).strip()
        if remainder.startswith("["):
            if not remainder.endswith("]"):
                return []
            try:
                parsed = next(csv.reader([remainder[1:-1]], skipinitialspace=True))
            except (csv.Error, StopIteration):
                return []
            values.extend(_strip_optional_quotes(item) for item in parsed)
        elif not remainder:
            for following in lines[index + 1:]:
                if re.match(r"^\S", following):
                    break
                item = re.match(r"^\s+-\s+(.+?)\s*$", following)
                if item:
                    values.append(_strip_optional_quotes(item.group(1)))
        else:
            return []
        break

    unique: list[str] = []
    seen: set[str] = set()
    for value in values:
        normalized = slugify(value)
        if value and normalized not in seen:
            seen.add(normalized)
            unique.append(value)
    return unique


def _frontmatter_title(content: str) -> Optional[str]:
    for line in _frontmatter_lines(content):
        match = re.match(r"^title\s*:\s*(.+?)\s*$", line)
        if match:
            return _strip_optional_quotes(match.group(1))
    title_match = re.search(r"^#\s+(.+?)\s*$", content, re.MULTILINE)
    return title_match.group(1).strip() if title_match else None


@dataclasses.dataclass
class ConceptData:
    title: str
    summary: str
    details: list[str] = dataclasses.field(default_factory=list)
    aliases: list[str] = dataclasses.field(default_factory=list)
    tags: list[str] = dataclasses.field(default_factory=list)
    sources: list[str] = dataclasses.field(default_factory=list)
    related_concepts: list[str] = dataclasses.field(default_factory=list)
    contradictions: list[str] = dataclasses.field(default_factory=list)


class KnowledgeGraphEngine:
    """Manages the living knowledge graph inside Obsidian vault's knowledge/ directory."""

    def __init__(self, vault_path: Path) -> None:
        self.vault_path = vault_path.resolve()
        self.knowledge_dir = self.vault_path / "knowledge"
        self.concepts_dir = self.knowledge_dir / "concepts"
        self.connections_dir = self.knowledge_dir / "connections"
        self.index_file = self.knowledge_dir / "index.md"
        self.log_file = self.knowledge_dir / "log.md"

    def ensure_graph_dirs(self) -> None:
        """Ensure all required directories and index/log anchors exist."""
        self.concepts_dir.mkdir(parents=True, exist_ok=True)
        self.connections_dir.mkdir(parents=True, exist_ok=True)

        if not self.index_file.is_file():
            initial_index = (
                "# Knowledge Base Index\n\n"
                "Living concept and connection index for Pikselzone Second Brain.\n\n"
                "| Article | Summary | Source | Updated |\n"
                "|---|---|---|---|\n"
            )
            atomic_write(self.index_file, initial_index, mode=0o660)

        if not self.log_file.is_file():
            initial_log = (
                "# Knowledge Mutation Log\n\n"
                "Chronological record of second-brain knowledge growth and reconciliation.\n\n"
            )
            atomic_write(self.log_file, initial_log, mode=0o660)

    def find_concept(self, title_or_alias: str) -> Optional[Path]:
        """Resolve a concept in deterministic, safe precedence order.

        A match is never selected arbitrarily.  Ambiguous title, alias, or
        normalized-slug input raises ``PolicyError`` so callers can degrade or
        fail explicitly rather than linking an unrelated concept.
        """
        raw = title_or_alias.strip().removeprefix("[[").removesuffix("]]")
        raw = raw.split("|", 1)[0].strip().removeprefix("concepts/")
        raw = raw.removesuffix(".md")
        target_slug = slugify(raw)

        # 1. Exact canonical slug/path.
        normalized_file = self.concepts_dir / f"{target_slug}.md"
        if normalized_file.is_file() and not is_conflicted_copy_path(normalized_file):
            return normalized_file

        records: list[tuple[Path, str | None, list[str]]] = []
        for path in sorted(self.concepts_dir.glob("*.md")):
            if is_conflicted_copy_path(path):
                continue
            try:
                content = path.read_text(encoding="utf-8")
            except OSError:
                continue
            records.append((path, _frontmatter_title(content), parse_frontmatter_aliases(content)))

        def unique_match(paths: list[Path], match_kind: str) -> Optional[Path]:
            unique = sorted(set(paths))
            if len(unique) > 1:
                raise PolicyError(f"ambiguous-concept-{match_kind}:{target_slug}")
            return unique[0] if unique else None

        # 2. Title (frontmatter title preferred, then H1 fallback).
        title_match = unique_match(
            [path for path, title, _ in records if title and title == title_or_alias.strip()],
            "title",
        )
        if title_match:
            return title_match

        # 3. Alias.
        alias_match = unique_match(
            [path for path, _, aliases in records if title_or_alias.strip() in aliases],
            "alias",
        )
        if alias_match:
            return alias_match

        # 4. Safe normalized fallback for spacing/case/hyphen variants.
        normalized_match = unique_match(
            [
                path for path, title, aliases in records
                if (title and slugify(title) == target_slug)
                or any(slugify(alias) == target_slug for alias in aliases)
                or path.stem == target_slug
            ],
            "normalized-slug",
        )
        return normalized_match

    def _canonical_concept_display(self, path: Path) -> str:
        """Use the concept's declared title when rendering a canonical link."""
        try:
            title = _frontmatter_title(path.read_text(encoding="utf-8"))
        except OSError:
            title = None
        return title or path.stem.replace("-", " ").title()

    def _resolve_related_concept_links(self, values: list[str]) -> list[tuple[Path, str]]:
        """Return existing targets only; unresolved related concepts are skipped."""
        resolved: list[tuple[Path, str]] = []
        seen: set[Path] = set()
        for value in values:
            try:
                path = self.find_concept(value)
            except PolicyError as exc:
                logger.warning("Skipping ambiguous related concept %r: %s", value, exc)
                continue
            if path and path not in seen:
                seen.add(path)
                resolved.append((path, self._canonical_concept_display(path)))
        return resolved

    def add_or_update_concept(self, data: ConceptData) -> Path:
        """Create a new concept or expand an existing one in-place, updating wikilinks and index."""
        self.ensure_graph_dirs()
        clean_summary, _ = redact_sensitive_text(data.summary)
        clean_title, _ = redact_sensitive_text(data.title)

        existing_path = self.find_concept(clean_title)
        today_str = dt.date.today().isoformat()
        now_str = iso_now()

        if existing_path and existing_path.is_file():
            # In-place update / expansion
            target_path = existing_path
            old_content = target_path.read_text(encoding="utf-8")

            # Check if there are contradictions/conflicts to reconcile
            contradiction_block = ""
            if data.contradictions:
                contradiction_block = f"\n\n### Çelişki Çözümü & Güncelleme ({today_str})\n"
                for item in data.contradictions:
                    clean_item, _ = redact_sensitive_text(item)
                    contradiction_block += f"- {clean_item}\n"

            # Check details expansion
            new_details = []
            for d in data.details:
                clean_d, _ = redact_sensitive_text(d)
                if clean_d not in old_content:
                    new_details.append(f"- {clean_d}")

            details_block = "\n" + "\n".join(new_details) if new_details else ""

            # Check new related concepts for wikilinks
            new_wikilinks = []
            for rc_path, rc_display in self._resolve_related_concept_links(data.related_concepts):
                wlink = f"[[concepts/{rc_path.stem}|{rc_display}]]"
                if wlink not in old_content:
                    new_wikilinks.append(f"- {wlink}")
            wikilinks_block = "\n" + "\n".join(new_wikilinks) if new_wikilinks else ""

            # Append updates under appropriate sections
            updated_content = old_content
            if "## Detaylar & Bulgular" in updated_content and details_block:
                updated_content = updated_content.replace(
                    "## Detaylar & Bulgular",
                    f"## Detaylar & Bulgular{details_block}",
                )
            elif details_block:
                updated_content += f"\n\n## Detaylar & Bulgular{details_block}"

            if contradiction_block:
                updated_content += contradiction_block

            if "## İlgili Bağlantılar" in updated_content and wikilinks_block:
                updated_content = updated_content.replace(
                    "## İlgili Bağlantılar",
                    f"## İlgili Bağlantılar{wikilinks_block}",
                )
            elif wikilinks_block:
                updated_content += f"\n\n## İlgili Bağlantılar{wikilinks_block}"

            # Update timestamp in frontmatter
            updated_content = re.sub(
                r'updated:\s*"[^"]*"',
                f'updated: "{today_str}"',
                updated_content,
            )

            atomic_write(target_path, updated_content, mode=0o660)
            self._log_mutation("UPDATE_CONCEPT", f"Expanded concept '{clean_title}' in {target_path.name}")
        else:
            # Create fresh concept file
            slug = slugify(clean_title)
            target_path = self.concepts_dir / f"{slug}.md"

            aliases_fmt = ", ".join(f'"{a}"' for a in data.aliases)
            tags_list = list(set(data.tags + ["#concept"]))
            tags_fmt = ", ".join(f'"{t}"' for t in tags_list)
            sources_fmt = ", ".join(f'"{s}"' for s in data.sources)

            wikilinks_lines = "\n".join(
                f"- [[concepts/{path.stem}|{display}]]"
                for path, display in self._resolve_related_concept_links(data.related_concepts)
            )
            details_lines = "\n".join(f"- {d}" for d in data.details)

            content = (
                f"---\n"
                f'title: "{clean_title}"\n'
                f"aliases: [{aliases_fmt}]\n"
                f"tags: [{tags_fmt}]\n"
                f'created: "{today_str}"\n'
                f'updated: "{today_str}"\n'
                f"sources: [{sources_fmt}]\n"
                f"---\n\n"
                f"# {clean_title}\n\n"
                f"## Özet\n{clean_summary}\n\n"
                f"## Detaylar & Bulgular\n{details_lines or '- Henüz ek detay bulunmuyor.'}\n\n"
                f"## İlgili Bağlantılar\n{wikilinks_lines or '- Henüz bağlı kavram yok.'}\n\n"
                f"## Kaynaklar & Kanıtlar\n" + "\n".join(f"- {s}" for s in data.sources) + "\n"
            )
            atomic_write(target_path, content, mode=0o660)
            self._log_mutation("CREATE_CONCEPT", f"Created concept '{clean_title}' in {target_path.name}")

        # Update index.md
        self._update_index(
            article=f"[[concepts/{target_path.stem}|{clean_title}]]",
            summary=clean_summary[:120].replace("|", "-"),
            source=data.sources[0] if data.sources else "internal",
            updated=today_str,
        )

        return target_path

    def connect_concepts(
        self,
        slug_a: str,
        slug_b: str,
        relation: str = "bağlantı",
        strength: float = 0.8,
        sources: list[str] | None = None,
    ) -> Path:
        """Alias to add_or_update_connection for graph integration."""
        return self.add_or_update_connection(
            concept_a=slug_a,
            concept_b=slug_b,
            relationship=relation,
            evidence=sources,
            source=sources[0] if sources else "graph",
        )

    def add_or_update_connection(
        self,
        concept_a: str,
        concept_b: str,
        relationship: str,
        evidence: list[str] | None = None,
        source: str = "session",
    ) -> Path:
        """Create or update a bidirectional relationship between two concepts."""
        self.ensure_graph_dirs()
        try:
            path_a = self.find_concept(concept_a)
            path_b = self.find_concept(concept_b)
        except PolicyError:
            raise
        if not path_a or not path_b:
            missing = "concept-a" if not path_a else "concept-b"
            raise PolicyError(f"connection-endpoint-not-found:{missing}")

        slug_a = path_a.stem
        slug_b = path_b.stem
        canonical_a = self._canonical_concept_display(path_a)
        canonical_b = self._canonical_concept_display(path_b)

        if slug_a == slug_b:
            raise PolicyError("cannot-connect-concept-to-itself")

        # Canonical sort prevents opposing files like a--b.md and b--a.md
        if slug_a > slug_b:
            slug_a, slug_b = slug_b, slug_a
            canonical_a, canonical_b = canonical_b, canonical_a

        conn_filename = f"{slug_a}--{slug_b}.md"
        target_path = self.connections_dir / conn_filename
        today_str = dt.date.today().isoformat()

        clean_rel, _ = redact_sensitive_text(relationship)
        clean_ev = [redact_sensitive_text(e)[0] for e in (evidence or [])]

        if target_path.is_file():
            # Update existing connection
            old = target_path.read_text(encoding="utf-8")
            if clean_rel not in old:
                updated = old + f"\n- **İlişki Güncellemesi ({today_str}):** {clean_rel}\n"
                atomic_write(target_path, updated, mode=0o660)
                self._log_mutation("UPDATE_CONNECTION", f"Updated connection {conn_filename}")
        else:
            # Create new connection
            ev_lines = "\n".join(f"- {e}" for e in clean_ev) if clean_ev else "- Doğrudan oturum bağlamı."
            content = (
                f"---\n"
                f'concept_a: "{canonical_a}"\n'
                f'concept_b: "{canonical_b}"\n'
                f'created: "{today_str}"\n'
                f'source: "{source}"\n'
                f"---\n\n"
                f"# İlişki: [[concepts/{slug_a}|{canonical_a}]] ↔ [[concepts/{slug_b}|{canonical_b}]]\n\n"
                f"## İlişki Niteliği\n{clean_rel}\n\n"
                f"## Kanıtlar & Bağlam\n{ev_lines}\n"
            )
            atomic_write(target_path, content, mode=0o660)
            self._log_mutation("CREATE_CONNECTION", f"Created connection {conn_filename}")

        # Cross-link in both concept files if they exist
        self._ensure_connection_wikilink_in_concept(slug_a, slug_b, target_path.stem)
        self._ensure_connection_wikilink_in_concept(slug_b, slug_a, target_path.stem)

        # Update index.md
        self._update_index(
            article=f"[[connections/{target_path.stem}|{canonical_a} ↔ {canonical_b}]]",
            summary=clean_rel[:120].replace("|", "-"),
            source=source,
            updated=today_str,
        )

        return target_path

    def _ensure_connection_wikilink_in_concept(self, source_slug: str, target_slug: str, conn_name: str) -> None:
        """Inject bidirectional connection wikilink into concept file."""
        c_file = self.concepts_dir / f"{source_slug}.md"
        if not c_file.is_file():
            return
        try:
            content = c_file.read_text(encoding="utf-8")
            conn_link = f"[[connections/{conn_name}]]"
            target_link = f"[[concepts/{target_slug}]]"

            if conn_link in content:
                return

            injection = f"\n- {target_link} bağlantısı: {conn_link}"
            if "## İlgili Bağlantılar" in content:
                content = content.replace("## İlgili Bağlantılar", f"## İlgili Bağlantılar{injection}")
            else:
                content += f"\n\n## İlgili Bağlantılar{injection}"

            atomic_write(c_file, content, mode=0o660)
        except Exception as exc:
            logger.warning("Error injecting connection wikilink into %s: %s", source_slug, exc)

    def _update_index(self, article: str, summary: str, source: str, updated: str) -> None:
        """Deterministic row insertion/replacement in knowledge/index.md."""
        if not self.index_file.is_file():
            self.ensure_graph_dirs()

        content = self.index_file.read_text(encoding="utf-8")
        lines = content.splitlines()
        new_lines: list[str] = []
        replaced = False

        new_row = f"| {article} | {summary} | {source} | {updated} |"

        for line in lines:
            if line.startswith("|") and not line.startswith("|---") and not line.startswith("| Article"):
                parts = [p.strip() for p in line.split("|")[1:-1]]
                if len(parts) >= 1 and parts[0] == article:
                    new_lines.append(new_row)
                    replaced = True
                    continue
            new_lines.append(line)

        if not replaced:
            new_lines.append(new_row)

        atomic_write(self.index_file, "\n".join(new_lines) + "\n", mode=0o660)

    def _log_mutation(self, action: str, description: str) -> None:
        """Append chronological audit entry to knowledge/log.md."""
        if not self.log_file.is_file():
            self.ensure_graph_dirs()

        now_str = iso_now()
        entry = (
            f"### {now_str} — {action}\n"
            f"- **Açıklama:** {description}\n\n"
        )
        content = self.log_file.read_text(encoding="utf-8")
        atomic_write(self.log_file, content + entry, mode=0o660)

    def grow_from_session_summary(self, summary: dict[str, Any], session_id: str = "session") -> int:
        """Extract durable concepts and connections from a completed session summary."""
        self.ensure_graph_dirs()
        created_or_updated = 0

        decisions = summary.get("decisions", [])
        learnings = summary.get("learnings", [])
        important = summary.get("important_conversations", [])

        all_items = decisions + learnings + important
        for text in all_items:
            clean_text, _ = redact_sensitive_text(text)
            # Find quoted or capitalized technical entities (e.g. "TwoBerries CAPI", "Meta CAPI")
            found_entities = re.findall(r"\b[A-Z][a-zA-Z0-9_-]+(?:\s+[A-Z][a-zA-Z0-9_-]+)*\b", clean_text)
            # Filter out non-concept capitalized words
            entities = [
                e for e in set(found_entities)
                if len(e) >= 4 and e.lower() not in {"pzt", "sal", "car", "per", "cum", "bugun", "yarin", "user", "assistant"}
            ]

            # If 1 entity found, create or update concept
            for entity in entities:
                concept_data = ConceptData(
                    title=entity,
                    summary=f"{entity} hakkında oturum kararı / öğrenilen bulgu.",
                    details=[clean_text],
                    sources=[session_id],
                )
                self.add_or_update_concept(concept_data)
                created_or_updated += 1

            # If 2+ entities found in same statement, form bidirectional connection
            if len(entities) >= 2:
                ent_list = sorted(entities)
                self.add_or_update_connection(
                    concept_a=ent_list[0],
                    concept_b=ent_list[1],
                    relationship=clean_text,
                    evidence=[f"Session {session_id}"],
                    source=session_id,
                )
                created_or_updated += 1

        return created_or_updated

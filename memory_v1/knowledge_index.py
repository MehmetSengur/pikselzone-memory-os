"""Deterministic, single-writer rendering of knowledge/index.md and log.md.

``index.md`` and ``log.md`` are the two shared knowledge files that both hosts
used to rewrite wholesale -- the workstation through the graph engine on every
session flush, the VPS through the LLM compiler's writes manifest.  Two
divergent full-file rewrites of the same synced markdown cannot be merged by
Obsidian Sync, which is what produced the ``(Conflicted copy pz-hermes ...)``
files and the index regression (161 concepts collapsing to a handful of rows).

They are therefore no longer model-authored and no longer written from a
session flush.  The compiler proposes only ``concepts/**`` and
``connections/**``; after a successful promotion the index is regenerated here,
deterministically, from the canonical concept files on disk, and the log gets a
deterministic audit line.  Same inputs always produce byte-identical output.
"""
from __future__ import annotations

import datetime as dt
import re
from pathlib import Path

from .core import atomic_write, iso_now
from .graph_engine import (
    _frontmatter_lines,
    _frontmatter_title,
    _strip_optional_quotes,
    is_conflicted_copy_path,
)

# Canonical anchor, byte-identical to KnowledgeGraphEngine.ensure_graph_dirs().
INDEX_TITLE = "# Knowledge Base Index"
INDEX_PROSE = "Living concept and connection index for Pikselzone Second Brain."
HEADER_ROW = "| Article | Summary | Source | Updated |"
SEPARATOR_ROW = "|---|---|---|---|"
SUMMARY_MAX_CHARS = 200

LOG_TITLE = "# Knowledge Mutation Log"
LOG_PROSE = "Deterministic compiler/promotion audit trail for the shared knowledge graph."

_OZET_HEADING_RE = re.compile(r"^#{1,6}\s+Özet\s*$", re.IGNORECASE)


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _cell(value: object) -> str:
    """Collapse to one line and neutralise the table delimiter."""
    return " ".join(str(value).split()).replace("|", "-")


def _frontmatter_scalar(content: str, key: str) -> str | None:
    pattern = re.compile(rf"^{re.escape(key)}\s*:\s*(.+?)\s*$")
    for line in _frontmatter_lines(content):
        match = pattern.match(line)
        if match:
            return _strip_optional_quotes(match.group(1)) or None
    return None


def _frontmatter_list(content: str, key: str) -> list[str]:
    """Parse ``key: [a, b]``, ``key: a`` and block ``key:\\n  - a`` forms."""
    lines = _frontmatter_lines(content)
    pattern = re.compile(rf"^{re.escape(key)}\s*:\s*(.*)$")
    values: list[str] = []
    for index, line in enumerate(lines):
        match = pattern.match(line)
        if not match:
            continue
        remainder = match.group(1).strip()
        if remainder.startswith("["):
            inner = remainder[1:-1] if remainder.endswith("]") else remainder[1:]
            values.extend(_strip_optional_quotes(part) for part in inner.split(","))
        elif remainder:
            values.append(_strip_optional_quotes(remainder))
        else:
            for following in lines[index + 1:]:
                if re.match(r"^\S", following):
                    break
                item = re.match(r"^\s+-\s+(.+?)\s*$", following)
                if item:
                    values.append(_strip_optional_quotes(item.group(1)))
        break
    return [value for value in (item.strip() for item in values) if value]


def _extract_ozet_summary(content: str) -> str:
    """First non-empty paragraph under the ``## Özet`` heading, single line."""
    collected: list[str] = []
    in_section = False
    for line in content.splitlines():
        stripped = line.strip()
        if not in_section:
            if _OZET_HEADING_RE.match(stripped):
                in_section = True
            continue
        if stripped.startswith("#"):
            break
        if not stripped:
            if collected:
                break
            continue
        collected.append(stripped)
    return _cell(" ".join(collected))[:SUMMARY_MAX_CHARS]


def concept_files(vault: Path) -> list[Path]:
    concepts_dir = Path(vault) / "knowledge" / "concepts"
    if not concepts_dir.is_dir():
        return []
    return [
        path
        for path in sorted(concepts_dir.glob("*.md"))
        if path.is_file() and not is_conflicted_copy_path(path)
    ]


def _render_row(path: Path, today: str) -> tuple[tuple[str, str], str]:
    content = _read_text(path)
    title = (
        _frontmatter_scalar(content, "title")
        or _frontmatter_title(content)
        or path.stem
    )
    slug = path.stem
    title_cell = _cell(title)
    # Pipe-free markdown link so recall._load_knowledge_index_entries, which
    # splits rows on "|" at fixed column offsets, parses every row cleanly.
    article = f"[{title_cell}](concepts/{slug}.md)"
    summary = _extract_ozet_summary(content)
    source = _cell("; ".join(_frontmatter_list(content, "sources")))
    updated = _cell(
        _frontmatter_scalar(content, "updated")
        or _frontmatter_scalar(content, "created")
        or today
    )
    return (title_cell.casefold(), slug), f"| {article} | {summary} | {source} | {updated} |"


def render_index(vault: Path) -> str:
    """Deterministic index text for the concept files currently on disk."""
    today = dt.date.today().isoformat()
    rows = sorted(
        (_render_row(path, today) for path in concept_files(vault)),
        key=lambda item: item[0],
    )
    lines = [INDEX_TITLE, "", INDEX_PROSE, "", HEADER_ROW, SEPARATOR_ROW]
    lines.extend(row for _, row in rows)
    return "\n".join(lines) + "\n"


def write_index(vault: Path, *, out: Path | None = None, dry_run: bool = False) -> str:
    """Render and (unless ``dry_run``) write ``knowledge/index.md``."""
    text = render_index(vault)
    if not dry_run:
        target = Path(out) if out is not None else Path(vault) / "knowledge" / "index.md"
        atomic_write(target, text, mode=0o660)
    return text


def append_promotion_log(
    vault: Path, *, batch_id: str, promoted: list[str], index_rows: int,
) -> Path:
    """Append one deterministic audit line for a successful promotion.

    Never model-authored: the entry is generated from the promotion result, so
    the file has exactly one writer and a stable shape.
    """
    log_path = Path(vault) / "knowledge" / "log.md"
    existing = _read_text(log_path)
    if not existing.strip():
        existing = f"{LOG_TITLE}\n\n{LOG_PROSE}\n"
    concepts = sorted(
        rel.removeprefix("knowledge/concepts/").removesuffix(".md")
        for rel in promoted if rel.startswith("knowledge/concepts/")
    )
    connections = sorted(
        rel.removeprefix("knowledge/connections/").removesuffix(".md")
        for rel in promoted if rel.startswith("knowledge/connections/")
    )
    entry = (
        f"\n- {iso_now()} — PROMOTE {batch_id}: "
        f"{len(concepts)} concept, {len(connections)} connection; "
        f"index rebuilt deterministically ({index_rows} rows)."
    )
    if concepts:
        entry += f"\n  - concepts: {', '.join(concepts)}"
    if connections:
        entry += f"\n  - connections: {', '.join(connections)}"
    atomic_write(log_path, existing.rstrip() + entry + "\n", mode=0o660)
    return log_path


def rebuild_after_promotion(vault: Path, *, batch_id: str, promoted: list[str]) -> dict:
    """Single deterministic post-promotion step: index rebuild + log audit."""
    text = write_index(vault)
    rows = sum(1 for line in text.splitlines() if line.startswith("| [") )
    append_promotion_log(vault, batch_id=batch_id, promoted=promoted, index_rows=rows)
    return {"index_rows": rows, "promoted": len(promoted)}


__all__ = [
    "INDEX_TITLE", "INDEX_PROSE", "HEADER_ROW", "SEPARATOR_ROW",
    "LOG_TITLE", "LOG_PROSE",
    "concept_files", "render_index", "write_index",
    "append_promotion_log", "rebuild_after_promotion",
]

#!/usr/bin/env python3
"""Deterministic rebuild of ``<vault>/knowledge/index.md`` from concept files.

The live index regressed to a handful of rows while ``knowledge/concepts/``
holds ~164 concept files.  This tool regenerates the index table directly from
the concept frontmatter on disk -- no LLM, no compiler, fully deterministic.

The emitted file uses the canonical ``ensure_graph_dirs`` anchor (title +
prose + ``| Article | Summary | Source | Updated |`` + separator) and
pipe-free markdown-link article cells so
:func:`memory_v1.recall._load_knowledge_index_entries`, which splits each row
on ``|`` and reads fixed column offsets, parses every row column-clean.

Usage::

    python3 scripts/rebuild_knowledge_index.py --vault <path> [--dry-run] [--out <path>]
"""
from __future__ import annotations

import argparse
import datetime as dt
import re
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from memory_v1.core import atomic_write
from memory_v1.graph_engine import (
    _frontmatter_lines,
    _frontmatter_title,
    _strip_optional_quotes,
    is_conflicted_copy_path,
    slugify,
)

# Canonical anchor -- byte-identical to what
# ``KnowledgeGraphEngine.ensure_graph_dirs`` writes for a fresh index.  The
# rebuild always emits this verbatim (determinism over preserving a
# possibly-corrupt on-disk title).
INDEX_TITLE = "# Knowledge Base Index"
INDEX_PROSE = "Living concept and connection index for Pikselzone Second Brain."
HEADER_ROW = "| Article | Summary | Source | Updated |"
SEPARATOR_ROW = "|---|---|---|---|"
SUMMARY_MAX_CHARS = 200

_OZET_HEADING_RE = re.compile(r"^#{1,6}\s+Özet\s*$", re.IGNORECASE)


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _cell(value: object) -> str:
    """Whitespace-collapse to a single line and neutralise the ``|`` delimiter."""
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


def _concept_files(concepts_dir: Path) -> list[Path]:
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
    slug = path.stem or slugify(title)
    title_cell = _cell(title)
    # Markdown link (relative to knowledge/index.md).  ``_cell`` already
    # stripped any literal ``|``, so the article cell carries no inner pipe
    # to shift the recall parser's fixed column offsets.
    article = f"[{title_cell}](concepts/{slug}.md)"
    summary = _extract_ozet_summary(content)
    source = _cell("; ".join(_frontmatter_list(content, "sources")))
    updated = _cell(
        _frontmatter_scalar(content, "updated")
        or _frontmatter_scalar(content, "created")
        or today
    )
    row = f"| {article} | {summary} | {source} | {updated} |"
    return (title_cell.casefold(), slug), row


def rebuild_index(vault: Path, *, out: Path | None = None, dry_run: bool = False) -> str:
    """Render ``knowledge/index.md`` from concept files and return the text.

    With ``dry_run`` nothing is written.  Otherwise the text is written to
    ``out`` when given, else ``<vault>/knowledge/index.md``, via
    :func:`memory_v1.core.atomic_write` with mode ``0o660``.
    """
    vault = Path(vault).expanduser().resolve()
    concepts_dir = vault / "knowledge" / "concepts"
    target = (
        Path(out).expanduser().resolve()
        if out is not None
        else vault / "knowledge" / "index.md"
    )
    today = dt.date.today().isoformat()

    rows = sorted(
        (_render_row(path, today) for path in _concept_files(concepts_dir)),
        key=lambda item: item[0],
    )

    lines = [INDEX_TITLE, "", INDEX_PROSE, "", HEADER_ROW, SEPARATOR_ROW]
    lines.extend(row for _, row in rows)
    text = "\n".join(lines) + "\n"

    if not dry_run:
        atomic_write(target, text, mode=0o660)
    return text


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Deterministically rebuild knowledge/index.md from concept frontmatter.",
    )
    parser.add_argument("--vault", required=True, type=Path, help="Obsidian vault root.")
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Write here instead of <vault>/knowledge/index.md.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the would-be index to stdout and write nothing.",
    )
    args = parser.parse_args(argv)

    text = rebuild_index(args.vault, out=args.out, dry_run=args.dry_run)
    row_count = sum(1 for line in text.splitlines() if "](concepts/" in line)

    if args.dry_run:
        sys.stdout.write(text)
    else:
        target = (
            args.out
            if args.out is not None
            else args.vault / "knowledge" / "index.md"
        )
        print(
            f"rebuilt {Path(target).expanduser().resolve()} ({row_count} concept rows)",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

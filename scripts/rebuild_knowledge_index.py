#!/usr/bin/env python3
"""CLI over the deterministic index renderer in ``memory_v1.knowledge_index``.

The rendering logic lives in the package because the compiler's promotion step
calls it too -- ``index.md`` has exactly one writer and one format.

Usage::

    python3 scripts/rebuild_knowledge_index.py --vault <path> [--dry-run] [--out <path>]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from memory_v1.knowledge_index import (  # noqa: E402  (path bootstrap above)
    HEADER_ROW, INDEX_PROSE, INDEX_TITLE, SEPARATOR_ROW, render_index, write_index,
)

__all__ = [
    "HEADER_ROW", "INDEX_PROSE", "INDEX_TITLE", "SEPARATOR_ROW",
    "render_index", "rebuild_index", "main",
]


def rebuild_index(vault: Path, *, out: Path | None = None, dry_run: bool = False) -> str:
    return write_index(vault, out=out, dry_run=dry_run)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Deterministically rebuild knowledge/index.md from concept frontmatter.",
    )
    parser.add_argument("--vault", required=True, type=Path, help="Obsidian vault root.")
    parser.add_argument("--out", type=Path, default=None,
                        help="Write here instead of <vault>/knowledge/index.md.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print the would-be index to stdout and write nothing.")
    args = parser.parse_args(argv)

    text = rebuild_index(args.vault, out=args.out, dry_run=args.dry_run)
    rows = sum(1 for line in text.splitlines() if "](concepts/" in line)
    if args.dry_run:
        sys.stdout.write(text)
    else:
        target = args.out if args.out is not None else args.vault / "knowledge" / "index.md"
        print(f"rebuilt {Path(target).expanduser().resolve()} ({rows} concept rows)",
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""V2.3 acceptance harness for cross-project associative recall.

Runs the REAL :func:`memory_v1.recall.associative_recall_fast` code path against
a disposable fixture vault built in a temp directory.  It never reads or writes
the live Obsidian vault, the project registry, the live knowledge index, sync
conflict files, or any production state.

Checks
------
1. Positive recall  -- a Luvaa-shaped prompt that never says "TwoBerries" must
   surface the historical TwoBerries case, capped at max_items.
2. Noise baseline   -- ~15 realistic but unrelated prompts.  A "clearly
   irrelevant" injection is one where the prompt and the injected concept share
   fewer than 2 meaningful tokens; those must be 0.  Overall injection rate must
   be <= NOISE_RATE_LIMIT.
3. Index-first      -- no full concepts/ scan, no connections/ traversal, at most
   MAX_CONCEPT_READS concept files opened.
4. Fail-open        -- a missing or corrupt index yields no injection and no
   exception.

Usage::

    python3 scripts/acceptance_associative_recall.py [--json] [--verbose]

Exit code 0 = PASS, 1 = FAIL.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import re
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from memory_v1 import recall as recall_mod
from memory_v1.core import MemoryConfig
from memory_v1.recall import associative_recall_fast

# --- acceptance thresholds (documented, not tuned per-run) -----------------
MAX_ITEMS = 3
MAX_CONCEPT_READS = 3
NOISE_RATE_LIMIT = 0.20          # <= 20% of noise prompts may inject at all
MIN_SHARED_TOKENS_FOR_DEFENSIBLE = 2

_STOPWORDS = {
    "bir", "bu", "su", "şu", "icin", "için", "ile", "ve", "de", "da", "mi", "mı",
    "ne", "var", "yok", "the", "and", "for", "with", "this", "that", "can", "you",
    "bana", "lutfen", "lütfen", "biraz", "daha", "gibi", "olan", "nasil", "nasıl",
    "et", "eder", "edelim", "yap", "yapar", "olabilir", "mu", "mü",
}


def _tokens(text: str) -> set[str]:
    return {
        w for w in re.findall(r"[a-z0-9_]+", text.lower())
        if len(w) > 2 and w not in _STOPWORDS
    }


# --- fixture ---------------------------------------------------------------
# The historical TwoBerries case, with provenance and the full
# Problem / Denenen / Sonuç / Neden arc that §8 asks the summarizer to produce.
_TWOBERRIES_CASE = """---
title: "Meta Catalog ID Mismatch"
aliases: ["catalog id mismatch", "content_ids mismatch"]
tags: ["#concept"]
created: "2026-08-14"
updated: "2026-08-30"
sources: ["twoberries:9c81291c831801b99d95621a6a29d799"]
authority: derived-memory-not-canonical
---

# Meta Catalog ID Mismatch

## Özet
Problem: Meta catalog eslesme orani cok dustu; purchase ve add_to_cart eventleri
katalog urunleriyle eslesmedi.
Denenen: feed parent product id korunurken event tarafinda variant content_ids
gonderildi.
Sonuc: basarisiz. AddToCart match rate %0,1 seviyesine indi.
Neden: feed identity stratejisi ile event content_ids stratejisi hizalanmamisti;
parent/variant secimi iki tarafta ayni olmak zorunda.

## Detaylar & Bulgular
- session_end (codex): katalog feed parent id gonderiyordu, pixel event variant id.
- session_end (codex): match rate dusuklugu once bidding sorunu sanildi, degildi.

## Kaynaklar & Kanıtlar
- twoberries:9c81291c831801b99d95621a6a29d799
"""

_DISTRACTORS: dict[str, tuple[str, str]] = {
    # slug: (index summary, concept Özet body)
    "aura-cache-sync": (
        "Redis baglantisi dogrulanmadan cache flush ve warmup yapilmamali",
        "Redis ping PONG vermeden cache flush edilmez; zone bazli warmup sirasi onemli.",
    ),
    "orion-rollback-placement": (
        "ORION deployment raporlarinda rollback durumu sonuc bolumunun hemen ardina gelir",
        "Rollback durumu sonuc bolumunun hemen ardinda raporlanir.",
    ),
    "ga4-consent-mode": (
        "GA4 consent mode v2 kurulumunda reklam sinyalleri izin durumuna bagli",
        "GA4 consent mode v2 ile ad_storage ve analytics_storage izinleri ayri yonetilir.",
    ),
    "dockerfile-multistage": (
        "Dockerfile'larda multi-stage build adimlari standarttir",
        "Builder stage ve slim final stage ayrimi standart kabul edildi.",
    ),
    # legacy generic junk that exists in the real vault; must stay down-weighted
    "api": ("api api api", "api api api generic artefact word."),
    "pass": ("pass status word", "pass fail status artefact."),
}

_NOISE_PROMPTS = [
    "bu python fonksiyonunu biraz daha okunur hale getirir misin",
    "testleri calistirdim ikisi kaldi, stack trace'e bakar misin",
    "commit mesajini duzeltip amend eder misin",
    "bu dosyadaki type hint'leri tamamla",
    "readme'ye kurulum adimlarini ekle",
    "su regex neden eslesmiyor anlamadim",
    "branch'i main uzerine rebase edebilir miyiz",
    "bu SQL sorgusu neden bu kadar yavas calisiyor",
    "yeni bir migration olustur ve rollback adimini basa yaz",
    "logrotate ayarini gunluk yapalim",
    "su JSON'u pydantic modeline cevirir misin",
    "nginx timeout degerini yukseltmek istiyorum",
    "bu bileşenin loading state'ini ekleyelim",
    "cron ifadesini her gece 3'e ayarla",
    "environment degiskenlerini .env.example'a yaz",
]


@dataclass
class Injection:
    prompt: str
    slugs: list[str]
    shared_tokens: dict[str, list[str]] = field(default_factory=dict)

    @property
    def clearly_irrelevant(self) -> bool:
        return any(
            len(toks) < MIN_SHARED_TOKENS_FOR_DEFENSIBLE
            for toks in self.shared_tokens.values()
        )


def _build_fixture(root: Path, *, index_state: str = "ok") -> MemoryConfig:
    vault = root / "vault"
    concepts = vault / "knowledge" / "concepts"
    connections = vault / "knowledge" / "connections"
    concepts.mkdir(parents=True)
    connections.mkdir(parents=True)

    (concepts / "meta-catalog-id-mismatch.md").write_text(_TWOBERRIES_CASE, encoding="utf-8")
    rows = [
        "| [Meta Catalog ID Mismatch](concepts/meta-catalog-id-mismatch.md) | "
        "Meta feed parent id ile event variant content_ids hizalanmayinca catalog "
        "match rate dustu; basarisiz pattern | twoberries:9c81291c | 2026-08-30 |"
    ]
    for slug, (summary, body) in _DISTRACTORS.items():
        (concepts / f"{slug}.md").write_text(
            f'---\ntitle: "{slug}"\nsources: ["fixture:1"]\n---\n\n'
            f"# {slug}\n\n## Özet\n{body}\n",
            encoding="utf-8",
        )
        rows.append(f"| [{slug}](concepts/{slug}.md) | {summary} | fixture:1 | 2026-08-20 |")

    # connections exist purely to prove they are never traversed
    for name in ("meta-catalog-id-mismatch--ga4-consent-mode", "api--pass"):
        (connections / f"{name}.md").write_text(
            f"# İlişki\n\nfixture connection {name}\n", encoding="utf-8"
        )

    index = vault / "knowledge" / "index.md"
    if index_state == "ok":
        index.write_text(
            "# Knowledge Base Index\n\n"
            "Living concept and connection index for Pikselzone Second Brain.\n\n"
            "| Article | Summary | Source | Updated |\n|---|---|---|---|\n"
            + "\n".join(rows) + "\n",
            encoding="utf-8",
        )
    elif index_state == "corrupt":
        index.write_text("\x00\x00 not a table at all \x00", encoding="utf-8")
    # index_state == "missing" -> write nothing

    return MemoryConfig.from_dict({
        "role": "workstation",
        "vault_path": str(vault),
        "state_path": str(root / "state"),
        "runtimes": ["codex", "claude"],
        "transcript_roots": {"codex": [str(root)], "claude": [str(root)]},
        "can_write_event_memory": True,
        "can_run_compiler": False,
        "provider": {"mode": "runtime-native"},
    })


@contextlib.contextmanager
def _tracked_reads():
    """Record every path associative_recall_fast opens through recall's reader."""
    opened: list[str] = []
    real = recall_mod.secure_read_text

    def tracker(path, **kwargs):
        opened.append(str(path))
        return real(path, **kwargs)

    recall_mod.secure_read_text = tracker
    try:
        yield opened
    finally:
        recall_mod.secure_read_text = real


def _slugs_in(rendered: str) -> list[str]:
    return sorted(set(re.findall(r"knowledge/concepts/([a-z0-9][a-z0-9_-]*)\.md", rendered)))


def _concept_text(config: MemoryConfig, slug: str) -> str:
    p = config.vault_path / "knowledge" / "concepts" / f"{slug}.md"
    return p.read_text(encoding="utf-8") if p.is_file() else ""


def run(verbose: bool = False) -> dict:
    report: dict = {}
    with tempfile.TemporaryDirectory(prefix="pz-acceptance-assoc-") as tmp:
        root = Path(tmp).resolve()
        config = _build_fixture(root)

        # --- 1 + 3. positive recall, with read instrumentation -------------
        luvaa_prompt = (
            "Luvaa'da Meta tarafinda urunler eventlerle dogru eslesmiyor, "
            "content_ids ve feed id stratejisinde bir sorun olabilir mi"
        )
        assert "twoberries" not in luvaa_prompt.lower(), "prompt must not name the source project"
        with _tracked_reads() as opened:
            positive = associative_recall_fast(config, luvaa_prompt, max_items=MAX_ITEMS)
        pos_slugs = _slugs_in(positive)
        concept_reads = [p for p in opened if "/knowledge/concepts/" in p]
        connection_reads = [p for p in opened if "/knowledge/connections/" in p]
        daily_reads = [p for p in opened if "/daily/" in p]
        index_reads = [p for p in opened if p.endswith("/knowledge/index.md")]

        report["POSITIVE_RECALL"] = "PASS" if "meta-catalog-id-mismatch" in pos_slugs else "FAIL"
        report["POSITIVE_MATCH_SOURCE"] = (
            "twoberries:9c81291c831801b99d95621a6a29d799"
            if "meta-catalog-id-mismatch" in pos_slugs else "none"
        )
        report["POSITIVE_INJECTION_COUNT"] = len(pos_slugs)
        report["_positive_slugs"] = pos_slugs
        report["_positive_carries_arc"] = all(
            marker in positive for marker in ("Problem:", "Denenen:", "Sonuc:", "Neden:")
        )
        report["_index_reads"] = len(index_reads)
        report["_concept_reads"] = len(concept_reads)

        report["FULL_GRAPH_SCAN"] = (
            "NO" if (
                len(concept_reads) <= MAX_CONCEPT_READS
                and not connection_reads
                and not daily_reads
                and len(index_reads) >= 1
            ) else "YES"
        )

        # --- 2. noise baseline -------------------------------------------
        injections: list[Injection] = []
        for prompt in _NOISE_PROMPTS:
            rendered = associative_recall_fast(config, prompt, max_items=MAX_ITEMS)
            slugs = _slugs_in(rendered)
            if not slugs:
                continue
            shared = {
                slug: sorted(_tokens(prompt) & _tokens(_concept_text(config, slug)))
                for slug in slugs
            }
            injections.append(Injection(prompt=prompt, slugs=slugs, shared_tokens=shared))

        report["NOISE_PROMPTS"] = len(_NOISE_PROMPTS)
        report["NOISE_INJECTIONS"] = len(injections)
        report["NOISE_RATE"] = round(len(injections) / len(_NOISE_PROMPTS), 4)
        report["CLEARLY_IRRELEVANT_INJECTIONS"] = sum(
            1 for i in injections if i.clearly_irrelevant
        )
        report["_noise_detail"] = [
            {"prompt": i.prompt, "slugs": i.slugs, "shared_tokens": i.shared_tokens,
             "clearly_irrelevant": i.clearly_irrelevant}
            for i in injections
        ]

        # --- 4. fail-open --------------------------------------------------
        fail_open_ok = True
        for state in ("missing", "corrupt"):
            with tempfile.TemporaryDirectory(prefix=f"pz-acceptance-{state}-") as t2:
                cfg2 = _build_fixture(Path(t2).resolve(), index_state=state)
                try:
                    out = associative_recall_fast(cfg2, luvaa_prompt, max_items=MAX_ITEMS)
                except Exception:
                    fail_open_ok = False
                    break
                if out:
                    fail_open_ok = False
                    break
        report["FAIL_OPEN"] = "PASS" if fail_open_ok else "FAIL"

    # --- verdict -----------------------------------------------------------
    failures: list[str] = []
    if report["POSITIVE_RECALL"] != "PASS":
        failures.append("positive recall did not surface the TwoBerries case")
    if not report["_positive_carries_arc"]:
        failures.append("injected case lost the Problem/Denenen/Sonuc/Neden arc")
    if report["POSITIVE_INJECTION_COUNT"] > MAX_ITEMS:
        failures.append(f"positive injection exceeded max_items={MAX_ITEMS}")
    if report["CLEARLY_IRRELEVANT_INJECTIONS"] != 0:
        failures.append("clearly irrelevant injection(s) present")
    if report["NOISE_RATE"] > NOISE_RATE_LIMIT:
        failures.append(f"noise rate {report['NOISE_RATE']} > {NOISE_RATE_LIMIT}")
    if report["FULL_GRAPH_SCAN"] != "NO":
        failures.append("full graph scan detected")
    if report["FAIL_OPEN"] != "PASS":
        failures.append("fail-open violated on missing/corrupt index")
    report["_failures"] = failures
    report["VERDICT"] = "PASS" if not failures else "FAIL"
    return report


def _print(report: dict, verbose: bool) -> None:
    for key in (
        "POSITIVE_RECALL", "POSITIVE_MATCH_SOURCE", "POSITIVE_INJECTION_COUNT",
        "NOISE_PROMPTS", "NOISE_INJECTIONS", "NOISE_RATE",
        "CLEARLY_IRRELEVANT_INJECTIONS", "FULL_GRAPH_SCAN", "FAIL_OPEN",
    ):
        print(f"{key}={report[key]}")
    print(f"VERDICT={report['VERDICT']}")
    if report["_failures"]:
        print("\nFAILURES:")
        for f in report["_failures"]:
            print(f"  - {f}")
    print(
        f"\n[detail] index reads={report['_index_reads']} "
        f"concept reads={report['_concept_reads']} (cap {MAX_CONCEPT_READS}) "
        f"positive slugs={report['_positive_slugs']} "
        f"arc preserved={report['_positive_carries_arc']}"
    )
    if report["_noise_detail"]:
        print("[detail] noise injections (matched lexical signal per concept):")
        for item in report["_noise_detail"]:
            for slug, toks in item["shared_tokens"].items():
                print(
                    f"  - {slug}: shared={toks or '[]'} "
                    f"{'CLEARLY-IRRELEVANT' if len(toks) < MIN_SHARED_TOKENS_FOR_DEFENSIBLE else 'defensible'}"
                    f"  <- {item['prompt'][:60]}"
                )
    elif verbose:
        print("[detail] noise injections: none")


def diagnose() -> None:
    """Show, per noise prompt, whether it died at the trivial gate or was scored
    and rejected -- so a clean noise baseline cannot be mistaken for a strong one."""
    from memory_v1.recall import _TRIVIAL_PROMPTS, _load_knowledge_index_entries, _tokenize

    with tempfile.TemporaryDirectory(prefix="pz-acceptance-diag-") as tmp:
        config = _build_fixture(Path(tmp).resolve())
        print(f"{'gate':<8} {'top':>6}  {'best index candidate':<28} prompt")
        print("-" * 100)
        gated = scored = 0
        for prompt in _NOISE_PROMPTS:
            n = " ".join(prompt.lower().split())
            trivial = (
                not n or n in _TRIVIAL_PROMPTS or len(n) < 12 or len(_tokenize(n)) < 3
            )
            items = sorted(
                _load_knowledge_index_entries(config, query=prompt),
                key=lambda i: -i.relevance_score,
            )
            top = items[0] if items else None
            slug = (
                re.search(r"concepts/([a-z0-9_-]+)", top.content).group(1) if top else "-"
            )
            gated += trivial
            scored += not trivial
            print(
                f"{'TRIVIAL' if trivial else 'scored':<8} "
                f"{(top.relevance_score if top else 0.0):>6.2f}  {slug:<28} {prompt[:50]}"
            )
        print(
            f"\nnoise prompts reaching the scorer: {scored}/{len(_NOISE_PROMPTS)} "
            f"(trivial-gated: {gated})"
        )

        positive = (
            "Luvaa'da Meta tarafinda urunler eventlerle dogru eslesmiyor, "
            "content_ids ve feed id stratejisinde bir sorun olabilir mi"
        )
        print("\npositive index candidates (concept gate is min_score=6.0):")
        for item in sorted(
            _load_knowledge_index_entries(config, query=positive),
            key=lambda i: -i.relevance_score,
        )[:4]:
            slug = re.search(r"concepts/([a-z0-9_-]+)", item.content).group(1)
            print(f"  {item.relevance_score:6.2f}  {slug}")

        weaker = (
            "Luvaa'da Meta tarafinda urunler eventlerle dogru eslesmiyor, "
            "feed id stratejisinde sorun olabilir mi"
        )
        print(
            "\nmargin check -- same prompt without the rare token 'content_ids' -> "
            f"{_slugs_in(associative_recall_fast(config, weaker, max_items=MAX_ITEMS)) or 'NO INJECTION'}"
        )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--json", action="store_true", help="emit the full report as JSON")
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--diagnose", action="store_true",
                    help="show per-noise-prompt gate/score breakdown, then exit")
    args = ap.parse_args(argv)
    if args.diagnose:
        diagnose()
        return 0
    report = run(verbose=args.verbose)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        _print(report, args.verbose)
    return 0 if report["VERDICT"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())

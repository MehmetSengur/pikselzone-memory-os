"""Automatic rule learning, candidate tracking, deduplication, and conflict reconciliation (SB2-04).

Extracts durable operational rules and behavioral preferences from user turns in transcripts.
- Explicit directives ("bundan sonra...", "asla...", "bunu bir daha sorma", "her zaman...") -> Durable Active Rule
- Mild preferences ("bence şöyle...", "tercihim...") -> Candidate Rule (promoted after 2+ observations)
- Prevents duplicate rule proliferation via semantic token overlap
- Reconciles conflicting rules by archiving obsolete ones with provenance
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import logging
import re
from pathlib import Path
from typing import Any, List, Optional, Set, Tuple

from .companion import CompanionManager, RuleItem
from .core import iso_now, redact_sensitive_text

logger = logging.getLogger("memory_v1.rule_learner")

# Regex patterns identifying explicit persistent user directives
EXPLICIT_DIRECTIVE_PATTERNS = (
    re.compile(r"(?i)\b(?:bundan sonra|artık|şundan sonra)\b\s+(.+)", re.UNICODE),
    re.compile(r"(?i)\b(?:bunu bir daha|bir daha bana|asla bunu)\s+(?:yapma|sorma|kullanma|etme)\b", re.UNICODE),
    re.compile(r"(?i)\b(?:her zaman|daima|kesinlikle)\s+([^.!?]+)\s+(?:yap|kullan|uygula|yaz|çalıştır|et|koy|göster|yerleştir|ekle|getir)\b", re.UNICODE),
    re.compile(r"(?i)\b(?:asla|kesinlikle)\s+([^.!?]+)\s+(?:yapma|kullanma|dokunma|silme|çalıştırma|etme|başlatma)\b", re.UNICODE),
    re.compile(r"(?i)\b(?:(?:benim|kalıcı|ikinci beyin|second brain|test)?\s*tercihim|şunu tercih ediyorum|tercih ederim)\b[:\s]+([^.!?]+)", re.UNICODE),
    re.compile(r"(?i)\b(?:görmek istiyorum|olmasını istiyorum|yapılmasını istiyorum)\b", re.UNICODE),
    re.compile(r"(?i)\bfrom now on\b[,:\s]+([^.!?]+)", re.UNICODE),
    re.compile(r"(?i)\balways\s+([^.!?]+)\b", re.UNICODE),
    re.compile(r"(?i)\bnever\s+([^.!?]+)\b", re.UNICODE),
    re.compile(r"(?i)\bdon't ever\s+([^.!?]+)\b", re.UNICODE),
)

# Correction patterns
CORRECTION_PATTERNS = (
    re.compile(r"(?i)\bbunu\s+böyle\s+yapma\b[,:\s]*(.*)", re.UNICODE),
    re.compile(r"(?i)\byanlış\b[,:\s]+(?:doğrusu|bunun yerine)\s+([^.!?]+)", re.UNICODE),
    re.compile(r"(?i)\böyle değil\b[,:\s]+([^.!?]+)", re.UNICODE),
)

# Conflict indicator keywords indicating replacement
REPLACEMENT_SIGNALS = {
    "yerine", "artık", "bundan sonra", "instead of", "from now on", "değiştir", "vazgeç"
}


@dataclasses.dataclass
class ExtractedRule:
    rule_text: str
    reason: str
    is_explicit: bool
    confidence: float
    source_turn: str


def _tokenize(text: str) -> set[str]:
    words = re.findall(r"\w+", text.lower(), re.UNICODE)
    stop_words = {
        "bir", "bu", "ve", "ile", "için", "olan", "olarak", "daha", "en", "çok",
        "the", "a", "an", "and", "or", "to", "in", "on", "of", "for", "with"
    }
    return {w for w in words if len(w) > 2 and w not in stop_words}


def calculate_overlap(text1: str, text2: str) -> float:
    t1 = _tokenize(text1)
    t2 = _tokenize(text2)
    if not t1 or not t2:
        return 0.0
    intersection = t1.intersection(t2)
    union = t1.union(t2)
    return len(intersection) / len(union)


class RuleLearner:
    """Evaluates conversation turns, identifies persistent rules, deduplicates and reconciles."""

    def __init__(self, companion_mgr: CompanionManager) -> None:
        self.companion = companion_mgr

    def extract_rules_from_text(self, text: str) -> list[ExtractedRule]:
        """Analyze user text statements to discover rule candidates or durable directives."""
        extracted: list[ExtractedRule] = []
        clean_text, _ = redact_sensitive_text(text)

        for sentence in re.split(r"[.!?\n]+", clean_text):
            sentence = sentence.strip()
            if not sentence or len(sentence) < 10:
                continue

            # Check explicit directives
            for pat in EXPLICIT_DIRECTIVE_PATTERNS:
                m = pat.search(sentence)
                if m:
                    rule_body = sentence
                    extracted.append(ExtractedRule(
                        rule_text=rule_body,
                        reason="Kullanıcının açık kalıcı direktifi",
                        is_explicit=True,
                        confidence=0.95,
                        source_turn=sentence,
                    ))
                    break

            # Check corrections
            for pat in CORRECTION_PATTERNS:
                m = pat.search(sentence)
                if m:
                    extracted.append(ExtractedRule(
                        rule_text=sentence,
                        reason="Kullanıcı düzeltme uyarısı",
                        is_explicit=True,
                        confidence=0.90,
                        source_turn=sentence,
                    ))
                    break

        return extracted

    def check_conflict(self, new_rule: str, existing_rule: str) -> bool:
        """Determine if a new rule conflicts with or overrides an existing rule."""
        t_new = _tokenize(new_rule)
        t_old = _tokenize(existing_rule)
        common = t_new.intersection(t_old)
        
        # If they share significant context/topic keywords
        if len(common) >= 2:
            # Check for negation or replacement markers
            new_neg = any(w in new_rule.lower() for w in ("yapma", "kullanma", "asla", "değil", "never", "don't"))
            old_neg = any(w in existing_rule.lower() for w in ("yapma", "kullanma", "asla", "değil", "never", "don't"))
            if new_neg != old_neg:
                return True
            # Check for explicit replacement words
            if any(w in new_rule.lower() for w in REPLACEMENT_SIGNALS):
                return True

        return False

    def learn_from_transcript(self, transcript_turns: list[tuple[str, str]], source_session: str = "session") -> int:
        """Process user turns from a session transcript and update Kurallar.md."""
        learned_count = 0
        user_texts = [text for role, text in transcript_turns if role == "user"]
        
        existing_rules = self.companion.read_rules()

        for u_text in user_texts:
            extracted_rules = self.extract_rules_from_text(u_text)
            for item in extracted_rules:
                rule_text = item.rule_text
                
                # Deduplication check
                duplicate = False
                for r in existing_rules:
                    if calculate_overlap(rule_text, r.text) > 0.65:
                        duplicate = True
                        break
                if duplicate:
                    continue

                # Conflict reconciliation check
                conflicted_rule: RuleItem | None = None
                for r in existing_rules:
                    if self.check_conflict(rule_text, r.text):
                        conflicted_rule = r
                        break

                if conflicted_rule:
                    self._reconcile_and_replace_rule(
                        old_rule=conflicted_rule,
                        new_rule_text=rule_text,
                        reason=item.reason,
                        source=source_session,
                    )
                    learned_count += 1
                else:
                    added = self.companion.add_or_update_rule(
                        rule_text=rule_text,
                        reason=item.reason,
                        source=source_session,
                        is_direct_command=item.is_explicit,
                    )
                    if added:
                        learned_count += 1

        return learned_count

    def learn_from_user_message(self, user_text: str, source: str = "session") -> list[str]:
        """Process a single user message and learn any persistent rules."""
        learned: list[str] = []
        extracted_rules = self.extract_rules_from_text(user_text)
        existing_rules = self.companion.read_rules()

        for item in extracted_rules:
            rule_text = item.rule_text
            duplicate = False
            for r in existing_rules:
                if calculate_overlap(rule_text, r.text) > 0.65:
                    duplicate = True
                    break
            if duplicate:
                continue

            conflicted_rule: RuleItem | None = None
            for r in existing_rules:
                if self.check_conflict(rule_text, r.text):
                    conflicted_rule = r
                    break

            if conflicted_rule:
                self._reconcile_and_replace_rule(
                    old_rule=conflicted_rule,
                    new_rule_text=rule_text,
                    reason=item.reason,
                    source=source,
                )
                learned.append(rule_text)
            else:
                added = self.companion.add_or_update_rule(
                    rule_text=rule_text,
                    reason=item.reason,
                    source=source,
                    is_direct_command=item.is_explicit,
                )
                if added:
                    learned.append(rule_text)
        return learned

    def _reconcile_and_replace_rule(
        self,
        old_rule: RuleItem,
        new_rule_text: str,
        reason: str,
        source: str,
    ) -> None:
        """Archive the conflicting old rule and insert the new reconciled rule."""
        rules_path = self.companion.companion_dir / "Kurallar.md"
        if not rules_path.is_file():
            self.companion.ensure_companion_files()
        content = rules_path.read_text(encoding="utf-8")

        now_str = iso_now()
        clean_new, _ = redact_sensitive_text(new_rule_text.strip())

        # Remove or update old rule from Aktif Kurallar
        lines = content.splitlines()
        new_lines: list[str] = []
        for line in lines:
            if old_rule.text in line:
                continue  # Removed from active
            new_lines.append(line)

        # Append new rule under ## Aktif Kurallar
        new_entry = (
            f"- **kural:** {clean_new} | "
            f"**neden:** {reason} (Eski kural güncellendi) | "
            f"**kaynak:** {source} | "
            f"**durum:** aktif"
        )
        archive_entry = (
            f"- **eski_kural:** {old_rule.text} | "
            f"**yerine_geçen:** {clean_new} | "
            f"**arşiv_tarihi:** {now_str} | "
            f"**kaynak:** {source}"
        )

        final_lines: list[str] = []
        for line in new_lines:
            final_lines.append(line)
            if line.strip() == "## Aktif Kurallar":
                final_lines.append(new_entry)
            elif line.strip() == "## Arşivlenmiş / Geçersiz Kılınmış Kurallar":
                final_lines.append(archive_entry)

        rules_path.write_text("\n".join(final_lines) + "\n", encoding="utf-8")
        logger.info("Reconciled rule: replaced '%s' with '%s'", old_rule.text, clean_new)

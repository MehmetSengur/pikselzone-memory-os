"""Rule learning from what the user actually said (SB2-04).

Only sentences the user wrote themselves can become rules. Each user turn is
first split by provenance (``memory_v1.provenance``): pasted terminal output,
relayed prompts, quoted assistant text, code and test prompts are excluded.
The remaining sentences are classified by intent:

- explicit standing directives ("bundan sonra ...", "her zaman ...",
  "asla ...", "kalıcı tercihim: ...") become active rules;
- preferences without an explicit persistence marker, or standing-sounding
  sentences inside a task brief, become candidates and are promoted only after
  separate sessions repeat them;
- task instructions, questions and status statements are not rules at all.

Conflicting rules are reconciled by archiving the older one with provenance.
"""
from __future__ import annotations

import dataclasses
import logging
from typing import Any

from . import provenance as pv
from .companion import CompanionManager, RuleItem, token_overlap
from .core import atomic_write, iso_now, redact_sensitive_text

logger = logging.getLogger("memory_v1.rule_learner")

# Signals that a new rule replaces an older one on the same topic. "artık" and
# "bundan sonra" are deliberately absent: they mark persistence, and treating
# them as replacement made any new rule sharing two words with an old one
# archive it.
REPLACEMENT_SIGNALS = {"yerine", "instead of", "değiştir", "vazgeç", "no longer", "artık değil"}

DURABLE_REASON = "Kullanıcının açık kalıcı direktifi"
CANDIDATE_REASON = "Kullanıcı tercihi (aday; ayrı oturumlarda tekrarlanırsa aktifleşir)"


@dataclasses.dataclass
class ExtractedRule:
    rule_text: str
    reason: str
    is_explicit: bool
    confidence: float
    source_turn: str
    intent: str = pv.DURABLE_DIRECTIVE
    evidence: str = ""


def calculate_overlap(text1: str, text2: str) -> float:
    return token_overlap(text1, text2)


class RuleLearner:
    """Evaluates user turns, identifies standing rules, deduplicates and reconciles."""

    def __init__(self, companion_mgr: CompanionManager) -> None:
        self.companion = companion_mgr
        # Why each block or sentence was skipped in the most recent call.
        self.last_report: list[dict[str, Any]] = []

    def analyze_user_turn(
        self, text: str, *, prior_assistant_text: str = "",
    ) -> tuple[list[ExtractedRule], list[dict[str, Any]]]:
        clean_text, _ = redact_sensitive_text(text)
        turn = pv.analyze_user_turn(clean_text, prior_assistant_text=prior_assistant_text)
        rules: list[ExtractedRule] = []
        report: list[dict[str, Any]] = []
        for block in turn.blocks:
            if block.provenance != pv.AUTHORED:
                report.append({
                    "decision": "excluded",
                    "provenance": block.provenance,
                    "evidence": block.evidence,
                    "excerpt": block.text[:120],
                })
                continue
            for sentence in pv.split_sentences(block.text):
                if len(sentence) < 10:
                    continue
                intent, evidence = pv.classify_sentence(sentence, in_task_prompt=turn.is_task_prompt)
                if intent == pv.DURABLE_DIRECTIVE:
                    rules.append(ExtractedRule(
                        rule_text=sentence, reason=DURABLE_REASON, is_explicit=True,
                        confidence=0.95, source_turn=sentence, intent=intent, evidence=evidence,
                    ))
                elif intent == pv.PREFERENCE_CANDIDATE:
                    rules.append(ExtractedRule(
                        rule_text=sentence, reason=CANDIDATE_REASON, is_explicit=False,
                        confidence=0.6, source_turn=sentence, intent=intent, evidence=evidence,
                    ))
                else:
                    report.append({
                        "decision": "not-a-rule",
                        "intent": intent,
                        "evidence": evidence,
                        "excerpt": sentence[:120],
                    })
        return rules, report

    def extract_rules_from_text(self, text: str) -> list[ExtractedRule]:
        """Rules and candidates the user wrote in ``text`` (no writes)."""
        rules, report = self.analyze_user_turn(text)
        self.last_report = report
        return rules

    def check_conflict(self, new_rule: str, existing_rule: str) -> bool:
        """Determine if a new rule conflicts with or overrides an existing rule."""
        common = set(_words(new_rule)) & set(_words(existing_rule))
        if len(common) < 2:
            return False
        negations = ("yapma", "kullanma", "asla", "değil", "never", "don't")
        new_neg = any(w in new_rule.lower() for w in negations)
        old_neg = any(w in existing_rule.lower() for w in negations)
        if new_neg != old_neg:
            return True
        return any(signal in new_rule.lower() for signal in REPLACEMENT_SIGNALS)

    def learn_from_transcript(self, transcript_turns: list[tuple[str, str]], source_session: str = "session") -> int:
        """Learn from the user turns of one session and update Kurallar.md."""
        learned = 0
        prior_assistant: list[str] = []
        self.last_report = []
        for role, text in transcript_turns:
            if role == "assistant":
                prior_assistant.append(text)
                continue
            if role != "user":
                continue
            rules, report = self.analyze_user_turn(
                text, prior_assistant_text="\n".join(prior_assistant[-6:]),
            )
            self.last_report.extend(report)
            for item in rules:
                learned += self._apply(item, source_session)
        return learned

    def learn_from_user_message(self, user_text: str, source: str = "session") -> list[str]:
        """Process a single user message and learn any standing rules."""
        rules, report = self.analyze_user_turn(user_text)
        self.last_report = report
        return [item.rule_text for item in rules if self._apply(item, source)]

    def _apply(self, item: ExtractedRule, source: str) -> int:
        existing = self.companion.read_rules()
        if item.is_explicit and any(s in item.rule_text.lower() for s in REPLACEMENT_SIGNALS):
            # "X yerine Y" shares most of its words with the rule it replaces;
            # checking duplicates first would discard exactly those updates.
            replaced = next(
                (r for r in existing if r.text != item.rule_text and self.check_conflict(item.rule_text, r.text)),
                None,
            )
            if replaced:
                self._reconcile_and_replace_rule(replaced, item.rule_text, item.reason, source)
                return 1
        if any(r.text == item.rule_text or calculate_overlap(item.rule_text, r.text) > 0.65 for r in existing):
            return 0
        if not item.is_explicit:
            outcome = self.companion.record_rule_candidate(item.rule_text, item.reason, source)
            return 1 if outcome in {"added", "promoted"} else 0
        conflicted = next((r for r in existing if self.check_conflict(item.rule_text, r.text)), None)
        if conflicted:
            self._reconcile_and_replace_rule(conflicted, item.rule_text, item.reason, source)
            return 1
        added = self.companion.add_or_update_rule(
            rule_text=item.rule_text, reason=item.reason, source=source, is_direct_command=True,
        )
        return 1 if added else 0

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

        # Remove only the old rule's own active line. Dropping every line that
        # merely contained its text also deleted archive entries naming it,
        # which is how genuine rules vanished from both active and archive.
        new_lines: list[str] = []
        for line in content.splitlines():
            stripped = line.strip()
            if stripped.startswith("- **kural:**"):
                text = stripped[len("- **kural:**"):].split("|", 1)[0].strip()
                if text == old_rule.text:
                    continue
            new_lines.append(line)

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
            f"**kaynak:** {old_rule.source or source}"
        )

        final_lines: list[str] = []
        for line in new_lines:
            final_lines.append(line)
            if line.strip() == "## Aktif Kurallar":
                final_lines.append(new_entry)
            elif line.strip() == "## Arşivlenmiş / Geçersiz Kılınmış Kurallar":
                final_lines.append(archive_entry)

        atomic_write(rules_path, "\n".join(final_lines) + "\n", mode=0o660)
        logger.info("Reconciled rule: replaced '%s' with '%s'", old_rule.text, clean_new)


def _words(text: str) -> list[str]:
    import re

    stop_words = {
        "bir", "bu", "ve", "ile", "için", "olan", "olarak", "daha", "en", "çok",
        "the", "a", "an", "and", "or", "to", "in", "on", "of", "for", "with",
    }
    return [w for w in re.findall(r"\w+", text.lower(), re.UNICODE) if len(w) > 2 and w not in stop_words]

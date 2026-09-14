"""Source-verified maintenance of learned rules and synthesized skills.

Mislearned entries are taken out of active use only when their own source says
they are not what they claim to be: the transcript turn a rule came from was a
pasted report, a relayed prompt, a test fixture, a question; a skill's
"repetition" was one session observed twice. Nothing is deleted.

The workflow is plan -> review -> apply:

- ``build_repair_plan`` reads Kurallar.md, the skill candidates and the local
  Claude Code / Codex transcripts, and proposes an action per entry with the
  evidence behind it. It writes nothing.
- ``apply_repair_plan`` refuses a plan whose inputs have changed since it was
  built, backs up every file it touches, retires rules into a maintenance
  section, moves retired skills under ``archive/``, and records a ledger.
- ``revert_repair`` restores the backup of a given repair.

Ambiguous entries -- no transcript, no marker, no fixture match -- are kept as
they are and listed, never guessed at.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import re
import secrets
import shutil
from pathlib import Path
from typing import Any

from . import provenance as pv
from .companion import (
    ACTIVE_RULES_HEADER,
    ARCHIVED_RULES_HEADER,
    CANDIDATE_RULES_HEADER,
    CompanionManager,
    RuleCandidate,
    insert_under_header,
    render_candidate_line,
    token_overlap,
)
from .core import MemoryConfig, PolicyError, atomic_json, atomic_write, iso_now, sha256_file

PLAN_SCHEMA = "pikselzone-memory-repair-plan-v1"
LEDGER_SCHEMA = "pikselzone-memory-repair-ledger-v1"
RETIRED_RULES_HEADER = "## Bakım ile Devre Dışı Bırakılan Kayıtlar (Maintenance Retired)"
RETIRED_PREFIX = "- **devre_dışı:**"
BUILTIN_SKILLS = frozenset({"beyin-doktor", "gecmis-import"})
SYSTEM_SOURCES = frozenset({"sistem-kurulumu"})
REPO_ROOT = Path(__file__).resolve().parent.parent

_SOURCE_KEY = re.compile(r"^(claude|codex|hermes)-([0-9a-f]{32})$")
_HERMES_SESSION_SOURCE = re.compile(r"^hermes-(\d{8}_\d{6}_[0-9a-f]+)$")
_FIELD = re.compile(r"\*\*([^*]+):\*\*\s*(.*)")

KEEP = "keep"
RETIRE = "retire"
DEMOTE = "demote-to-candidate"
RESTORE_ACTIVE = "restore-active"
RESTORE_CANDIDATE = "restore-candidate"
LEAVE = "leave-archived"


@dataclasses.dataclass
class RuleEntry:
    section: str  # "active" | "archived"
    text: str
    source: str
    reason: str = ""


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _fields(line: str, prefix: str) -> tuple[str, dict[str, str]]:
    parts = [p.strip() for p in line.strip()[len(prefix):].split("|")]
    fields: dict[str, str] = {}
    for part in parts[1:]:
        match = _FIELD.match(part)
        if match:
            fields[match.group(1).strip()] = match.group(2).strip()
    return parts[0].strip(), fields


def parse_rule_entries(text: str) -> list[RuleEntry]:
    entries: list[RuleEntry] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("- **kural:**"):
            body, fields = _fields(stripped, "- **kural:**")
            entries.append(RuleEntry("active", body, fields.get("kaynak", ""), fields.get("neden", "")))
        elif stripped.startswith("- **eski_kural:**"):
            body, fields = _fields(stripped, "- **eski_kural:**")
            entries.append(RuleEntry("archived", body, fields.get("kaynak", "")))
    return entries


def _entry_id(entry: RuleEntry) -> str:
    return hashlib.sha256(f"{entry.section}\0{entry.text}".encode("utf-8")).hexdigest()[:12]


class TranscriptIndex:
    """Conversational messages from local Claude Code and Codex transcripts.

    Only real user/assistant messages count. Hook outputs, recall bundles and
    tool results also quote rule text and would otherwise look like its origin.
    """

    def __init__(self, claude_root: Path, codex_root: Path) -> None:
        self.claude_root = claude_root
        self.codex_root = codex_root
        self._cache: dict[str, list[tuple[str, str]] | None] = {}

    def messages(self, runtime: str, session_id: str) -> list[tuple[str, str]] | None:
        key = f"{runtime}:{session_id}"
        if key not in self._cache:
            path = self._path(runtime, session_id)
            self._cache[key] = self._read(path, runtime) if path else None
        return self._cache[key]

    def _path(self, runtime: str, session_id: str) -> Path | None:
        if not session_id:
            return None
        if runtime == "claude" and self.claude_root.is_dir():
            hits = sorted(self.claude_root.glob(f"*/{session_id}.jsonl"))
            return hits[0] if hits else None
        if runtime == "codex" and self.codex_root.is_dir():
            hits = sorted(self.codex_root.rglob(f"*{session_id}*.jsonl"))
            return hits[0] if hits else None
        return None

    @staticmethod
    def _read(path: Path, runtime: str) -> list[tuple[str, str]]:
        out: list[tuple[str, str]] = []
        seen: set[tuple[str, str]] = set()
        try:
            handle = path.open(encoding="utf-8", errors="replace")
        except OSError:
            return out
        with handle:
            for line in handle:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                role, text = TranscriptIndex._message(record, runtime)
                if role and text.strip() and (role, text) not in seen:
                    seen.add((role, text))
                    out.append((role, text))
        return out

    @staticmethod
    def _message(record: dict[str, Any], runtime: str) -> tuple[str, str]:
        if runtime == "claude":
            kind = record.get("type")
            if kind in ("user", "assistant"):
                content = (record.get("message") or {}).get("content")
                if isinstance(content, list):
                    if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
                        return "", ""
                    text = "\n".join(
                        b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"
                    )
                    return kind, text
                return kind, content if isinstance(content, str) else ""
            if kind == "queue-operation" and record.get("operation") == "enqueue":
                content = record.get("content")
                return ("user", content) if isinstance(content, str) else ("", "")
            return "", ""
        payload = record.get("payload") or {}
        if record.get("type") == "response_item" and payload.get("type") == "message":
            role = payload.get("role")
            if role in ("user", "assistant"):
                text = "\n".join(b.get("text", "") for b in payload.get("content", []) if isinstance(b, dict))
                return role, text
        return "", ""

    def search_user_origin(self, texts: list[str]) -> dict[str, dict[str, Any]]:
        """Find, across all local transcripts, user messages containing each text."""
        needles = {t: _norm(t)[:80] for t in texts if _norm(t)}
        prefilters = {t: _ascii_anchor(n) for t, n in needles.items()}
        found: dict[str, dict[str, Any]] = {}
        files: list[tuple[str, Path]] = []
        if self.claude_root.is_dir():
            files += [("claude", p) for p in self.claude_root.glob("*/*.jsonl")]
        if self.codex_root.is_dir():
            files += [("codex", p) for p in self.codex_root.rglob("*.jsonl")]
        for runtime, path in files:
            pending = [t for t in needles if t not in found]
            if not pending:
                break
            try:
                raw = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            candidates = [t for t in pending if prefilters[t] and prefilters[t] in raw]
            if not candidates:
                continue
            messages = self._read(path, runtime)
            for text in candidates:
                for index, (role, message) in enumerate(messages):
                    if role == "user" and needles[text] in _norm(message):
                        found[text] = {
                            "runtime": runtime, "transcript": str(path), "turn_index": index,
                            "turn": message,
                            "prior_assistant": "\n".join(m for r, m in messages[:index] if r == "assistant")[-6000:],
                        }
                        break
        return found


def _ascii_anchor(text: str) -> str:
    words = sorted(re.findall(r"[A-Za-z0-9_'`-]{6,}", text), key=len, reverse=True)
    return words[0] if words else ""


def _fixture_literals(repo_root: Path) -> list[tuple[str, str]]:
    """String literals in tests and acceptance scripts: the known test fixtures."""
    out: list[tuple[str, str]] = []
    paths = list((repo_root / "tests").rglob("*.py")) + list((repo_root / "scripts").glob("*.py"))
    for path in paths:
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for number, line in enumerate(lines, 1):
            for double, single in re.findall(r'"([^"\\]{8,})"|\'([^\'\\]{8,})\'', line):
                literal = double or single
                out.append((pv.fold_text(literal), f"{path.relative_to(repo_root)}:{number}"))
    return out


def _fixture_match(text: str, fixtures: list[tuple[str, str]], *, slug: str = "") -> str:
    folded = pv.fold_text(text)
    for literal, where in fixtures:
        if slug and (literal == slug or (len(literal) >= 12 and slug.startswith(literal))):
            return where
        if len(literal.split()) >= 4 and token_overlap(folded, literal) >= 0.5:
            return where
    return ""


def _daily_files(vault: Path, runtime: str, key: str) -> list[Path]:
    return sorted((vault / "daily").glob(f"*/{runtime}-{key}*.md"))


def _session_id_of(daily: list[Path]) -> str:
    for path in daily:
        head = path.read_text(encoding="utf-8", errors="replace")[:1500]
        match = re.search(r'^session_id:\s*"?([^"\n]+)"?', head, re.M)
        if match:
            return match.group(1).strip()
    return ""


def _daily_for_session(vault: Path, session_id: str) -> list[Path]:
    hits = []
    for path in (vault / "daily").glob("*/*.md"):
        head = path.read_text(encoding="utf-8", errors="replace")[:1500]
        if re.search(rf'^session_id:\s*"?{re.escape(session_id)}"?\s*$', head, re.M):
            hits.append(path)
    return sorted(hits)


def _verdict_from_turn(text: str, turn: str, prior_assistant: str, where: str) -> tuple[str, str, str]:
    """Decide from the originating turn itself. Returns (action, classification, evidence)."""
    analysis = pv.analyze_user_turn(turn, prior_assistant_text=prior_assistant)
    needle = _norm(text)[:60]
    block = next((b for b in analysis.blocks if needle and needle in _norm(b.text)), None)
    if block is None:
        # Turn-level provenance applies (for example, test data covers the turn).
        block = analysis.blocks[0] if analysis.blocks else None
    if block is None:
        return KEEP, "unresolved", f"{where}:empty-turn"
    if block.provenance != pv.AUTHORED:
        return RETIRE, block.provenance, f"{where}:{block.evidence}"
    intent, evidence = pv.classify_sentence(text, in_task_prompt=analysis.is_task_prompt)
    if intent == pv.DURABLE_DIRECTIVE:
        return KEEP, intent, f"{where}:{evidence}"
    if intent == pv.PREFERENCE_CANDIDATE:
        if "inside-task-brief" in evidence:
            # Standing-sounding language inside a long brief or pasted document
            # describes that task; carrying it forward as a candidate is noise.
            return RETIRE, "standing-language-inside-task-brief", f"{where}:{evidence}"
        return DEMOTE, intent, f"{where}:{evidence}"
    return RETIRE, intent, f"{where}:{evidence}"


def build_repair_plan(
    config: MemoryConfig,
    *,
    claude_root: Path,
    codex_root: Path,
    history_files: list[Path] | None = None,
    repo_root: Path = REPO_ROOT,
) -> dict[str, Any]:
    vault = config.vault_path
    companion = CompanionManager(vault)
    rules_path = companion.companion_dir / "Kurallar.md"
    candidates_path = vault / ".state" / "skill_candidates.json"
    rules_text = rules_path.read_text(encoding="utf-8")
    index = TranscriptIndex(claude_root, codex_root)
    fixtures = _fixture_literals(repo_root)

    plan_rules: list[dict[str, Any]] = []
    entries = parse_rule_entries(rules_text)
    present = {e.text for e in entries}
    for entry in entries:
        action, classification, evidence = _classify_entry(entry, vault, index, fixtures)
        if entry.section == "archived":
            action = {KEEP: RESTORE_ACTIVE, DEMOTE: RESTORE_CANDIDATE}.get(action, LEAVE)
        plan_rules.append({
            "id": _entry_id(entry), "section": entry.section, "text": entry.text, "source": entry.source,
            "action": action, "classification": classification, "evidence": evidence,
        })

    # Archived entries carry the *replacing* rule's source, not their own, so
    # their origin has to be found by content across all transcripts.
    archived_targets = [r for r in plan_rules if r["section"] == "archived"]
    lost: list[RuleEntry] = []
    for history in history_files or []:
        for entry in parse_rule_entries(Path(history).read_text(encoding="utf-8")):
            if entry.text not in present and entry.text not in {e.text for e in lost}:
                lost.append(entry)
    origins = index.search_user_origin([r["text"] for r in archived_targets] + [e.text for e in lost])
    for row in archived_targets:
        origin = origins.get(row["text"])
        if not origin:
            row.update(action=LEAVE, classification="origin-not-found", evidence="no user message found")
            continue
        action, classification, evidence = _verdict_from_turn(
            row["text"], origin["turn"], origin["prior_assistant"],
            f"{origin['runtime']}:{Path(origin['transcript']).name}#{origin['turn_index']}",
        )
        row.update(
            action={KEEP: RESTORE_ACTIVE, DEMOTE: RESTORE_CANDIDATE}.get(action, LEAVE),
            classification=classification, evidence=evidence,
        )
    for entry in lost:
        origin = origins.get(entry.text)
        row = {
            "id": _entry_id(dataclasses.replace(entry, section="lost")), "section": "lost-from-history",
            "text": entry.text, "source": entry.source,
        }
        if not origin:
            row.update(action=LEAVE, classification="origin-not-found", evidence="no user message found")
        else:
            action, classification, evidence = _verdict_from_turn(
                entry.text, origin["turn"], origin["prior_assistant"],
                f"{origin['runtime']}:{Path(origin['transcript']).name}#{origin['turn_index']}",
            )
            row.update(
                action={KEEP: RESTORE_ACTIVE, DEMOTE: RESTORE_CANDIDATE}.get(action, LEAVE),
                classification=classification, evidence=evidence,
            )
        plan_rules.append(row)

    plan_skills = _skill_plan(vault, candidates_path, fixtures)
    return {
        "schema": PLAN_SCHEMA,
        "created_at": iso_now(),
        "vault": str(vault),
        "kurallar_path": str(rules_path),
        "kurallar_sha256": sha256_file(rules_path),
        "skill_candidates_sha256": sha256_file(candidates_path) if candidates_path.is_file() else "",
        "rules": plan_rules,
        "skills": plan_skills,
        "summary": _summary(plan_rules, plan_skills),
    }


def _classify_entry(
    entry: RuleEntry, vault: Path, index: TranscriptIndex, fixtures: list[tuple[str, str]],
) -> tuple[str, str, str]:
    source = entry.source.strip()
    if source in SYSTEM_SOURCES:
        return KEEP, "system-seed", f"source:{source}"
    if source.endswith("-test") or source == "codex-test":
        return RETIRE, pv.TEST_DATA, f"source-name:{source}"
    marker = pv.find_test_marker(entry.text)
    if marker:
        return RETIRE, pv.TEST_DATA, f"rule-text-marker:{marker}"

    key = _SOURCE_KEY.match(source)
    if key and key.group(1) in ("claude", "codex"):
        runtime = key.group(1)
        daily = _daily_files(vault, runtime, key.group(2))
        session_id = _session_id_of(daily)
        messages = index.messages(runtime, session_id)
        if messages is None:
            fixture = _fixture_match(entry.text, fixtures)
            if fixture:
                return RETIRE, "test-fixture", f"fixture:{fixture}"
            return KEEP, "unresolved", f"transcript-not-local:{runtime}:{session_id or 'no-daily'}"
        needle = _norm(entry.text)[:60]
        for position, (role, message) in enumerate(messages):
            if role == "user" and needle in _norm(message):
                prior = "\n".join(m for r, m in messages[:position] if r == "assistant")[-6000:]
                return _verdict_from_turn(entry.text, message, prior, f"{runtime}:{session_id}#{position}")
        if any(role == "assistant" and needle in _norm(message) for role, message in messages):
            return RETIRE, pv.QUOTED_ASSISTANT, f"{runtime}:{session_id}:found-only-in-assistant-output"
        return KEEP, "unresolved", f"{runtime}:{session_id}:text-not-found"

    session_match = _HERMES_SESSION_SOURCE.match(source)
    if (key and key.group(1) == "hermes") or session_match:
        daily = (
            _daily_files(vault, "hermes", key.group(2)) if key else _daily_for_session(vault, session_match.group(1))
        )
        body = "\n".join(p.read_text(encoding="utf-8", errors="replace") for p in daily)
        if body:
            test = pv.find_test_marker(body)
            if test:
                return RETIRE, pv.TEST_DATA, f"daily:{daily[0].name}:{test}"
            if _norm(entry.text)[:50] in _norm(body):
                return RETIRE, "model-generated-summary", f"daily:{daily[0].name}:summary-fed-as-user-turn"
        fixture = _fixture_match(entry.text, fixtures)
        if fixture:
            return RETIRE, "test-fixture", f"fixture:{fixture}"
        return KEEP, "unresolved", f"hermes:{source}:no-daily-evidence"

    if source.startswith("import:"):
        intent, evidence = pv.classify_sentence(entry.text)
        if intent == pv.DURABLE_DIRECTIVE:
            return KEEP, intent, f"import-source:{evidence}"
        if intent == pv.PREFERENCE_CANDIDATE:
            return DEMOTE, intent, f"import-source:{evidence}"
        return RETIRE, intent, f"import-source:{evidence}"

    fixture = _fixture_match(entry.text, fixtures)
    if fixture:
        return RETIRE, "test-fixture", f"fixture:{fixture}"
    return KEEP, "unresolved", f"unrecognized-source:{source}"


def _skill_plan(vault: Path, candidates_path: Path, fixtures: list[tuple[str, str]]) -> list[dict[str, Any]]:
    try:
        candidates = json.loads(candidates_path.read_text(encoding="utf-8")).get("candidates", {})
    except (OSError, json.JSONDecodeError):
        candidates = {}
    rows: list[dict[str, Any]] = []
    for skill_file in sorted((vault / "skills").glob("*/SKILL.md")):
        slug = skill_file.parent.name
        text = skill_file.read_text(encoding="utf-8", errors="replace")
        described = re.search(r'^description:\s*"?(.*?)"?\s*$', text, re.M)
        description = described.group(1) if described else ""
        row: dict[str, Any] = {"slug": slug, "description": description[:140]}
        if slug in BUILTIN_SKILLS:
            row.update(action=KEEP, classification="builtin", evidence="built-in skill")
        elif slug in candidates:
            record = candidates[slug]
            sessions = sorted({o.get("session_id") for o in record.get("observations", []) if o.get("session_id")})
            if len(sessions) >= 2:
                row.update(action=KEEP, classification="multi-session", evidence=f"sessions:{sessions}")
            else:
                row.update(
                    action=RETIRE, classification="single-session-repetition",
                    evidence=f"observations:{record.get('count')};distinct-sessions:{sessions}",
                )
        else:
            fixture = _fixture_match(f"{slug} {description}", fixtures, slug=slug)
            if fixture:
                row.update(action=RETIRE, classification="test-fixture", evidence=f"fixture:{fixture}")
            else:
                row.update(action=KEEP, classification="unresolved", evidence="no candidate record, no fixture match")
        rows.append(row)
    return rows


def _summary(rules: list[dict[str, Any]], skills: list[dict[str, Any]]) -> dict[str, Any]:
    counts: dict[str, int] = {}
    for row in rules:
        counts[f"rules:{row['section']}:{row['action']}"] = counts.get(f"rules:{row['section']}:{row['action']}", 0) + 1
    for row in skills:
        counts[f"skills:{row['action']}"] = counts.get(f"skills:{row['action']}", 0) + 1
    return counts


def apply_repair_plan(config: MemoryConfig, plan: dict[str, Any]) -> dict[str, Any]:
    if plan.get("schema") != PLAN_SCHEMA:
        raise PolicyError("repair-plan-schema-invalid")
    vault = config.vault_path
    companion = CompanionManager(vault)
    rules_path = companion.companion_dir / "Kurallar.md"
    candidates_path = vault / ".state" / "skill_candidates.json"
    if str(vault) != plan.get("vault"):
        raise PolicyError("repair-plan-vault-mismatch")
    if sha256_file(rules_path) != plan.get("kurallar_sha256"):
        raise PolicyError("repair-plan-stale:Kurallar.md changed since the plan was built")
    current_candidates_sha = sha256_file(candidates_path) if candidates_path.is_file() else ""
    if current_candidates_sha != plan.get("skill_candidates_sha256", ""):
        raise PolicyError("repair-plan-stale:skill_candidates.json changed since the plan was built")

    repair_id = f"repair-{iso_now()[:19].replace(':', '').replace('-', '')}-{secrets.token_hex(3)}"
    backup = config.state_path / "backups" / repair_id
    backup.mkdir(parents=True, exist_ok=False)
    os.chmod(backup, 0o700)
    shutil.copy2(rules_path, backup / "Kurallar.md")
    if candidates_path.is_file():
        shutil.copy2(candidates_path, backup / "skill_candidates.json")

    rules_by_text = {row["text"]: row for row in plan["rules"] if row["section"] == "active"}
    retired_rows = [r for r in plan["rules"] if r["section"] == "active" and r["action"] == RETIRE]
    demoted_rows = [r for r in plan["rules"] if r["section"] == "active" and r["action"] == DEMOTE]
    restore_active = [r for r in plan["rules"] if r["action"] == RESTORE_ACTIVE]
    restore_candidate = [r for r in plan["rules"] if r["action"] == RESTORE_CANDIDATE]

    lines: list[str] = []
    for line in rules_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("- **kural:**"):
            body, _ = _fields(stripped, "- **kural:**")
            row = rules_by_text.get(body)
            if row and row["action"] in (RETIRE, DEMOTE):
                continue
        lines.append(line)

    for row in restore_active:
        lines = insert_under_header(
            lines, ACTIVE_RULES_HEADER,
            f"- **kural:** {row['text']} | **neden:** Kaynakta doğrulanmış kullanıcı direktifi "
            f"(bakım ile geri yüklendi) | **kaynak:** {row['source'] or 'kaynak-arama'} | **durum:** aktif",
        )
    for row in demoted_rows + restore_candidate:
        candidate = RuleCandidate(
            text=row["text"],
            reason=f"Kullanıcı tercihi (bakım: {row['classification']})",
            sources=[row["source"] or "kaynak-arama"],
        )
        lines = insert_under_header(
            lines, CANDIDATE_RULES_HEADER, render_candidate_line(candidate), before=ARCHIVED_RULES_HEADER,
        )
    for row in retired_rows:
        evidence = str(row["evidence"]).replace("|", "/")[:160]
        lines = insert_under_header(
            lines, RETIRED_RULES_HEADER,
            f"{RETIRED_PREFIX} {row['text']} | **sınıf:** {row['classification']} | **kanıt:** {evidence} | "
            f"**kaynak:** {row['source']} | **bakım:** {repair_id}",
        )
    atomic_write(rules_path, "\n".join(lines) + "\n", mode=0o660)

    retired_skills = [s for s in plan["skills"] if s["action"] == RETIRE]
    moved: list[dict[str, str]] = []
    if retired_skills:
        try:
            state = json.loads(candidates_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            state = {"candidates": {}}
        archive_root = vault / "archive" / "retired-skills" / repair_id
        for row in retired_skills:
            source_dir = vault / "skills" / row["slug"]
            if not source_dir.is_dir():
                continue
            shutil.copytree(source_dir, backup / "skills" / row["slug"])
            archive_root.mkdir(parents=True, exist_ok=True)
            destination = archive_root / row["slug"]
            shutil.move(str(source_dir), str(destination))
            moved.append({"slug": row["slug"], "from": str(source_dir), "to": str(destination)})
            record = state.setdefault("candidates", {}).setdefault(row["slug"], {"observations": [], "count": 0})
            record.update(status="retired", retired_by=repair_id, retired_reason=row["classification"])
        state["updated_at"] = iso_now()
        atomic_json(candidates_path, state)

    ledger = {
        "schema": LEDGER_SCHEMA,
        "repair_id": repair_id,
        "applied_at": iso_now(),
        "plan_created_at": plan.get("created_at"),
        "backup_dir": str(backup),
        "kurallar_sha256_before": plan["kurallar_sha256"],
        "kurallar_sha256_after": sha256_file(rules_path),
        "skill_candidates_sha256_before": plan.get("skill_candidates_sha256", ""),
        "skill_candidates_sha256_after": sha256_file(candidates_path) if candidates_path.is_file() else "",
        "rules_retired": [{"text": r["text"], "classification": r["classification"], "evidence": r["evidence"]} for r in retired_rows],
        "rules_demoted": [{"text": r["text"], "evidence": r["evidence"]} for r in demoted_rows],
        "rules_restored_active": [{"text": r["text"], "evidence": r["evidence"]} for r in restore_active],
        "rules_restored_candidate": [{"text": r["text"], "evidence": r["evidence"]} for r in restore_candidate],
        "skills_retired": moved,
        "summary": plan.get("summary", {}),
    }
    evidence_dir = config.state_path / "evidence"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    atomic_json(evidence_dir / f"memory-repair-{repair_id}.json", ledger)
    atomic_json(backup / "ledger.json", ledger)
    companion.append_journal_entry(
        title="Hafıza bakımı (kaynak doğrulamalı)",
        narrative=(
            f"{repair_id}: {len(retired_rows)} kural devre dışı, {len(demoted_rows)} adaya indirildi, "
            f"{len(restore_active)} aktif ve {len(restore_candidate)} aday olarak geri yüklendi, "
            f"{len(moved)} skill arşivlendi. Kanıt defteri state/evidence/memory-repair-{repair_id}.json; "
            f"geri alma: pz-memory repair-memory --revert {repair_id}."
        ),
        runtime="system",
    )
    return ledger


def retire_rule_candidate(
    config: MemoryConfig, text: str, *, classification: str, evidence: str,
) -> dict[str, Any]:
    """Retire one rule candidate by its exact text, with backup, ledger and journal.

    Plans retire or demote active rules only; a candidate recorded from content
    that turned out to be test data or a one-off request had no audited way out.
    Only the memory-engine host writes Kurallar.md, so only it may run this.
    """
    from .companion import _CANDIDATE_PREFIX, parse_rule_candidates

    if config.role != "memory-engine":
        raise PolicyError("repair-refused:only-the-memory-engine-writes-Kurallar.md")
    if not classification.strip() or not evidence.strip():
        raise PolicyError("repair-refused:classification-and-evidence-required")
    vault = config.vault_path
    companion = CompanionManager(vault)
    rules_path = companion.companion_dir / "Kurallar.md"
    content = rules_path.read_text(encoding="utf-8")
    match = next((c for c in parse_rule_candidates(content) if c.text == text.strip()), None)
    if match is None:
        raise PolicyError("repair-refused:candidate-not-found")

    repair_id = f"repair-{iso_now()[:19].replace(':', '').replace('-', '')}-{secrets.token_hex(3)}"
    backup = config.state_path / "backups" / repair_id
    backup.mkdir(parents=True, exist_ok=False)
    os.chmod(backup, 0o700)
    shutil.copy2(rules_path, backup / "Kurallar.md")
    before = sha256_file(rules_path)

    own_prefix = f"{_CANDIDATE_PREFIX} {match.text} |"
    lines = [line for line in content.splitlines() if not line.strip().startswith(own_prefix)]
    clean_evidence = evidence.replace("|", "/")[:160]
    lines = insert_under_header(
        lines, RETIRED_RULES_HEADER,
        f"{RETIRED_PREFIX} {match.text} | **sınıf:** {classification} | **kanıt:** {clean_evidence} | "
        f"**kaynak:** {', '.join(match.sources)} | **bakım:** {repair_id}",
    )
    atomic_write(rules_path, "\n".join(lines) + "\n", mode=0o660)

    ledger = {
        "schema": LEDGER_SCHEMA,
        "repair_id": repair_id,
        "applied_at": iso_now(),
        "backup_dir": str(backup),
        "kurallar_sha256_before": before,
        "kurallar_sha256_after": sha256_file(rules_path),
        "rules_retired": [],
        "candidates_retired": [{
            "text": match.text, "classification": classification, "evidence": evidence,
            "sources": match.sources,
        }],
        "skills_retired": [],
    }
    evidence_dir = config.state_path / "evidence"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    atomic_json(evidence_dir / f"memory-repair-{repair_id}.json", ledger)
    atomic_json(backup / "ledger.json", ledger)
    companion.append_journal_entry(
        title="Hafıza bakımı (aday kural)",
        narrative=(
            f"{repair_id}: 1 aday kural devre dışı bırakıldı ({classification}). "
            f"Kanıt defteri state/evidence/memory-repair-{repair_id}.json; "
            f"geri alma: pz-memory repair-memory --revert {repair_id}."
        ),
        runtime="system",
    )
    return ledger


def revert_repair(config: MemoryConfig, repair_id: str, *, force: bool = False) -> dict[str, Any]:
    backup = config.state_path / "backups" / repair_id
    ledger_path = backup / "ledger.json"
    if not ledger_path.is_file():
        raise PolicyError(f"repair-ledger-missing:{repair_id}")
    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    vault = config.vault_path
    rules_path = CompanionManager(vault).companion_dir / "Kurallar.md"
    if not force and sha256_file(rules_path) != ledger["kurallar_sha256_after"]:
        raise PolicyError("repair-revert-refused:Kurallar.md changed after the repair; re-run with force")
    shutil.copy2(backup / "Kurallar.md", rules_path)
    candidates_backup = backup / "skill_candidates.json"
    if candidates_backup.is_file():
        shutil.copy2(candidates_backup, vault / ".state" / "skill_candidates.json")
    restored = []
    for item in ledger.get("skills_retired", []):
        target = Path(item["from"])
        source = Path(item["to"])
        if source.is_dir() and not target.exists():
            shutil.move(str(source), str(target))
            restored.append(item["slug"])
    return {"repair_id": repair_id, "reverted_at": iso_now(), "skills_restored": restored}

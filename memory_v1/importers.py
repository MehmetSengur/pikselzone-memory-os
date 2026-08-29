"""Multi-Format History Import & Second-Brain Distillation Engine (SB2-11).

Supports importing past conversations into the second brain:
- Claude export JSON & transcript JSONL
- ChatGPT conversations.json
- Codex rollout JSONL (legacy and modern)
- Gemini takeout JSON
- Raw Markdown chat transcripts

Pipeline:
1. Parse & Normalize: extract chronological (user, assistant) turns.
2. Filter & Redact: discard trivial chit-chat, redact secrets and tokens.
3. Distill: extract rules -> Kurallar.md, concepts -> knowledge/, workflows -> skills/.
4. Receipt & Audit: generate signed audit receipt detailing extracted items.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import json
import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .companion import CompanionManager
from .core import MemoryConfig, PolicyError, atomic_json, atomic_write, iso_now, sha256_bytes
from .graph_engine import ConceptData, KnowledgeGraphEngine
from .rule_learner import RuleLearner
from .skill_engine import SkillEngine, WorkflowObservation

logger = logging.getLogger("memory_v1.importers")

SECRET_REGEX = re.compile(
    r"(?i)(sk-[A-Za-z0-9_-]{20,}|ghp_[A-Za-z0-9]{36,}|"
    r"Bearer\s+[A-Za-z0-9._-]{20,}|AIzaSy[A-Za-z0-9_-]{33})"
)


@dataclasses.dataclass
class ConversationTurn:
    role: str  # "user" or "assistant"
    text: str


@dataclasses.dataclass
class ImportedSession:
    source_format: str
    source_id: str
    title: str
    turns: list[ConversationTurn] = dataclasses.field(default_factory=list)


@dataclasses.dataclass
class ImportReceipt:
    schema: str = "pikselzone-history-import-receipt-v1"
    source_format: str = ""
    source_path: str = ""
    timestamp: str = ""
    total_sessions_found: int = 0
    sessions_imported: int = 0
    rules_extracted: int = 0
    concepts_extracted: int = 0
    skills_updated: int = 0
    receipt_sha256: str = ""


class HistoryImportEngine:
    """Parses, redacts, distills, and commits external conversation histories into the second brain."""

    def __init__(self, config: MemoryConfig) -> None:
        self.config = config
        self.vault_path = config.vault_path
        self.state_path = config.state_path
        self.companion = CompanionManager(self.vault_path)
        self.graph = KnowledgeGraphEngine(self.vault_path)
        self.rules = RuleLearner(self.companion)
        self.skills = SkillEngine(self.vault_path)

    def import_file(self, file_path: Path, source_format: Optional[str] = None) -> ImportReceipt:
        """Auto-detect format (or use specified) and import into the second brain."""
        path = file_path.resolve()
        if not path.is_file():
            raise PolicyError(f"import-source-not-found:{path}")

        fmt = source_format or self._detect_format(path)
        sessions = self._parse_file(path, fmt)

        return self._distill_and_commit(sessions, source_format=fmt, source_path=str(path))

    def _detect_format(self, path: Path) -> str:
        name = path.name.lower()
        if name.endswith(".jsonl"):
            return "codex"
        if name.endswith(".md"):
            return "markdown"
        if name == "conversations.json":
            return "chatgpt"

        try:
            with open(path, "r", encoding="utf-8") as f:
                head = f.read(2048)
                if '"mapping":' in head:
                    return "chatgpt"
                if '"chat_messages":' in head or '"chat_messages_count":' in head:
                    return "claude"
                if '"candidates":' in head or '"contents":' in head:
                    return "gemini"
        except Exception:
            pass

        return "markdown"

    def _parse_file(self, path: Path, fmt: str) -> list[ImportedSession]:
        content = path.read_text(encoding="utf-8", errors="replace")

        if fmt == "chatgpt":
            return self._parse_chatgpt(content)
        elif fmt == "claude":
            return self._parse_claude(content)
        elif fmt == "codex":
            return self._parse_codex(content)
        elif fmt == "gemini":
            return self._parse_gemini(content)
        elif fmt == "markdown":
            return self._parse_markdown(content, source_id=path.stem)
        else:
            raise PolicyError(f"unsupported-import-format:{fmt}")

    def _parse_chatgpt(self, content: str) -> list[ImportedSession]:
        sessions: list[ImportedSession] = []
        try:
            data = json.loads(content)
            if not isinstance(data, list):
                data = [data]
        except Exception:
            return sessions

        for item in data:
            if not isinstance(item, dict):
                continue
            sess_id = item.get("id") or item.get("conversation_id") or "chatgpt-sess"
            title = item.get("title") or "ChatGPT Conversation"
            mapping = item.get("mapping", {})

            turns: list[ConversationTurn] = []
            for node_id, node in mapping.items():
                if not isinstance(node, dict):
                    continue
                msg = node.get("message")
                if not isinstance(msg, dict):
                    continue
                author = msg.get("author", {})
                role = author.get("role")
                if role not in {"user", "assistant"}:
                    continue
                content_obj = msg.get("content", {})
                parts = content_obj.get("parts", [])
                text_parts = [str(p) for p in parts if isinstance(p, (str, int, float))]
                full_text = "\n".join(text_parts).strip()
                if full_text:
                    turns.append(ConversationTurn(role=role, text=full_text))

            if turns:
                sessions.append(ImportedSession(source_format="chatgpt", source_id=sess_id, title=title, turns=turns))
        return sessions

    def _parse_claude(self, content: str) -> list[ImportedSession]:
        sessions: list[ImportedSession] = []
        try:
            data = json.loads(content)
            if not isinstance(data, list):
                data = [data]
        except Exception:
            return sessions

        for item in data:
            if not isinstance(item, dict):
                continue
            sess_id = item.get("uuid") or item.get("id") or "claude-sess"
            title = item.get("name") or "Claude Conversation"
            chat_messages = item.get("chat_messages", [])

            turns: list[ConversationTurn] = []
            for m in chat_messages:
                if not isinstance(m, dict):
                    continue
                sender = m.get("sender")
                role = "user" if sender == "human" else "assistant"
                text = m.get("text", "").strip()
                if text:
                    turns.append(ConversationTurn(role=role, text=text))

            if turns:
                sessions.append(ImportedSession(source_format="claude", source_id=sess_id, title=title, turns=turns))
        return sessions

    def _parse_codex(self, content: str) -> list[ImportedSession]:
        from .transcripts import parse_codex_transcript
        turns_data = parse_codex_transcript(content)
        turns = [ConversationTurn(role=t.role, text=t.text) for t in turns_data]
        if not turns:
            return []
        return [ImportedSession(source_format="codex", source_id="codex-import", title="Codex Rollout Session", turns=turns)]

    def _parse_gemini(self, content: str) -> list[ImportedSession]:
        sessions: list[ImportedSession] = []
        try:
            data = json.loads(content)
            if not isinstance(data, list):
                data = [data]
        except Exception:
            return sessions

        for item in data:
            if not isinstance(item, dict):
                continue
            sess_id = item.get("id") or "gemini-sess"
            title = item.get("title") or "Gemini Conversation"
            contents = item.get("contents") or item.get("messages") or []

            turns: list[ConversationTurn] = []
            for c in contents:
                if not isinstance(c, dict):
                    continue
                role = "user" if c.get("role") == "user" else "assistant"
                parts = c.get("parts", [])
                text_parts = [p.get("text", "") if isinstance(p, dict) else str(p) for p in parts]
                text = "\n".join(text_parts).strip()
                if text:
                    turns.append(ConversationTurn(role=role, text=text))

            if turns:
                sessions.append(ImportedSession(source_format="gemini", source_id=sess_id, title=title, turns=turns))
        return sessions

    def _parse_markdown(self, content: str, source_id: str = "markdown-import") -> list[ImportedSession]:
        turns: list[ConversationTurn] = []
        pattern = re.compile(r"^(?:#{1,3}\s*(User|Assistant|Human|Claude|AI|ChatGPT|Codex)):\s*\n?(.*?)(?=(?:^#{1,3}\s*(?:User|Assistant|Human|Claude|AI|ChatGPT|Codex):|\Z))", re.M | re.S | re.I)

        matches = list(pattern.finditer(content))
        if matches:
            for m in matches:
                speaker = m.group(1).lower()
                text = m.group(2).strip()
                role = "user" if speaker in {"user", "human"} else "assistant"
                if text:
                    turns.append(ConversationTurn(role=role, text=text))
        else:
            # Fallback simple alternating blocks
            lines = content.splitlines()
            current_role = "user"
            buffer = []
            for line in lines:
                if line.lower().startswith("user:") or line.lower().startswith("human:"):
                    if buffer:
                        turns.append(ConversationTurn(role=current_role, text="\n".join(buffer).strip()))
                        buffer = []
                    current_role = "user"
                elif line.lower().startswith("assistant:") or line.lower().startswith("ai:"):
                    if buffer:
                        turns.append(ConversationTurn(role=current_role, text="\n".join(buffer).strip()))
                        buffer = []
                    current_role = "assistant"
                else:
                    buffer.append(line)
            if buffer:
                turns.append(ConversationTurn(role=current_role, text="\n".join(buffer).strip()))

        if not turns:
            return []
        return [ImportedSession(source_format="markdown", source_id=source_id, title="Markdown Transcript", turns=turns)]

    def _distill_and_commit(
        self, sessions: list[ImportedSession], source_format: str, source_path: str
    ) -> ImportReceipt:
        now_str = iso_now()
        rules_count = 0
        concepts_count = 0
        skills_count = 0
        imported_sessions = 0

        for sess in sessions:
            # 1. Filter out trivial / greeting sessions
            total_chars = sum(len(t.text) for t in sess.turns)
            if len(sess.turns) < 2 or total_chars < 50:
                continue

            # Redact secrets across all turns
            clean_turns = []
            for t in sess.turns:
                clean_text = SECRET_REGEX.sub("[REDACTED_SECRET]", t.text)
                clean_turns.append(ConversationTurn(role=t.role, text=clean_text))

            user_texts = [t.text for t in clean_turns if t.role == "user"]
            asst_texts = [t.text for t in clean_turns if t.role == "assistant"]

            # 2. Extract rules from user directives
            for u_text in user_texts:
                learned = self.rules.learn_from_user_message(u_text, source=f"import:{source_format}:{sess.source_id}")
                if learned:
                    rules_count += len(learned)

            # 3. Extract technical entities / concepts from assistant content
            combined_asst = " ".join(asst_texts)
            detected_entities = re.findall(r"\b[A-Z][a-zA-Z0-9_\-\.]{2,}\b", combined_asst)
            tech_whitelist = {
                "FastAPI", "Redis", "Docker", "PostgreSQL", "Next.js", "Celery",
                "Kubernetes", "Tailwind", "RabbitMQ", "Kafka", "Elasticsearch",
                "Pytest", "ClickHouse", "Supabase", "Prisma", "Sentry", "Traefik",
            }
            valid_techs = {e for e in detected_entities if e in tech_whitelist}

            for tech in valid_techs:
                # Add concept note
                self.graph.add_or_update_concept(ConceptData(
                    title=tech,
                    summary=f"Teknik bileşen: {tech} mimarisi ve kullanımı.",
                    details=[f"Geçmiş sohbetten aktarıldı: {sess.title}"],
                    sources=[f"import:{source_format}:{sess.source_id}"],
                ))
                concepts_count += 1

            # 4. Extract repeated workflow patterns for skill candidates
            for u_text in user_texts:
                if any(kw in u_text.lower() for kw in ["nasıl yapılır", "adımlar", "süreci çalıştır", "deploy et"]):
                    self.skills.record_workflow(WorkflowObservation(
                        workflow_name=sess.title[:40],
                        trigger=u_text[:80],
                        steps=[t.text[:120] for t in asst_texts[:3]],
                    ))
                    skills_count += 1

            imported_sessions += 1

        # 5. Build signed receipt
        receipt_data = {
            "schema": "pikselzone-history-import-receipt-v1",
            "source_format": source_format,
            "source_path": source_path,
            "timestamp": now_str,
            "total_sessions_found": len(sessions),
            "sessions_imported": imported_sessions,
            "rules_extracted": rules_count,
            "concepts_extracted": concepts_count,
            "skills_updated": skills_count,
        }
        encoded = json.dumps(receipt_data, sort_keys=True).encode("utf-8")
        receipt_sha = sha256_bytes(encoded)
        receipt_data["receipt_sha256"] = receipt_sha

        imports_dir = self.state_path / "imports"
        imports_dir.mkdir(parents=True, exist_ok=True)
        ts_slug = re.sub(r"[^\w]", "-", now_str)
        receipt_file = imports_dir / f"import-receipt-{ts_slug}.json"
        atomic_json(receipt_file, receipt_data)

        logger.info(
            "Imported %d/%d sessions from %s (rules=%d, concepts=%d, skills=%d)",
            imported_sessions, len(sessions), source_format, rules_count, concepts_count, skills_count
        )

        return ImportReceipt(
            source_format=source_format,
            source_path=source_path,
            timestamp=now_str,
            total_sessions_found=len(sessions),
            sessions_imported=imported_sessions,
            rules_extracted=rules_count,
            concepts_extracted=concepts_count,
            skills_updated=skills_count,
            receipt_sha256=receipt_sha,
        )

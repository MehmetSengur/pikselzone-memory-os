"""Second-Brain Companion Memory Schema and Management Layer.

Manages living, autonomous second-brain documents in the Obsidian vault:
- Core.md: Identity, user model, working style, key entities, tools
- Kurallar.md: Learned rules, preferences, candidates, conflict reconciliation
- Last-Session.md: Operational continuity across sessions and runtimes
- Threads.md: Multi-session active, paused, blocked, and resolved topics
- Journal.md: High-level narrative reflections and episodic memory
- Threads-Archive.md: Archive for closed/resolved threads
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .core import (
    PolicyError,
    SchemaError,
    atomic_write,
    ensure_safe_directory,
    iso_now,
    redact_sensitive_text,
    secure_read_text,
    sha256_bytes,
)

logger = logging.getLogger("memory_v1.companion")


@dataclasses.dataclass
class RuleItem:
    text: str
    reason: str = ""
    source: str = ""
    status: str = "active"  # "active", "candidate", "archived"
    observations: int = 1
    updated_at: str = ""


@dataclasses.dataclass
class LastSessionData:
    runtime: str
    session_id: str
    completed_items: list[str] = dataclasses.field(default_factory=list)
    decisions: list[str] = dataclasses.field(default_factory=list)
    pending_items: list[str] = dataclasses.field(default_factory=list)
    next_steps: list[str] = dataclasses.field(default_factory=list)
    active_project: str = ""
    user_questions: list[str] = dataclasses.field(default_factory=list)
    updated_at: str = ""


@dataclasses.dataclass
class ThreadItem:
    thread_id: str
    title: str
    status: str = "active"  # "active", "paused", "blocked", "resolved"
    context: str = ""
    open_items: list[str] = dataclasses.field(default_factory=list)
    blockers: str = ""
    created_at: str = ""
    updated_at: str = ""


DEFAULT_CORE_SEED = """---
type: companion-core
version: 2.0
updated_at: {now}
---

# İkinci Beyin — Çekirdek Kimlik ve Kullanıcı Modeli

## Kullanıcı Kimliği & Profil
- **Kullanıcı:** Mehmet Emin Şengür (Pikselzone)
- **Rol:** Kurucu / Sistem Mimarı / Mühendis
- **Şirketler & Projeler:** Pikselzone, Hermes AI OS, Twoberries, Gemkal, SEO/GEO A8

## Çalışma Tarzı & İletişim Tercihleri
- **İletişim:** Net, teknik, eyleme dönük, gereksiz özet ve formaliteden uzak.
- **Karar Verme:** Güvenlik sınırları (ödeme, credential, prod mutation) hariç otonomi default.
- **Kod & Mimari:** Temiz, modüler, test güdümlü, regression-proof.

## Sık Kullanılan Araçlar & Ortamlar
- **Runtimes:** Claude Code (subscription CLI), Codex (subscription CLI), Hermes Agent (PluginLlm)
- **Hafıza & Vault:** Obsidian (ortak yerel ve VPS senkronizasyonu)
- **Platform:** macOS, Linux (VPS), zsh, Python 3 stdlib

## Uzun Vadeli Hedefler & İlkeler
- **İkinci Beyin:** Kendi hafızasını büyüten, kurallar çıkaran, skill üreten yaşayan AI OS.
- **Güvenilirlik:** Hata durumunda agent'ı kilitlemeyen, graceful degraded çalışan sistemler.

## AI Partner Rolü & Davranış İlkeleri
- Proaktif yardım, gereksiz izin istemeden memory ve knowledge katmanlarını güncelleme.
- Güvenlik sınırlarını ödünsüz koruma.
"""

DEFAULT_RULES_SEED = """---
type: companion-rules
version: 2.0
updated_at: {now}
---

# İkinci Beyin — Kurallar ve Öğrenilmiş Tercihler

## Aktif Kurallar
- **kural:** Normal second-brain ve memory işlemlerinde otonomi varsayılandır; her küçük değişiklik için onay isteme. | **neden:** Kullanıcıyı mikro-yönetimle boğmamak ve ikinci beyni yaşayan kılmak. | **kaynak:** sistem-kurulumu | **durum:** aktif
- **kural:** API anahtarları, şifreler, ödeme işlemleri, prod veritabanı silme ve dış dünyaya mesaj gönderme işlemlerinde kesinlikle dur ve izin iste. | **neden:** Telafisi olmayan güvenlik ve finansal riskleri önlemek. | **kaynak:** sistem-kurulumu | **durum:** aktif
- **kural:** Bir hafıza alt bileşeni hata verirse ana oturumu durdurma; degraded çalış, health state üret ve devam et. | **neden:** Hafıza sistemi kullanıcı işini engellememeli, desteklemelidir. | **kaynak:** sistem-kurulumu | **durum:** aktif

## Kural Adayları (Candidate Rules)

## Arşivlenmiş / Geçersiz Kılınmış Kurallar
"""

DEFAULT_LAST_SESSION_SEED = """---
type: companion-last-session
version: 2.0
runtime: system
session_id: initial-bootstrap
updated_at: {now}
---

# Son Oturum Sürekliliği (Last Session)

## Ne Yapıldı
- Second Brain V2 mimari temeli kuruldu.
- Codex eski ve yeni rollout formatları regression testleriyle doğrulandı.

## Alınan Kararlar
- Second Brain memory schema (Core, Kurallar, Last-Session, Threads, Journal) aktive edildi.
- Aşırı korumacı derived memory kısıtlamaları kaldırılarak default autonomy sağlandı.

## Yarım Kalanlar & Açık Noktalar
- SB2-03: Startup context ve targeted recall adaptasyonu.

## Sıradaki Doğal Adımlar
- Startup context injection'ın yeni Companion schema ile entegre edilmesi.
- Otomatik kural çıkarma ve reconcile mekanizmasının test edilmesi.

## Aktif Proje / Görev
- Pikselzone Memory OS Second Brain V2 Migration

## Kullanıcıya Sorulacaklar
- Yok (Otonom checkpoint akışı devam ediyor).
"""

DEFAULT_THREADS_SEED = """---
type: companion-threads
version: 2.0
updated_at: {now}
---

# Aktif Konular (Threads)

## Aktif Konular (Active)
### [THREAD-001] Second Brain V2 Göçü ve Otonomi
- **Durum:** active
- **Başlangıç:** {date} | **Son Güncelleme:** {now}
- **Bağlam:** Memory V1'in yaşayan, kendi kurallarını ve skill'lerini geliştiren ikinci beyne dönüştürülmesi.
- **Açık İşler:**
  - SB2-01: Codex rollout uyumluluğu (Tamamlandı)
  - SB2-02: Second-brain memory schema (İlerliyor)
  - SB2-03..SB2-12: Recall, kurallar, knowledge, skills, doctor ve parity
- **Blokaj / Engel:** Yok

## Beklemedeki Konular (Paused / Blocked)

## Çözümlenen Konular (Resolved - Ready for Archive)
"""

DEFAULT_JOURNAL_SEED = """---
type: companion-journal
version: 2.0
updated_at: {now}
---

# İkinci Beyin — Günlük Anlatı ve Yansımalar (Journal)

## {now} — [system] Second Brain V2 Başlangıcı
Memory V1'in aşırı korumacı ve pasif yapısı terk edilerek Second Brain V2 fazına geçildi. Sistem artık oturumlar arası gerçek süreklilik (continuity), öğrenilmiş kurallar (Kurallar.md) ve yaşayan bir bilgi tabanı oluşturacak şekilde tasarlandı.
"""


_CONTINUITY_SCOPE_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")


class CompanionManager:
    """Manages reading, updating, and reconciling companion memory files.

    When ``continuity_scope`` is given (a project slug, or "hermes"), the
    operational-continuity documents (Last-Session, Threads, Threads-Archive) are
    routed to ``<vault>/continuity/<scope>.md`` and ``<vault>/threads/<scope>.md``
    so per-project active state never bleeds across projects.  Identity (Core),
    learned rules (Kurallar) and the Journal always stay shared in
    ``companion/``.  ``continuity_scope=None`` keeps the pre-V2.3 shared layout.
    """

    def __init__(self, vault_path: Path, continuity_scope: str | None = None) -> None:
        self.vault_path = vault_path.resolve()
        self.companion_dir = self._resolve_companion_dir()
        if continuity_scope is not None and not _CONTINUITY_SCOPE_RE.fullmatch(continuity_scope):
            raise SchemaError(f"continuity-scope-invalid:{continuity_scope[:64]}")
        self.continuity_scope = continuity_scope

    def _resolve_companion_dir(self) -> Path:
        # Check standard Avenox directory first, else use standard companion
        avenox_style = self.vault_path / "🔮 850-Companion"
        if avenox_style.is_dir():
            return avenox_style
        standard = self.vault_path / "companion"
        return standard

    @property
    def _last_session_path(self) -> Path:
        if self.continuity_scope:
            return self.vault_path / "continuity" / f"{self.continuity_scope}.md"
        return self.companion_dir / "Last-Session.md"

    @property
    def _threads_path(self) -> Path:
        if self.continuity_scope:
            return self.vault_path / "threads" / f"{self.continuity_scope}.md"
        return self.companion_dir / "Threads.md"

    @property
    def _threads_archive_path(self) -> Path:
        if self.continuity_scope:
            return self.vault_path / "threads" / f"{self.continuity_scope}-Archive.md"
        return self.companion_dir / "Threads-Archive.md"

    def ensure_companion_files(self) -> None:
        """Create initial companion seed documents if not present.

        Core / Kurallar / Journal are always shared.  Last-Session and Threads
        are seeded in ``companion/`` only for the unscoped layout; a scoped
        manager seeds ``continuity/<scope>.md`` and ``threads/<scope>.md``
        instead so it never drops an unused shared file.
        """
        ensure_safe_directory(self.companion_dir, create=True)
        now_str = iso_now()
        date_str = dt.datetime.now().strftime("%Y-%m-%d")

        seeds = {
            "Core.md": DEFAULT_CORE_SEED.format(now=now_str),
            "Kurallar.md": DEFAULT_RULES_SEED.format(now=now_str),
            "Journal.md": DEFAULT_JOURNAL_SEED.format(now=now_str),
        }
        if not self.continuity_scope:
            seeds["Last-Session.md"] = DEFAULT_LAST_SESSION_SEED.format(now=now_str)
            seeds["Threads.md"] = DEFAULT_THREADS_SEED.format(now=now_str, date=date_str)

        for filename, content in seeds.items():
            target = self.companion_dir / filename
            if not target.exists():
                atomic_write(target, content, mode=0o660)

        if self.continuity_scope:
            ensure_safe_directory(self.vault_path / "continuity", create=True)
            ensure_safe_directory(self.vault_path / "threads", create=True)
            if not self._last_session_path.exists():
                atomic_write(
                    self._last_session_path,
                    DEFAULT_LAST_SESSION_SEED.format(now=now_str), mode=0o660,
                )
            if not self._threads_path.exists():
                atomic_write(
                    self._threads_path,
                    DEFAULT_THREADS_SEED.format(now=now_str, date=date_str), mode=0o660,
                )

    # -------------------------------------------------------------------------
    # Core / Identity Management
    # -------------------------------------------------------------------------
    def read_core(self) -> str:
        core_path = self.companion_dir / "Core.md"
        if not core_path.is_file():
            self.ensure_companion_files()
        text, _ = secure_read_text(core_path, root=self.vault_path, max_bytes=256 * 1024)
        return text

    def update_core(self, content: str) -> None:
        core_path = self.companion_dir / "Core.md"
        redacted, _ = redact_sensitive_text(content)
        atomic_write(core_path, redacted, mode=0o660)

    # -------------------------------------------------------------------------
    # Kurallar (Learned Rules & Preferences)
    # -------------------------------------------------------------------------
    def read_rules(self) -> list[RuleItem]:
        rules_path = self.companion_dir / "Kurallar.md"
        if not rules_path.is_file():
            self.ensure_companion_files()
        text, _ = secure_read_text(rules_path, root=self.vault_path, max_bytes=256 * 1024)
        rules: list[RuleItem] = []
        for line in text.splitlines():
            line = line.strip()
            if not line.startswith("- **kural:**"):
                continue
            # Parse `- **kural:** <text> | **neden:** <reason> | **kaynak:** <src> | **durum:** <status>`
            parts = [p.strip() for p in line.lstrip("- ").split("|")]
            rule_text = ""
            reason = ""
            source = ""
            status = "active"
            for part in parts:
                if part.startswith("**kural:**"):
                    rule_text = part.replace("**kural:**", "").strip()
                elif part.startswith("**neden:**"):
                    reason = part.replace("**neden:**", "").strip()
                elif part.startswith("**kaynak:**"):
                    source = part.replace("**kaynak:**", "").strip()
                elif part.startswith("**durum:**"):
                    status = part.replace("**durum:**", "").strip()
            if rule_text:
                rules.append(RuleItem(text=rule_text, reason=reason, source=source, status=status))
        return rules

    def add_or_update_rule(
        self,
        rule_text: str,
        reason: str = "",
        source: str = "",
        is_direct_command: bool = False,
    ) -> bool:
        """Add a new rule or candidate.
        If is_direct_command is True (e.g. user said 'bundan sonra bunu yapma'),
        it immediately becomes active. Otherwise it is registered as a candidate.
        Prevents duplicates and reconciles conflicting rules.
        """
        rules_path = self.companion_dir / "Kurallar.md"
        if not rules_path.is_file():
            self.ensure_companion_files()
        text, _ = secure_read_text(rules_path, root=self.vault_path, max_bytes=256 * 1024)

        clean_rule, _ = redact_sensitive_text(rule_text.strip())
        clean_reason, _ = redact_sensitive_text(reason.strip())
        now_str = iso_now()

        # Check for existing duplicate rule
        existing_rules = self.read_rules()
        for r in existing_rules:
            # Semantic equality / high token overlap check
            if clean_rule.lower() in r.text.lower() or r.text.lower() in clean_rule.lower():
                return False

        # Format rule line
        new_entry = (
            f"- **kural:** {clean_rule} | "
            f"**neden:** {clean_reason or 'Kullanıcı geri bildirimi ve çalışma tercihi'} | "
            f"**kaynak:** {source or 'oturum'} | "
            f"**durum:** aktif"
        )

        lines = text.splitlines()
        inserted = False
        new_lines: list[str] = []
        for line in lines:
            new_lines.append(line)
            if line.strip() == "## Aktif Kurallar" and not inserted:
                new_lines.append(new_entry)
                inserted = True

        if not inserted:
            new_lines.append("\n## Aktif Kurallar")
            new_lines.append(new_entry)

        atomic_write(rules_path, "\n".join(new_lines) + "\n", mode=0o660)
        return True

    # -------------------------------------------------------------------------
    # Last-Session (Operational Continuity)
    # -------------------------------------------------------------------------
    def read_last_session(self) -> str:
        p = self._last_session_path
        if not p.is_file():
            self.ensure_companion_files()
        text, _ = secure_read_text(p, root=self.vault_path, max_bytes=256 * 1024)
        return text

    def write_last_session(self, data: LastSessionData) -> None:
        p = self._last_session_path
        if self.continuity_scope:
            ensure_safe_directory(self.vault_path / "continuity", create=True)
        now_str = data.updated_at or iso_now()

        completed_str = "\n".join(f"- {item}" for item in data.completed_items) or "- Belirtilmedi."
        decisions_str = "\n".join(f"- {item}" for item in data.decisions) or "- Belirtilmedi."
        pending_str = "\n".join(f"- {item}" for item in data.pending_items) or "- Yok."
        next_str = "\n".join(f"- {item}" for item in data.next_steps) or "- Sonraki doğal adımlar bekleniyor."
        questions_str = "\n".join(f"- {item}" for item in data.user_questions) or "- Yok."

        doc = f"""---
type: companion-last-session
version: 2.0
runtime: {data.runtime}
session_id: {data.session_id}
updated_at: {now_str}
---

# Son Oturum Sürekliliği (Last Session)

## Ne Yapıldı
{completed_str}

## Alınan Kararlar
{decisions_str}

## Yarım Kalanlar & Açık Noktalar
{pending_str}

## Sıradaki Doğal Adımlar
{next_str}

## Aktif Proje / Görev
- {data.active_project or 'Genel Geliştirme'}

## Kullanıcıya Sorulacaklar
{questions_str}
"""
        redacted, _ = redact_sensitive_text(doc)
        atomic_write(p, redacted, mode=0o660)

    # -------------------------------------------------------------------------
    # Threads Management
    # -------------------------------------------------------------------------
    def read_threads(self) -> str:
        p = self._threads_path
        if not p.is_file():
            self.ensure_companion_files()
        text, _ = secure_read_text(p, root=self.vault_path, max_bytes=256 * 1024)
        return text

    def update_thread(self, thread: ThreadItem) -> None:
        p = self._threads_path
        if not p.is_file():
            self.ensure_companion_files()
        text, _ = secure_read_text(p, root=self.vault_path, max_bytes=256 * 1024)

        now_str = iso_now()
        updated_date = dt.datetime.now().strftime("%Y-%m-%d")

        thread_block = f"""### [{thread.thread_id}] {thread.title}
- **Durum:** {thread.status}
- **Başlangıç:** {thread.created_at or updated_date} | **Son Güncelleme:** {now_str}
- **Bağlam:** {thread.context}
- **Açık İşler:**
"""
        for item in thread.open_items:
            thread_block += f"  - {item}\n"
        if thread.blockers:
            thread_block += f"- **Blokaj / Engel:** {thread.blockers}\n"
        else:
            thread_block += "- **Blokaj / Engel:** Yok\n"

        # Check if thread already exists in text
        pattern = re.compile(rf"###\s*\[{re.escape(thread.thread_id)}\][^\n]*\n(?:(?!\n###)[^\n]*\n)*", re.MULTILINE)
        if pattern.search(text):
            new_text = pattern.sub(thread_block + "\n", text)
        else:
            # Append under ## Aktif Konular (Active)
            target_section = "## Aktif Konular (Active)"
            if target_section in text:
                new_text = text.replace(target_section, f"{target_section}\n{thread_block}")
            else:
                new_text = text + f"\n\n{target_section}\n{thread_block}"

        redacted, _ = redact_sensitive_text(new_text)
        atomic_write(p, redacted, mode=0o660)

    def archive_resolved_threads(self) -> int:
        """Move resolved threads from Threads.md to Threads-Archive.md."""
        threads_path = self._threads_path
        archive_path = self._threads_archive_path
        if not threads_path.is_file():
            return 0
        text, _ = secure_read_text(threads_path, root=self.vault_path, max_bytes=256 * 1024)

        resolved_pattern = re.compile(
            r"(###\s*\[([A-Za-z0-9_-]+)\][^\n]*\n(?:(?!\n###)[^\n]*\n)*)",
            re.MULTILINE,
        )

        archived_count = 0
        remaining_text = text
        archived_blocks: list[str] = []

        for match in resolved_pattern.finditer(text):
            block = match.group(1)
            if "- **Durum:** resolved" in block or "- **Durum:** closed" in block:
                archived_blocks.append(block.strip())
                remaining_text = remaining_text.replace(block, "")
                archived_count += 1

        if archived_count > 0:
            atomic_write(threads_path, remaining_text.strip() + "\n", mode=0o660)
            archive_header = "# İkinci Beyin — Arşivlenmiş Konular (Threads Archive)\n\n"
            existing_archive = ""
            if archive_path.is_file():
                existing_archive, _ = secure_read_text(archive_path, root=self.vault_path, max_bytes=1024 * 1024)
            if not existing_archive:
                existing_archive = archive_header
            updated_archive = existing_archive.rstrip() + "\n\n" + "\n\n".join(archived_blocks) + "\n"
            atomic_write(archive_path, updated_archive, mode=0o660)

        return archived_count

    # -------------------------------------------------------------------------
    # Journal (Narrative Reflections)
    # -------------------------------------------------------------------------
    def append_journal_entry(self, title: str, narrative: str, runtime: str = "system") -> None:
        p = self.companion_dir / "Journal.md"
        if not p.is_file():
            self.ensure_companion_files()
        text, _ = secure_read_text(p, root=self.vault_path, max_bytes=512 * 1024)

        now_str = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
        clean_narrative, _ = redact_sensitive_text(narrative.strip())
        clean_title, _ = redact_sensitive_text(title.strip())

        entry = f"\n\n## {now_str} — [{runtime}] {clean_title}\n{clean_narrative}\n"
        updated = text.rstrip() + entry
        atomic_write(p, updated, mode=0o660)

    def read_latest_journal_entry(self, max_lines: int = 15) -> str:
        p = self.companion_dir / "Journal.md"
        if not p.is_file():
            self.ensure_companion_files()
        text, _ = secure_read_text(p, root=self.vault_path, max_bytes=512 * 1024)

        entries = re.split(r"\n(?=##\s+\d{4}-\d{2}-\d{2})", text)
        if len(entries) <= 1:
            return ""
        latest = entries[-1].strip()
        lines = latest.splitlines()[:max_lines]
        return "\n".join(lines)

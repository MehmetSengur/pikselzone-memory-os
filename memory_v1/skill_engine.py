"""Self-generating & self-updating skills engine (SB2-06).

Detects repeated operational workflows (2+ observations), synthesizes standard
production-ready skills under skills/<slug>/SKILL.md, and iteratively updates
existing skills when new parameters, edge-cases, or recovery steps are discovered.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import json
import logging
import re
from pathlib import Path
from typing import Any, List, Optional, Set, Tuple

from .core import (
    MemoryConfig, PolicyError, SchemaError, atomic_json, atomic_write,
    iso_now, redact_sensitive_text, reject_symlink_chain,
)
from .graph_engine import slugify

logger = logging.getLogger("memory_v1.skill_engine")


@dataclasses.dataclass
class WorkflowObservation:
    workflow_name: str
    goal: str = ""
    steps: list[str] = dataclasses.field(default_factory=list)
    tools_or_scripts: list[str] = dataclasses.field(default_factory=list)
    session_id: str = "session"
    success: bool = True
    edge_cases: list[str] = dataclasses.field(default_factory=list)
    trigger: str = ""


@dataclasses.dataclass
class SkillSpec:
    slug: str
    name: str
    description: str
    triggers: list[str]
    prerequisites: list[str]
    steps: list[str]
    tools_or_scripts: list[str]
    expected_output: str
    recovery_steps: list[str]
    version: str = "1.0.0"
    created_at: str = ""
    updated_at: str = ""
    execution_count: int = 2
    changelog: list[str] = dataclasses.field(default_factory=list)


class SkillEngine:
    """Manages skill candidates, synthesis of SKILL.md, and iterative evolutions."""

    def __init__(self, vault_path: Path) -> None:
        self.vault_path = vault_path.resolve()
        self.skills_dir = self.vault_path / "skills"
        self.state_dir = self.vault_path / ".state"
        self.candidates_file = self.state_dir / "skill_candidates.json"

    def ensure_skills_dirs(self) -> None:
        """Create skills and state directories and ensure built-in skills exist."""
        self.skills_dir.mkdir(parents=True, exist_ok=True)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        if not self.candidates_file.is_file():
            atomic_json(self.candidates_file, {"candidates": {}, "updated_at": iso_now()})
        self._ensure_builtin_skills()

    def _ensure_builtin_skills(self) -> None:
        """Ensure standard built-in skills (beyin-doktor, gecmis-import) are present."""
        # 1. beyin-doktor
        doktor_dir = self.skills_dir / "beyin-doktor"
        doktor_file = doktor_dir / "SKILL.md"
        if not doktor_file.is_file():
            doktor_dir.mkdir(parents=True, exist_ok=True)
            doktor_content = (
                "---\n"
                "name: \"beyin-doktor\"\n"
                "description: \"Pikselzone Second Brain ve Memory OS mekanik sağlık denetimi.\"\n"
                "version: \"1.0.0\"\n"
                "triggers: [\"beyin doktor\", \"doktor\", \"sağlık kontrolü\", \"memory doctor\"]\n"
                f"created_at: \"{iso_now()}\"\n"
                f"updated_at: \"{iso_now()}\"\n"
                "execution_count: 1\n"
                "---\n\n"
                "# Skill: Beyin Doktor (Pikselzone Memory Health Check)\n\n"
                "## 1. Ne Zaman Tetiklenir\n"
                "Kullanıcı 'doktor', 'beyin doktor', 'sağlık kontrolü' dediğinde veya sistemde tutarsızlık şüphesi olduğunda.\n\n"
                "## 2. Önkoşullar\n"
                "- Python 3 kurulu olmalı.\n"
                "- Vault dizini erişilebilir olmalı.\n\n"
                "## 3. Adım Adım Çalışma Planı\n"
                "1. `python3 -m memory_v1.cli --config <config> doctor` komutunu çalıştır.\n"
                "2. Hook ve transcript izinlerini denetle.\n"
                "3. Companion dosyalarını (Core, Kurallar, Last-Session, Threads, Journal) doğrula.\n"
                "4. Knowledge indeks boyutu ve öksüz wikilink taramasını yap.\n"
                "5. Çıktıyı tek bir sağlık tablosunda raporla.\n\n"
                "## 4. Kullanılacak Script / Araçlar\n"
                "- `memory_v1.doctor`\n"
                "- `memory_v1.cli`\n\n"
                "## 5. Beklenen Çıktı ve Başarı Kriteri\n"
                "Tüm kritik satırların 🟢 (OK) olması, kırmızı (🔴) satırlar için onarım önerisi sunulması.\n\n"
                "## 6. Hata Durumunda Kurtarma Adımı\n"
                "Hata durumunda `doctor --fix` veya companion restore çalıştırılır.\n"
            )
            atomic_write(doktor_file, doktor_content, mode=0o660)

        # 2. gecmis-import
        import_dir = self.skills_dir / "gecmis-import"
        import_file = import_dir / "SKILL.md"
        if not import_file.is_file():
            import_dir.mkdir(parents=True, exist_ok=True)
            import_content = (
                "---\n"
                "name: \"gecmis-import\"\n"
                "description: \"Claude, ChatGPT, Codex veya Gemini sohbet geçmişlerini vault daily hafızasına aktarır.\"\n"
                "version: \"1.0.0\"\n"
                "triggers: [\"geçmiş import\", \"takeout\", \"chatgpt geçmişi\", \"claude geçmişi import\"]\n"
                f"created_at: \"{iso_now()}\"\n"
                f"updated_at: \"{iso_now()}\"\n"
                "execution_count: 1\n"
                "---\n\n"
                "# Skill: Geçmiş Import (Chat History Importer)\n\n"
                "## 1. Ne Zaman Tetiklenir\n"
                "Kullanıcı eski yapay zeka oturumlarını veya export dosyalarını sisteme tanıtmak istediğinde.\n\n"
                "## 2. Önkoşullar\n"
                "- Kullanıcının sağladığı export dosyası (.zip, .json veya .md).\n"
                "- Yeterli disk alanı.\n\n"
                "## 3. Adım Adım Çalışma Planı\n"
                "1. Export dosya yolunu al ve güvenliğini kontrol et.\n"
                "2. İlgili formata (ChatGPT, Claude, Codex, Gemini) uygun parser'ı çağır.\n"
                "3. Konuşmaları aylık gruplara ayırarak `daily/import-YYYY-MM.md` olarak yaz.\n"
                "4. Compiler'a veya hafıza indeksine kademeli işleme için bildir.\n\n"
                "## 4. Kullanılacak Script / Araçlar\n"
                "- `memory_v1.importers`\n\n"
                "## 5. Beklenen Çıktı ve Başarı Kriteri\n"
                "Geçmiş konuşmaların vault şemasına uygun event formatına dönüştürülmesi.\n\n"
                "## 6. Hata Durumunda Kurtarma Adımı\n"
                "Bozuk JSON kayıtları atlanır, sağlam konuşmalar kaydedilir ve hata raporlanır.\n"
            )
            atomic_write(import_file, import_content, mode=0o660)

    def record_workflow_observation(self, obs: WorkflowObservation) -> Optional[Path]:
        """Record an observed workflow pattern; auto-synthesize SKILL.md on 2nd repetition."""
        self.ensure_skills_dirs()
        slug = slugify(obs.workflow_name)

        try:
            state = json.loads(self.candidates_file.read_text(encoding="utf-8"))
        except Exception:
            state = {"candidates": {}, "updated_at": iso_now()}

        candidates = state.setdefault("candidates", {})
        candidate = candidates.get(slug, {
            "name": obs.workflow_name,
            "goal": obs.goal,
            "observations": [],
            "count": 0,
            "steps": obs.steps,
            "tools": obs.tools_or_scripts,
            "edge_cases": obs.edge_cases,
        })

        candidate["count"] += 1
        candidate["observations"].append({
            "session_id": obs.session_id,
            "observed_at": iso_now(),
            "success": obs.success,
        })

        # Merge steps and tools
        for step in obs.steps:
            if step not in candidate["steps"]:
                candidate["steps"].append(step)
        for tool in obs.tools_or_scripts:
            if tool not in candidate["tools"]:
                candidate["tools"].append(tool)
        for ec in obs.edge_cases:
            if ec not in candidate.setdefault("edge_cases", []):
                candidate["edge_cases"].append(ec)

        candidates[slug] = candidate
        state["updated_at"] = iso_now()
        atomic_json(self.candidates_file, state)

        # If observed 2 or more times, auto-materialize into a standard skill!
        if candidate["count"] >= 2:
            spec = SkillSpec(
                slug=slug,
                name=obs.workflow_name,
                description=obs.goal,
                triggers=[obs.workflow_name.lower(), slug.replace("-", " ")],
                prerequisites=["Gereken çalışma ortamı ve konfigürasyon hazır olmalı."],
                steps=candidate["steps"],
                tools_or_scripts=candidate["tools"],
                expected_output=f"{obs.workflow_name} başarıyla tamamlanmış olmalı.",
                recovery_steps=["Hata durumunda logları incele ve oturum sürekliliğine not düş."],
                execution_count=candidate["count"],
                changelog=[f"v1.0.0 ({dt.date.today().isoformat()}): {candidate['count']} oturum tekrarından otomatik sentezlendi."],
            )
            return self.materialize_skill(spec)

        return None

    record_workflow = record_workflow_observation

    def materialize_skill(self, spec: SkillSpec) -> Path:
        """Render and save a standard production-ready SKILL.md."""
        self.ensure_skills_dirs()
        skill_dir = self.skills_dir / spec.slug
        skill_dir.mkdir(parents=True, exist_ok=True)
        skill_file = skill_dir / "SKILL.md"

        today_str = dt.date.today().isoformat()
        created = spec.created_at or today_str
        updated = spec.updated_at or today_str

        triggers_fmt = json.dumps(spec.triggers, ensure_ascii=False)
        prereqs_lines = "\n".join(f"- {p}" for p in spec.prerequisites)
        steps_lines = "\n".join(f"{idx+1}. {s}" for idx, s in enumerate(spec.steps))
        tools_lines = "\n".join(f"- `{t}`" for t in spec.tools_or_scripts)
        recovery_lines = "\n".join(f"- {r}" for r in spec.recovery_steps)
        changelog_lines = "\n".join(f"- {c}" for c in (spec.changelog or [f"v{spec.version} ({today_str}): Başlangıç sürümü."]))

        content = (
            f"---\n"
            f'name: "{spec.slug}"\n'
            f'description: "{spec.description}"\n'
            f'version: "{spec.version}"\n'
            f"triggers: {triggers_fmt}\n"
            f'created_at: "{created}"\n'
            f'updated_at: "{updated}"\n'
            f"execution_count: {spec.execution_count}\n"
            f"---\n\n"
            f"# Skill: {spec.name}\n\n"
            f"## 1. Ne Zaman Tetiklenir (Triggers)\n"
            f"Tetikleyici ifadeler: {', '.join(spec.triggers)}\n\n"
            f"## 2. Önkoşullar (Prerequisites)\n"
            f"{prereqs_lines}\n\n"
            f"## 3. Adım Adım Çalışma Planı (Execution Workflow)\n"
            f"{steps_lines}\n\n"
            f"## 4. Kullanılacak Script / Araçlar (Tools & Scripts)\n"
            f"{tools_lines}\n\n"
            f"## 5. Beklenen Çıktı ve Başarı Kriteri (Expected Output)\n"
            f"{spec.expected_output}\n\n"
            f"## 6. Hata Durumunda Kurtarma Adımı (Recovery)\n"
            f"{recovery_lines}\n\n"
            f"## 7. Sürüm Geçmişi ve Öğrenilen İyileştirmeler (Changelog)\n"
            f"{changelog_lines}\n"
        )
        atomic_write(skill_file, content, mode=0o660)
        logger.info("Materialized skill '%s' at %s", spec.name, skill_file)
        return skill_file

    def update_skill_with_learnings(
        self,
        slug: str,
        new_step: str | None = None,
        new_param: str | None = None,
        edge_case: str | None = None,
        recovery_tweak: str | None = None,
    ) -> bool:
        """Iteratively update an existing SKILL.md with newly learned behavior and bump version."""
        skill_file = self.skills_dir / slug / "SKILL.md"
        if not skill_file.is_file():
            return False

        content = skill_file.read_text(encoding="utf-8")
        today_str = dt.date.today().isoformat()

        # Parse version
        v_match = re.search(r'version:\s*"(\d+)\.(\d+)\.(\d+)"', content)
        if v_match:
            major, minor, patch = int(v_match.group(1)), int(v_match.group(2)), int(v_match.group(3))
            new_version = f"{major}.{minor + 1}.0"
        else:
            new_version = "1.1.0"

        changes: list[str] = []

        if new_step and new_step not in content:
            clean_step, _ = redact_sensitive_text(new_step)
            content = content.replace(
                "## 4. Kullanılacak Script",
                f"- Ek Adım: {clean_step}\n\n## 4. Kullanılacak Script",
            )
            changes.append(f"Yeni adım eklendi: {clean_step}")

        if new_param and new_param not in content:
            clean_param, _ = redact_sensitive_text(new_param)
            content = content.replace(
                "## 5. Beklenen Çıktı",
                f"- Parametre / Seçenek: `{clean_param}`\n\n## 5. Beklenen Çıktı",
            )
            changes.append(f"Yeni parametre: {clean_param}")

        if edge_case and edge_case not in content:
            clean_ec, _ = redact_sensitive_text(edge_case)
            content = content.replace(
                "## 7. Sürüm Geçmişi",
                f"- **Özel Durum (Edge-case):** {clean_ec}\n\n## 7. Sürüm Geçmişi",
            )
            changes.append(f"Edge case yakalandı: {clean_ec}")

        if recovery_tweak and recovery_tweak not in content:
            clean_rec, _ = redact_sensitive_text(recovery_tweak)
            content = content.replace(
                "## 7. Sürüm Geçmişi",
                f"- **Ek Kurtarma Prosedürü:** {clean_rec}\n\n## 7. Sürüm Geçmişi",
            )
            changes.append(f"Kurtarma prosedürü güncellendi: {clean_rec}")

        if not changes:
            return False

        # Bump version & update timestamps
        content = re.sub(r'version:\s*"[^"]*"', f'version: "{new_version}"', content)
        content = re.sub(r'updated_at:\s*"[^"]*"', f'updated_at: "{iso_now()}"', content)

        # Append to changelog section
        log_entry = f"- v{new_version} ({today_str}): " + "; ".join(changes)
        content = content.strip() + f"\n{log_entry}\n"

        atomic_write(skill_file, content, mode=0o660)
        logger.info("Updated skill '%s' to version %s with %d improvements", slug, new_version, len(changes))
        return True

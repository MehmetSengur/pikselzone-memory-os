"""Startup recall budget: every category keeps a share; selection is audited.

The live Hermes bundle carried identity, rules and eight skills and nothing
else, because shedding removed whole categories in a fixed order (daily events
first, skills last) regardless of relevance. These tests pin the replacement.
"""
from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path

from memory_v1.core import MemoryConfig
from memory_v1.events import EventWriter
from memory_v1.recall import TARGET_BUDGET_CHARS, build_startup_recall_bundle


def _event(session_id: str, context: str, decision: str) -> str:
    return EventWriter._render(
        runtime="claude", agent_id="claude", session_id=session_id, event="session_end",
        events_seen=["session_end"], created_at="2026-09-14T10:00:00+03:00",
        source_model="haiku", source_provider="anthropic-subscription", root_task_id="task",
        kanban_ids=[], source_digest="a" * 64,
        summary={
            "context": [context], "important_conversations": [], "decisions": [decision],
            "learnings": [], "open_items": [], "evidence": ["kanıt"],
        },
        redaction_count=0,
    )


class StartupBudgetAllocationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name).resolve()
        self.vault = root / "vault"
        self.state = root / "state"
        (self.vault / "companion").mkdir(parents=True)
        self.state.mkdir()
        self.config = MemoryConfig.from_dict({
            "role": "workstation", "vault_path": str(self.vault), "state_path": str(self.state),
            "runtimes": ["claude", "codex"], "transcript_roots": {"claude": [str(root)], "codex": [str(root)]},
            "can_write_event_memory": True, "can_run_compiler": False,
            "models": {"flush": "gpt-5.6-luna", "compiler": "gpt-5.6-terra"},
            "provider": {"mode": "runtime-native"}, "context_budget_chars": TARGET_BUDGET_CHARS,
        })
        (self.vault / "companion" / "Core.md").write_text("# Core\n- Kullanıcı: Pikselzone kurucusu\n", encoding="utf-8")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    # --- corpus builders --------------------------------------------------
    def _rules(self, active: int, *, candidates: int = 0, retired: bool = False) -> None:
        lines = ["# Kurallar", "", "## Aktif Kurallar"]
        lines += [f"- **kural:** Aktif kural numarası {i} için uzunca bir açıklama metni burada yer alıyor | "
                  f"**neden:** test | **kaynak:** s{i} | **durum:** aktif" for i in range(active)]
        lines += ["", "## Kural Adayları (Candidate Rules)"]
        lines += [f"- **aday:** ADAY-GIZLI-{i} | **neden:** aday | **kaynaklar:** s{i} | **gözlem:** 1 | **durum:** aday"
                  for i in range(candidates)]
        lines += ["", "## Arşivlenmiş / Geçersiz Kılınmış Kurallar",
                  "- **eski_kural:** ARSIV-GIZLI | **yerine_geçen:** x | **arşiv_tarihi:** t | **kaynak:** s"]
        if retired:
            lines += ["", "## Bakım ile Devre Dışı Bırakılan Kayıtlar (Maintenance Retired)",
                      "- **devre_dışı:** EMEKLI-GIZLI | **sınıf:** relay-payload | **kanıt:** k | **kaynak:** s | **bakım:** r"]
        (self.vault / "companion" / "Kurallar.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    def _skills(self, count: int) -> None:
        for i in range(count):
            d = self.vault / "skills" / f"skill-{i:02d}"
            d.mkdir(parents=True)
            steps = "\n".join(f"{n}. UZUN-ADIM-{i}-{n} " + "x" * 120 for n in range(1, 9))
            (d / "SKILL.md").write_text(
                f'---\nname: "skill-{i:02d}"\ndescription: "Skill {i} kısa açıklaması"\n---\n\n'
                f"# Skill {i}\n\n## 3. Adım Adım Çalışma Planı (Execution Workflow)\n{steps}\n",
                encoding="utf-8",
            )

    def _index(self, rows: int) -> None:
        body = ["# Index", "", "| Article | Summary | Source | Updated |", "|---|---|---|---|"]
        body += [f"| [Kavram {i}](concepts/kavram-{i}.md) | Kavram {i} için bir özet cümlesi | s | 2026-09-01 |"
                 for i in range(rows)]
        (self.vault / "knowledge").mkdir(parents=True, exist_ok=True)
        (self.vault / "knowledge" / "index.md").write_text("\n".join(body) + "\n", encoding="utf-8")

    def _daily(self, count: int) -> None:
        day = self.vault / "daily" / "2026-09-14"
        day.mkdir(parents=True, exist_ok=True)
        for i in range(count):
            (day / f"claude-{i:032x}.md").write_text(
                _event(f"sess-{i}", f"GUNLUK-OLAY-{i} bağlamı", f"karar {i}"), encoding="utf-8",
            )

    def _last_session(self, long: bool = False) -> None:
        extra = "\n".join(f"- SUREKLILIK-SATIRI-{i} " + "y" * 150 for i in range(40)) if long else "- kısa süreklilik"
        (self.vault / "companion" / "Last-Session.md").write_text(
            f"# Son Oturum\n\n## Ne Yapıldı\n{extra}\n", encoding="utf-8",
        )

    def _projects(self, names: list[str]) -> None:
        (self.vault / "continuity").mkdir(exist_ok=True)
        for offset, name in enumerate(names):
            path = self.vault / "continuity" / f"{name}.md"
            path.write_text(
                f"---\nupdated_at: 2026-09-1{offset}T10:00:00+03:00\n---\n## Ne Yapıldı\n- {name} ILERLEME\n"
                f"## Yarım Kalanlar & Açık Noktalar\n- {name} ACIK-IS\n",
                encoding="utf-8",
            )
            stamp = time.time() - (len(names) - offset) * 60
            os.utime(path, (stamp, stamp))

    # --- tests ------------------------------------------------------------
    def test_large_skills_and_index_do_not_evict_continuity_or_daily(self):
        self._rules(10)
        self._skills(20)
        self._index(150)
        self._daily(5)
        self._last_session(long=True)
        bundle = build_startup_recall_bundle(self.config, runtime="hermes")
        audit = bundle.selection_audit
        self.assertLessEqual(bundle.total_chars, TARGET_BUDGET_CHARS)
        for kind in ("continuity", "daily_event", "knowledge_index", "skill"):
            self.assertTrue(audit["categories"][kind]["selected"], f"{kind} got nothing")
        self.assertIn("GUNLUK-OLAY-", bundle.text)
        self.assertIn("## 5. Recent Daily Event Tail", bundle.text)

    def test_dropped_items_are_audited_with_reasons(self):
        self._rules(5)
        # More rows than the whole budget can hold, even with every other category empty.
        self._index(400)
        bundle = build_startup_recall_bundle(self.config, runtime="hermes")
        knowledge = bundle.selection_audit["categories"]["knowledge_index"]
        self.assertGreater(knowledge["dropped_count"], 0)
        self.assertTrue({d["reason"] for d in knowledge["dropped_sample"]} <= {"category-cap", "total-budget"})

    def test_skills_are_one_line_summaries(self):
        self._rules(3)
        self._skills(4)
        bundle = build_startup_recall_bundle(self.config, runtime="hermes")
        self.assertIn("- skill-00: Skill 0 kısa açıklaması", bundle.text)
        self.assertNotIn("UZUN-ADIM-", bundle.text)

    def test_only_active_rules_reach_the_bundle(self):
        self._rules(3, candidates=2, retired=True)
        bundle = build_startup_recall_bundle(self.config, runtime="hermes")
        self.assertIn("Aktif kural numarası 0", bundle.text)
        for hidden in ("ADAY-GIZLI", "ARSIV-GIZLI", "EMEKLI-GIZLI"):
            self.assertNotIn(hidden, bundle.text)
        self.assertIn("2 aday tercih", bundle.text)

    def test_large_rules_are_bounded_and_say_how_many_were_left_out(self):
        self._rules(200)
        bundle = build_startup_recall_bundle(self.config, runtime="hermes")
        self.assertLessEqual(bundle.total_chars, TARGET_BUDGET_CHARS)
        self.assertRegex(bundle.text, r"\[… \d+ satır daha: companion/Kurallar\.md\]")

    def test_missing_categories_stay_empty(self):
        self._rules(2)
        bundle = build_startup_recall_bundle(self.config, runtime="hermes")
        audit = bundle.selection_audit["categories"]
        self.assertTrue(audit["skill"]["empty"])
        self.assertTrue(audit["daily_event"]["empty"])
        self.assertNotIn("Synthesized Skills", bundle.text)
        self.assertNotIn("Recent Daily Event Tail", bundle.text)

    def test_unscoped_session_needs_explicit_project_grants(self):
        self._rules(2)
        self._projects(["alpha", "beta"])
        unscoped = build_startup_recall_bundle(self.config, runtime="hermes")
        self.assertNotIn("beta ACIK-IS", unscoped.text)
        import dataclasses
        permitted = dataclasses.replace(self.config, memory={'projects':['alpha','beta']})
        granted = build_startup_recall_bundle(permitted, runtime='hermes')
        self.assertIn('beta ACIK-IS', granted.text)

    def test_digest_is_capped_to_recent_projects(self):
        self._rules(2)
        self._projects([f"proj{i}" for i in range(7)])
        bundle = build_startup_recall_bundle(self.config, runtime="hermes")
        digests = [i for i in bundle.selected_item_ids if i.startswith("tier-b-project-")]
        self.assertLessEqual(len(digests), 4)
        self.assertNotIn("proj0 ILERLEME", bundle.text)

    def test_scoped_session_does_not_get_other_projects(self):
        self._rules(2)
        self._projects(["alpha", "beta"])
        scoped = build_startup_recall_bundle(self.config, runtime="claude", continuity_scope="alpha")
        self.assertNotIn("Proje sürekliliği: beta", scoped.text)

    def test_physically_insufficient_budget_is_reported(self):
        (self.vault / "companion" / "Core.md").write_text(
            "# Core\n" + "\n".join(f"- satır {i} " + "z" * 90 for i in range(30)) + "\n", encoding="utf-8",
        )
        self._rules(40)
        bundle = build_startup_recall_bundle(self.config, runtime="hermes", budget_chars=1500)
        self.assertTrue(bundle.selection_audit["physically_insufficient"])
        self.assertLessEqual(bundle.total_chars, 1500)


if __name__ == "__main__":
    unittest.main()

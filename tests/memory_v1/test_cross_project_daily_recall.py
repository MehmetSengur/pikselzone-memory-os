"""A decision kept only in another project's daily note must reach a prompt.

2026-09-25 incident: Orchestrator asked for "the n8n → Hermes project that
makes videos with Higgsfield". The answer sat in a pikselzone-memory-os daily
note from 2026-09-20; the compiler had never promoted it and the prompt hook
read concepts only, so nothing surfaced.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from memory_v1.core import MemoryConfig
from memory_v1.knowledge_promoter import select_and_stage_batch
from memory_v1.memory_policy import compile_reason, resolve_policy
from memory_v1.recall import associative_recall_fast
from memory_v1.recall_access import source_meta

_QUERY = "n8n'den Hermes'e geçen, Higgsfield ile video yapan proje hangisi?"

_SENGUR_SOCIAL = {
    "context": [
        "Şengür sosyal içerik/video görevi için `pz-sengur` korunmalı; izolasyon ayrı `sengur-social` worktree ile sağlandı.",
        "Video pilotu tamamlandı: 5 saniye, 720x1280, sessiz. Sosyal hesaplarda yayın yapılmadı.",
        "Kullanıcı CLI yerine Higgsfield MCP üzerinden ilerlemek istediğini netleştirdi.",
    ],
    "decisions": ["Higgsfield işlemlerinde MCP kullanılacak; CLI yolu kullanılmayacak."],
    "evidence": ["Commit: `56e0f66`, branch: `codex/sengur-social-20260919`."],
}


def _event(session: str, project: str, sections: dict, *, dash_in_records: bool = False) -> str:
    scope = {"owner": "", "project": project, "visibility": "project"}
    records = []
    if dash_in_records:
        # Tool output quoted inside critical_records, on the header's one line.
        records = [{"quoted": "--- BEGIN OUTPUT ---"}]
    front = {
        "schema": "pikselzone-memory-event-v1", "runtime": "codex", "agent_id": "codex-main",
        "session_id": session, "event": "session_end", "events_seen": ["session_end"],
        "created_at": "2026-09-20T10:36:47+03:00", "source_runtime": "codex",
        "source_model": "gpt-6-astra", "project": project, "memory_scope": scope,
        "root_task_id": "unknown", "kanban_ids": [], "source_sha256": "a" * 64,
        "secret_redactions": 0, "generated_by": "pikselzone-memory-v1",
        "authority": "derived-session-memory-not-operational-truth",
    }
    lines = ["---"]
    if dash_in_records:
        lines.append("critical_records_probe: " + json.dumps(records))
    lines += [f"{k}: {json.dumps(v, ensure_ascii=False)}" for k, v in front.items()]
    lines.append("---")
    titles = {"context": "Bağlam", "important_conversations": "Önemli Konuşmalar",
              "decisions": "Alınan Kararlar", "learnings": "Öğrenilenler",
              "open_items": "Açık Konular", "evidence": "Kanıtlar"}
    for field, title in titles.items():
        lines.append("")
        lines.append(f"## {title}")
        for bullet in sections.get(field) or ["unknown"]:
            lines.append(f"- {bullet}")
    return "\n".join(lines) + "\n"


class _Vault(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="pz-test-xproj-")
        self.root = Path(self._tmp.name).resolve()
        self.vault = self.root / "vault"
        (self.vault / "knowledge" / "concepts").mkdir(parents=True)
        (self.vault / "knowledge" / "index.md").write_text(
            "| Article | Summary | Source | Updated |\n|---|---|---|---|\n", encoding="utf-8")
        day = self.vault / "daily" / "2026-09-20"
        day.mkdir(parents=True)
        self.target = day / "codex-sengur-social.md"
        self.target.write_text(_event("s-social", "pikselzone-memory-os", _SENGUR_SOCIAL), encoding="utf-8")
        # Unrelated notes so the rare word is rare, as it is in the live vault.
        for i, topic in enumerate(("Kanban kartları güncellendi ve Hermes gateway yeniden başlatıldı.",
                                   "Supabase PAT süresi doldu; SEO hattı bekliyor.",
                                   "Hermes profil politikası ve Meta katalog eşleşmesi incelendi.",
                                   "Testler çalıştırıldı, rapor yazıldı; video yok.")):
            (day / f"claude-other-{i}.md").write_text(
                _event(f"s-{i}", "pikselzone-orchestrator", {"context": [topic]}), encoding="utf-8")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def config(self, *, project: str, projects: list[str] | None = None, role: str = "workstation"):
        runtimes = ["hermes"] if role == "memory-engine" else ["codex", "claude"]
        memory = {"project": project}
        if projects is not None:
            memory["projects"] = projects
        return MemoryConfig.from_dict({
            "role": role, "vault_path": str(self.vault), "state_path": str(self.root / "state"),
            "runtimes": runtimes,
            "transcript_roots": {r: [str(self.root)] for r in runtimes},
            "can_write_event_memory": True, "can_run_compiler": role == "memory-engine",
            "provider": {"mode": "runtime-native"}, "memory": memory,
        })


class AccessGateFrontmatterTest(unittest.TestCase):
    def test_a_dash_run_inside_the_header_does_not_hide_the_scope(self) -> None:
        text = _event("s", "pikselzone-memory-os", _SENGUR_SOCIAL, dash_in_records=True)
        meta = source_meta(text, "daily/2026-09-20/x.md")
        self.assertEqual(meta.get("project"), "pikselzone-memory-os")
        self.assertEqual(meta.get("visibility"), "project")


class CompileEligibilityTest(unittest.TestCase):
    def test_default_policy_keeps_the_old_rule(self) -> None:
        p = resolve_policy({})
        self.assertIsNone(compile_reason({"project": "unscoped"}, p))
        self.assertIsNone(compile_reason({"visibility": "shared", "project": "x"}, p))
        self.assertEqual(compile_reason({"project": "x", "visibility": "project"}, p), "scope-excluded")
        self.assertEqual(compile_reason({"owner": "o", "visibility": "private"}, p), "scope-excluded")

    def test_a_granted_project_may_be_compiled_but_owner_private_never(self) -> None:
        p = resolve_policy({"projects": ["x"]})
        self.assertIsNone(compile_reason({"project": "x", "visibility": "project"}, p))
        self.assertEqual(compile_reason({"owner": "o", "project": "x", "visibility": "private"}, p),
                         "scope-excluded")

    def test_a_legacy_project_event_follows_the_same_grant(self) -> None:
        self.assertEqual(compile_reason({"project": "x"}, resolve_policy({})), "scope-excluded")
        self.assertIsNone(compile_reason({"project": "x"}, resolve_policy({"projects": ["x"]})))


class CompilerStagingTest(_Vault):
    def _stage(self, projects):
        cfg = self.config(project="unscoped", projects=projects, role="memory-engine")
        return select_and_stage_batch(cfg, outbox_root=self.root / "outbox", max_events=10)

    def test_project_events_are_not_staged_without_a_grant(self) -> None:
        self.assertIsNone(self._stage(None))

    def test_a_granted_project_event_is_staged(self) -> None:
        payload = self._stage(["pikselzone-memory-os"])
        self.assertIsNotNone(payload)
        self.assertIn("daily/2026-09-20/codex-sengur-social.md", payload["event_digests"])
        self.assertFalse(any("claude-other" in k for k in payload["event_digests"]))


class CrossProjectDailyRecallTest(_Vault):
    def test_orchestrator_with_a_grant_is_shown_the_sengur_social_note(self) -> None:
        cfg = self.config(project="pikselzone-orchestrator",
                          projects=["pikselzone-memory-os"])
        out = associative_recall_fast(cfg, _QUERY)
        self.assertIn("daily/2026-09-20/codex-sengur-social.md", out)
        self.assertIn("project: pikselzone-memory-os", out)
        self.assertIn("Higgsfield", out)
        self.assertIn("[DERIVED MEMORY", out)

    def test_without_a_grant_the_other_projects_note_stays_private(self) -> None:
        cfg = self.config(project="pikselzone-orchestrator")
        self.assertNotIn("codex-sengur-social", associative_recall_fast(cfg, _QUERY))

    def test_the_asking_sessions_own_note_is_not_echoed(self) -> None:
        cfg = self.config(project="pikselzone-memory-os")
        self.assertIn("codex-sengur-social", associative_recall_fast(cfg, _QUERY))
        self.assertNotIn("codex-sengur-social",
                         associative_recall_fast(cfg, _QUERY, exclude_session_id="s-social"))

    def test_a_generic_prompt_injects_no_daily_note(self) -> None:
        cfg = self.config(project="pikselzone-orchestrator", projects=["pikselzone-memory-os"])
        for prompt in ("testleri çalıştır ve sonucu raporla",
                       "kanban kartlarını güncelle ve commit et lütfen",
                       "lütfen bu python fonksiyonunu biraz daha okunur yap"):
            self.assertNotIn("daily/", associative_recall_fast(cfg, prompt), prompt)

    def test_critical_records_are_not_delivered(self) -> None:
        cfg = self.config(project="pikselzone-memory-os")
        out = associative_recall_fast(cfg, _QUERY)
        self.assertNotIn("critical_records", out)
        self.assertLessEqual(len(out), 2400)


if __name__ == "__main__":
    unittest.main()


class CompilerIgnoresProfilePolicyTest(_Vault):
    """On the VPS the root Hermes home's empty profile grants replaced the
    operator's grants, so a granted project still never reached the compiler."""

    def test_stage_uses_the_config_grants_not_the_profile(self) -> None:
        import io
        from contextlib import redirect_stdout
        from unittest import mock
        from memory_v1 import cli

        config_path = self.root / "memory-config.json"
        config_path.write_text(json.dumps({
            "role": "memory-engine", "vault_path": str(self.vault),
            "state_path": str(self.root / "state"), "runtimes": ["hermes"],
            "transcript_roots": {"hermes": [str(self.root / "hermes-data")]},
            "can_write_event_memory": True, "can_run_compiler": True,
            "provider": {"mode": "runtime-native"},
            "memory": {"projects": ["pikselzone-memory-os"]},
        }), encoding="utf-8")
        profile = {"owner": "root-home", "project": "unscoped", "projects": [],
                   "shared": True, "mode": "normal", "status": "configured"}
        out = io.StringIO()
        with mock.patch("memory_v1.profile_integration.active_settings", return_value=profile), \
                redirect_stdout(out):
            cli.main(["--config", str(config_path), "stage-knowledge-batch",
                      "--outbox", str(self.root / "outbox"), "--max-events", "5"])
        self.assertIn('"staged"', out.getvalue())

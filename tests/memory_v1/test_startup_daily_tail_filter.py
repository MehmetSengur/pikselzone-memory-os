"""§5: SessionStart Tier D (recent daily tail) is filtered by project scope."""
from __future__ import annotations

import hashlib
import sys
import tempfile
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from memory_v1.core import MemoryConfig, iso_now
from memory_v1.events import EventWriter
from memory_v1.recall import _load_recent_daily_tail, build_startup_recall_bundle

_SUMMARY = {
    "status": "memory",
    "context": ["some context"],
    "important_conversations": ["a chat"],
    "decisions": ["a decision"],
    "learnings": ["a learning"],
    "open_items": ["an open item"],
    "evidence": ["fixture"],
}


def _render_event(project: str | None) -> str:
    digest = hashlib.sha256(f"src-{project}".encode()).hexdigest()
    return EventWriter._render(
        runtime="claude", agent_id="claude-main", session_id=f"s-{project}",
        event="session_end", events_seen=["session_end"], created_at=iso_now(),
        source_model="haiku", root_task_id="unknown", kanban_ids=[],
        source_digest=digest, summary=_SUMMARY, redaction_count=0, project=project,
    )


class StartupDailyTailFilterTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="pz-test-tail-")
        self.root = Path(self._tmp.name).resolve()
        self.vault = self.root / "vault"
        day = self.vault / "daily" / "2026-09-01"
        day.mkdir(parents=True)
        for name, project in [
            ("claude-luv.md", "luvaa"),
            ("claude-tb.md", "twoberries"),
            ("hermes-x.md", "unscoped"),
        ]:
            (day / name).write_text(_render_event(project), encoding="utf-8")
        # a legacy event with no `project:` frontmatter key
        legacy = _render_event("luvaa").replace('project: "luvaa"\n', "")
        (day / "claude-legacy.md").write_text(legacy, encoding="utf-8")

        for sub in ("companion", "knowledge/concepts", "knowledge/connections"):
            (self.vault / sub).mkdir(parents=True, exist_ok=True)
        (self.vault / "knowledge" / "index.md").write_text(
            "# Knowledge Base Index\n\nx\n\n| Article | Summary | Source | Updated |\n|---|---|---|---|\n",
            encoding="utf-8",
        )

        self.cfg = MemoryConfig.from_dict({
            "role": "workstation",
            "vault_path": str(self.vault),
            "state_path": str(self.root / "state"),
            "runtimes": ["codex", "claude"],
            "transcript_roots": {"codex": [str(self.root)], "claude": [str(self.root)]},
            "can_write_event_memory": True,
            "can_run_compiler": False,
            "provider": {"mode": "runtime-native"},
        })

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_project_filter_keeps_only_matching_events(self) -> None:
        items = _load_recent_daily_tail(self.cfg, max_events=10, project_filter="luvaa")
        blob = " ".join(i.source_file for i in items)
        self.assertIn("claude-luv.md", blob)
        self.assertNotIn("claude-tb.md", blob)
        self.assertNotIn("hermes-x.md", blob)
        self.assertNotIn("claude-legacy.md", blob)  # no project key -> no match

    def test_none_filter_is_legacy_behaviour(self) -> None:
        items = _load_recent_daily_tail(self.cfg, max_events=10, project_filter=None)
        self.assertGreaterEqual(len(items), 4)

    def test_bundle_tier_d_scoped_to_project(self) -> None:
        bundle = build_startup_recall_bundle(
            self.cfg, runtime="claude", continuity_scope="luvaa", project_filter="luvaa",
        )
        self.assertIn("claude-luv", bundle.text)
        self.assertNotIn("claude-tb", bundle.text)
        self.assertNotIn("hermes-x", bundle.text)

    def test_bundle_hermes_tier_d_scoped_to_unscoped(self) -> None:
        bundle = build_startup_recall_bundle(
            self.cfg, runtime="hermes", continuity_scope="hermes", project_filter="unscoped",
        )
        self.assertIn("hermes-x", bundle.text)
        self.assertNotIn("claude-luv", bundle.text)
        self.assertNotIn("claude-tb", bundle.text)


if __name__ == "__main__":
    unittest.main()

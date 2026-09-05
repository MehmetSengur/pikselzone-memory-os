"""Unit tests for memory_v1.project_registry (V2.3 authority record + capture gate)."""
from __future__ import annotations

import sys
import tempfile
import unittest
import unicodedata
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from memory_v1 import project_registry as pr


class TestProjectRegistry(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="pz-test-registry-")
        self.root = Path(self._tmp.name).resolve()
        self.state = self.root / "state"
        self.repo_a = self.root / "repo-a"
        self.repo_b = self.root / "repo-b"
        self.repo_c = self.root / "repo-c"
        for d in (self.repo_a, self.repo_b, self.repo_c):
            d.mkdir()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    # --- registration --------------------------------------------------------
    def test_register_creates_entry_and_file(self) -> None:
        entry = pr.register(self.state, self.repo_a, "luvaa")
        self.assertEqual(entry.project, "luvaa")
        self.assertEqual(entry.root, str(self.repo_a))
        self.assertTrue(pr.registry_path(self.state).is_file())
        self.assertEqual([e.root for e in pr.load_registry(self.state)], [str(self.repo_a)])

    def test_same_project_multiple_roots_appends(self) -> None:
        pr.register(self.state, self.repo_a, "luvaa")
        pr.register(self.state, self.repo_b, "luvaa")
        pr.register(self.state, self.repo_c, "luvaa")
        roots = sorted(e.root for e in pr.lookup(self.state, "luvaa"))
        self.assertEqual(roots, sorted(str(d) for d in (self.repo_a, self.repo_b, self.repo_c)))
        self.assertIsInstance(pr.lookup(self.state, "luvaa"), list)

    def test_reregister_same_root_updates_project(self) -> None:
        pr.register(self.state, self.repo_a, "luvaa")
        pr.register(self.state, self.repo_a, "twoberries")
        entries = pr.load_registry(self.state)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].project, "twoberries")

    def test_register_rejects_bad_slug_and_reserved(self) -> None:
        for bad in ("Bad_Slug", "UPPER", "has space", "-lead", "trail-", "a" * 65, ""):
            with self.assertRaises(pr.RegistryError):
                pr.register(self.state, self.repo_a, bad)
        for reserved in ("hermes", "unscoped", "shared", "_shared", "startup"):
            with self.assertRaises(pr.RegistryError):
                pr.register(self.state, self.repo_a, reserved)

    def test_register_rejects_relative_or_missing_root(self) -> None:
        with self.assertRaises(pr.RegistryError):
            pr.register(self.state, Path("relative/path"), "luvaa")
        with self.assertRaises(pr.RegistryError):
            pr.register(self.state, self.root / "does-not-exist", "luvaa")

    # --- unregister --------------------------------------------------------
    def test_unregister_is_isolated_to_one_root(self) -> None:
        pr.register(self.state, self.repo_a, "luvaa")
        pr.register(self.state, self.repo_b, "luvaa")
        self.assertTrue(pr.unregister(self.state, self.repo_a))
        remaining = [e.root for e in pr.lookup(self.state, "luvaa")]
        self.assertEqual(remaining, [str(self.repo_b)])
        # Registry-level unregister never touches vault continuity files.
        self.assertFalse(pr.unregister(self.state, self.repo_a))  # already gone -> False

    # --- verify_under_root ------------------------------------------------
    def test_verify_under_root(self) -> None:
        (self.repo_a / "src").mkdir()
        self.assertTrue(pr.verify_under_root(self.repo_a, self.repo_a))
        self.assertTrue(pr.verify_under_root(self.repo_a / "src", self.repo_a))
        self.assertFalse(pr.verify_under_root(self.repo_b, self.repo_a))
        self.assertFalse(pr.verify_under_root(self.root, self.repo_a))

    # --- resolve_capture: the single authority rule ----------------------
    def test_resolve_capture_on_for_exact_match(self) -> None:
        pr.register(self.state, self.repo_a, "luvaa")
        (self.repo_a / "sub").mkdir()
        d = pr.resolve_capture(
            self.state, cwd=self.repo_a / "sub", project="luvaa", project_root=self.repo_a
        )
        self.assertTrue(d.capture)
        self.assertEqual(d.reason, "ok")
        self.assertEqual(d.project, "luvaa")

    def test_resolve_capture_fail_closed_variants(self) -> None:
        pr.register(self.state, self.repo_a, "luvaa")
        cases = {
            "no-project-arg": dict(cwd=self.repo_a, project=None, project_root=None),
            "not-in-registry": dict(cwd=self.repo_b, project="luvaa", project_root=self.repo_b),
            "project-mismatch": dict(cwd=self.repo_a, project="twoberries", project_root=self.repo_a),
            "cwd-outside-root": dict(cwd=self.repo_b, project="luvaa", project_root=self.repo_a),
        }
        for expected_reason, kwargs in cases.items():
            d = pr.resolve_capture(self.state, **kwargs)
            self.assertFalse(d.capture, expected_reason)
            self.assertEqual(d.reason, expected_reason)

    def test_resolve_capture_rejects_reserved_project_arg(self) -> None:
        d = pr.resolve_capture(
            self.state, cwd=self.repo_a, project="hermes", project_root=self.repo_a
        )
        self.assertFalse(d.capture)
        self.assertEqual(d.reason, "project-slug-invalid")

    # --- macOS Unicode identity -----------------------------------------
    def _unicode_repo(self, name: str) -> Path:
        path = self.root / unicodedata.normalize("NFD", name)
        path.mkdir()
        return path

    def test_resolve_capture_accepts_nfc_hook_root_for_nfd_registry_root(self) -> None:
        repo = self._unicode_repo("İçerik-Otomasyon")
        pr.register(self.state, repo, "icerik-otomasyon")
        nfc_root = Path(unicodedata.normalize("NFC", str(repo)))
        self.assertTrue(nfc_root.is_dir(), "filesystem must prove NFC/NFD equivalence")

        decision = pr.resolve_capture(
            self.state, cwd=nfc_root, project="icerik-otomasyon", project_root=nfc_root
        )
        self.assertTrue(decision.capture)
        self.assertEqual(decision.reason, "ok")

    def test_resolve_capture_accepts_nfd_hook_root_for_nfc_registry_root(self) -> None:
        repo = self._unicode_repo("İçerik-Otomasyon")
        nfc_root = Path(unicodedata.normalize("NFC", str(repo)))
        self.assertTrue(nfc_root.is_dir(), "filesystem must prove NFC/NFD equivalence")
        pr.register(self.state, nfc_root, "icerik-otomasyon")

        decision = pr.resolve_capture(
            self.state, cwd=repo, project="icerik-otomasyon", project_root=repo
        )
        self.assertTrue(decision.capture)
        self.assertEqual(decision.reason, "ok")

    def test_unicode_normalisation_does_not_match_different_path(self) -> None:
        repo = self._unicode_repo("İçerik-Otomasyon")
        sibling = self._unicode_repo("İçerik-Otomasyon-archive")
        pr.register(self.state, repo, "icerik-otomasyon")

        decision = pr.resolve_capture(
            self.state, cwd=sibling, project="icerik-otomasyon", project_root=sibling
        )
        self.assertFalse(decision.capture)
        self.assertEqual(decision.reason, "not-in-registry")

    def test_unicode_normalisation_does_not_allow_prefix_sibling_or_escape(self) -> None:
        repo = self._unicode_repo("İçerik-Otomasyon")
        prefix_sibling = self._unicode_repo("İçerik-Otomasyon-copy")
        outside = self.root / "outside"
        outside.mkdir()
        pr.register(self.state, repo, "icerik-otomasyon")
        nfc_root = Path(unicodedata.normalize("NFC", str(repo)))

        for cwd in (prefix_sibling, outside):
            decision = pr.resolve_capture(
                self.state, cwd=cwd, project="icerik-otomasyon", project_root=nfc_root
            )
            self.assertFalse(decision.capture)
            self.assertEqual(decision.reason, "cwd-outside-root")

    def test_unicode_normalisation_preserves_symlink_escape_rejection(self) -> None:
        repo = self._unicode_repo("İçerik-Otomasyon")
        outside = self.root / "outside"
        outside.mkdir()
        escape = repo / "escape"
        escape.symlink_to(outside, target_is_directory=True)
        pr.register(self.state, repo, "icerik-otomasyon")
        nfc_root = Path(unicodedata.normalize("NFC", str(repo)))

        decision = pr.resolve_capture(
            self.state, cwd=escape, project="icerik-otomasyon", project_root=nfc_root
        )
        self.assertFalse(decision.capture)
        self.assertEqual(decision.reason, "cwd-outside-root")

    # --- corrupt / missing registry ------------------------------------
    def test_missing_registry_is_empty_not_error(self) -> None:
        self.assertEqual(pr.load_registry(self.state), [])

    def test_corrupt_registry_raises(self) -> None:
        p = pr.registry_path(self.state)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text('{"schema": "wrong", "projects": []}', encoding="utf-8")
        with self.assertRaises(pr.RegistryError):
            pr.load_registry(self.state)


if __name__ == "__main__":
    unittest.main()

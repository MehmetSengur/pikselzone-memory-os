"""Independent policy baseline verifier against committed git artifacts."""
from __future__ import annotations

import hashlib
import os
import stat
import subprocess
from pathlib import Path
from typing import Any

from .core import MemoryConfig, PolicyError


BASELINE_FILE = Path(__file__).resolve().parent.parent / "policy" / "expected-memory-plugin-baseline.tsv"


def load_expected_baseline(baseline_path: Path | None = None) -> list[tuple[str, str, str, str]]:
    path = baseline_path or BASELINE_FILE
    if not path.exists():
        raise PolicyError(f"expected-baseline-file-missing:{path}")
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("	")
        if len(parts) != 4:
            raise PolicyError(f"malformed-baseline-line:{line}")
        rows.append((parts[0], parts[1], parts[2], parts[3]))
    return rows


def verify_local_git_against_baseline(repo_root: Path | None = None) -> tuple[bool, str]:
    root = repo_root or Path(__file__).resolve().parent.parent
    plugin_root = root / "hermes_plugins" / "pz-memory-v1"
    init_sha = hashlib.sha256((plugin_root / "__init__.py").read_bytes()).hexdigest()
    yaml_sha = hashlib.sha256((plugin_root / "plugin.yaml").read_bytes()).hexdigest()
    kg_sha = hashlib.sha256((plugin_root / "knowledge_generator.py").read_bytes()).hexdigest()

    expected_shas = {
        "__init__.py": init_sha,
        "plugin.yaml": yaml_sha,
        "knowledge_generator.py": kg_sha,
    }

    rows = load_expected_baseline()
    for want_sha, want_og, want_mode, rel in rows:
        fname = Path(rel).name
        if expected_shas.get(fname) != want_sha:
            return False, f"git-source-sha-drift:{fname}:want-{want_sha}-got-{expected_shas.get(fname)}"
        if want_og != "pzhermes:pzvault":
            return False, f"invalid-expected-owner:{want_og}"
        if want_mode != "0640":
            return False, f"invalid-expected-mode:{want_mode}"
    return True, "verified"


def verify_live_against_baseline(live_root: Path) -> tuple[bool, str]:
    rows = load_expected_baseline()
    for want_sha, want_og, want_mode, rel in rows:
        target = live_root / rel
        if not target.exists():
            return False, f"missing-live-file:{rel}"
        data = target.read_bytes()
        actual_sha = hashlib.sha256(data).hexdigest()
        if actual_sha != want_sha:
            return False, f"live-sha-mismatch:{rel}:want-{want_sha}-got-{actual_sha}"
        st = target.stat()
        try:
            import grp
            import pwd
            got_og = f"{pwd.getpwuid(st.st_uid).pw_name}:{grp.getgrgid(st.st_gid).gr_name}"
        except KeyError:
            got_og = f"{st.st_uid}:{st.st_gid}"
        if got_og != want_og:
            return False, f"live-owner-mismatch:{rel}:want-{want_og}-got-{got_og}"
        mode = format(st.st_mode & 0o7777, "04o")
        if mode != want_mode:
            return False, f"live-mode-mismatch:{rel}:want-{want_mode}-got-{mode}"
    return True, "verified"

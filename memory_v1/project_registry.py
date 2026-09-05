"""Project registry: the authority record mapping repo roots to project slugs.

The registry does NOT infer a project at runtime.  Hooks carry their identity
explicitly (``--project`` / ``--project-root``, baked in by ``memory register``);
the registry is consulted only to *authorise* that identity.  Registry file:
``<state_path>/registry/projects.json``.

Single authority rule (see V2.3 plan 1c) applied on every claude/codex hook call:

    hook --project=<slug> --project-root=<root>
      AND registry has an exact {project: <slug>, root: <root>} entry
      AND cwd is within <root>
          -> CAPTURE ON
    otherwise -> CAPTURE OFF (fail-closed; no warn-and-continue)
"""
from __future__ import annotations

import dataclasses
import os
import re
import unicodedata
from pathlib import Path
from typing import Any

from .core import ConfigError, PolicyError, atomic_json, iso_now, path_within

REGISTRY_SCHEMA = "pikselzone-project-registry-v1"
SLUG_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?")
# Reserved scopes that are not real registrable projects.
RESERVED_SLUGS = frozenset({"unscoped", "hermes", "shared", "_shared", "startup"})


class RegistryError(PolicyError):
    """Fail-closed registry violation."""


@dataclasses.dataclass(frozen=True)
class ProjectEntry:
    project: str
    root: str
    registered_at: str

    def to_dict(self) -> dict[str, str]:
        return {"project": self.project, "root": self.root, "registered_at": self.registered_at}


def registry_path(state_path: Path) -> Path:
    return state_path / "registry" / "projects.json"


def validate_slug(project: str) -> str:
    value = str(project or "").strip()
    if not SLUG_RE.fullmatch(value):
        raise RegistryError(f"project-slug-invalid:{value[:64]}")
    if value in RESERVED_SLUGS:
        raise RegistryError(f"project-slug-reserved:{value}")
    return value


def _normalize_root(root: Path | str) -> str:
    """Return the filesystem-canonical identity string for a project root.

    ``Path.resolve`` remains the authority for traversal and symlink handling.
    macOS may preserve a decomposed Unicode spelling when it renders that path,
    even though its NFC spelling names the very same directory.  We use an NFC
    spelling only after ``samefile`` proves that it resolves to that directory;
    on filesystems where the spellings are distinct, the resolved spelling is
    retained.  This is identity canonicalisation, never fuzzy matching.
    """
    path = Path(str(root))
    if not path.is_absolute():
        raise RegistryError("project-root-not-absolute")
    resolved = path.resolve(strict=False)
    nfc = Path(unicodedata.normalize("NFC", str(resolved)))
    try:
        if nfc != resolved and nfc.exists() and os.path.samefile(resolved, nfc):
            resolved = nfc.resolve(strict=False)
    except OSError:
        # A missing or inaccessible NFC spelling cannot be treated as an
        # equivalent root.  Keep the filesystem-resolved spelling fail-closed.
        pass
    return str(resolved)


def load_registry(state_path: Path) -> list[ProjectEntry]:
    path = registry_path(state_path)
    try:
        import json

        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except (OSError, ValueError) as exc:
        raise RegistryError(f"registry-read:{exc.__class__.__name__}") from exc
    if not isinstance(raw, dict) or raw.get("schema") != REGISTRY_SCHEMA:
        raise RegistryError("registry-schema-invalid")
    projects = raw.get("projects")
    if not isinstance(projects, list):
        raise RegistryError("registry-projects-invalid")
    entries: list[ProjectEntry] = []
    for item in projects:
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("project"), str)
            or not isinstance(item.get("root"), str)
        ):
            raise RegistryError("registry-entry-invalid")
        entries.append(
            ProjectEntry(
                project=item["project"],
                root=item["root"],
                registered_at=str(item.get("registered_at", "")),
            )
        )
    return entries


def _write_registry(state_path: Path, entries: list[ProjectEntry]) -> None:
    payload: dict[str, Any] = {
        "schema": REGISTRY_SCHEMA,
        "updated_at": iso_now(),
        "projects": [entry.to_dict() for entry in sorted(entries, key=lambda e: (e.project, e.root))],
    }
    atomic_json(registry_path(state_path), payload)


def register(state_path: Path, root: Path | str, project: str) -> ProjectEntry:
    """Add (or, for an already-known root, update) a root -> project mapping.

    A project may own many roots: registering a second root under the same slug
    appends a new entry, it is not an error.  Re-registering the same root updates
    its project.
    """
    slug = validate_slug(project)
    norm_root = _normalize_root(root)
    if not Path(norm_root).is_dir():
        raise RegistryError(f"project-root-not-a-directory:{norm_root}")
    entries = [
        e for e in load_registry(state_path)
        if _normalize_root(e.root) != norm_root
    ]
    entry = ProjectEntry(project=slug, root=norm_root, registered_at=iso_now())
    entries.append(entry)
    _write_registry(state_path, entries)
    return entry


def unregister(state_path: Path, root: Path | str) -> bool:
    """Remove the entry for exactly ``root``.  Other roots of the same project
    are untouched; ``continuity/<slug>.md`` is left in place."""
    norm_root = _normalize_root(root)
    entries = load_registry(state_path)
    kept = [e for e in entries if _normalize_root(e.root) != norm_root]
    if len(kept) == len(entries):
        return False
    _write_registry(state_path, kept)
    return True


def lookup(state_path: Path, project: str) -> list[ProjectEntry]:
    """All roots registered under ``project`` (introspection / doctor only)."""
    return [e for e in load_registry(state_path) if e.project == project]


def lookup_root(state_path: Path, root: Path | str) -> ProjectEntry | None:
    """The entry whose filesystem-canonical root matches ``root``, if any."""
    norm_root = _normalize_root(root)
    for entry in load_registry(state_path):
        if _normalize_root(entry.root) == norm_root:
            return entry
    return None


def verify_under_root(cwd: Path | str, project_root: Path | str) -> bool:
    try:
        return path_within(
            Path(_normalize_root(cwd)), Path(_normalize_root(project_root))
        )
    except (OSError, ValueError, RegistryError):
        return False


@dataclasses.dataclass(frozen=True)
class CaptureDecision:
    capture: bool
    reason: str
    project: str | None = None
    root: str | None = None


def resolve_capture(
    state_path: Path,
    *,
    cwd: Path | str,
    project: str | None,
    project_root: Path | str | None,
) -> CaptureDecision:
    """Apply the single authority rule for a claude/codex hook invocation.

    Fail-closed: any deviation from an exact registry match yields
    ``capture=False`` with a machine-readable ``reason``.
    """
    if not project or not project_root:
        return CaptureDecision(False, "no-project-arg")
    try:
        slug = validate_slug(project)
    except RegistryError:
        return CaptureDecision(False, "project-slug-invalid")
    try:
        norm_root = _normalize_root(project_root)
    except RegistryError:
        return CaptureDecision(False, "project-root-invalid")
    entry = lookup_root(state_path, norm_root)
    if entry is None:
        return CaptureDecision(False, "not-in-registry", project=slug, root=norm_root)
    if entry.project != slug:
        return CaptureDecision(False, "project-mismatch", project=slug, root=norm_root)
    if not verify_under_root(cwd, norm_root):
        return CaptureDecision(False, "cwd-outside-root", project=slug, root=norm_root)
    return CaptureDecision(True, "ok", project=slug, root=norm_root)


__all__ = [
    "REGISTRY_SCHEMA",
    "RegistryError",
    "ProjectEntry",
    "CaptureDecision",
    "registry_path",
    "validate_slug",
    "load_registry",
    "register",
    "unregister",
    "lookup",
    "lookup_root",
    "verify_under_root",
    "resolve_capture",
]

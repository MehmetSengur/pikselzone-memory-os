"""Install / remove the Memory OS lifecycle hooks in a project repo.

``memory register <root> --project <slug>`` merges five hook entries into the
repo's ``.claude/settings.local.json`` and ``.codex/hooks.json``:

    SessionStart  SessionEnd  PreCompact  Stop  UserPromptSubmit

Every entry's command is
``PYTHONPATH=<memory-os> python3 -m memory_v1.hook_runner --config <abs> \\
  --runtime <rt> --event <E> --project <slug> --project-root <root>``
(no ``cd`` -- the hook must run in the session's real cwd so the capture gate's
``verify_under_root`` check works).

Merges are non-destructive: existing ``permissions``, a ``PreToolUse`` guard,
and any other hooks are preserved.  Our own entries are identified by the
``memory_v1.hook_runner`` marker in the command string, so ``unregister`` removes
exactly those and nothing else.  Both operations are idempotent.
"""
from __future__ import annotations

import json
import shlex
from pathlib import Path

from .core import atomic_write

MEMORY_MARKER = "memory_v1.hook_runner"
MEMORY_EVENTS = ("SessionStart", "SessionEnd", "PreCompact", "Stop", "UserPromptSubmit")
_EVENT_TIMEOUT = {
    "SessionStart": 5,
    "UserPromptSubmit": 5,
    "SessionEnd": 10,
    "PreCompact": 10,
    "Stop": 10,
}
_RUNTIME_FILE = {
    "claude": (".claude", "settings.local.json"),
    "codex": (".codex", "hooks.json"),
}


def _command(
    *, memory_os_root: Path, config_path: Path, runtime: str, event: str,
    project: str, project_root: Path,
) -> str:
    return (
        f"PYTHONPATH={shlex.quote(str(memory_os_root))} python3 -m memory_v1.hook_runner "
        f"--config {shlex.quote(str(config_path))} "
        f"--runtime {runtime} --event {event} "
        f"--project {shlex.quote(project)} "
        f"--project-root {shlex.quote(str(project_root))}"
    )


def _our_block(command: str, event: str) -> dict:
    return {"hooks": [{"type": "command", "command": command, "timeout": _EVENT_TIMEOUT[event]}]}


def _is_ours(entry: object) -> bool:
    if not isinstance(entry, dict):
        return False
    for hook in entry.get("hooks", []):
        if isinstance(hook, dict) and MEMORY_MARKER in str(hook.get("command", "")):
            return True
    return False


def _load(path: Path) -> dict:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        raise ValueError(f"hook-file-unreadable:{path}:{exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError(f"hook-file-not-object:{path}")
    return raw


def _dump(data: dict) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def _merge(existing: dict, our_events: dict[str, dict]) -> dict:
    """Return a copy of ``existing`` with our five hook entries (re)installed,
    every non-memory hook and top-level key untouched."""
    result = json.loads(json.dumps(existing))  # deep copy, JSON-safe by construction
    hooks = result.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError("hook-file-hooks-not-object")
    for event, block in our_events.items():
        current = hooks.get(event, [])
        if not isinstance(current, list):
            raise ValueError(f"hook-file-event-not-list:{event}")
        kept = [entry for entry in current if not _is_ours(entry)]
        hooks[event] = kept + [block]
    return result


def _strip(existing: dict) -> dict:
    """Return a copy with only our entries removed; empty event keys and an
    empty ``hooks`` map are pruned so ``unregister`` leaves no residue."""
    result = json.loads(json.dumps(existing))
    hooks = result.get("hooks")
    if not isinstance(hooks, dict):
        return result
    for event in list(hooks.keys()):
        current = hooks.get(event, [])
        if not isinstance(current, list):
            continue
        kept = [entry for entry in current if not _is_ours(entry)]
        if kept:
            hooks[event] = kept
        else:
            del hooks[event]
    if not hooks:
        del result["hooks"]
    return result


def _runtime_path(root: Path, runtime: str) -> Path:
    sub, name = _RUNTIME_FILE[runtime]
    return root / sub / name


def install(
    root: Path, *, runtime: str, memory_os_root: Path, config_path: Path, project: str,
) -> bool:
    """Merge our five hooks into the repo's hook file for ``runtime``.

    Returns True if the file content changed, False if it was already current.
    """
    if runtime not in _RUNTIME_FILE:
        raise ValueError(f"unknown-runtime:{runtime}")
    target = _runtime_path(root, runtime)
    existing = _load(target)
    our_events = {
        event: _our_block(
            _command(
                memory_os_root=memory_os_root, config_path=config_path,
                runtime=runtime, event=event, project=project, project_root=root,
            ),
            event,
        )
        for event in MEMORY_EVENTS
    }
    merged = _merge(existing, our_events)
    new_text = _dump(merged)
    old_text = target.read_text(encoding="utf-8") if target.is_file() else None
    if new_text == old_text:
        return False
    atomic_write(target, new_text, mode=0o600)
    return True


def uninstall(root: Path, *, runtime: str) -> bool:
    """Remove exactly our hook entries from the repo's hook file for ``runtime``.

    Returns True if the file changed.  A hook file that becomes ``{}`` is left
    in place (empty), not deleted."""
    if runtime not in _RUNTIME_FILE:
        raise ValueError(f"unknown-runtime:{runtime}")
    target = _runtime_path(root, runtime)
    if not target.is_file():
        return False
    existing = _load(target)
    stripped = _strip(existing)
    new_text = _dump(stripped)
    old_text = target.read_text(encoding="utf-8")
    if new_text == old_text:
        return False
    atomic_write(target, new_text, mode=0o600)
    return True


def gitignore_unignored(root: Path) -> list[str]:
    """Paths that ``memory register`` writes but that ``<root>/.gitignore`` does
    not appear to exclude -- returned so the CLI can warn (it never edits
    .gitignore itself)."""
    wanted = [".claude/settings.local.json", ".codex/hooks.json"]
    gi = root / ".gitignore"
    if not gi.is_file():
        return wanted
    try:
        lines = {ln.strip().lstrip("/") for ln in gi.read_text(encoding="utf-8").splitlines()}
    except OSError:
        return wanted
    missing = []
    for path in wanted:
        base = path.split("/")[-1]
        if path in lines or base in lines or f"{path.split('/')[0]}/" in lines:
            continue
        missing.append(path)
    return missing


__all__ = [
    "MEMORY_MARKER",
    "MEMORY_EVENTS",
    "install",
    "uninstall",
    "gitignore_unignored",
]

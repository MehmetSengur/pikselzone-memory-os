"""Read-only view of the native Hermes finalize backlog.

The Hermes plugin owns its own state inside the runtime's shared data directory
(``<hermes-data>/memory-v1/state``): turn checkpoints, settlement records and,
since the finalize-retry work, bounded retry records.  The engine cannot write
there, but the doctor must be able to *see* it, because the two observations
that already existed were both misleading on their own:

* ``health/flush-hermes`` is last-write-wins.  One session settling normally
  overwrites the ``blocked`` row an earlier failed session left behind, so a
  healthy latest flush silently hid unresolved work.
* ``pending_checkpoints`` counts the engine's own ``queue/pending`` directory,
  which is a Claude/Codex path and is always empty on the memory engine.

Nothing here infers failure from file counts alone.  A checkpoint file may
legitimately outlive its settlement (a later turn in a settled session keeps its
own checkpoint), so a session is only reported as unresolved when it has no
settlement record *and* no retry record accounting for it.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .core import MemoryConfig

FINALIZE_RETRY_SCHEMA = "pikselzone-memory-hermes-finalize-retry-v1"
CHECKPOINT_SCHEMA = "pikselzone-memory-turn-checkpoint-v2"
SETTLEMENT_SCHEMA = "pikselzone-memory-hermes-settlement-v1"

#: Bounds so a doctor run stays cheap no matter how large the backlog grows.
MAX_SCANNED_FILES = 5000
MAX_RECORD_BYTES = 256 * 1024
MAX_REPORTED_SESSIONS = 10

RETRY_STATUS_KEYS = {
    "retry-scheduled": "scheduled",
    "retry-exhausted": "exhausted",
    "permanent": "permanent",
    "hold-unclassified": "hold",
}


def hermes_runtime_base(config: MemoryConfig) -> Path | None:
    """Locate the plugin's runtime base, the way the publisher already does.

    Returns ``None`` when this deployment has no Hermes runtime, so a
    workstation never reports on a directory that is not part of its topology.
    """
    if "hermes" not in config.runtimes:
        return None
    roots = config.transcript_roots.get("hermes", ())
    if roots:
        return Path(roots[0]) / "memory-v1"
    return config.state_path.parent / "hermes-data" / "memory-v1"


def _read_bounded_json(path: Path) -> dict[str, Any]:
    """Parse one small JSON file, refusing symlinks and oversized content."""
    try:
        if path.is_symlink() or not path.is_file():
            return {}
        if path.stat().st_size > MAX_RECORD_BYTES:
            return {}
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def _scan(directory: Path, schema: str) -> list[dict[str, Any]]:
    if not directory.is_dir() or directory.is_symlink():
        return []
    records: list[dict[str, Any]] = []
    try:
        names = sorted(directory.glob("*.json"))[:MAX_SCANNED_FILES]
    except OSError:
        return []
    for path in names:
        record = _read_bounded_json(path)
        if record.get("schema") == schema:
            records.append(record)
    return records


def backlog_metrics(config: MemoryConfig) -> dict[str, Any]:
    """Bounded, deterministic counts of the plugin's unfinished finalize work."""
    empty: dict[str, Any] = {
        "available": False,
        "scheduled": 0,
        "hold": 0,
        "permanent": 0,
        "exhausted": 0,
        "retry_tracked_sessions": 0,
        "sessions_with_checkpoints": 0,
        "settled_sessions": 0,
        "unresolved_sessions": 0,
        "unresolved_sample": [],
    }
    base = hermes_runtime_base(config)
    if base is None or not base.is_dir():
        return empty

    state = base / "state"
    counts = {"scheduled": 0, "hold": 0, "permanent": 0, "exhausted": 0}
    retry_sessions: set[str] = set()
    for record in _scan(state / "finalize-retry", FINALIZE_RETRY_SCHEMA):
        key = RETRY_STATUS_KEYS.get(str(record.get("status")))
        if key:
            counts[key] += 1
        session_id = record.get("session_id")
        if isinstance(session_id, str) and session_id:
            retry_sessions.add(session_id)

    checkpoint_sessions: set[str] = set()
    for record in _scan(state / "checkpoints", CHECKPOINT_SCHEMA):
        session_id = record.get("session_id")
        if record.get("runtime") == "hermes" and isinstance(session_id, str) and session_id:
            checkpoint_sessions.add(session_id)

    settled_sessions: set[str] = set()
    for record in _scan(state / "settlements", SETTLEMENT_SCHEMA):
        session_id = record.get("session_id")
        if isinstance(session_id, str) and session_id:
            settled_sessions.add(session_id)

    # Unresolved: raw turns are preserved, but nothing -- neither a settlement
    # nor a retry record -- accounts for them.  These are the sessions that
    # predate the retry contract, or whose failure was never recorded.
    unresolved = sorted(checkpoint_sessions - settled_sessions - retry_sessions)
    return {
        "available": True,
        **counts,
        "retry_tracked_sessions": len(retry_sessions),
        "sessions_with_checkpoints": len(checkpoint_sessions),
        "settled_sessions": len(checkpoint_sessions & settled_sessions),
        "unresolved_sessions": len(unresolved),
        "unresolved_sample": unresolved[:MAX_REPORTED_SESSIONS],
    }

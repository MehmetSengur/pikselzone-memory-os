"""Bounded, observable retry state for pending lifecycle checkpoints.

A checkpoint is the durable half of the capture contract: the lifecycle hook
writes raw normalized transcript data before it returns, and a detached worker
turns it into a daily event artifact.  When that worker hits a *transient*
provider failure -- a usage limit, a timeout, a killed subprocess -- the raw
checkpoint is correctly preserved, but nothing ever looked at it again.  A real
``session_end`` checkpoint sat pending for four days that way while its session
already had a ``pre_compact`` daily artifact claiming the work was unverified.

This module adds the missing half without a new scheduler or daemon: a small
sidecar record per checkpoint, a conservative transient/permanent failure
classification, and a bounded selection helper the existing SessionStart
recovery path uses to re-spawn the same detached drain worker.

Invariants:

* Raw checkpoints are never deleted here.  Only a successful, duplicate, or
  settled drain removes them, exactly as before.
* Permanent failures (schema, policy, unsafe path, malformed input) are never
  scheduled for automatic retry.
* Retry is bounded by ``MAX_DRAIN_ATTEMPTS`` and spaced by exponential backoff,
  so a persistently failing checkpoint cannot loop.
* Every decision is written down, so ``doctor`` can report it.
"""
from __future__ import annotations

import datetime as dt
import json
import re
from pathlib import Path
from typing import Any

from .core import (
    MemoryConfig, MemoryError, atomic_json, ensure_safe_directory, path_within,
    safe_unlink, write_health,
)


RETRY_SCHEMA = "pikselzone-memory-checkpoint-retry-v1"
RETRY_HEALTH_COMPONENT = "checkpoint-retry"

#: A checkpoint that keeps failing must stop asking.  Five attempts spread over
#: the backoff schedule below covers an ordinary provider outage without ever
#: becoming an unbounded loop.
MAX_DRAIN_ATTEMPTS = 5
BASE_BACKOFF_SECONDS = 300
MAX_BACKOFF_SECONDS = 6 * 3600

#: Only checkpoints older than this are treated as stale.  A younger one still
#: belongs to the worker the lifecycle hook just spawned.
STALE_CHECKPOINT_MIN_AGE_SECONDS = 900

#: Startup recovery is a 5-second hook.  It spawns a hard-capped number of
#: detached workers and returns.
MAX_STALE_RECOVERY_SPAWNS = 2

#: Terminal boundaries are the only ones stale recovery promotes.  A pending
#: ``turn_complete`` checkpoint is deliberately not a promotion boundary (see
#: ``drain_checkpoint``); it is recovered by the existing current-session path,
#: absorbed by the next terminal flush, or promoted by idle finalize below.
STALE_RECOVERABLE_EVENTS = ("session_end", "pre_compact", "session_finalize", "session_reset")

#: Idle finalize is a workstation concern: a Codex Desktop/App thread that is
#: never archived, closed, or left unopened for 30 minutes never reaches
#: SessionEnd, so its turns would otherwise stay pending forever.  It runs only
#: from SessionStart; it is not a timer.  Hermes owns its own native lifecycle
#: and is never swept here.
IDLE_FINALIZE_RUNTIMES = ("codex", "claude")
MAX_IDLE_FINALIZE_SPAWNS = 2

CHECKPOINT_NAME_RE = re.compile(
    r"^(?P<runtime>codex|claude|hermes)-(?P<session_key>[0-9a-f]{32})"
    r"-(?P<event>[a-z_]+)-(?P<token>[0-9a-f]{16})\.json$"
)

#: Checked first.  These describe a configuration or contract that no amount of
#: waiting repairs, even though the provider raised them.
PERMANENT_MARKERS = (
    "recursion-detected",
    "credential-missing",
    "keychain-read-failed",
    "api-base-forbidden",
    "prohibits-silent-api-fallback",
    "unsupported-runtime",
    "unknown-provider-mode",
    "unconfigured",
    "already-settled",
)

#: Checked second.  Everything that is not explicitly listed here stays
#: permanent, so an unknown failure mode never becomes an automatic loop.
TRANSIENT_MARKERS = (
    "-timeout",
    "-process-failed",
    "-exec-error",
    "provider-transport",
    "provider-http-408",
    "provider-http-429",
    "provider-http-500",
    "provider-http-502",
    "provider-http-503",
    "provider-http-504",
    "provider-http-529",
    "-turn-failed",
    "-no-message",
    "-error-response",
    "provider-output-empty",
    "-result-empty",
)


def classify_drain_failure(exc: BaseException) -> str:
    """Return ``"retryable"`` or ``"permanent"`` for a failed drain.

    Only :class:`ProviderBlocked`-shaped transport failures are retryable.
    Schema, policy and configuration errors describe malformed or unsafe input
    and are permanent by construction: retrying them cannot change the outcome
    and would burn the bounded attempt budget of a checkpoint that may still be
    repairable by hand.
    """
    from .core import ProviderBlocked

    reason = str(exc)
    if any(marker in reason for marker in PERMANENT_MARKERS):
        return "permanent"
    if not isinstance(exc, ProviderBlocked):
        return "permanent"
    if any(marker in reason for marker in TRANSIENT_MARKERS):
        return "retryable"
    return "permanent"


def retry_dir(config: MemoryConfig) -> Path:
    """Sidecar directory.  Deliberately *not* inside ``queue/pending`` so the
    session checkpoint glob never sees a retry record as a checkpoint."""
    return config.state_path / "queue" / "retry"


def retry_state_path(config: MemoryConfig, queue_path: Path) -> Path | None:
    """Map a pending checkpoint to its sidecar, or ``None`` if the name is not
    a checkpoint this module is willing to track."""
    pending = config.state_path / "queue" / "pending"
    if not queue_path.is_absolute() or not path_within(queue_path, pending):
        return None
    if not CHECKPOINT_NAME_RE.match(queue_path.name):
        return None
    return retry_dir(config) / queue_path.name


def load_retry_state(config: MemoryConfig, queue_path: Path) -> dict[str, Any]:
    path = retry_state_path(config, queue_path)
    if path is None or not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(value, dict) or value.get("schema") != RETRY_SCHEMA:
        return {}
    return value


def _backoff_seconds(attempts: int) -> int:
    exponent = max(0, min(attempts - 1, 16))
    return min(MAX_BACKOFF_SECONDS, BASE_BACKOFF_SECONDS * (2 ** exponent))


def _parse_iso(value: Any) -> dt.datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed


def record_drain_failure(
    config: MemoryConfig, queue_path: Path, exc: BaseException
) -> dict[str, Any]:
    """Persist one bounded failure observation and return the new state."""
    path = retry_state_path(config, queue_path)
    if path is None:
        return {}
    match = CHECKPOINT_NAME_RE.match(queue_path.name)
    assert match is not None  # guaranteed by retry_state_path
    previous = load_retry_state(config, queue_path)
    classification = classify_drain_failure(exc)
    attempts = int(previous.get("attempts") or 0) + 1
    now = dt.datetime.now().astimezone()
    if classification == "permanent":
        status = "permanent"
        next_attempt_after = None
    elif attempts >= MAX_DRAIN_ATTEMPTS:
        status = "retry-exhausted"
        next_attempt_after = None
    else:
        status = "retry-scheduled"
        next_attempt_after = (
            now + dt.timedelta(seconds=_backoff_seconds(attempts))
        ).isoformat(timespec="seconds")
    state = {
        "schema": RETRY_SCHEMA,
        "checkpoint_id": queue_path.name,
        "runtime": match.group("runtime"),
        "session_key": match.group("session_key"),
        "event": match.group("event"),
        "attempts": attempts,
        "max_attempts": MAX_DRAIN_ATTEMPTS,
        "classification": classification,
        "status": status,
        "last_error_type": exc.__class__.__name__,
        "last_reason": str(exc)[:500],
        "first_failure_at": previous.get("first_failure_at") or now.isoformat(timespec="seconds"),
        "last_failure_at": now.isoformat(timespec="seconds"),
        "next_attempt_after": next_attempt_after,
    }
    try:
        ensure_safe_directory(path.parent, create=True)
        atomic_json(path, state)
    except (MemoryError, OSError):
        return state
    _write_retry_health(config)
    return state


def clear_retry_state(config: MemoryConfig, queue_path: Path) -> None:
    """Drop the sidecar once the checkpoint is settled one way or another."""
    path = retry_state_path(config, queue_path)
    if path is None or not path.is_file():
        return
    try:
        safe_unlink(path, root=retry_dir(config))
    except (MemoryError, OSError):
        return
    _write_retry_health(config)


def retry_states(config: MemoryConfig) -> list[dict[str, Any]]:
    directory = retry_dir(config)
    if not directory.is_dir():
        return []
    states: list[dict[str, Any]] = []
    for path in sorted(directory.glob("*.json")):
        if not path.is_file():
            continue
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(value, dict) and value.get("schema") == RETRY_SCHEMA:
            states.append(value)
    return states


def retry_summary(config: MemoryConfig) -> dict[str, int]:
    counts = {"scheduled": 0, "exhausted": 0, "permanent": 0}
    for state in retry_states(config):
        status = state.get("status")
        if status == "retry-scheduled":
            counts["scheduled"] += 1
        elif status == "retry-exhausted":
            counts["exhausted"] += 1
        elif status == "permanent":
            counts["permanent"] += 1
    return counts


def _write_retry_health(config: MemoryConfig) -> None:
    counts = retry_summary(config)
    stuck = counts["exhausted"] + counts["permanent"]
    detail = (
        f"scheduled={counts['scheduled']};"
        f"exhausted={counts['exhausted']};permanent={counts['permanent']}"
    )
    try:
        write_health(
            config.state_path, RETRY_HEALTH_COMPONENT,
            "warn" if (stuck or counts["scheduled"]) else "ok", detail,
        )
    except OSError:
        pass


def retry_due(state: dict[str, Any], *, now: dt.datetime | None = None) -> bool:
    """A checkpoint with no recorded failure is due; a permanent or exhausted
    one never is; a scheduled one waits out its backoff."""
    if not state:
        return True
    status = state.get("status")
    if status in {"permanent", "retry-exhausted"}:
        return False
    if int(state.get("attempts") or 0) >= MAX_DRAIN_ATTEMPTS:
        return False
    scheduled_for = _parse_iso(state.get("next_attempt_after"))
    if scheduled_for is None:
        return True
    return (now or dt.datetime.now().astimezone()) >= scheduled_for


def find_stale_recoverable_checkpoints(
    config: MemoryConfig, *, runtime: str, now: dt.datetime | None = None,
    limit: int = MAX_STALE_RECOVERY_SPAWNS,
    min_age_seconds: int = STALE_CHECKPOINT_MIN_AGE_SECONDS,
) -> list[Path]:
    """Terminal checkpoints of this runtime whose drain never completed.

    Bounded on every axis: terminal events only, older than ``min_age_seconds``,
    past their backoff, oldest first, at most ``limit`` results.
    """
    pending = config.state_path / "queue" / "pending"
    if not pending.is_dir():
        return []
    moment = now or dt.datetime.now().astimezone()
    cutoff = moment.timestamp() - max(0, min_age_seconds)
    candidates: list[tuple[float, Path]] = []
    for path in pending.glob(f"{runtime}-*.json"):
        match = CHECKPOINT_NAME_RE.match(path.name)
        if match is None or match.group("event") not in STALE_RECOVERABLE_EVENTS:
            continue
        try:
            info = path.lstat()
        except OSError:
            continue
        if not path.is_file() or info.st_mtime > cutoff:
            continue
        if not retry_due(load_retry_state(config, path), now=moment):
            continue
        candidates.append((info.st_mtime, path))
    candidates.sort()
    return [path for _, path in candidates[:max(0, limit)]]


def find_idle_turn_batches(
    config: MemoryConfig, *, now: dt.datetime | None = None,
    limit: int = MAX_IDLE_FINALIZE_SPAWNS,
    idle_seconds: int | None = None,
    exclude: frozenset[tuple[str, str]] = frozenset(),
) -> list[Path]:
    """One representative turn checkpoint per workstation session gone idle.

    A session qualifies only when every pending checkpoint it has is a raw
    ``turn_complete`` (a pending terminal one belongs to stale recovery and
    already absorbs the turns), its newest checkpoint is older than the idle
    window, and its oldest checkpoint -- the one a batch drain records retry
    state on -- is past its backoff.  Draining the representative promotes all
    of that session's pending turns as one batch.  Only filenames and mtimes
    are read, so this stays inside the startup hook budget.
    """
    window = config.idle_finalize_seconds if idle_seconds is None else idle_seconds
    pending = config.state_path / "queue" / "pending"
    if window <= 0 or limit <= 0 or not pending.is_dir():
        return []
    moment = now or dt.datetime.now().astimezone()
    cutoff = moment.timestamp() - window
    sessions: dict[tuple[str, str], list[tuple[float, str, Path]]] = {}
    blocked: set[tuple[str, str]] = set()
    for path in pending.glob("*.json"):
        match = CHECKPOINT_NAME_RE.match(path.name)
        if match is None or match.group("runtime") not in IDLE_FINALIZE_RUNTIMES:
            continue
        identity = (match.group("runtime"), match.group("session_key"))
        if match.group("event") != "turn_complete":
            blocked.add(identity)
            continue
        try:
            info = path.lstat()
        except OSError:
            continue
        if not path.is_file():
            continue
        sessions.setdefault(identity, []).append((info.st_mtime, path.name, path))
    candidates: list[tuple[float, Path]] = []
    for identity, entries in sessions.items():
        if identity in blocked or identity in exclude:
            continue
        entries.sort()
        newest = entries[-1][0]
        if newest > cutoff:
            continue
        representative = entries[0][2]
        if not retry_due(load_retry_state(config, representative), now=moment):
            continue
        candidates.append((newest, representative))
    candidates.sort()
    return [path for _, path in candidates[:limit]]


def prune_orphan_retry_states(config: MemoryConfig) -> int:
    """Drop sidecars whose checkpoint is gone.  Keeps the observable state
    honest without ever touching a raw checkpoint."""
    directory = retry_dir(config)
    pending = config.state_path / "queue" / "pending"
    if not directory.is_dir():
        return 0
    removed = 0
    for path in sorted(directory.glob("*.json")):
        if not path.is_file() or (pending / path.name).exists():
            continue
        try:
            safe_unlink(path, root=directory)
        except (MemoryError, OSError):
            continue
        removed += 1
    if removed:
        _write_retry_health(config)
    return removed


__all__ = [
    "RETRY_SCHEMA",
    "RETRY_HEALTH_COMPONENT",
    "MAX_DRAIN_ATTEMPTS",
    "STALE_CHECKPOINT_MIN_AGE_SECONDS",
    "MAX_STALE_RECOVERY_SPAWNS",
    "STALE_RECOVERABLE_EVENTS",
    "classify_drain_failure",
    "retry_dir",
    "retry_state_path",
    "load_retry_state",
    "record_drain_failure",
    "clear_retry_state",
    "retry_states",
    "retry_summary",
    "retry_due",
    "find_stale_recoverable_checkpoints",
    "IDLE_FINALIZE_RUNTIMES",
    "MAX_IDLE_FINALIZE_SPAWNS",
    "find_idle_turn_batches",
    "prune_orphan_retry_states",
]

"""Bounded, observable retry state for failed Hermes session finalization.

``on_session_finalize`` is the only boundary where the plugin turns a session's
normalized transcript into a durable event artifact.  When its summarizer call
failed -- a provider usage limit, a timeout -- the raw turn checkpoints were
correctly preserved and the log said the source "remains retryable", but
nothing ever retried it: startup discovery is deliberately provider-free and
``_recover_pending_turn_checkpoints`` has no caller.  A finalized session whose
``ended_at`` is already stamped never receives another finalize callback, so
"retryable" meant "abandoned in practice".

This module is the missing durable half.  It is intentionally dependency-free
(standard library only, no Hermes and no Memory OS imports) so the engine-side
doctor can parse the same records and so it can be unit tested on its own.

Invariants:

* Raw checkpoints are never touched here.  This module only writes, reads and
  deletes its own sidecar records.
* A record is scoped to ``(session_id, source digest)``, exactly like the
  settlement record it mirrors.  A later turn produces a different digest and
  therefore a different record, so a retry can never suppress newer work.
* Permanent failures (trust, auth, schema, policy) are never scheduled.
* Failures that cannot be classified are held for an operator, never retried
  automatically -- an unrecognized error must not become an endless loop.
* Attempts are bounded by ``MAX_ATTEMPTS`` and spaced by exponential backoff.
* Records carry a classified reason code and the exception *type* only.  The
  provider's raw message can contain transcript fragments, tokens or account
  identifiers, so it is never persisted.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import posixpath
import re
from typing import Any, Optional

RETRY_SCHEMA = "pikselzone-memory-hermes-finalize-retry-v1"

#: Five attempts across the backoff schedule below covers an ordinary provider
#: outage or a day-long subscription quota window without looping forever.
MAX_ATTEMPTS = 5
BASE_BACKOFF_SECONDS = 300
MAX_BACKOFF_SECONDS = 6 * 3600

#: A subscription quota window routinely outlives the generic schedule -- the
#: observed production failure reset in about an hour, but a daily plan limit
#: does not.  Five short attempts would exhaust in under three hours and
#: abandon the session for a reason we know is temporary, so a quota failure
#: gets its own longer, still bounded horizon.
QUOTA_REASONS = frozenset({"usage-limit"})
QUOTA_MAX_ATTEMPTS = 8
QUOTA_BASE_BACKOFF_SECONDS = 3600
QUOTA_MAX_BACKOFF_SECONDS = 24 * 3600

STATUS_SCHEDULED = "retry-scheduled"
STATUS_EXHAUSTED = "retry-exhausted"
STATUS_PERMANENT = "permanent"
STATUS_HOLD = "hold-unclassified"

CLASSIFICATION_RETRYABLE = "retryable"
CLASSIFICATION_PERMANENT = "permanent"
CLASSIFICATION_UNKNOWN = "unknown"

RECORD_NAME_RE = re.compile(
    r"^hermes-(?P<session>[0-9a-f]{32})(?:-(?P<owner>[0-9a-f]{8}))?"
    r"-(?P<source>[0-9a-f]{16})\.json$"
)

# Ordered most specific first: the first pattern that matches decides both the
# reason code and, through _REASON_CLASSIFICATION, whether a retry is allowed.
_REASON_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("usage-limit", re.compile(r"usage[_ -]?limit|quota|insufficient[_ -]?quota|billing", re.I)),
    ("rate-limit", re.compile(r"rate[_ -]?limit|too many requests|\b429\b", re.I)),
    ("trust-denied", re.compile(r"trust|override[_ -]?denied|not allowed|forbidden|\b403\b", re.I)),
    ("auth", re.compile(r"unauthor|authenticat|credential|api[_ -]?key|\b401\b", re.I)),
    ("timeout", re.compile(r"timeout|timed out|deadline", re.I)),
    ("connection", re.compile(
        r"connection|network|unreachable|reset by peer|temporarily unavailable|dns", re.I)),
    ("server-error", re.compile(r"overload|server error|bad gateway|\b5\d{2}\b", re.I)),
    ("schema", re.compile(r"schema|validation|invalid json|jsondecode|parse", re.I)),
    ("policy", re.compile(r"policy|refus|content filter|moderation", re.I)),
)

_REASON_CLASSIFICATION: dict[str, str] = {
    "usage-limit": CLASSIFICATION_RETRYABLE,
    "rate-limit": CLASSIFICATION_RETRYABLE,
    "timeout": CLASSIFICATION_RETRYABLE,
    "connection": CLASSIFICATION_RETRYABLE,
    "server-error": CLASSIFICATION_RETRYABLE,
    # A settlement or outbox write that failed is local I/O, not provider state.
    "settlement-write": CLASSIFICATION_RETRYABLE,
    "stage-write": CLASSIFICATION_RETRYABLE,
    # A finalize that arrived while this process was inside its own summarizer
    # call.  Nothing is wrong with the session; it just needs a later attempt.
    "guard-deferred": CLASSIFICATION_RETRYABLE,
    "trust-denied": CLASSIFICATION_PERMANENT,
    "auth": CLASSIFICATION_PERMANENT,
    "schema": CLASSIFICATION_PERMANENT,
    "policy": CLASSIFICATION_PERMANENT,
    "unknown": CLASSIFICATION_UNKNOWN,
}


def retry_dir(base_dir: str) -> str:
    """Directory holding finalize-retry sidecars for this Memory OS base."""
    return posixpath.join(base_dir, "state", "finalize-retry")


def _session_hash(session_id: str) -> str:
    return hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:32]


def owner_hash(database: str) -> str:
    """Short, stable identity for the SessionDB that owns a session.

    Two profiles can hold rows with the same session id.  Without this the
    record for one of them would silently overwrite the other's.
    """
    if not database:
        return ""
    return hashlib.sha256(str(database).encode("utf-8")).hexdigest()[:8]


def record_path(base_dir: str, session_id: str, source_sha: str, database: str = "") -> str:
    """Record path scoped to owner and source digest."""
    owner = owner_hash(database)
    suffix = f"-{owner}" if owner else ""
    return posixpath.join(
        retry_dir(base_dir),
        f"hermes-{_session_hash(session_id)}{suffix}-{source_sha[:16]}.json",
    )


def classify_failure(
    exc: Optional[BaseException], reason_code: Optional[str] = None,
) -> tuple[str, str]:
    """Return ``(classification, reason_code)`` for one finalize failure.

    An explicit ``reason_code`` wins: the caller already knows a local write
    failed and does not need the text of a provider error to say so.  Anything
    that matches no known pattern stays ``unknown`` on purpose -- guessing that
    an unrecognized failure is transient is how a retry loop becomes infinite.
    """
    if reason_code:
        return _REASON_CLASSIFICATION.get(reason_code, CLASSIFICATION_UNKNOWN), reason_code
    if exc is None:
        return CLASSIFICATION_UNKNOWN, "unknown"
    haystack = f"{exc.__class__.__name__}: {exc}"
    for code, pattern in _REASON_PATTERNS:
        if pattern.search(haystack):
            return _REASON_CLASSIFICATION[code], code
    return CLASSIFICATION_UNKNOWN, "unknown"


def schedule_for(reason_code: str) -> tuple[int, int, int]:
    """Return ``(max_attempts, base_backoff, max_backoff)`` for one reason."""
    if reason_code in QUOTA_REASONS:
        return QUOTA_MAX_ATTEMPTS, QUOTA_BASE_BACKOFF_SECONDS, QUOTA_MAX_BACKOFF_SECONDS
    return MAX_ATTEMPTS, BASE_BACKOFF_SECONDS, MAX_BACKOFF_SECONDS


def _backoff_seconds(attempts: int, base: int = BASE_BACKOFF_SECONDS,
                     cap: int = MAX_BACKOFF_SECONDS) -> int:
    exponent = max(0, min(attempts - 1, 16))
    return min(cap, base * (2 ** exponent))


def parse_iso(value: Any) -> Optional[dt.datetime]:
    """Parse a timezone-aware ISO timestamp, or return None."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed


def _read_record(path: str) -> dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, ValueError, TypeError):
        return {}
    if not isinstance(value, dict) or value.get("schema") != RETRY_SCHEMA:
        return {}
    return value


def load_record(
    base_dir: str, session_id: str, source_sha: str, database: str = "",
) -> dict[str, Any]:
    return _read_record(record_path(base_dir, session_id, source_sha, database))


def _write_record(path: str, state: dict[str, Any]) -> bool:
    directory = posixpath.dirname(path)
    tmp_path = f"{path}.{os.getpid()}.tmp"
    try:
        os.makedirs(directory, mode=0o770, exist_ok=True)
        with open(tmp_path, "w", encoding="utf-8") as handle:
            json.dump(state, handle, indent=2, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_path, 0o660)
        os.replace(tmp_path, path)
        return True
    except OSError:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        return False


def record_failure(
    base_dir: str,
    *,
    session_id: str,
    source_sha: str,
    profile: str = "",
    database: str = "",
    exc: Optional[BaseException] = None,
    reason_code: Optional[str] = None,
    artifact_produced: Optional[bool] = None,
    event_path: Optional[str] = None,
    event_sha256: Optional[str] = None,
    now: Optional[dt.datetime] = None,
) -> dict[str, Any]:
    """Persist one bounded failure observation and return the new record.

    The returned record is meaningful even when the write failed, so a caller
    can still log and report what it decided.
    """
    if not session_id or not source_sha:
        return {}
    path = record_path(base_dir, session_id, source_sha, database)
    previous = _read_record(path)
    classification, code = classify_failure(exc, reason_code)
    attempts = int(previous.get("attempts") or 0) + 1
    moment = now or dt.datetime.now().astimezone()
    max_attempts, base_backoff, backoff_cap = schedule_for(code)

    if classification == CLASSIFICATION_PERMANENT:
        status, next_attempt_after = STATUS_PERMANENT, None
    elif classification == CLASSIFICATION_UNKNOWN:
        # Held for an operator: visible in doctor, never retried on its own.
        status, next_attempt_after = STATUS_HOLD, None
    elif attempts >= max_attempts:
        status, next_attempt_after = STATUS_EXHAUSTED, None
    else:
        status = STATUS_SCHEDULED
        next_attempt_after = (
            moment + dt.timedelta(
                seconds=_backoff_seconds(attempts, base_backoff, backoff_cap)
            )
        ).isoformat(timespec="seconds")

    state = {
        "schema": RETRY_SCHEMA,
        "runtime": "hermes",
        "session_id": session_id,
        "source_sha256": source_sha,
        # Ownership coordinates: recovery reopens exactly this database rather
        # than searching profiles, so a reused session id cannot be confused.
        "profile": profile or str(previous.get("profile") or ""),
        "database": database or str(previous.get("database") or ""),
        "attempts": attempts,
        "max_attempts": max_attempts,
        "classification": classification,
        "status": status,
        # Deliberately no raw provider message: it can carry transcript text,
        # tokens or account identifiers.
        "reason_code": code,
        "error_type": exc.__class__.__name__ if exc is not None else "",
        "first_failure_at": previous.get("first_failure_at") or moment.isoformat(timespec="seconds"),
        "last_failure_at": moment.isoformat(timespec="seconds"),
        "next_attempt_after": next_attempt_after,
        # Whether the content artifact for this source already exists.  Once it
        # does, a retry must settle rather than summarize and publish again --
        # the publisher may have already taken the staged file away.
        "artifact_produced": bool(
            previous.get("artifact_produced") if artifact_produced is None else artifact_produced
        ),
        "event_path": event_path if event_path is not None else previous.get("event_path"),
        "event_sha256": (
            event_sha256 if event_sha256 is not None else previous.get("event_sha256")
        ),
    }
    _write_record(path, state)
    return state


def clear_record(
    base_dir: str, session_id: str, source_sha: str, database: str = "",
) -> None:
    """Drop the sidecar once this source digest is settled one way or another."""
    if not session_id or not source_sha:
        return
    candidates = [record_path(base_dir, session_id, source_sha, database)]
    if database:
        # Also drop a record written before this session's owner was known.
        candidates.append(record_path(base_dir, session_id, source_sha))
    for candidate in candidates:
        try:
            os.unlink(candidate)
        except OSError:
            pass


def iter_records(base_dir: str) -> list[dict[str, Any]]:
    """All valid records, oldest failure first."""
    directory = retry_dir(base_dir)
    try:
        names = sorted(name for name in os.listdir(directory) if RECORD_NAME_RE.match(name))
    except OSError:
        return []
    records: list[dict[str, Any]] = []
    for name in names:
        record = _read_record(posixpath.join(directory, name))
        if record:
            records.append(record)
    records.sort(key=lambda item: str(item.get("first_failure_at") or ""))
    return records


def summary(base_dir: str) -> dict[str, int]:
    counts = {"scheduled": 0, "exhausted": 0, "permanent": 0, "hold": 0}
    for record in iter_records(base_dir):
        status = record.get("status")
        if status == STATUS_SCHEDULED:
            counts["scheduled"] += 1
        elif status == STATUS_EXHAUSTED:
            counts["exhausted"] += 1
        elif status == STATUS_PERMANENT:
            counts["permanent"] += 1
        elif status == STATUS_HOLD:
            counts["hold"] += 1
    return counts


def is_due(record: dict[str, Any], *, now: Optional[dt.datetime] = None) -> bool:
    """Only a scheduled record whose backoff has elapsed is due."""
    if not record or record.get("status") != STATUS_SCHEDULED:
        return False
    if int(record.get("attempts") or 0) >= int(record.get("max_attempts") or MAX_ATTEMPTS):
        return False
    scheduled_for = parse_iso(record.get("next_attempt_after"))
    if scheduled_for is None:
        return False
    return (now or dt.datetime.now().astimezone()) >= scheduled_for


def due_records(
    base_dir: str, *, now: Optional[dt.datetime] = None, limit: int = 2,
) -> list[dict[str, Any]]:
    """Bounded, oldest-first selection of records whose retry time has arrived."""
    moment = now or dt.datetime.now().astimezone()
    due = [record for record in iter_records(base_dir) if is_due(record, now=moment)]
    return due[:max(0, limit)]

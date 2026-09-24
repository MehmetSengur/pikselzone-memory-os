"""Put permanent Hermes finalize verdicts back on the bounded retry path.

A permanent verdict is final by design: trust, auth, schema and policy failures
do not heal on their own, so the plugin never reschedules them. When the cause
was on our side and has since been fixed -- old engine code still loaded in a
long-lived process, a model allowlist that named the wrong slug -- those
verdicts strand real sessions for good.

This module is the operator's way out, shaped like ``repair-memory``:

* ``build_requeue_plan`` changes nothing and pins each record by its SHA-256;
* ``apply_requeue_plan`` refuses any record that changed since the plan, backs
  every touched record up, flips it to ``retry-scheduled`` and writes a ledger;
* ``revert_requeue`` restores the backups of records still in the requeued state.

It never summarizes, never writes an event, a receipt or a settlement. The
plugin's own recovery (``_recover_one_finalize_retry``) picks the records up,
re-reads the session from its SessionDB, clears it when the source moved on or
was settled meanwhile, and records a fresh verdict when it fails again -- so a
requeued record is bounded by the same attempt limit as any other.
"""
from __future__ import annotations

import json
import os
import secrets
import shutil
from pathlib import Path
from typing import Any

from .core import MemoryConfig, PolicyError, atomic_json, iso_now, sha256_file
from .hermes_backlog import FINALIZE_RETRY_SCHEMA, hermes_runtime_base

PLAN_SCHEMA = "pikselzone-memory-hermes-requeue-plan-v1"
LEDGER_SCHEMA = "pikselzone-memory-hermes-requeue-ledger-v1"
DEFAULT_REASONS = ("schema", "trust-denied")
STATUS_PERMANENT = "permanent"
STATUS_SCHEDULED = "retry-scheduled"


def _retry_dir(config: MemoryConfig) -> Path:
    base = hermes_runtime_base(config)
    if base is None:
        raise PolicyError("requeue-no-hermes-runtime")
    return base / "state" / "finalize-retry"


def _read(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) and value.get("schema") == FINALIZE_RETRY_SCHEMA else {}


def build_requeue_plan(config: MemoryConfig, *, reasons: tuple[str, ...] = DEFAULT_REASONS) -> dict[str, Any]:
    """List the permanent records whose reason is in ``reasons``; change nothing."""
    directory = _retry_dir(config)
    rows = []
    for path in sorted(directory.glob("hermes-*.json")) if directory.is_dir() else []:
        record = _read(path)
        if record.get("status") != STATUS_PERMANENT or record.get("reason_code") not in reasons:
            continue
        rows.append({
            "file": path.name, "sha256": sha256_file(path),
            "session_id": record.get("session_id"), "profile": record.get("profile"),
            "reason_code": record.get("reason_code"), "error_type": record.get("error_type"),
            "first_failure_at": record.get("first_failure_at"),
        })
    by_reason: dict[str, int] = {}
    for row in rows:
        by_reason[row["reason_code"]] = by_reason.get(row["reason_code"], 0) + 1
    return {
        "schema": PLAN_SCHEMA, "created_at": iso_now(), "retry_dir": str(directory),
        "reasons": list(reasons), "records": rows,
        "summary": {"records": len(rows), "by_reason": by_reason},
    }


def apply_requeue_plan(config: MemoryConfig, plan: dict[str, Any]) -> dict[str, Any]:
    if plan.get("schema") != PLAN_SCHEMA:
        raise PolicyError("requeue-plan-schema-invalid")
    directory = _retry_dir(config)
    if str(directory) != plan.get("retry_dir"):
        raise PolicyError("requeue-plan-directory-mismatch")
    requeue_id = f"requeue-{iso_now()[:19].replace(':', '').replace('-', '')}-{secrets.token_hex(3)}"
    backup = config.state_path / "backups" / requeue_id
    backup.mkdir(parents=True, exist_ok=False)
    os.chmod(backup, 0o700)
    now = iso_now()
    requeued, skipped = [], []
    for row in plan.get("records", []):
        path = directory / str(row.get("file", ""))
        if path.parent != directory or not path.is_file() or sha256_file(path) != row.get("sha256"):
            skipped.append({"file": row.get("file"), "reason": "changed-since-plan"})
            continue
        record = _read(path)
        if record.get("status") != STATUS_PERMANENT:
            skipped.append({"file": row.get("file"), "reason": "no-longer-permanent"})
            continue
        shutil.copy2(path, backup / path.name)
        record.update(
            status=STATUS_SCHEDULED, classification="retryable", next_attempt_after=now,
            requeued_at=now, requeue_id=requeue_id,
            requeued_from={"status": STATUS_PERMANENT, "reason_code": record.get("reason_code")},
        )
        stat = path.stat()
        atomic_json(path, record)
        os.chmod(path, stat.st_mode & 0o7777)
        requeued.append({"file": path.name, "session_id": record.get("session_id"),
                         "profile": record.get("profile"), "sha256_after": sha256_file(path)})
    ledger = {
        "schema": LEDGER_SCHEMA, "requeue_id": requeue_id, "applied_at": now,
        "plan_created_at": plan.get("created_at"), "retry_dir": str(directory),
        "backup_dir": str(backup), "requeued": requeued, "skipped": skipped,
        "summary": {"requeued": len(requeued), "skipped": len(skipped)},
        "revert": f"pz-memory requeue-hermes-finalize --revert {requeue_id}",
    }
    evidence = config.state_path / "evidence"
    evidence.mkdir(parents=True, exist_ok=True)
    atomic_json(evidence / f"hermes-{requeue_id}.json", ledger)
    atomic_json(backup / "ledger.json", ledger)
    return ledger


def revert_requeue(config: MemoryConfig, requeue_id: str, *, force: bool = False) -> dict[str, Any]:
    """Restore records that are still exactly as the requeue left them."""
    backup = config.state_path / "backups" / requeue_id
    ledger_path = backup / "ledger.json"
    if not requeue_id.startswith("requeue-") or not ledger_path.is_file():
        raise PolicyError("requeue-ledger-missing")
    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    directory = Path(ledger["retry_dir"])
    restored, left = [], []
    for row in ledger["requeued"]:
        path = directory / row["file"]
        if not path.exists():
            # The retry already settled or superseded it; nothing to put back.
            left.append({"file": row["file"], "reason": "resolved-since"})
            continue
        if not force and sha256_file(path) != row["sha256_after"]:
            left.append({"file": row["file"], "reason": "changed-since-requeue"})
            continue
        shutil.copy2(backup / row["file"], path)
        restored.append(row["file"])
    return {"requeue_id": requeue_id, "restored": len(restored), "left": left}

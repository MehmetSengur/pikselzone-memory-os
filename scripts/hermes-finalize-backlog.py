#!/usr/bin/env python3
"""Read-only inventory of the native Hermes finalize backlog.

Reports what the plugin has left unfinished: bounded retry records waiting on
backoff, records held for an operator, and legacy sessions whose raw turn
checkpoints predate the retry contract and are therefore accounted for by
nothing at all.

This script never calls a provider, never drains, replays, deletes or archives
anything, and never writes to the runtime state it inspects.  Adopting a legacy
session into the retry contract is a separate, explicit operator decision; see
``runbooks/hermes-finalize-retry-acceptance.md``.

Usage::

    python3 scripts/hermes-finalize-backlog.py --config /srv/pz-hermes/memory-config.json
    python3 scripts/hermes-finalize-backlog.py --config <path> --json

Exit code is always 0 for a successful inspection; a backlog is information,
not a failure.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from memory_v1.core import MemoryConfig  # noqa: E402
from memory_v1.hermes_backlog import (  # noqa: E402
    CHECKPOINT_SCHEMA, FINALIZE_RETRY_SCHEMA, SETTLEMENT_SCHEMA,
    _read_bounded_json, _scan, backlog_metrics, hermes_runtime_base,
)


def _load_config(path: Path) -> MemoryConfig:
    return MemoryConfig.from_dict(json.loads(path.read_text(encoding="utf-8")))


def collect(config: MemoryConfig) -> dict:
    """Gather the inventory without touching a single byte of runtime state."""
    base = hermes_runtime_base(config)
    metrics = backlog_metrics(config)
    inventory: dict = {
        "schema": "pikselzone-memory-hermes-finalize-inventory-v1",
        "runtime_base": str(base) if base else None,
        "metrics": metrics,
        "retry_records": [],
        "unresolved_sessions": [],
    }
    if not metrics["available"] or base is None:
        return inventory

    state = base / "state"
    for record in _scan(state / "finalize-retry", FINALIZE_RETRY_SCHEMA):
        inventory["retry_records"].append({
            key: record.get(key)
            for key in (
                "session_id", "profile", "status", "classification", "reason_code",
                "error_type", "attempts", "max_attempts", "first_failure_at",
                "last_failure_at", "next_attempt_after",
            )
        })

    # Legacy sessions: raw checkpoints preserved, but no settlement and no
    # retry record.  Report the oldest observation so an operator can judge age.
    settled = {
        record.get("session_id") for record in _scan(state / "settlements", SETTLEMENT_SCHEMA)
    }
    tracked = {
        record.get("session_id") for record in _scan(state / "finalize-retry", FINALIZE_RETRY_SCHEMA)
    }
    observed: dict[str, str] = {}
    turns: dict[str, int] = {}
    for record in _scan(state / "checkpoints", CHECKPOINT_SCHEMA):
        session_id = record.get("session_id")
        if not isinstance(session_id, str) or record.get("runtime") != "hermes":
            continue
        turns[session_id] = turns.get(session_id, 0) + 1
        seen_at = str(record.get("observed_at") or "")
        if session_id not in observed or seen_at < observed[session_id]:
            observed[session_id] = seen_at
    for session_id in sorted(set(observed) - settled - tracked):
        inventory["unresolved_sessions"].append({
            "session_id": session_id,
            "turn_checkpoints": turns.get(session_id, 0),
            "first_observed_at": observed.get(session_id, ""),
        })
    return inventory


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--config", type=Path, required=True, help="Memory OS config path")
    parser.add_argument("--json", action="store_true", help="emit the raw inventory")
    args = parser.parse_args(argv)

    inventory = collect(_load_config(args.config))
    if args.json:
        print(json.dumps(inventory, indent=2, ensure_ascii=False))
        return 0

    metrics = inventory["metrics"]
    if not metrics["available"]:
        print("No Hermes runtime state for this deployment.")
        return 0
    print(f"Hermes runtime base: {inventory['runtime_base']}")
    print(
        "Retry records: "
        f"scheduled={metrics['scheduled']} hold={metrics['hold']} "
        f"permanent={metrics['permanent']} exhausted={metrics['exhausted']}"
    )
    for record in inventory["retry_records"]:
        print(
            f"  - {record['session_id']} [{record['status']}] "
            f"reason={record['reason_code']} attempts={record['attempts']}/{record['max_attempts']} "
            f"next={record['next_attempt_after']}"
        )
    print(
        f"Sessions with raw checkpoints: {metrics['sessions_with_checkpoints']} "
        f"(settled={metrics['settled_sessions']}, retry-tracked={metrics['retry_tracked_sessions']})"
    )
    print(f"Unresolved legacy sessions: {metrics['unresolved_sessions']}")
    for entry in inventory["unresolved_sessions"]:
        print(
            f"  - {entry['session_id']} turns={entry['turn_checkpoints']} "
            f"first_observed={entry['first_observed_at']}"
        )
    print("\nRead-only inspection. Nothing was drained, replayed, deleted or archived.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

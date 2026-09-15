"""Deliver memory that idle finalize promoted after a session had started.

The startup recall bundle is built inside the 5-second SessionStart hook, and
idle finalize only *spawns* its drains there: a provider call takes tens of
seconds to minutes.  The session that triggered the finalize therefore starts
without that thread's newest memory, and waiting for it would stall startup.

This module closes the gap without waiting.  SessionStart records which
same-project threads it is finalizing.  Each later UserPromptSubmit of that
session checks them once: a thread promoted since the session started is
delivered as a condensed, sanitized summary; one still being processed or
failed is announced once so the agent knows its startup memory is incomplete.

Bounds: two hours per session, a fixed character budget, the same project as
the startup bundle (cross-project knowledge stays with associative recall), and
never raw unpromoted turn text.
"""
from __future__ import annotations

import datetime as dt
import json
import re
from pathlib import Path
from typing import Any

from .core import (
    MemoryConfig, atomic_json, ensure_safe_directory, iso_now, path_within, safe_unlink,
    session_key, sha256_file,
)
from .events import parse_event_artifact


LATE_RECALL_SCHEMA = "pikselzone-memory-late-recall-v1"
LATE_RECALL_EVIDENCE_SCHEMA = "pikselzone-memory-late-recall-evidence-v1"
LATE_RECALL_TTL_SECONDS = 2 * 3600
LATE_RECALL_BUDGET_CHARS = 1500
_ITEM_CHARS = 220
_KEY_RE = re.compile(r"[0-9a-f]{32}")


def _marker_dir(config: MemoryConfig) -> Path:
    return config.state_path / "recall" / "late"


def _marker_path(config: MemoryConfig, runtime: str, session_id: str) -> Path:
    return _marker_dir(config) / f"{runtime}-{session_key(session_id)}.json"


def _parse_time(value: Any) -> dt.datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = dt.datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def record_pending_finalize(
    config: MemoryConfig, *, runtime: str, session_id: str, project: str | None,
    checkpoints: list[Path], now: dt.datetime | None = None,
) -> Path | None:
    """Remember the same-project threads this SessionStart is finalizing.

    Written before the drains are spawned, so a drain can never finish before
    the marker's start time.
    """
    if runtime not in {"codex", "claude"} or not project:
        return None
    targets: list[dict[str, Any]] = []
    for checkpoint in checkpoints:
        try:
            item = json.loads(checkpoint.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(item, dict) or item.get("project") != project:
            continue
        target_runtime = item.get("runtime")
        target_session = item.get("session_id")
        if target_runtime not in {"codex", "claude"} or not isinstance(target_session, str):
            continue
        targets.append({
            "runtime": target_runtime,
            "session_key": session_key(target_session),
            "checkpoint": checkpoint.name,
            "status": "pending",
        })
    if not targets:
        return None
    moment = now or dt.datetime.now().astimezone()
    path = _marker_path(config, runtime, session_id)
    ensure_safe_directory(path.parent, create=True)
    atomic_json(path, {
        "schema": LATE_RECALL_SCHEMA,
        "runtime": runtime,
        "session_key": session_key(session_id),
        "project": project,
        "created_at": moment.isoformat(timespec="seconds"),
        "expires_at": (moment + dt.timedelta(seconds=LATE_RECALL_TTL_SECONDS)).isoformat(
            timespec="seconds"
        ),
        "notice_shown": False,
        "targets": targets,
    })
    return path


def _remove(config: MemoryConfig, path: Path) -> None:
    try:
        safe_unlink(path, root=_marker_dir(config))
    except Exception:
        pass


def _condensed(artifact: dict[str, Any]) -> list[str]:
    sections = artifact["sections"]
    picks = (
        ("Bağlam", sections.get("context", [])[:2]),
        ("Karar", sections.get("decisions", [])[:3]),
        ("Açık", sections.get("open_items", [])[:2]),
        ("Kanıt", sections.get("evidence", [])[:2]),
    )
    return [
        f"- {label}: {item[:_ITEM_CHARS]}"
        for label, items in picks for item in items if item and item != "unknown"
    ]


def deliver_late_recall(
    config: MemoryConfig, *, runtime: str, session_id: str, now: dt.datetime | None = None,
) -> str:
    """Return late-recall context for this prompt, or ``""``.  Updates the
    marker so every thread is delivered or announced at most once."""
    from .recall import sanitize_untrusted_memory
    from .retry import load_retry_state

    path = _marker_path(config, runtime, session_id)
    if not path.is_file():
        return ""
    try:
        marker = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    if not isinstance(marker, dict) or marker.get("schema") != LATE_RECALL_SCHEMA:
        return ""
    moment = now or dt.datetime.now().astimezone()
    started = _parse_time(marker.get("created_at"))
    expires = _parse_time(marker.get("expires_at"))
    targets = marker.get("targets")
    if started is None or expires is None or moment >= expires or not isinstance(targets, list):
        _remove(config, path)
        return ""

    pending_dir = config.state_path / "queue" / "pending"
    daily_root = config.vault_path / "daily"
    blocks: list[str] = []
    delivered: list[dict[str, str]] = []
    still_pending = 0
    newly_failed = 0
    for target in targets:
        if not isinstance(target, dict) or target.get("status") != "pending":
            continue
        target_runtime = target.get("runtime")
        key = target.get("session_key")
        checkpoint = target.get("checkpoint")
        if (
            target_runtime not in {"codex", "claude"} or not isinstance(key, str)
            or not _KEY_RE.fullmatch(key) or not isinstance(checkpoint, str)
            or "/" in checkpoint
        ):
            target["status"] = "invalid"
            continue
        try:
            state = json.loads(
                (config.state_path / "sessions" / target_runtime / f"{key}.json")
                .read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError):
            state = {}
        updated = _parse_time(state.get("updated_at")) if isinstance(state, dict) else None
        if updated is not None and updated >= started:
            event_path = state.get("event_path")
            if isinstance(event_path, str):
                candidate = Path(event_path)
                if (
                    candidate.is_absolute() and path_within(candidate, daily_root)
                    and candidate.name == f"{target_runtime}-{key}.md" and candidate.is_file()
                ):
                    try:
                        artifact = parse_event_artifact(candidate.read_text(encoding="utf-8"))
                    except Exception:
                        artifact = None
                    if artifact is not None and artifact.get("project") == marker.get("project"):
                        lines = _condensed(artifact)
                        if lines:
                            rel = candidate.relative_to(config.vault_path)
                            blocks.append(f"### {rel}\n" + "\n".join(lines))
                        target["status"] = "delivered"
                        delivered.append({
                            "event_path": str(candidate),
                            "event_sha256": sha256_file(candidate),
                        })
                        continue
            if state.get("status") == "empty":
                target["status"] = "empty"
                continue
        checkpoint_path = pending_dir / checkpoint
        if checkpoint_path.exists():
            retry = load_retry_state(config, checkpoint_path)
            if retry.get("status") in {"permanent", "retry-exhausted", "retry-scheduled"}:
                target["status"] = "failed"
                newly_failed += 1
            else:
                still_pending += 1
            continue
        # Settled without a newer promotion (already covered earlier).
        target["status"] = "settled"

    parts: list[str] = []
    if blocks:
        parts.append(
            "Bu oturum açıldıktan sonra hafızaya işlenen, kapanmamış thread özetleri:"
        )
        parts.extend(blocks)
    if still_pending and not marker.get("notice_shown"):
        parts.append(
            f"Not: {still_pending} kapanmamış thread'in son turları henüz hafızaya "
            "işleniyor; bu oturumun başlangıç hafızası onları içermiyor."
        )
        marker["notice_shown"] = True
    if newly_failed:
        parts.append(
            f"Not: {newly_failed} kapanmamış thread hafızaya işlenemedi (retry kaydı "
            "var); başlangıç hafızası onları içermiyor."
        )

    if still_pending:
        try:
            atomic_json(path, marker)
        except Exception:
            pass
    else:
        _remove(config, path)
    if not parts:
        return ""

    body, _ = sanitize_untrusted_memory("\n".join(parts))
    text = (
        "=== PIKSELZONE LATE RECALL (idle finalize) ===\n"
        "[DERIVED MEMORY — verify against operational truth]\n" + body
    )[:LATE_RECALL_BUDGET_CHARS]
    if delivered:
        evidence = config.state_path / "evidence" / f"late-recall-{runtime}.json"
        try:
            ensure_safe_directory(evidence.parent, create=True)
            atomic_json(evidence, {
                "schema": LATE_RECALL_EVIDENCE_SCHEMA,
                "runtime": runtime,
                "session_key": session_key(session_id),
                "project": marker.get("project"),
                "observed_at": iso_now(),
                "delivered": delivered,
                "text": text,
            })
        except Exception:
            pass
    return text


__all__ = [
    "LATE_RECALL_SCHEMA",
    "LATE_RECALL_TTL_SECONDS",
    "record_pending_finalize",
    "deliver_late_recall",
]

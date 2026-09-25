"""Deliver memory that idle finalize promoted after a session had started.

The startup recall bundle is built inside the 5-second SessionStart hook, and
idle finalize only *spawns* its drains there: a provider call takes tens of
seconds to minutes.  The session that triggered the finalize therefore starts
without that thread's newest memory, and waiting for it would stall startup.

This module closes the gap without waiting.  SessionStart records which
same-project threads it is finalizing.  Each later UserPromptSubmit of that
session checks them:

* a thread promoted since the session started is delivered as one condensed,
  sanitized block -- whole or not at all, never cut at the character budget;
  a block that does not fit waits for the next prompt;
* a thread still in flight, or waiting on a scheduled retry, stays tracked and
  is announced once;
* only a permanent or exhausted failure ends tracking, announced once.

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

LATE_RECALL_HEADER = (
    "=== PIKSELZONE LATE RECALL (idle finalize) ===\n"
    "[DERIVED MEMORY — verify against operational truth]"
)
LATE_RECALL_INTRO = "Bu oturum açıldıktan sonra hafızaya işlenen, kapanmamış thread özetleri:"

#: A condensed block has at most these items, each cut to ``_ITEM_CHARS``
#: *before* it becomes a block, so one block plus the header and intro always
#: fits the budget (pinned by a test).  The block itself is never cut.
_BLOCK_PICKS = (("Bağlam", "context", 1), ("Karar", "decisions", 2),
                ("Açık", "open_items", 1), ("Kanıt", "evidence", 1))
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


def condensed_block(artifact: dict[str, Any], rel_path: str) -> str:
    """The whole unit late recall delivers for one promoted thread."""
    from .recall import sanitize_untrusted_memory

    sections = artifact["sections"]
    lines = [f"### {rel_path}"]
    for label, field, limit in _BLOCK_PICKS:
        items = [item for item in sections.get(field, []) if item and item != "unknown"]
        lines.extend(f"- {label}: {item[:_ITEM_CHARS]}" for item in items[:limit])
    text, _ = sanitize_untrusted_memory("\n".join(lines))
    return text


def _promoted_artifact(
    config: MemoryConfig, target: dict[str, Any], state: dict[str, Any], project: Any,
) -> tuple[Path, dict[str, Any]] | None:
    event_path = state.get("event_path")
    if not isinstance(event_path, str):
        return None
    candidate = Path(event_path)
    if not (
        candidate.is_absolute() and path_within(candidate, config.vault_path / "daily")
        and candidate.name == f"{target['runtime']}-{target['session_key']}.md"
        and candidate.is_file()
    ):
        return None
    try:
        artifact = parse_event_artifact(candidate.read_text(encoding="utf-8"))
    except Exception:
        return None
    from .recall_access import source_reason
    if source_reason(config, str(candidate.relative_to(config.vault_path)), project=project):
        return None
    if artifact.get("project") != project:
        return None
    return candidate, artifact


def deliver_late_recall(
    config: MemoryConfig, *, runtime: str, session_id: str, now: dt.datetime | None = None,
) -> str:
    """Return late-recall context for this prompt, or ``""``.

    A target is marked ``delivered`` only when its whole block is in the
    returned text; notices are marked shown only when they are in it too.
    """
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
    ready: list[tuple[dict[str, Any], str, dict[str, str]]] = []
    in_flight = 0
    retrying: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    for target in targets:
        if not isinstance(target, dict) or target.get("status") != "pending":
            continue
        key = target.get("session_key")
        checkpoint = target.get("checkpoint")
        if (
            target.get("runtime") not in {"codex", "claude"} or not isinstance(key, str)
            or not _KEY_RE.fullmatch(key) or not isinstance(checkpoint, str)
            or "/" in checkpoint
        ):
            target["status"] = "invalid"
            continue
        try:
            state = json.loads(
                (config.state_path / "sessions" / target["runtime"] / f"{key}.json")
                .read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError):
            state = {}
        if not isinstance(state, dict):
            state = {}
        updated = _parse_time(state.get("updated_at"))
        if updated is not None and updated >= started:
            promoted = _promoted_artifact(config, target, state, marker.get("project"))
            if promoted is not None:
                candidate, artifact = promoted
                block = condensed_block(artifact, str(candidate.relative_to(config.vault_path)))
                ready.append((target, block, {
                    "event_path": str(candidate),
                    "event_sha256": sha256_file(candidate),
                    "state_updated_at": state.get("updated_at"),
                }))
                continue
            if state.get("status") == "empty":
                target["status"] = "empty"
                continue
        checkpoint_path = pending_dir / checkpoint
        if checkpoint_path.exists():
            retry_status = load_retry_state(config, checkpoint_path).get("status")
            if retry_status in {"permanent", "retry-exhausted"}:
                target["status"] = "failed"
                failed.append(target)
            elif retry_status == "retry-scheduled":
                # Still tracked: the retry runs at a later SessionStart and
                # may promote the thread within this session's window.
                retrying.append(target)
            else:
                in_flight += 1
            continue
        # Settled without a newer promotion (already covered earlier).
        target["status"] = "settled"

    notices: list[tuple[str, Any]] = []
    if in_flight and not marker.get("notice_shown"):
        notices.append((
            f"Not: {in_flight} kapanmamış thread'in son turları henüz hafızaya "
            "işleniyor; bu oturumun başlangıç hafızası onları içermiyor.",
            "in-flight",
        ))
    unannounced_retry = [t for t in retrying if not t.get("retry_notice_shown")]
    if unannounced_retry:
        notices.append((
            f"Not: {len(unannounced_retry)} kapanmamış thread geçici bir hata nedeniyle "
            "henüz hafızaya işlenemedi; bir sonraki oturum açılışında yeniden denenecek "
            "ve bu oturum sürerken başarılı olursa buraya eklenecek.",
            unannounced_retry,
        ))
    if failed:
        notices.append((
            f"Not: {len(failed)} kapanmamış thread hafızaya işlenemedi (kalıcı hata, "
            "retry kaydı korunuyor); başlangıç hafızası onları içermiyor.",
            "failed",
        ))

    parts = [LATE_RECALL_HEADER]

    def fits(extra: str) -> bool:
        return len("\n".join(parts + [extra])) <= LATE_RECALL_BUDGET_CHARS

    delivered: list[dict[str, str]] = []
    for target, block, receipt in ready:
        addition = block if delivered else f"{LATE_RECALL_INTRO}\n{block}"
        if not fits(addition):
            continue  # stays pending, whole, for the next prompt
        parts.append(addition)
        target["status"] = "delivered"
        delivered.append(receipt)
    for text, owner in notices:
        if not fits(text):
            continue
        parts.append(text)
        if owner == "in-flight":
            marker["notice_shown"] = True
        elif isinstance(owner, list):
            for target in owner:
                target["retry_notice_shown"] = True
    # Failed targets are final whether or not their notice fit.

    if any(isinstance(t, dict) and t.get("status") == "pending" for t in targets):
        try:
            atomic_json(path, marker)
        except Exception:
            pass
    else:
        _remove(config, path)
    if len(parts) == 1:
        return ""
    text = "\n".join(parts)
    if delivered:
        evidence = config.state_path / "evidence" / f"late-recall-{runtime}.json"
        try:
            ensure_safe_directory(evidence.parent, create=True)
            atomic_json(evidence, {
                "schema": LATE_RECALL_EVIDENCE_SCHEMA,
                "runtime": runtime,
                "session_key": session_key(session_id),
                "project": marker.get("project"),
                "session_started_at": marker.get("created_at"),
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
    "LATE_RECALL_BUDGET_CHARS",
    "condensed_block",
    "record_pending_finalize",
    "deliver_late_recall",
]

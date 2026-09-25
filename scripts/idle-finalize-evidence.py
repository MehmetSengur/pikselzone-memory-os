#!/usr/bin/env python3
"""Read-only acceptance evidence for idle finalize.

Acceptance needs both promotion-to-recall paths proven separately in the real
application, so a run has five phases, all recorded in one ``--out`` file:

  capture         a Codex Desktop thread holds the canary in raw turn checkpoints
  promote         idle finalize (not SessionEnd/PreCompact) settled exactly those
                  turns into a durable daily artifact
  recall-late     the session whose SessionStart triggered the finalize received
                  the artifact through late recall, after it had started, and its
                  assistant stated the canary without the user typing it
  recall-startup  a different session, started after promotion, received the
                  artifact in its startup bundle, with the same statement rule
  verdict         PASS only when all four phases passed

It never writes Memory OS state, the vault, or runtime files; its only output
is the ``--out`` JSON.  See runbooks/idle-finalize-acceptance.md.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import sys
from pathlib import Path

_CHECKOUT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
sys.path = [p for p in sys.path if p not in ("", ".", os.getcwd())]
sys.path.insert(0, _CHECKOUT)

from memory_v1.adapters import resolve_codex_rollout, settled_turn_digests  # noqa: E402
from memory_v1.core import MemoryConfig, session_key, transcript_turns  # noqa: E402
from memory_v1.events import parse_event_artifact  # noqa: E402

TERMINAL_EVENTS = {"session_end", "pre_compact", "session_finalize", "session_reset"}
PHASES = ("capture", "promote", "recall_late", "recall_startup")


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _time(value) -> dt.datetime | None:
    try:
        parsed = dt.datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo is not None else None


def capture(config: MemoryConfig, record: dict, *, thread: str, canary: str) -> bool:
    rollout = resolve_codex_rollout(config, thread)
    originator = None
    if rollout is not None:
        with rollout.open(encoding="utf-8") as handle:
            originator = json.loads(handle.readline()).get("payload", {}).get("originator")
    key = session_key(thread)
    pending = config.state_path / "queue" / "pending"
    turns, terminal_pending = [], []
    for path in sorted(pending.glob(f"codex-{key}-*.json")):
        item = json.loads(path.read_text(encoding="utf-8"))
        if item.get("event") in TERMINAL_EVENTS:
            terminal_pending.append(path.name)
        elif canary in item.get("normalized_transcript", ""):
            turns.append({
                "checkpoint": path.name, "checkpoint_sha256": _sha(path),
                "event": item.get("event"), "source_digest": item.get("source_digest"),
                "turn_id": item.get("turn_id"), "hook_observed_at": item.get("hook_observed_at"),
                "project": item.get("project"),
            })
    reasons = []
    if rollout is None or originator != "Codex Desktop":
        reasons.append("not-a-codex-desktop-thread")
    if not turns:
        reasons.append("no-pending-turn-checkpoint-with-canary")
    if terminal_pending:
        reasons.append("terminal-checkpoint-pending-idle-path-cannot-be-proven")
    record["capture"] = {
        "pass": not reasons, "reasons": reasons, "thread": thread, "session_key": key,
        "canary": canary, "rollout": str(rollout) if rollout else None,
        "originator": originator, "checkpoints": turns, "terminal_pending": terminal_pending,
    }
    return not reasons


def promote(config: MemoryConfig, record: dict) -> bool:
    captured = record.get("capture") or {}
    if not captured.get("pass"):
        record["promote"] = {"pass": False, "reasons": ["capture-phase-not-passed"]}
        return False
    key = captured["session_key"]
    state_file = config.state_path / "sessions" / "codex" / f"{key}.json"
    state = json.loads(state_file.read_text(encoding="utf-8")) if state_file.is_file() else {}
    settled = set(settled_turn_digests(config, runtime="codex", state_key=key))
    pending = config.state_path / "queue" / "pending"
    turns = [{
        "checkpoint": turn["checkpoint"],
        "raw_removed": not (pending / turn["checkpoint"]).exists(),
        "digest_settled": turn["source_digest"] in settled,
    } for turn in captured["checkpoints"]]
    event_path = Path(state["event_path"]) if isinstance(state.get("event_path"), str) else None
    artifact = text = None
    if event_path is not None and event_path.is_file():
        text = event_path.read_text(encoding="utf-8")
        artifact = parse_event_artifact(text)
    events_seen = artifact.get("events_seen", []) if artifact else []
    reasons = []
    if not all(t["raw_removed"] and t["digest_settled"] for t in turns):
        reasons.append("captured-turns-not-settled")
    if artifact is None:
        reasons.append("no-durable-artifact")
    elif captured["canary"] not in text:
        reasons.append("canary-not-in-artifact")
    if "checkpoint_recovery" not in events_seen:
        reasons.append("idle-finalize-did-not-promote")
    if TERMINAL_EVENTS & set(events_seen):
        # The canary turns may have been absorbed by the terminal boundary;
        # the idle path is then not proven.  Run promote right after the drain.
        reasons.append("terminal-event-present-idle-path-not-isolated")
    record["promote"] = {
        "pass": not reasons, "reasons": reasons, "turns": turns,
        "state_status": state.get("status"), "state_updated_at": state.get("updated_at"),
        "event_path": str(event_path) if event_path else None,
        "event_sha256": _sha(event_path) if event_path and event_path.is_file() else None,
        "events_seen": events_seen, "project": artifact.get("project") if artifact else None,
    }
    return not reasons


def _session_transcript(config: MemoryConfig, runtime: str, session_id: str) -> Path | None:
    if runtime == "codex":
        return resolve_codex_rollout(config, session_id)
    matches = [
        path for root in config.transcript_roots.get("claude", ())
        for path in Path(root).glob(f"*/{session_id}.jsonl") if path.is_file()
    ]
    return matches[0] if len(matches) == 1 else None


def _statement(config: MemoryConfig, runtime: str, session_id: str, canary: str) -> dict:
    transcript = _session_transcript(config, runtime, session_id)
    user_typed = assistant_said = False
    if transcript is not None:
        for role, text in transcript_turns(
            transcript, allowed_roots=config.transcript_roots.get(runtime, ()), strict=False,
        ):
            if canary in text:
                user_typed = user_typed or role == "user"
                assistant_said = assistant_said or role == "assistant"
    return {
        "transcript": str(transcript) if transcript else None,
        "assistant_stated_canary": assistant_said, "user_typed_canary": user_typed,
    }


def _statement_reasons(statement: dict) -> list[str]:
    reasons = []
    if not statement["assistant_stated_canary"]:
        reasons.append("assistant-did-not-state-canary")
    if statement["user_typed_canary"]:
        reasons.append("user-typed-canary")
    return reasons


def recall_late(config: MemoryConfig, record: dict, *, runtime: str, session: str) -> bool:
    promoted = record.get("promote") or {}
    if not promoted.get("pass"):
        record["recall_late"] = {"pass": False, "reasons": ["promote-phase-not-passed"]}
        return False
    canary = record["capture"]["canary"]
    evidence_file = config.state_path / "evidence" / f"late-recall-{runtime}.json"
    evidence = json.loads(evidence_file.read_text(encoding="utf-8")) if evidence_file.is_file() else {}
    receipt = next((
        item for item in evidence.get("delivered", [])
        if item.get("event_path") == promoted["event_path"]
    ), None)
    started = _time(evidence.get("session_started_at"))
    promoted_at = _time((receipt or {}).get("state_updated_at"))
    reasons = []
    if evidence.get("session_key") != session_key(session):
        reasons.append("no-late-recall-evidence-for-session")
    elif receipt is None:
        reasons.append("artifact-not-delivered-by-late-recall")
    elif started is None or promoted_at is None or promoted_at < started:
        reasons.append("promotion-not-after-session-start")
    if canary not in (evidence.get("text") or ""):
        reasons.append("canary-not-in-late-recall-text")
    statement = _statement(config, runtime, session, canary)
    reasons += _statement_reasons(statement)
    record["recall_late"] = {
        "pass": not reasons, "reasons": reasons, "runtime": runtime, "session": session,
        "evidence": str(evidence_file), "session_started_at": evidence.get("session_started_at"),
        "delivered": receipt, **statement,
    }
    return not reasons


def recall_startup(config: MemoryConfig, record: dict, *, runtime: str, session: str) -> bool:
    promoted = record.get("promote") or {}
    if not promoted.get("pass"):
        record["recall_startup"] = {"pass": False, "reasons": ["promote-phase-not-passed"]}
        return False
    canary = record["capture"]["canary"]
    evidence_file = config.state_path / "evidence" / f"recall-{runtime}.json"
    evidence = json.loads(evidence_file.read_text(encoding="utf-8")) if evidence_file.is_file() else {}
    reasons = []
    if evidence.get("session_key") != session:
        reasons.append("no-startup-evidence-for-session")
    elif canary not in (evidence.get("bundle_snapshot") or ""):
        reasons.append("canary-not-in-startup-bundle")
    observed = _time(evidence.get("observed_at"))
    promoted_at = _time(promoted.get("state_updated_at"))
    if observed is None or promoted_at is None or observed < promoted_at:
        reasons.append("startup-not-after-promotion")
    late_session = (record.get("recall_late") or {}).get("session")
    if late_session and late_session == session:
        reasons.append("same-session-as-late-recall")
    statement = _statement(config, runtime, session, canary)
    reasons += _statement_reasons(statement)
    record["recall_startup"] = {
        "pass": not reasons, "reasons": reasons, "runtime": runtime, "session": session,
        "evidence": str(evidence_file), "observed_at": evidence.get("observed_at"), **statement,
    }
    return not reasons


def verdict(record: dict) -> bool:
    missing = [phase for phase in PHASES if not (record.get(phase) or {}).get("pass")]
    late = (record.get("recall_late") or {}).get("session")
    startup = (record.get("recall_startup") or {}).get("session")
    if late and startup and late == startup:
        missing.append("recall-sessions-not-distinct")
    record["verdict"] = {"pass": not missing, "failed_or_missing": missing}
    return not missing


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    phases = parser.add_subparsers(dest="phase", required=True)
    cap = phases.add_parser("capture")
    cap.add_argument("--thread", required=True)
    cap.add_argument("--canary", required=True)
    phases.add_parser("promote")
    for name in ("recall-late", "recall-startup"):
        sub = phases.add_parser(name)
        sub.add_argument("--runtime", required=True, choices=("claude", "codex"))
        sub.add_argument("--session", required=True)
    phases.add_parser("verdict")
    args = parser.parse_args()
    record = json.loads(args.out.read_text(encoding="utf-8")) if args.out.is_file() else {}
    config = MemoryConfig.load(args.config)
    if args.phase == "capture":
        ok, key = capture(config, record, thread=args.thread, canary=args.canary), "capture"
    elif args.phase == "promote":
        ok, key = promote(config, record), "promote"
    elif args.phase == "recall-late":
        ok, key = recall_late(config, record, runtime=args.runtime, session=args.session), "recall_late"
    elif args.phase == "recall-startup":
        ok, key = recall_startup(config, record, runtime=args.runtime, session=args.session), "recall_startup"
    else:
        ok, key = verdict(record), "verdict"
    args.out.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(record[key], ensure_ascii=False, indent=2))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

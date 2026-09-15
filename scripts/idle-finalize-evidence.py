#!/usr/bin/env python3
"""Read-only acceptance evidence for idle finalize.

Proves the real-app chain in three phases, each run against live state:

  capture  a Codex Desktop thread holds the canary in raw turn checkpoints
  promote  those exact turns were settled into a durable daily artifact
  recall   a new session received the artifact (startup bundle or late recall)
           and its assistant stated the canary without the user typing it

It never writes Memory OS state, the vault, or runtime files; its only output
is the JSON file given by ``--out``.  See runbooks/idle-finalize-acceptance.md.
"""
from __future__ import annotations

import argparse
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


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load(out: Path) -> dict:
    return json.loads(out.read_text(encoding="utf-8")) if out.is_file() else {}


def _save(out: Path, record: dict) -> None:
    out.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def capture(config: MemoryConfig, args, record: dict) -> bool:
    rollout = resolve_codex_rollout(config, args.thread)
    originator = None
    if rollout is not None:
        with rollout.open(encoding="utf-8") as handle:
            originator = json.loads(handle.readline()).get("payload", {}).get("originator")
    key = session_key(args.thread)
    pending = config.state_path / "queue" / "pending"
    turns = []
    for path in sorted(pending.glob(f"codex-{key}-*.json")):
        item = json.loads(path.read_text(encoding="utf-8"))
        if args.canary in item.get("normalized_transcript", ""):
            turns.append({
                "checkpoint": path.name, "checkpoint_sha256": _sha(path),
                "event": item.get("event"), "source_digest": item.get("source_digest"),
                "turn_id": item.get("turn_id"), "hook_observed_at": item.get("hook_observed_at"),
                "project": item.get("project"),
            })
    ok = rollout is not None and originator == "Codex Desktop" and bool(turns)
    record["capture"] = {
        "pass": ok, "thread": args.thread, "session_key": key, "canary": args.canary,
        "rollout": str(rollout) if rollout else None, "originator": originator,
        "checkpoints": turns,
    }
    return ok


def promote(config: MemoryConfig, args, record: dict) -> bool:
    captured = record.get("capture") or {}
    if not captured.get("pass"):
        record["promote"] = {"pass": False, "reason": "capture-phase-not-passed"}
        return False
    key = captured["session_key"]
    state_file = config.state_path / "sessions" / "codex" / f"{key}.json"
    state = json.loads(state_file.read_text(encoding="utf-8")) if state_file.is_file() else {}
    settled = set(settled_turn_digests(config, runtime="codex", state_key=key))
    pending = config.state_path / "queue" / "pending"
    checks = []
    for turn in captured["checkpoints"]:
        checks.append({
            "checkpoint": turn["checkpoint"],
            "raw_removed": not (pending / turn["checkpoint"]).exists(),
            "digest_settled": turn["event"] != "turn_complete" or turn["source_digest"] in settled,
        })
    event_path = Path(state["event_path"]) if isinstance(state.get("event_path"), str) else None
    artifact = None
    if event_path is not None and event_path.is_file():
        artifact = parse_event_artifact(event_path.read_text(encoding="utf-8"))
    canary_in_artifact = bool(event_path and event_path.is_file()
                              and args_canary(captured) in event_path.read_text(encoding="utf-8"))
    ok = (
        bool(checks) and all(c["raw_removed"] and c["digest_settled"] for c in checks)
        and artifact is not None and canary_in_artifact
    )
    record["promote"] = {
        "pass": ok, "turns": checks, "state_status": state.get("status"),
        "event_path": str(event_path) if event_path else None,
        "event_sha256": _sha(event_path) if event_path and event_path.is_file() else None,
        "events_seen": artifact.get("events_seen") if artifact else None,
        "project": artifact.get("project") if artifact else None,
        "canary_in_artifact": canary_in_artifact,
    }
    return ok


def args_canary(captured: dict) -> str:
    return captured["canary"]


def _new_session_transcript(config: MemoryConfig, runtime: str, session_id: str) -> Path | None:
    if runtime == "codex":
        return resolve_codex_rollout(config, session_id)
    matches = [
        path for root in config.transcript_roots.get("claude", ())
        for path in Path(root).glob(f"*/{session_id}.jsonl") if path.is_file()
    ]
    return matches[0] if len(matches) == 1 else None


def recall(config: MemoryConfig, args, record: dict) -> bool:
    promoted = record.get("promote") or {}
    captured = record.get("capture") or {}
    if not promoted.get("pass"):
        record["recall"] = {"pass": False, "reason": "promote-phase-not-passed"}
        return False
    canary = captured["canary"]
    evidence_dir = config.state_path / "evidence"
    channel = None
    startup = evidence_dir / f"recall-{args.runtime}.json"
    if startup.is_file():
        value = json.loads(startup.read_text(encoding="utf-8"))
        if value.get("session_key") == args.session and canary in (value.get("bundle_snapshot") or ""):
            channel = {"channel": "startup", "evidence": str(startup), "observed_at": value.get("observed_at")}
    late = evidence_dir / f"late-recall-{args.runtime}.json"
    if channel is None and late.is_file():
        value = json.loads(late.read_text(encoding="utf-8"))
        if value.get("session_key") == session_key(args.session) and canary in (value.get("text") or ""):
            channel = {"channel": "late-recall", "evidence": str(late), "observed_at": value.get("observed_at")}
    transcript = _new_session_transcript(config, args.runtime, args.session)
    user_typed = assistant_said = False
    if transcript is not None:
        for role, text in transcript_turns(
            transcript, allowed_roots=config.transcript_roots.get(args.runtime, ()), strict=False,
        ):
            if canary in text:
                if role == "user":
                    user_typed = True
                elif role == "assistant":
                    assistant_said = True
    ok = channel is not None and assistant_said and not user_typed
    record["recall"] = {
        "pass": ok, "runtime": args.runtime, "session": args.session, **(channel or {}),
        "transcript": str(transcript) if transcript else None,
        "assistant_stated_canary": assistant_said, "user_typed_canary": user_typed,
    }
    return ok


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    phases = parser.add_subparsers(dest="phase", required=True)
    cap = phases.add_parser("capture")
    cap.add_argument("--thread", required=True)
    cap.add_argument("--canary", required=True)
    phases.add_parser("promote")
    rec = phases.add_parser("recall")
    rec.add_argument("--runtime", required=True, choices=("claude", "codex"))
    rec.add_argument("--session", required=True)
    args = parser.parse_args()
    config = MemoryConfig.load(args.config)
    record = _load(args.out)
    ok = {"capture": capture, "promote": promote, "recall": recall}[args.phase](config, args, record)
    _save(args.out, record)
    print(json.dumps(record[args.phase], ensure_ascii=False, indent=2))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

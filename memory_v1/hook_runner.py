"""Fast lifecycle hook: durable checkpoint first, detached Luna drain second."""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

from .adapters import (
    MAX_TURN_CHECKPOINTS_PER_SESSION,
    checkpoint_hook,
    find_pending_turn_checkpoint,
    load_hook_input,
    pending_turn_checkpoint_count,
)
from .core import MemoryConfig, MemoryError, ensure_safe_directory, write_health
from .provider import scrubbed_subprocess_env


def build_drain_command(
    config_path: Path,
    queue_path: Path,
    python_bin: str | None = None,
) -> list[str]:
    executable = python_bin or sys.executable or "python3"
    return [
        executable, "-m", "memory_v1.cli",
        "--config", str(config_path.resolve()),
        "drain", "--queue", str(queue_path.resolve()),
    ]


def _spawn_drain(config_path: Path, queue_path: Path, log_path: Path) -> None:
    """Start a best-effort worker; lifecycle hooks themselves stay nonblocking."""
    repo_root = Path(__file__).resolve().parents[1]
    env = scrubbed_subprocess_env({"PYTHONPATH": str(repo_root)})
    env.pop("PZ_MEMORY_INVOKED_BY", None)
    with log_path.open("ab") as log:
        subprocess.Popen(
            build_drain_command(config_path, queue_path),
            cwd=str(repo_root), env=env, stdin=subprocess.DEVNULL,
            stdout=log, stderr=log, start_new_session=True, close_fds=True,
        )


def main(argv: list[str] | None = None) -> int:
    if os.environ.get("PZ_MEMORY_INVOKED_BY") == "memory-v1":
        return 0
    parser = argparse.ArgumentParser(prog="pz-memory-hook")
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--runtime", required=True, choices=("codex", "claude", "hermes"))
    parser.add_argument("--event", required=True)
    args = parser.parse_args(argv)
    config = MemoryConfig.load(args.config)
    try:
        raw_stdin = sys.stdin.read()
        try:
            log_dir = config.state_path / "logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            (log_dir / f"hook-{args.runtime}-{args.event}-stdin.json").write_text(raw_stdin, encoding="utf-8")
        except OSError:
            pass

        if args.event == "SessionStart":
            try:
                from .parity import SharedBrainParityManager
                SharedBrainParityManager(config.vault_path).align_shared_brain()
            except Exception:
                pass
            from .recall import (
                build_startup_recall_bundle,
                find_runtime_session_artifact,
                write_recall_evidence,
                RECALL_EVIDENCE_PROVENANCE_NATIVE,
            )
            payload = load_hook_input(None, raw_stdin)
            transcript_p = (
                payload.get("transcript_path")
                or payload.get("rollout_path")
                or payload.get("transcriptPath")
            )
            startup_session_id = None
            if transcript_p and isinstance(transcript_p, str):
                m = re.search(
                    r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})",
                    transcript_p,
                )
                if m:
                    startup_session_id = m.group(1)
            if not startup_session_id:
                startup_session_id = (
                    payload.get("thread_id")
                    or payload.get("threadId")
                    or payload.get("conversation_id")
                    or payload.get("session_id")
                    or payload.get("sessionId")
                    or "startup"
                )
            bundle = build_startup_recall_bundle(
                config, runtime=args.runtime, session_key=startup_session_id
            )
            art_path, art_sha = find_runtime_session_artifact(config, args.runtime, startup_session_id)
            write_recall_evidence(
                config,
                bundle,
                lifecycle_event="SessionStart",
                provenance=RECALL_EVIDENCE_PROVENANCE_NATIVE,
                session_artifact_path=str(art_path) if art_path else None,
                session_artifact_sha256=art_sha or "",
            )
            try:
                write_health(config.state_path, f"recall-{args.runtime}", "ok")
            except OSError:
                pass
            # A resumed runtime session may be the first reliable lifecycle
            # boundary after a crash.  Recover only its own pending raw turns;
            # the detached worker is intentionally best-effort so provider
            # failure never prevents normal runtime startup.
            if isinstance(startup_session_id, str) and startup_session_id != "startup":
                pending_turn = find_pending_turn_checkpoint(
                    config, runtime=args.runtime, session_id=startup_session_id
                )
                if pending_turn:
                    log_dir = config.state_path / "logs"
                    ensure_safe_directory(log_dir, create=True)
                    _spawn_drain(args.config, pending_turn, log_dir / f"drain-{args.runtime}.log")
            wire = {
                "continue": True,
                "hookSpecificOutput": {
                    "hookEventName": "SessionStart",
                    "additionalContext": bundle.text,
                },
            }
            print(json.dumps(wire, ensure_ascii=False))
            return 0
        payload = load_hook_input(None, raw_stdin)
        queue_path = checkpoint_hook(
            config, runtime=args.runtime, payload=payload, event_override=args.event
        )
        log_dir = config.state_path / "logs"
        ensure_safe_directory(log_dir, create=True)
        if args.event == "Stop":
            session_id = payload.get("session_id") or payload.get("sessionId") or payload.get("thread_id") or payload.get("threadId")
            if isinstance(session_id, str) and pending_turn_checkpoint_count(
                config, runtime=args.runtime, session_id=session_id
            ) >= MAX_TURN_CHECKPOINTS_PER_SESSION:
                # The documented bounded batch policy is the only ordinary
                # turn path that may promote.  It is still detached and leaves
                # raw checkpoints intact if the provider is unavailable.
                _spawn_drain(args.config, queue_path, log_dir / f"drain-{args.runtime}.log")
        else:
            _spawn_drain(args.config, queue_path, log_dir / f"drain-{args.runtime}.log")
        return 0
    except (MemoryError, OSError) as exc:
        try:
            write_health(config.state_path, f"hook-{args.runtime}", "blocked", str(exc))
        except OSError:
            pass
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

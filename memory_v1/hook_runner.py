"""Fast lifecycle hook: durable checkpoint first, detached Luna drain second."""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

from .adapters import checkpoint_hook, load_hook_input
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
            raw_stdin = sys.stdin.read()
            payload = load_hook_input(None, raw_stdin)
            transcript_p = (
                payload.get("transcript_path")
                or payload.get("rollout_path")
                or payload.get("transcriptPath")
            )
            session_key = None
            if transcript_p and isinstance(transcript_p, str):
                m = re.search(
                    r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})",
                    transcript_p,
                )
                if m:
                    session_key = m.group(1)
            if not session_key:
                session_key = (
                    payload.get("thread_id")
                    or payload.get("threadId")
                    or payload.get("conversation_id")
                    or payload.get("session_id")
                    or payload.get("sessionId")
                    or "startup"
                )
            bundle = build_startup_recall_bundle(
                config, runtime=args.runtime, session_key=session_key
            )
            art_path, art_sha = find_runtime_session_artifact(config, args.runtime, session_key)
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
            wire = {
                "continue": True,
                "hookSpecificOutput": {
                    "hookEventName": "SessionStart",
                    "additionalContext": bundle.text,
                },
            }
            print(json.dumps(wire, ensure_ascii=False))
            return 0
        raw_stdin = sys.stdin.read()
        payload = load_hook_input(None, raw_stdin)
        queue_path = checkpoint_hook(
            config, runtime=args.runtime, payload=payload, event_override=args.event
        )
        log_dir = config.state_path / "logs"
        ensure_safe_directory(log_dir, create=True)
        repo_root = Path(__file__).resolve().parents[1]
        env = scrubbed_subprocess_env({"PYTHONPATH": str(repo_root)})
        env.pop("PZ_MEMORY_INVOKED_BY", None)
        cmd = build_drain_command(args.config, queue_path)
        with (log_dir / f"drain-{args.runtime}.log").open("ab") as log:
            subprocess.Popen(
                cmd,
                cwd=str(repo_root),
                env=env,
                stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                start_new_session=True, close_fds=True,
            )
        return 0
    except (MemoryError, OSError) as exc:
        try:
            write_health(config.state_path, f"hook-{args.runtime}", "blocked", str(exc))
        except OSError:
            pass
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

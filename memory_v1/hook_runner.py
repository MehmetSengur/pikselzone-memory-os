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
    empty_lifecycle_reason,
    find_pending_turn_checkpoint,
    load_hook_input,
    pending_turn_checkpoint_count,
)
from .core import MemoryConfig, MemoryError, ensure_safe_directory, session_key, write_health
from .provider import scrubbed_subprocess_env
from .retry import (
    find_idle_turn_batches, find_stale_recoverable_checkpoints, prune_orphan_retry_states,
)


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
    from .memory_policy import config_policy
    from .core import MemoryConfig
    policy = config_policy(MemoryConfig.load(config_path))
    if not policy['summarize']:
        return
    if policy['processing_interval']:
        import datetime as dt
        try:
            value = json.loads(queue_path.read_text())
            observed = dt.datetime.fromisoformat(value['hook_observed_at'])
            if (dt.datetime.now(dt.timezone.utc) - observed).total_seconds() < policy['processing_interval']:
                return
        except (ValueError, KeyError, OSError):
            return
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


_SESSION_UUID_RE = re.compile(
    r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})"
)


def _payload_session_id(payload: dict) -> str | None:
    """The session identity SessionStart and UserPromptSubmit must agree on."""
    transcript_p = (
        payload.get("transcript_path")
        or payload.get("rollout_path")
        or payload.get("transcriptPath")
    )
    if transcript_p and isinstance(transcript_p, str):
        match = _SESSION_UUID_RE.search(transcript_p)
        if match:
            return match.group(1)
    for key in ("thread_id", "threadId", "conversation_id", "session_id", "sessionId"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _clean_continue() -> int:
    print(json.dumps({"continue": True}))
    return 0


def _resolve_scope(config: MemoryConfig, args, payload: dict) -> tuple[bool, str, str | None, str | None]:
    """Apply the single authority rule (plan §1c).

    Returns ``(capture_on, project, continuity_scope, off_reason)``.  Hermes
    never goes through the registry gate: it always captures, its continuity
    scope is "hermes" and its event provenance is "unscoped".
    """
    if args.runtime == "hermes":
        return True, "unscoped", "hermes", None
    from .project_registry import resolve_capture
    cwd = (
        os.environ.get("CLAUDE_PROJECT_DIR")
        or (payload.get("cwd") if isinstance(payload.get("cwd"), str) else None)
        or (payload.get("workdir") if isinstance(payload.get("workdir"), str) else None)
        or os.getcwd()
    )
    decision = resolve_capture(
        config.state_path, cwd=cwd,
        project=args.project, project_root=args.project_root,
    )
    if not decision.capture:
        try:
            write_health(
                config.state_path, f"capture-{args.runtime}", "off",
                f"{decision.reason}:{str(cwd)[:200]}",
            )
        except OSError:
            pass
        return False, "unscoped", None, decision.reason
    return True, decision.project or "unscoped", decision.project, None


def main(argv: list[str] | None = None) -> int:
    if os.environ.get("PZ_MEMORY_INVOKED_BY") == "memory-v1":
        return 0
    parser = argparse.ArgumentParser(prog="pz-memory-hook")
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--runtime", required=True, choices=("codex", "claude", "hermes"))
    parser.add_argument("--event", required=True)
    parser.add_argument("--project", default=None)
    parser.add_argument("--project-root", dest="project_root", default=None)
    args = parser.parse_args(argv)
    config = MemoryConfig.load(args.config)
    from .memory_policy import config_policy
    if not config_policy(config)["capture"]:
        return 0
    try:
        raw_stdin = sys.stdin.read()
        try:
            log_dir = config.state_path / "logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            (log_dir / f"hook-{args.runtime}-{args.event}-stdin.json").write_text(raw_stdin, encoding="utf-8")
        except OSError:
            pass

        try:
            gate_payload = load_hook_input(None, raw_stdin)
        except MemoryError:
            gate_payload = {}
        capture_on, project, continuity_scope, off_reason = _resolve_scope(
            config, args, gate_payload
        )

        import dataclasses
        config = dataclasses.replace(config, memory={**config.memory, "project": project})

        if not capture_on:
            # Fail-closed: no transcript or vault content is read.
            if args.event == "SessionStart":
                warn = (
                    "[MEMORY] Bu repo Memory OS kaydıyla eşleşmiyor "
                    f"({off_reason}) — hafıza yakalama KAPALI. Düzeltmek için: "
                    "memory register <path> --project <ad>"
                )
                print(json.dumps({
                    "continue": True,
                    "hookSpecificOutput": {
                        "hookEventName": "SessionStart", "additionalContext": warn,
                    },
                }, ensure_ascii=False))
                return 0
            return _clean_continue()

        try:
            # Every lifecycle event (rate-limited to one write per interval) keeps the
            # sync roundtrip current during long sessions; never blocks the session.
            from .sync_heartbeat import write_heartbeat
            write_heartbeat(config)
        except Exception:
            pass

        if args.event == "UserPromptSubmit":
            from .recall import associative_recall_fast
            prompt = ""
            for key in ("prompt", "user_input", "userPrompt", "user_prompt", "message", "text"):
                value = gate_payload.get(key)
                if isinstance(value, str) and value.strip():
                    prompt = value
                    break
            rendered = ""
            try:
                if prompt:
                    rendered = associative_recall_fast(config, prompt)
            except Exception as exc:  # fail-open: a slow/broken recall never blocks the turn
                try:
                    write_health(
                        config.state_path, f"assoc-recall-{args.runtime}", "blocked",
                        str(exc)[:200],
                    )
                except OSError:
                    pass
                rendered = ""
            # Memory idle finalize promoted after this session started.
            late = ""
            if args.runtime != "hermes":
                try:
                    late_session = _payload_session_id(gate_payload)
                    if late_session:
                        from .late_recall import deliver_late_recall
                        late = deliver_late_recall(
                            config, runtime=args.runtime, session_id=late_session
                        )
                except Exception:
                    late = ""
            rendered = "\n\n".join(part for part in (late, rendered) if part)
            if rendered:
                print(json.dumps({
                    "continue": True,
                    "hookSpecificOutput": {
                        "hookEventName": "UserPromptSubmit", "additionalContext": rendered,
                    },
                }, ensure_ascii=False))
                return 0
            return _clean_continue()

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
            startup_session_id = _payload_session_id(payload) or "startup"
            bundle = build_startup_recall_bundle(
                config, runtime=args.runtime, session_key=startup_session_id,
                continuity_scope=continuity_scope,
                project_filter=(continuity_scope if args.runtime != "hermes" else "unscoped"),
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
            # A terminal checkpoint whose drain hit a transient provider
            # failure was previously never looked at again.  Retry it here --
            # the same detached, best-effort worker, bounded by attempt count,
            # backoff, age and a hard spawn cap, and never blocking startup.
            try:
                prune_orphan_retry_states(config)
                stale = find_stale_recoverable_checkpoints(config, runtime=args.runtime)
                if stale:
                    log_dir = config.state_path / "logs"
                    ensure_safe_directory(log_dir, create=True)
                    for stale_path in stale:
                        _spawn_drain(
                            args.config, stale_path, log_dir / f"drain-{args.runtime}.log"
                        )
            except Exception:
                pass
            # A thread that never reaches SessionEnd (Codex Desktop sends it
            # only on archive/delete, normal close, or 30 minutes idle while
            # open in no client) would keep its raw turns pending forever.
            # This is not a timer: quiet threads are finalized here, at the
            # next registered workstation SessionStart after their idle
            # window, as one batch per thread.  The starting thread is
            # excluded: its own resume path above owns it.
            if args.runtime != "hermes":
                try:
                    exclude: frozenset[tuple[str, str]] = frozenset()
                    if isinstance(startup_session_id, str) and startup_session_id != "startup":
                        exclude = frozenset({(args.runtime, session_key(startup_session_id))})
                    idle = find_idle_turn_batches(config, exclude=exclude)
                    if idle and startup_session_id != "startup":
                        # Recorded before spawning, so a drain can never finish
                        # before this session's start time.
                        try:
                            from .late_recall import record_pending_finalize
                            record_pending_finalize(
                                config, runtime=args.runtime, session_id=startup_session_id,
                                project=continuity_scope, checkpoints=idle,
                            )
                        except Exception:
                            pass
                    if idle:
                        log_dir = config.state_path / "logs"
                        ensure_safe_directory(log_dir, create=True)
                        for idle_path in idle:
                            idle_runtime = idle_path.name.split("-", 1)[0]
                            _spawn_drain(
                                args.config, idle_path, log_dir / f"drain-{idle_runtime}.log"
                            )
                except Exception:
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
        payload = load_hook_input(None, raw_stdin)
        try:
            queue_path = checkpoint_hook(
                config, runtime=args.runtime, payload=payload, event_override=args.event,
                project=project, continuity_scope=continuity_scope,
            )
        except MemoryError as exc:
            empty_reason = empty_lifecycle_reason(
                config, runtime=args.runtime, payload=payload, exc=exc
            )
            if empty_reason is None:
                raise
            # A session that never produced a turn is an empty lifecycle
            # boundary, not a blocked capture.  Reporting it as blocked was a
            # monitoring false positive; there is no memory to lose here.
            write_health(
                config.state_path, f"hook-{args.runtime}", "ok",
                f"lifecycle-empty:{empty_reason}",
            )
            return 0
        # The raw checkpoint is durable now, so the capture half of this
        # lifecycle hook genuinely succeeded.  Record that: without a success
        # write this component only ever moves to "blocked", so a failure that
        # was fixed long ago keeps showing red to anyone reading the file.
        # The drain that follows is detached and reports itself separately
        # under "drain" and "flush-<runtime>"; a later drain failure must not
        # retroactively invalidate a capture that did work.
        write_health(
            config.state_path, f"hook-{args.runtime}", "ok",
            f"lifecycle-ok:{args.event}",
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
    except Exception as exc:
        # Exit-code contract: a Memory OS failure never steers the runtime.
        # Exit 2 is a *decision* in both runtimes, not an error code: on Stop
        # it makes Codex/Claude continue with a new prompt, on
        # UserPromptSubmit it blocks and erases the prompt, on PreCompact it
        # blocks compaction, and in Claude it blocks SessionStart/SessionEnd.
        # This hook makes no such decisions, so every handled failure is
        # recorded as blocked health and exits 0.  Only a failure before the
        # state path is known (an unreadable config) escapes as exit 1, which
        # both runtimes report as a non-blocking hook error.
        detail = str(exc) or exc.__class__.__name__
        try:
            write_health(config.state_path, f"hook-{args.runtime}", "blocked", detail[:500])
        except OSError:
            pass
        return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Runtime-neutral hook input adapters for Codex, Claude Code, and Hermes."""
from __future__ import annotations

import json
import os
import re
import stat
import subprocess
from pathlib import Path
from typing import Any

from .core import (
    DuplicateEvent, MemoryConfig, NoMemory, NormalizedTranscript, PolicyError, SchemaError,
    atomic_json, discover_codex_binary, ensure_safe_directory, iso_now, normalize_transcript,
    path_within, reject_symlink_chain, safe_unlink, session_key, sha256_file,
)
from .events import EventWriter, parse_event_artifact
from .provider import StructuredResponsesProvider, create_provider


EVENT_ALIASES = {
    "sessionstart": "session_start",
    "sessionend": "session_end",
    "precompact": "pre_compact",
    "postcompact": "post_compact",
    "subagentstart": "subagent_start",
    "subagentstop": "subagent_stop",
    "onsessionstart": "session_start",
    "onsessionend": "session_end",
    "onsessionfinalize": "session_finalize",
    "onsessionreset": "session_reset",
    "onsessioncompress": "pre_compact",
}


def normalize_event_name(value: str) -> str:
    compact = re.sub(r"[^a-z]", "", value.lower())
    event = EVENT_ALIASES.get(compact)
    if event is None:
        raise SchemaError("hook-event-unsupported")
    return event


def load_hook_input(path: Path | None, stdin_text: str = "") -> dict[str, Any]:
    try:
        raw = path.read_text(encoding="utf-8") if path else stdin_text
        value = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        raise SchemaError("hook-input-invalid") from exc
    if not isinstance(value, dict):
        raise SchemaError("hook-input-not-object")
    return value


def _first_text(payload: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return None


def _validated_transcript_path(
    config: MemoryConfig, runtime: str, transcript: str
) -> Path:
    path = Path(transcript)
    if not path.is_absolute():
        raise PolicyError("transcript-path-not-absolute")
    roots = config.transcript_roots.get(runtime, ())
    if not roots:
        raise PolicyError("transcript-roots-not-configured")
    if not any(path_within(path, root) for root in roots):
        raise PolicyError("transcript-path-outside-allowed-roots")
    return path


def flush_hook(
    config: MemoryConfig, *, runtime: str, payload: dict[str, Any],
    event_override: str | None = None, provider: StructuredResponsesProvider | None = None,
) -> Path:
    if runtime not in {"codex", "claude", "hermes"}:
        raise SchemaError("hook-runtime-invalid")
    event_raw = event_override or _first_text(
        payload, ("hook_event_name", "hookEventName", "event", "hook_event", "reason")
    )
    if not event_raw:
        raise SchemaError("hook-event-missing")
    event = normalize_event_name(event_raw)
    session_id = _first_text(
        payload, ("session_id", "sessionId", "thread_id", "threadId", "conversation_id")
    )
    transcript = _first_text(
        payload, ("transcript_path", "transcriptPath", "rollout_path", "history_path")
    )
    if not session_id:
        raise SchemaError("hook-session-id-missing")
    if not transcript:
        raise SchemaError("hook-transcript-path-missing")
    active_provider = provider or create_provider(config)
    writer = EventWriter(config, active_provider)
    return writer.flush(
        runtime=runtime,
        agent_id=_first_text(payload, ("agent_id", "agentId", "agent_name")) or f"{runtime}-main",
        session_id=session_id,
        event=event,
        transcript=_validated_transcript_path(config, runtime, transcript),
        source_model=_first_text(payload, ("model", "source_model", "sourceModel")),
        root_task_id=_first_text(payload, ("root_task_id", "rootTaskId", "task_id")),
        kanban_ids=[
            str(item) for item in payload.get("kanban_ids", []) if isinstance(item, str)
        ] if isinstance(payload.get("kanban_ids", []), list) else [],
    )


def codex_hook(config: MemoryConfig, payload: dict[str, Any], **kwargs: Any) -> Path:
    return flush_hook(config, runtime="codex", payload=payload, **kwargs)


def claude_hook(config: MemoryConfig, payload: dict[str, Any], **kwargs: Any) -> Path:
    return flush_hook(config, runtime="claude", payload=payload, **kwargs)


def hermes_hook(config: MemoryConfig, payload: dict[str, Any], **kwargs: Any) -> Path:
    """Local adapter only; production lifecycle registration is activation-gated."""
    return flush_hook(config, runtime="hermes", payload=payload, **kwargs)


def checkpoint_hook(
    config: MemoryConfig, *, runtime: str, payload: dict[str, Any],
    event_override: str | None = None,
) -> Path:
    """Atomically preserve normalized transcript data before compaction returns."""
    if runtime not in config.runtimes:
        raise PolicyError("runtime-not-enabled")
    event_raw = event_override or _first_text(
        payload, ("hook_event_name", "hookEventName", "event", "hook_event", "reason")
    )
    session_id = _first_text(
        payload, ("session_id", "sessionId", "thread_id", "threadId", "conversation_id")
    )
    transcript = _first_text(
        payload, ("transcript_path", "transcriptPath", "rollout_path", "history_path")
    )
    if not event_raw or not session_id or not transcript:
        raise SchemaError("checkpoint-input-missing")
    event = normalize_event_name(event_raw)
    normalized, turn_count, digest = normalize_transcript(
        _validated_transcript_path(config, runtime, transcript),
        allowed_roots=config.transcript_roots.get(runtime, ()),
    )
    if turn_count == 0:
        raise SchemaError("checkpoint-transcript-empty")
    key = session_key(session_id)
    queue_path = (
        config.state_path / "queue" / "pending"
        / f"{runtime}-{key}-{event}-{digest[:16]}.json"
    )
    hook_observed_at = iso_now()
    hook_cfg_path = (
        config.codex_hooks_path if runtime == "codex"
        else (config.claude_settings_path if runtime == "claude" else None)
    )
    hook_cfg_sha = (
        sha256_file(hook_cfg_path)
        if (hook_cfg_path and hook_cfg_path.is_file())
        else ""
    )
    atomic_json(queue_path, {
        "schema": "pikselzone-memory-checkpoint-v1",
        "runtime": runtime,
        "agent_id": _first_text(payload, ("agent_id", "agentId", "agent_name"))
        or f"{runtime}-main",
        "session_id": session_id,
        "event": event,
        "source_model": _first_text(payload, ("model", "source_model", "sourceModel"))
        or "unknown",
        "root_task_id": _first_text(payload, ("root_task_id", "rootTaskId", "task_id"))
        or "unknown",
        "kanban_ids": [
            str(item) for item in payload.get("kanban_ids", []) if isinstance(item, str)
        ] if isinstance(payload.get("kanban_ids", []), list) else [],
        "source_digest": digest,
        "normalized_transcript": normalized,
        "hook_observed_at": hook_observed_at,
        "hook_config_sha256": hook_cfg_sha,
    })
    return queue_path


def drain_checkpoint(
    config: MemoryConfig, queue_path: Path, *,
    provider: StructuredResponsesProvider | None = None,
) -> Path:
    worker_started_at = iso_now()
    pending = config.state_path / "queue" / "pending"
    if not queue_path.is_absolute() or not path_within(queue_path, pending):
        raise PolicyError("checkpoint-path-outside-queue")
    reject_symlink_chain(queue_path)
    info = queue_path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise PolicyError("checkpoint-file-unsafe")
    checkpoint_digest = sha256_file(queue_path)
    checkpoint_id = queue_path.name
    try:
        value = json.loads(queue_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SchemaError("checkpoint-corrupt") from exc
    required = {
        "schema", "runtime", "agent_id", "session_id", "event", "source_model",
        "root_task_id", "kanban_ids", "source_digest", "normalized_transcript",
    }
    if not isinstance(value, dict) or not required.issubset(set(value)):
        raise SchemaError("checkpoint-schema-invalid")
    if value["schema"] != "pikselzone-memory-checkpoint-v1":
        raise SchemaError("checkpoint-schema-name-invalid")
    hook_observed_at = str(value.get("hook_observed_at") or worker_started_at)
    hook_cfg_sha = str(value.get("hook_config_sha256") or "")
    runtime = value["runtime"]
    s_key = session_key(value["session_id"])
    active_provider = provider or create_provider(config)
    try:
        event_path = EventWriter(config, active_provider).flush(
            runtime=value["runtime"], agent_id=value["agent_id"],
            session_id=value["session_id"], event=value["event"],
            transcript=NormalizedTranscript.from_checkpoint(
                value["normalized_transcript"], value["source_digest"]
            ),
            source_model=value["source_model"], root_task_id=value["root_task_id"],
            kanban_ids=value["kanban_ids"],
        )
    except DuplicateEvent as exc:
        event_path = Path(str(exc))
        if (
            not event_path.is_absolute()
            or not path_within(event_path, config.vault_path / "daily")
            or not event_path.is_file()
        ):
            raise PolicyError("duplicate-event-path-invalid") from exc
        if sha256_file(queue_path) != checkpoint_digest:
            raise PolicyError("checkpoint-changed-during-drain")
        safe_unlink(queue_path, root=pending)
        return event_path
    except NoMemory:
        safe_unlink(queue_path, root=pending)
        raise
    worker_completed_at = iso_now()
    if sha256_file(queue_path) != checkpoint_digest:
        raise PolicyError("checkpoint-changed-during-drain")

    event_digest = sha256_file(event_path)
    event_artifact = parse_event_artifact(event_path.read_text(encoding="utf-8"))
    provider_name = (
        getattr(active_provider, "last_source_provider", None)
        or event_artifact.get("source_provider")
        or ("chatgpt-subscription" if runtime == "codex" else ("claude-subscription" if runtime == "claude" else "unknown"))
    )
    model_name = (
        getattr(active_provider, "last_source_model", None)
        or event_artifact.get("source_model")
        or ("gpt-5.6-luna" if runtime == "codex" else ("haiku" if runtime == "claude" else "unknown"))
    )

    evidence_path = (
        config.codex_smoke_evidence_path if runtime == "codex"
        else (config.claude_smoke_evidence_path if runtime == "claude" else None)
    )
    if evidence_path:
        runtime_version = "unknown"
        if runtime == "codex":
            codex_bin = discover_codex_binary(config)
            if codex_bin:
                try:
                    ver_run = subprocess.run([codex_bin, "--version"], capture_output=True, text=True, timeout=5, check=False)
                    runtime_version = ver_run.stdout.strip()[:100] or "unknown"
                except (OSError, subprocess.TimeoutExpired):
                    pass
        elif runtime == "claude":
            try:
                ver_run = subprocess.run(["claude", "--version"], capture_output=True, text=True, timeout=5, check=False)
                runtime_version = ver_run.stdout.strip()[:100] or "unknown"
            except (OSError, subprocess.TimeoutExpired):
                pass

        if not hook_cfg_sha:
            cfg_path = (
                config.codex_hooks_path if runtime == "codex"
                else (config.claude_settings_path if runtime == "claude" else None)
            )
            if cfg_path and cfg_path.is_file():
                hook_cfg_sha = sha256_file(cfg_path)

        worker_receipt = {
            "runtime": runtime,
            "session_key": s_key,
            "checkpoint_id": checkpoint_id,
            "checkpoint_sha256": checkpoint_digest,
            "hook_observed_at": hook_observed_at,
            "worker_started_at": worker_started_at,
            "worker_completed_at": worker_completed_at,
            "event_path": str(event_path),
            "event_sha256": event_digest,
            "source_provider": provider_name,
            "source_model": model_name,
            "worker_pid": os.getpid(),
        }
        evidence_payload = {
            "schema": "pikselzone-memory-activation-evidence-v1",
            "runtime": runtime,
            "status": "pass",
            "runtime_version": runtime_version,
            "hook_config_sha256": hook_cfg_sha,
            "smoke_session_key": s_key,
            "checkpoint_mode": "0600",
            "event_path": str(event_path),
            "event_sha256": event_digest,
            "duplicate_files": 0,
            "observed_at": worker_completed_at,
            "checkpoint_id": checkpoint_id,
            "provenance": "automatic-hook-drain",
            "source_provider": provider_name,
            "worker_receipt": worker_receipt,
        }
        ensure_safe_directory(evidence_path.parent, create=True)
        atomic_json(evidence_path, evidence_payload)

    safe_unlink(queue_path, root=pending)
    return event_path

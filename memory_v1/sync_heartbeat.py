"""Is the shared vault actually moving between the workstation and the engine?

Obsidian Sync on the Mac only runs while the Obsidian app is open. When it is
closed, the VPS keeps serving the memory it already has, but nothing new from
the Mac arrives, and neither side said so. A past "Fully synced" line on the
VPS only proves the VPS reached the sync service, not that the Mac did.

The workstation writes a heartbeat file (``companion/sync-heartbeat/<host>.md``)
with an increasing sequence number when a session starts, at most once per
interval; no background job is added. The memory-engine publisher records,
on its own clock, when it first saw each new sequence and writes an
acknowledgement file only when something new arrived. Each side then judges
freshness on its own clock:

- the workstation compares its latest sequence with the acknowledged one; an
  old acknowledgement never stands in for the current state;
- the engine reports how long ago it last saw a new heartbeat from each
  workstation, as a warning only: its own memory stays usable.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

from .core import MemoryConfig, atomic_json, atomic_write, ensure_safe_directory

SCHEMA = "pikselzone-sync-heartbeat-v1"
ACK_SCHEMA = "pikselzone-sync-ack-v1"
HEARTBEAT_REL = Path("companion") / "sync-heartbeat"
ACK_PREFIX = "_ack-"
MIN_INTERVAL_SECONDS = 600
UNACKED_WARN_SECONDS = 15 * 60
FOREIGN_QUIET_WARN_SECONDS = 24 * 3600
HISTORY_KEEP = 50
_HOST_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _host() -> str:
    from .learning_inbox import host_label
    return host_label()


def _now(now: float | None) -> float:
    return dt.datetime.now().timestamp() if now is None else now


def _iso(epoch: float) -> str:
    return dt.datetime.fromtimestamp(epoch).astimezone().isoformat(timespec="seconds")


def heartbeat_dir(config: MemoryConfig) -> Path:
    return config.vault_path / HEARTBEAT_REL


def _state_file(config: MemoryConfig, name: str) -> Path:
    return config.state_path / "sync" / name


def _load(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _render(meta: dict[str, Any]) -> str:
    return "\n".join(["---", *(f"{k}: {json.dumps(v, ensure_ascii=False)}" for k, v in meta.items()), "---", ""])


def _parse(text: str) -> dict[str, Any]:
    lines = text.splitlines()
    if not lines or lines[0] != "---" or "---" not in lines[1:]:
        return {}
    meta: dict[str, Any] = {}
    for line in lines[1:lines.index("---", 1)]:
        key, sep, raw = line.partition(":")
        if not sep:
            return {}
        try:
            meta[key.strip()] = json.loads(raw.strip())
        except json.JSONDecodeError:
            return {}
    return meta


# --- workstation -------------------------------------------------------------

def write_heartbeat(config: MemoryConfig, *, now: float | None = None) -> dict[str, Any] | None:
    """Write the next heartbeat if the interval has passed. Engines do not heartbeat."""
    if config.role == "memory-engine":
        return None
    moment = _now(now)
    state_path = _state_file(config, "heartbeat.json")
    state = _load(state_path)
    last = float(state.get("written_at_epoch") or 0)
    if last and moment - last < MIN_INTERVAL_SECONDS:
        return None
    seq = int(state.get("seq") or 0) + 1
    host = _host()
    directory = heartbeat_dir(config)
    ensure_safe_directory(directory, create=True)
    meta = {"schema": SCHEMA, "host": host, "seq": seq, "written_at": _iso(moment)}
    atomic_write(directory / f"{host}.md", _render(meta).encode("utf-8"), mode=0o660)
    history = {str(k): v for k, v in (state.get("history") or {}).items()}
    history[str(seq)] = moment
    for key in sorted(history, key=int)[:-HISTORY_KEEP]:
        history.pop(key)
    ensure_safe_directory(state_path.parent, create=True)
    atomic_json(state_path, {"seq": seq, "written_at_epoch": moment, "host": host, "history": history})
    return meta


def _acked_seq(config: MemoryConfig, host: str) -> tuple[int, str]:
    best, seen_at = 0, ""
    directory = heartbeat_dir(config)
    if not directory.is_dir():
        return best, seen_at
    for path in sorted(directory.glob(f"{ACK_PREFIX}*.md")):
        meta = _parse(path.read_text(encoding="utf-8"))
        if meta.get("schema") != ACK_SCHEMA:
            continue
        entry = (meta.get("hosts") or {}).get(host) or {}
        if int(entry.get("seq") or 0) > best:
            best, seen_at = int(entry["seq"]), str(entry.get("first_seen_at") or "")
    return best, seen_at


def obsidian_app_running() -> bool | None:
    if sys.platform != "darwin":
        return None
    try:
        return subprocess.run(["pgrep", "-x", "Obsidian"], capture_output=True, timeout=5).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return None


# --- engine --------------------------------------------------------------------

def acknowledge_heartbeats(config: MemoryConfig, *, now: float | None = None) -> dict[str, Any]:
    """Record newly arrived heartbeat sequences on this host's clock."""
    if config.role != "memory-engine":
        return {"status": "skipped", "reason": "not-the-engine"}
    moment = _now(now)
    directory = heartbeat_dir(config)
    observed_path = _state_file(config, "observed.json")
    observed = _load(observed_path)
    changed = []
    if directory.is_dir():
        for path in sorted(directory.glob("*.md")):
            if path.name.startswith(ACK_PREFIX) or path.is_symlink():
                continue
            meta = _parse(path.read_text(encoding="utf-8"))
            host = str(meta.get("host") or "")
            if meta.get("schema") != SCHEMA or not host or not isinstance(meta.get("seq"), int):
                continue
            known = observed.get(host) or {}
            if meta["seq"] > int(known.get("seq") or 0):
                observed[host] = {"seq": meta["seq"], "first_seen_at_epoch": moment}
                changed.append(host)
    if changed:
        ensure_safe_directory(observed_path.parent, create=True)
        atomic_json(observed_path, observed)
        ack = {
            "schema": ACK_SCHEMA, "engine": _host(), "updated_at": _iso(moment),
            "hosts": {h: {"seq": v["seq"], "first_seen_at": _iso(v["first_seen_at_epoch"])} for h, v in observed.items()},
        }
        ensure_safe_directory(directory, create=True)
        atomic_write(directory / f"{ACK_PREFIX}{_HOST_RE.sub('-', _host())}.md", _render(ack).encode("utf-8"), mode=0o660)
    return {"status": "ok", "new": changed}


# --- doctor rows ------------------------------------------------------------------

def sync_roundtrip_row(config: MemoryConfig, *, now: float | None = None) -> dict[str, str]:
    moment = _now(now)
    if config.role == "memory-engine":
        observed = _load(_state_file(config, "observed.json"))
        if not observed:
            return {"check": "sync_heartbeat_arrival", "status": "unknown", "detail": "no-workstation-heartbeat-seen-yet"}
        parts, quiet = [], False
        for host, entry in sorted(observed.items()):
            age = int(moment - float(entry.get("first_seen_at_epoch") or 0))
            parts.append(f"{host}:seq={entry.get('seq')},last_new={age // 60}m_ago")
            quiet = quiet or age > FOREIGN_QUIET_WARN_SECONDS
        detail = ";".join(parts)
        if quiet:
            detail += ";workstation-quiet-or-not-syncing(engine-memory-still-usable)"
        return {"check": "sync_heartbeat_arrival", "status": "warn" if quiet else "pass", "detail": detail}

    state = _load(_state_file(config, "heartbeat.json"))
    seq = int(state.get("seq") or 0)
    if not seq:
        return {"check": "sync_roundtrip", "status": "unknown", "detail": "no-heartbeat-written-yet"}
    acked, seen_at = _acked_seq(config, str(state.get("host") or _host()))
    if acked >= seq:
        return {"check": "sync_roundtrip", "status": "pass", "detail": f"seq={seq} acknowledged by engine at {seen_at}"}
    history = state.get("history") or {}
    first_unacked = float(history.get(str(acked + 1)) or state.get("written_at_epoch") or moment)
    age = int(moment - first_unacked)
    app = obsidian_app_running()
    app_text = "unknown" if app is None else ("running" if app else "not-running")
    detail = f"local_seq={seq};acked_seq={acked};unacknowledged_for={age // 60}m;obsidian_app={app_text}"
    if age <= UNACKED_WARN_SECONDS:
        return {"check": "sync_roundtrip", "status": "pass", "detail": detail + ";in-flight"}
    return {"check": "sync_roundtrip", "status": "warn", "detail": detail + ";local-changes-not-yet-on-engine"}


def learning_inbox_row(config: MemoryConfig, *, now: float | None = None) -> dict[str, str]:
    from .learning_inbox import inbox_status
    status = inbox_status(config)
    pending, age = status["pending"], status["oldest_age_seconds"]
    limit = 10 * 60 if config.role == "memory-engine" else 60 * 60
    detail = f"pending={pending}" + (f";oldest={age // 60}m" if age is not None else "")
    if config.role != "memory-engine":
        detail += ";merged-by-engine-after-sync"
    return {
        "check": "learning_inbox",
        "status": "warn" if pending and age is not None and age > limit else "pass",
        "detail": detail,
    }

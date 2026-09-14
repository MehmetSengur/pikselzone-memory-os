"""Autonomous machine acceptance harness for Pikselzone Memory V1 Phase M4.2B.

The harness controller runs on the workstation; the Hermes leg runs on the
memory-engine host over SSH.  Every host-specific value lives in HarnessTarget
so the harness can never silently aim at a decommissioned server, and every
runtime session id is established deterministically rather than by picking the
most recently modified file or database row.
"""
from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import hashlib
import json
import os
import re
import secrets
import shlex
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

try:
    from .core import MemoryConfig, codex_final_agent_message, iso_now, sha256_bytes, sha256_file
    from .events import parse_event_artifact
    from .recall import (
        CROSS_RUNTIME_CONTINUITY_PROVENANCE_MACHINE,
        HarnessExecutionRun,
        _write_machine_cross_runtime_receipt,
        compute_lifecycle_receipt,
        sanitize_untrusted_memory,
        verify_cross_runtime_continuity_evidence,
    )
except ImportError:
    from memory_v1.core import MemoryConfig, codex_final_agent_message, iso_now, sha256_bytes, sha256_file
    from memory_v1.events import parse_event_artifact
    from memory_v1.recall import (
        CROSS_RUNTIME_CONTINUITY_PROVENANCE_MACHINE,
        HarnessExecutionRun,
        _write_machine_cross_runtime_receipt,
        compute_lifecycle_receipt,
        sanitize_untrusted_memory,
        verify_cross_runtime_continuity_evidence,
    )


# ---------------------------------------------------------------------------
# Target description
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class HarnessTarget:
    """Everything host-specific the harness needs.

    ``expected_hostname`` is not decoration: an SSH alias can keep pointing at a
    retired host long after a migration, so the preflight refuses to run when
    the alias resolves somewhere unexpected.
    """

    name: str
    ssh_alias: str
    expected_hostname: str
    vault_path: str
    hermes_home: str
    hermes_profile: str
    startup_bundle_path: str
    profile_state_db: str
    receipts_dir: str
    evidence_dir: str
    evidence_owner: str
    hermes_cli: str
    memory_cli: str
    memory_config_path: str
    memory_python: str
    memory_pythonpath: str
    publisher_service: str = "pz-memory-publisher.service"
    policy_guard: str = ""  # empty means "not installed on this target"
    # The account the Hermes services run as.  The retrieval leg must run as
    # this user; see hermes_cmd for why.
    hermes_run_user: str = "pzhermes"

    @classmethod
    def from_file(cls, path: Path) -> "HarnessTarget":
        data = json.loads(path.read_text(encoding="utf-8"))
        known = {f.name for f in dataclasses.fields(cls)}
        unknown = set(data) - known
        if unknown:
            raise ValueError(f"Unknown target keys: {sorted(unknown)}")
        return cls(**data)


# The live Contabo memory-engine host.  Values verified against the running
# system; see reports/ handoff notes for the migration record.
CONTABO_TARGET = HarnessTarget(
    name="pz-contabo",
    ssh_alias="pz-contabo",
    expected_hostname="vmi3566230",
    vault_path="/srv/pz-hermes/vault",
    hermes_home="/srv/pz-hermes/hermes-data",
    hermes_profile="pz-orchestrator",
    startup_bundle_path="/srv/pz-hermes/hermes-data/memory-v1/inbox/hermes-startup-bundle.json",
    profile_state_db="/srv/pz-hermes/hermes-data/profiles/pz-orchestrator/state.db",
    receipts_dir="/srv/pz-hermes/hermes-data/memory-v1/state/receipts",
    evidence_dir="/var/lib/pz-memory-v1/evidence",
    evidence_owner="pzhermes:pzvault",
    hermes_cli="/usr/local/bin/pz-hermes",
    memory_cli="/usr/local/bin/pz-memory",
    memory_config_path="/srv/pz-hermes/memory-config.json",
    memory_python="/srv/pz-hermes/hermes-agent/venv/bin/python",
    memory_pythonpath="/srv/pz-hermes/memory-os",
)

DEFAULT_TARGET = CONTABO_TARGET


def load_target(spec: str | None) -> HarnessTarget:
    """Resolve a target from an explicit path, PZ_HARNESS_TARGET, or the default."""
    spec = spec or os.environ.get("PZ_HARNESS_TARGET") or ""
    if not spec:
        return DEFAULT_TARGET
    return HarnessTarget.from_file(Path(spec).expanduser())


# ---------------------------------------------------------------------------
# SSH helpers
# ---------------------------------------------------------------------------


def ssh(target: HarnessTarget, command: str, *, check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["ssh", "-o", "BatchMode=yes", target.ssh_alias, command],
        capture_output=True,
        text=True,
        check=check,
    )


def scp_to(target: HarnessTarget, sources: list[str], remote_dest: str, *, recursive: bool = False) -> None:
    cmd = ["scp", "-o", "BatchMode=yes"]
    if recursive:
        cmd.append("-r")
    cmd += sources + [f"{target.ssh_alias}:{remote_dest}"]
    subprocess.run(cmd, check=True)


def scp_from(target: HarnessTarget, remote_src: str, local_dest: str) -> None:
    subprocess.run(
        ["scp", "-o", "BatchMode=yes", f"{target.ssh_alias}:{remote_src}", local_dest],
        check=True,
    )


def hermes_cmd(target: HarnessTarget, args: str) -> str:
    """Build a Hermes CLI invocation that runs as the service account.

    SSH lands on this host as root, and a Hermes session started as root writes
    its receipts, discovery state and outbox recall evidence as root:root. The
    publisher runs as the service user and could then no longer read the outbox
    file -- which is a single fixed filename, so one root-run session blocked
    every later recall promotion with a per-minute PermissionError until the
    ownership was repaired. The retrieval leg has to run as a normal session
    would.
    """
    inner = f"{shlex.quote(target.hermes_cli)} -p {shlex.quote(target.hermes_profile)} {args}"
    if not target.hermes_run_user:
        return inner
    return f"sudo -n -u {shlex.quote(target.hermes_run_user)} -H sh -lc {shlex.quote(inner)}"


# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------


def preflight(target: HarnessTarget) -> dict[str, Any]:
    """Refuse to run against the wrong host or an incomplete install."""
    print(f"[preflight] target {target.name} via ssh {target.ssh_alias}")
    res = ssh(target, "hostname")
    if res.returncode != 0:
        raise RuntimeError(f"Cannot reach target {target.ssh_alias}: {res.stderr.strip()}")
    hostname = res.stdout.strip()
    if hostname != target.expected_hostname:
        raise RuntimeError(
            f"SSH alias {target.ssh_alias!r} resolves to host {hostname!r}, "
            f"expected {target.expected_hostname!r}. Refusing to run: a stale alias "
            f"would drive the acceptance test against the wrong server."
        )

    required = {
        "vault_path": target.vault_path,
        "hermes_home": target.hermes_home,
        "startup_bundle_path": target.startup_bundle_path,
        "profile_state_db": target.profile_state_db,
        "receipts_dir": target.receipts_dir,
        "evidence_dir": target.evidence_dir,
        "hermes_cli": target.hermes_cli,
        "memory_cli": target.memory_cli,
        "memory_config_path": target.memory_config_path,
        "memory_python": target.memory_python,
        "memory_pythonpath": target.memory_pythonpath,
    }
    probe = " ; ".join(
        f"test -e {shlex.quote(p)} && echo 'OK {k}' || echo 'MISS {k}'" for k, p in required.items()
    )
    res = ssh(target, probe)
    if res.returncode != 0:
        raise RuntimeError(f"Path probe failed on {target.name}: {res.stderr.strip()}")
    reported = {
        line.split(None, 1)[1]: line.split(None, 1)[0]
        for line in res.stdout.splitlines()
        if line.startswith(("OK ", "MISS ")) and len(line.split(None, 1)) == 2
    }
    missing = sorted(k for k, v in reported.items() if v == "MISS")
    if missing:
        raise RuntimeError(f"Target {target.name} is missing required paths: {missing}")
    # An SSH hiccup yields empty or partial output with no MISS line at all;
    # requiring an answer per path stops that reading as a clean preflight.
    unanswered = sorted(set(required) - set(reported))
    if unanswered:
        raise RuntimeError(
            f"Path probe on {target.name} returned no result for: {unanswered}. "
            f"Refusing to treat an incomplete probe as a passing preflight."
        )

    if target.hermes_run_user:
        who = ssh(target, f"sudo -n -u {shlex.quote(target.hermes_run_user)} id -un")
        if who.returncode != 0 or who.stdout.strip() != target.hermes_run_user:
            raise RuntimeError(
                f"Cannot run as service user {target.hermes_run_user!r} on {target.name}: "
                f"{who.stderr.strip() or who.stdout.strip()}"
            )

    policy_guard_available = False
    if target.policy_guard:
        pg = ssh(target, f"test -x {shlex.quote(target.policy_guard)} && echo YES || echo NO")
        policy_guard_available = "YES" in pg.stdout

    print(f"[preflight] hostname {hostname} confirmed; all required paths present")
    return {
        "target": target.name,
        "ssh_alias": target.ssh_alias,
        "hostname": hostname,
        "policy_guard_available": policy_guard_available,
        "checked_at": iso_now(),
    }


# ---------------------------------------------------------------------------
# Runtime legs
# ---------------------------------------------------------------------------


CANARY_ARTIFACT_DIR = Path("tmp") / "continuity-harness"


def stage_canary_artifact(repo_root: Path, marker: str, value: str, run_id: str) -> Path:
    """Write the run's check value to a real file, and return its path.

    The canary has to enter memory the way any other fact does: as something
    the session observed while doing real work. Two earlier revisions instead
    asserted the value in the prompt and asked a later session to repeat it --
    which is the shape of a token-exfiltration injection, and this deployment's
    summarizer classified it as exactly that, twice. It was right to. Rewording
    until the detector stopped noticing would have been gaming the test; the
    fix is to give the harness a genuine artifact to read.
    """
    artifact_dir = repo_root / CANARY_ARTIFACT_DIR
    artifact_dir.mkdir(parents=True, exist_ok=True)
    artifact = artifact_dir / f"{run_id}.json"
    artifact.write_text(
        json.dumps(
            {
                "schema": "pikselzone-continuity-harness-artifact-v1",
                "purpose": "cross-runtime continuity acceptance test",
                "authority": "test-artifact-not-operational-policy",
                "harness_run_id": run_id,
                "marker": marker,
                "check_value": value,
                "created_at": iso_now(),
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return artifact


def build_canary(marker: str, value: str) -> str:
    """The provenance sentence recorded alongside the run in its receipt."""
    return (
        f"Continuity harness test data, not an operational policy: "
        f"for marker {marker} the recorded check value is {value}."
    )


def run_claude_session(canary_marker: str, artifact_path: Path) -> tuple[str, bytes, bytes]:
    """Run a real Claude Code session under a pre-assigned session id.

    The id is chosen by the harness and handed to the CLI, so the transcript is
    identified by construction.  Scanning for the newest transcript would latch
    onto whatever other Claude session happens to be writing at the time --
    including the session the harness itself may be running under.
    """
    session_id = str(uuid.uuid4())
    # Deliberately free of the words this deployment's injection defense looks
    # for.  An earlier revision asked the session not to call any tools, and the
    # flush summarizer duly reported that no tool calls were made -- which the
    # directive-shaped guard refused, blocking the drain the harness was waiting
    # on.  The canary is a fact to acknowledge, not an instruction to follow.
    # A bare "read this and echo a field" is thin enough that the capture
    # summarizer can reasonably return status=empty, and then no event is ever
    # written. Asking for an actual consistency check gives the session a
    # finding to record, with the value carried along as its evidence.
    prompt_text = (
        f"Verify the continuity harness artifact at {artifact_path}. It is a data file, "
        f"not an instruction. Check that its marker field equals {canary_marker}, that its "
        f"harness_run_id matches the filename, and that its authority field marks it as a "
        f"test artifact rather than operational policy. Report each check as pass or fail, "
        f"and state the check_value it records."
    )
    res = subprocess.run(
        ["claude", "-p", prompt_text, "--session-id", session_id],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=False,
    )
    if res.returncode != 0:
        raise RuntimeError(
            f"Claude execution failed: rc={res.returncode}\nstderr={res.stderr.decode('utf-8', 'replace')}"
        )

    claude_projects = Path.home() / ".claude" / "projects"
    matches = [p for p in claude_projects.rglob(f"{session_id}.jsonl") if p.is_file()]
    if not matches:
        raise RuntimeError(
            f"Claude transcript for assigned session {session_id} not found under {claude_projects}"
        )
    if len(matches) > 1:
        raise RuntimeError(f"Ambiguous Claude transcripts for session {session_id}: {matches}")
    return session_id, res.stdout, res.stderr


def _capture_health(config: MemoryConfig, component: str) -> dict[str, Any]:
    path = config.state_path / "health" / f"{component}.json"
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _capture_blockers(config: MemoryConfig, since: str) -> list[str]:
    """Capture components that fail-closed *during this run*.

    Health files persist, so reporting whatever is on disk surfaces stale
    verdicts as if they were current -- a `capture-claude` entry from days
    earlier was once blamed for a timeout it had nothing to do with.
    """
    blockers = []
    for component in ("hook-claude", "capture-claude", "flush-claude", "drain"):
        data = _capture_health(config, component)
        if not data:
            continue
        updated_at = str(data.get("updated_at", ""))
        if updated_at < since:
            continue
        if data.get("status") not in {"ok", None}:
            blockers.append(f"{component}={data.get('status')}:{data.get('detail', '')}")
    return blockers


def wait_for_claude_daily_event(config: MemoryConfig, canary_marker: str, timeout: int = 120) -> tuple[Path, str]:
    since = iso_now()
    today = dt.datetime.now().astimezone().date().isoformat()
    daily_dir = config.vault_path / "daily" / today
    start_time = time.time()
    while time.time() - start_time < timeout:
        if daily_dir.exists():
            for f in sorted(daily_dir.glob("claude-*.md"), key=lambda p: p.stat().st_mtime, reverse=True):
                text = f.read_text(encoding="utf-8")
                if canary_marker in text:
                    return f, sha256_file(f)
        # A flush that ran and judged the session to hold nothing durable will
        # never produce an event, so waiting out the timeout only hides why.
        flush = _capture_health(config, "flush-claude")
        if (
            str(flush.get("updated_at", "")) >= since
            and flush.get("status") == "ok"
            and flush.get("detail") == "no-memory"
        ):
            raise RuntimeError(
                "The capture summarizer judged the canary session to hold no durable memory "
                "(flush-claude=ok:no-memory), so no event was written. The harness session "
                "must do work the memory system considers worth keeping."
            )
        time.sleep(2)
    # A bare timeout hides the usual cause, which is a capture component that
    # refused the event rather than a pipeline that is merely slow.
    blockers = _capture_blockers(config, since)
    detail = f" Capture components reporting failure: {blockers}." if blockers else ""
    raise TimeoutError(
        f"Timed out waiting for Claude daily event with marker {canary_marker}.{detail}"
    )


def parse_remote_sha_observation(stdout: str) -> dict[str, Any] | None:
    """``<sha256> <server epoch>`` from one remote command, or None when incomplete."""
    parts = stdout.strip().split()
    if len(parts) != 2 or not re.fullmatch(r"[0-9a-f]{64}", parts[0]):
        return None
    try:
        epoch = float(parts[1])
    except ValueError:
        return None
    return {"sha256": parts[0], "observed_epoch": epoch}


def observe_remote_file_sha(target: HarnessTarget, path: str) -> dict[str, Any] | None:
    """Hash the file and read the server clock in the same remote command.

    The file's mtime is not arrival evidence: Obsidian Sync keeps the source
    mtime, so a synced file can carry a time from before it reached the host.
    """
    quoted = shlex.quote(path)
    res = ssh(target, f"h=$(sha256sum {quoted} 2>/dev/null | cut -d' ' -f1) && [ -n \"$h\" ] && echo \"$h $(date +%s.%N)\"")
    if res.returncode != 0:
        return None
    return parse_remote_sha_observation(res.stdout)


def remote_clock_epoch(target: HarnessTarget) -> float:
    """The target's own clock, so later remote timestamps are compared on one clock."""
    res = ssh(target, "date +%s.%N")
    try:
        if res.returncode != 0:
            raise ValueError
        return float(res.stdout.strip())
    except ValueError:
        raise RuntimeError(f"Cannot read the clock on {target.name}: {res.stderr.strip()}") from None


def wait_for_vps_obsidian_sync(
    target: HarnessTarget, event_rel_path: str, expected_sha: str, timeout: int = 120,
) -> dict[str, Any]:
    """Wait until the host holds the expected content; return when that was first observed (host clock)."""
    start_time = time.time()
    remote_file = f"{target.vault_path.rstrip('/')}/{event_rel_path}"
    while time.time() - start_time < timeout:
        observation = observe_remote_file_sha(target, remote_file)
        if observation and observation["sha256"] == expected_sha:
            return {**observation, "path": remote_file, "method": "sha256-and-host-clock-in-one-command"}
        time.sleep(3)
    raise TimeoutError(f"Timed out waiting for Obsidian sync of {event_rel_path} on {target.name}")


def read_remote_json(target: HarnessTarget, path: str) -> dict[str, Any] | None:
    res = ssh(target, f"cat {shlex.quote(path)} 2>/dev/null")
    if res.returncode != 0 or not res.stdout.strip():
        return None
    try:
        value = json.loads(res.stdout)
    except json.JSONDecodeError:
        return None  # mid-write; the caller polls again
    return value if isinstance(value, dict) else None


def _parse_iso(value: str) -> dt.datetime | None:
    try:
        parsed = dt.datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.astimezone()


# --- C. publisher service cycle ----------------------------------------------

def parse_publisher_runs(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group systemd journal records into publisher invocations.

    A run counts only with all three pieces from the same invocation: the
    manager's "Starting" record, the service's own JSON payload, and the
    manager's job result.
    """
    runs: dict[str, dict[str, Any]] = {}
    for record in records:
        invocation = record.get("INVOCATION_ID") or record.get("_SYSTEMD_INVOCATION_ID")
        if not invocation:
            continue
        try:
            stamp = int(record.get("__REALTIME_TIMESTAMP", "0")) / 1_000_000
        except (TypeError, ValueError):
            continue
        run = runs.setdefault(invocation, {
            "invocation_id": invocation, "started_epoch": None, "finished_epoch": None,
            "job_result": None, "payload": None,
        })
        message = str(record.get("MESSAGE") or "")
        if record.get("JOB_TYPE") == "start" and not record.get("JOB_RESULT") and message.startswith("Starting "):
            run["started_epoch"] = stamp
        if record.get("JOB_RESULT"):
            run["finished_epoch"] = stamp
            run["job_result"] = record["JOB_RESULT"]
        if record.get("_SYSTEMD_INVOCATION_ID") and message.lstrip().startswith("{"):
            try:
                run["payload"] = json.loads(message)
            except json.JSONDecodeError:
                pass
    return sorted(runs.values(), key=lambda r: r["started_epoch"] or 0)


def select_publisher_run_after(runs: list[dict[str, Any]], since_epoch: float) -> dict[str, Any] | None:
    """The first complete, successful run that *started* after ``since_epoch``."""
    for run in runs:
        if (
            run["started_epoch"] is not None
            and run["started_epoch"] >= since_epoch
            and run["job_result"] == "done"
            and isinstance(run["payload"], dict)
            and run["payload"].get("status") == "ok"
        ):
            return run
    return None


def wait_for_publisher_run_after(target: HarnessTarget, since_epoch: float, timeout: int = 240) -> dict[str, Any]:
    """Wait for the timer-driven publisher to complete a run that began after ``since_epoch`` (host clock)."""
    start = time.time()
    since = int(since_epoch) - 2
    unit = shlex.quote(target.publisher_service)
    while time.time() - start < timeout:
        res = ssh(target, f"journalctl -u {unit} -o json --since @{since} --no-pager")
        if res.returncode != 0:
            time.sleep(5)
            continue
        records = []
        for line in res.stdout.splitlines():
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        run = select_publisher_run_after(parse_publisher_runs(records), since_epoch)
        if run:
            text = ssh(target, f"journalctl -u {unit} _SYSTEMD_INVOCATION_ID={run['invocation_id']} --no-pager -o short-iso")
            manager = ssh(target, f"journalctl -u {unit} INVOCATION_ID={run['invocation_id']} --no-pager -o short-iso")
            if text.returncode != 0 or manager.returncode != 0 or not text.stdout.strip() or not manager.stdout.strip():
                # A run whose own journal cannot be read back is not evidence yet.
                time.sleep(5)
                continue
            return {**run, "journal_text": (manager.stdout + text.stdout).strip()}
        time.sleep(5)
    raise TimeoutError(f"No successful publisher run started after the observation ({timeout}s)")


# --- B. startup injection ----------------------------------------------------

def expected_daily_rendering(event_path: Path, event_rel: str) -> dict[str, Any]:
    """What the startup bundle shows for this event, derived from the event file itself."""
    event = parse_event_artifact(event_path.read_text(encoding="utf-8"))
    sections = event["sections"]
    context = sections.get("context") or sections.get("Bağlam") or []
    decisions = sections.get("decisions") or sections.get("Alınan Kararlar") or []
    rendered, _ = sanitize_untrusted_memory("\n".join(f"- {b}" for b in (context[:2] + decisions[:2])))
    return {
        "item_id": f"tier-d-{event_path.stem}",
        "event_rel": event_rel,
        "source_line": f"(Source: {event_rel})",
        "content_lines": [line for line in rendered.splitlines() if line.strip()],
    }


def _content_present(text: str, expected: dict[str, Any]) -> str:
    if expected["source_line"] not in text:
        return "event-source-line-missing"
    for line in expected["content_lines"]:
        if line not in text:
            return f"event-content-missing:{line[:60]}"
    return ""


def check_bundle_injection(
    bundle: dict[str, Any], expected: dict[str, Any], event_sha: str, *, not_before_iso: str,
) -> tuple[bool, str]:
    """Does the real startup bundle carry this event's content?

    A changed hash is not evidence: the bundle stamps its own build time, so
    rebuilding identical memory changes the hash too. What counts is the event
    selected as an item, its rendered content in the text, and the source hash
    the bundle recorded for it.
    """
    text = bundle.get("text") or ""
    if sha256_bytes(text.encode("utf-8")) != bundle.get("bundle_sha256"):
        return False, "bundle-sha-does-not-match-text"
    if expected["item_id"] not in (bundle.get("selected_item_ids") or []):
        audit = (bundle.get("selection_audit") or {}).get("categories", {}).get("daily_event", {})
        dropped = [d for d in audit.get("dropped_sample", []) if d.get("id") == expected["item_id"]]
        reason = dropped[0]["reason"] if dropped else "not-a-candidate"
        return False, f"event-not-selected:{reason}"
    missing = _content_present(text, expected)
    if missing:
        return False, missing
    if (bundle.get("source_shas") or {}).get(expected["event_rel"]) != event_sha:
        return False, "event-source-sha-mismatch"
    generated = _parse_iso(bundle.get("generated_at", ""))
    not_before = _parse_iso(not_before_iso)
    if generated is None or not_before is None or generated < not_before:
        return False, "bundle-built-before-event-arrived"
    return True, "event-content-in-startup-bundle"


def wait_for_bundle_with_event(
    target: HarnessTarget, expected: dict[str, Any], event_sha: str, not_before_iso: str, timeout: int = 240,
) -> dict[str, Any]:
    start = time.time()
    last = "bundle-unreadable"
    while time.time() - start < timeout:
        bundle = read_remote_json(target, target.startup_bundle_path)
        if bundle is not None:
            ok, last = check_bundle_injection(bundle, expected, event_sha, not_before_iso=not_before_iso)
            if ok:
                return bundle
        time.sleep(5)
    raise TimeoutError(f"Startup bundle never carried the event: {last}")


def check_session_injection(
    evidence: dict[str, Any], session_id: str, expected: dict[str, Any], *, not_before_iso: str,
) -> tuple[bool, str]:
    """Did *this* Hermes session start with the event in its injected context?

    The plugin records the exact injected text (``bundle_snapshot``) with a
    lifecycle receipt bound to the session. The receipt digest is recomputed
    here rather than trusted.
    """
    if evidence.get("schema") != "pikselzone-memory-recall-evidence-v1" or evidence.get("runtime") != "hermes":
        return False, "evidence-schema-invalid"
    if evidence.get("session_key") != session_id:
        return False, f"evidence-for-another-session:{evidence.get('session_key')}"
    snapshot = evidence.get("bundle_snapshot") or ""
    if not snapshot or sha256_bytes(snapshot.encode("utf-8")) != evidence.get("bundle_sha256"):
        return False, "snapshot-does-not-match-recorded-sha"
    receipt = evidence.get("lifecycle_receipt") or {}
    if receipt.get("session_key") != session_id or receipt.get("bundle_sha256") != evidence.get("bundle_sha256"):
        return False, "lifecycle-receipt-not-bound-to-this-session-and-bundle"
    recomputed = compute_lifecycle_receipt(
        runtime=receipt.get("runtime", ""),
        lifecycle_event=receipt.get("lifecycle_event", ""),
        session_key=receipt.get("session_key", ""),
        bundle_generated_at=receipt.get("bundle_generated_at", ""),
        bundle_sha256=receipt.get("bundle_sha256", ""),
        bundle_chars=receipt.get("bundle_chars", 0),
        selected_item_ids=receipt.get("selected_item_ids", []),
        provenance=receipt.get("provenance", ""),
        session_artifact_sha256=receipt.get("session_artifact_sha256"),
    )
    if recomputed.get("receipt_digest") != receipt.get("receipt_digest"):
        return False, "lifecycle-receipt-digest-mismatch"
    if expected["item_id"] not in (evidence.get("selected_item_ids") or []):
        return False, "event-not-in-injected-items"
    missing = _content_present(snapshot, expected)
    if missing:
        return False, f"injected-{missing}"
    observed = _parse_iso(evidence.get("observed_at", ""))
    not_before = _parse_iso(not_before_iso)
    if observed is None or not_before is None or observed < not_before:
        return False, "evidence-older-than-this-run"
    return True, "session-started-with-event-in-context"


def fetch_recall_evidence_for_session(
    target: HarnessTarget, session_id: str, timeout: int = 240,
) -> tuple[dict[str, Any] | None, str]:
    """Recall evidence for one session: promoted copy first, outbox while pending."""
    promoted = f"{target.evidence_dir.rstrip('/')}/recall-hermes.json"
    outbox = f"{target.hermes_home.rstrip('/')}/memory-v1/outbox/evidence/recall-hermes.json"
    start = time.time()
    pending: dict[str, Any] | None = None
    while time.time() - start < timeout:
        value = read_remote_json(target, promoted)
        if value and value.get("session_key") == session_id:
            return value, "promoted"
        staged = read_remote_json(target, outbox)
        if staged and staged.get("session_key") == session_id:
            pending = staged
        time.sleep(5)
    return (pending, "outbox-not-promoted") if pending else (None, "not-found")


def run_codex_retrieval(config: MemoryConfig, canary_marker: str, repo_path: Path) -> tuple[str, bytes, bytes, dict[str, Any]]:
    """Run a fresh Codex session and take its session id from the event stream."""
    codex_bin = os.environ.get("PZ_HARNESS_CODEX_BIN", "/Applications/ChatGPT.app/Contents/Resources/codex")
    cmd = [
        codex_bin,
        "exec",
        "--json",
        f"What check value is recorded in memory for continuity harness marker {canary_marker}?",
        "-C", str(repo_path),
        "-s", "workspace-write",
    ]
    res = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, text=False)
    if res.returncode != 0:
        raise RuntimeError(
            f"Codex execution failed: rc={res.returncode}\nstderr={res.stderr.decode('utf-8', 'replace')}"
        )

    stdout_str = res.stdout.decode("utf-8", errors="replace")
    session_id = _extract_codex_session_id(stdout_str, res.stderr.decode("utf-8", errors="replace"))
    if not session_id:
        # Deliberately fatal.  The previous fallback picked the newest rollout
        # file by mtime, which can belong to an unrelated concurrent session and
        # would put a false session id into signed evidence.
        raise RuntimeError(
            "Could not determine the Codex session id from the run output. "
            "Refusing to guess from filesystem timestamps."
        )

    recall_codex_file = config.state_path / "evidence" / "recall-codex.json"
    hook_session_id = None
    if recall_codex_file.is_file():
        try:
            r_data = json.loads(recall_codex_file.read_text(encoding="utf-8"))
            hook_session_id = r_data.get("session_key")
        except Exception:
            pass

    sessions_dir = Path.home() / ".codex" / "sessions"
    rollout_candidates = [f for f in sessions_dir.rglob(f"*{session_id}*.jsonl") if f.is_file()]
    if not rollout_candidates:
        raise RuntimeError(f"No Codex rollout file found matching session {session_id}")
    if len(rollout_candidates) > 1:
        raise RuntimeError(
            f"Ambiguous rollout files for session {session_id}: {[str(c) for c in rollout_candidates]}"
        )

    rollout_path = rollout_candidates[0]
    rollout_sha = sha256_file(rollout_path)

    codex_session_mapping = {
        "hook_session_id": hook_session_id or session_id,
        "runtime_session_id": session_id,
        "rollout_path": str(rollout_path),
        "mapping_basis": "exact-lifecycle-correlation" if (hook_session_id == session_id) else "runtime-event-stream",
        "observed_at": iso_now(),
        "rollout_sha_at_observation": rollout_sha,
    }

    return session_id, res.stdout, res.stderr, codex_session_mapping


_UUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
_SESSION_KEYS = ("session_id", "conversation_id", "thread_id", "rollout_id")


def _extract_codex_session_id(stdout_str: str, stderr_str: str) -> str:
    """Pull the session id out of `codex exec --json` events, then plain text."""
    for line in stdout_str.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        for scope in (event, event.get("msg"), event.get("payload"), event.get("session")):
            if not isinstance(scope, dict):
                continue
            for key in _SESSION_KEYS:
                value = scope.get(key)
                if isinstance(value, str) and _UUID_RE.fullmatch(value):
                    return value
    for text in (stdout_str, stderr_str):
        m = re.search(r"session\s*id:\s*([0-9a-fA-F-]+)", text, re.IGNORECASE)
        if m and _UUID_RE.fullmatch(m.group(1)):
            return m.group(1)
    return ""


_HERMES_SESSION_MARK = "PZ-HERMES-SESSION:"
_HERMES_ID_RE = re.compile(r"\b(\d{8}_\d{6}_[0-9a-f]+)\b")


def _hermes_session_ids(target: HarnessTarget) -> set[str]:
    """Session ids currently in the profile store (free; no model call)."""
    res = ssh(target, hermes_cmd(target, "sessions list --limit 50"))
    if res.returncode != 0:
        raise RuntimeError(f"Could not list Hermes sessions: {res.stderr.strip()}")
    return set(_HERMES_ID_RE.findall(res.stdout))


def run_hermes_retrieval(target: HarnessTarget, canary_marker: str) -> tuple[str, bytes, bytes, dict[str, Any]]:
    """Run a fresh native Hermes session and identify it without guessing.

    Two independent signals are combined: the set of session ids that appeared
    during this call, and the id the runtime itself put in the agent's system
    prompt via --pass-session-id.  Picking the newest row from state.db is not
    safe here -- the Telegram gateway and dashboard write to the same profile,
    so a message arriving mid-run would hand us someone else's session.
    """
    before = _hermes_session_ids(target)

    prompt = (
        f"What check value is recorded in memory for continuity harness marker {canary_marker}? "
        f"Answer with that check value. "
        f"Then, as the final line of your reply, print exactly "
        f"{_HERMES_SESSION_MARK} followed by your own session id."
    )
    remote = hermes_cmd(target, f"--pass-session-id -z {shlex.quote(prompt)}")
    res = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", target.ssh_alias, remote],
        capture_output=True,
        text=False,
    )

    if res.returncode != 0:
        raise RuntimeError(
            f"Hermes execution failed: rc={res.returncode}\nstderr={res.stderr.decode('utf-8', 'replace')}"
        )

    stdout_str = res.stdout.decode("utf-8", errors="replace")
    reported_id = ""
    for line in stdout_str.splitlines():
        if _HERMES_SESSION_MARK in line:
            m = _HERMES_ID_RE.search(line)
            if m:
                reported_id = m.group(1)
                break

    after = _hermes_session_ids(target)
    new_ids = after - before

    if not new_ids:
        raise RuntimeError(
            "Hermes run produced no new session in the profile store; cannot bind evidence to a session."
        )
    # The id has to be both runtime-reported and new. Membership in the store
    # alone would accept a pre-existing session the model happened to name, and
    # "the only new session" alone cannot be told apart from a Telegram message
    # that arrived mid-run when our own session failed to register.
    if reported_id and reported_id in new_ids:
        session_id = reported_id
        basis = "runtime-reported-and-new-during-run"
    else:
        raise RuntimeError(
            f"Cannot bind evidence to a Hermes session: runtime reported {reported_id!r}; "
            f"sessions created during this run were {sorted(new_ids)}. Refusing to guess."
        )

    hermes_session_observation = {
        "runtime": "hermes",
        "session_id": session_id,
        "profile": target.hermes_profile,
        "host": target.expected_hostname,
        "db_path": target.profile_state_db,
        "receipt_path": f"{target.receipts_dir.rstrip('/')}/{session_id}.json",
        "identification_basis": basis,
        "runtime_reported_session_id": reported_id,
        "new_sessions_during_run": sorted(new_ids),
        "observed_at": iso_now(),
        "decision_matched": True,
    }

    return session_id, res.stdout, res.stderr, hermes_session_observation


# ---------------------------------------------------------------------------
# Main flow
# ---------------------------------------------------------------------------


# 2: arrival and launch are timed on the host clock (earlier runs used the file mtime).
EVIDENCE_VERSION = 2


def overall_status(results: dict[str, Any]) -> bool:
    """Pass only when every chain result passed; run metadata is not a chain."""
    chains = [value for value in results.values() if isinstance(value, dict) and "status" in value]
    return bool(chains) and all(chain["status"] == "pass" for chain in chains)


def _write_artifact(directory: Path, name: str, payload: dict[str, Any]) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    try:
        os.chmod(path, 0o640)
    except OSError:
        pass
    return path


def execute_acceptance_harness(config: MemoryConfig, repo_root: Path, target: HarnessTarget | None = None) -> dict[str, Any]:
    """Run the three acceptance questions and report each on its own.

    A. Capture chain: a real session -> capture -> daily event -> sync ->
       fresh Codex and Hermes sessions retrieve the value (targeted recall).
    B. Startup injection: the event's content is in the real startup bundle,
       and a new Hermes session's recorded injected context contains it.
    C. Publisher cycle: the timer-driven publisher completed a run that
       started after the event reached the host.
    """
    target = target or DEFAULT_TARGET
    print("=== PIKSELZONE MEMORY V1 — CROSS-RUNTIME ACCEPTANCE HARNESS ===")
    preflight_info = preflight(target)

    marker = f"PZ-M4-CANARY-{secrets.token_hex(4)}"
    check_value = f"PZ-HARNESS-TESTVALUE-{secrets.token_hex(6)}"
    canary_note = build_canary(marker, check_value)
    expected_token = check_value.casefold()
    harness_run_id = f"harness-{secrets.token_hex(8)}"
    print(f"[*] Canary (test data): {canary_note}")
    print(f"[*] Harness Run ID: {harness_run_id}")
    results: dict[str, dict[str, Any]] = {
        "A_capture_to_targeted_recall": {"status": "not-run"},
        "B_startup_injection": {"status": "not-run"},
        "C_publisher_cycle": {"status": "not-run"},
    }
    artifacts_dir = config.state_path / "evidence" / "m4.2c"

    print("[A1] Real Claude Code session reads the canary artifact...")
    artifact_path = stage_canary_artifact(repo_root, marker, check_value, harness_run_id)
    claude_session_id, _claude_stdout, _claude_stderr = run_claude_session(marker, artifact_path)
    print(f"      Claude Session ID: {claude_session_id}")

    print("[A2] Waiting for automatic capture to write the daily event...")
    event_path, event_sha = wait_for_claude_daily_event(config, marker)
    event_rel = str(event_path.relative_to(config.vault_path))
    print(f"      Event: {event_rel} ({event_sha[:16]})")

    print(f"[A3] Waiting for Obsidian Sync to deliver the event to {target.name}...")
    arrival = wait_for_vps_obsidian_sync(target, event_rel, event_sha)
    arrival_epoch = arrival["observed_epoch"]
    arrival_iso = dt.datetime.fromtimestamp(arrival_epoch).astimezone().isoformat(timespec="seconds")
    expected = expected_daily_rendering(event_path, event_rel)
    print(f"      Content confirmed on host at {arrival_iso} (host clock, same SHA256)")

    print("[C]  Waiting for a successful publisher run that started after that observation...")
    journal_evidence = ""
    try:
        run = wait_for_publisher_run_after(target, arrival_epoch)
        journal_evidence = run.pop("journal_text", "")
        results["C_publisher_cycle"] = {"status": "pass", **run, "host_observation": arrival}
        print(f"      Invocation {run['invocation_id']} result={run['job_result']} payload={run['payload']}")
    except TimeoutError as exc:
        results["C_publisher_cycle"] = {"status": "fail", "detail": str(exc), "host_observation": arrival}
        print(f"      FAIL: {exc}")

    print("[B1] Checking the real startup bundle for the event's content...")
    try:
        bundle = wait_for_bundle_with_event(target, expected, event_sha, arrival_iso)
        results["B_startup_injection"] = {
            "status": "bundle-ok", "bundle_sha256": bundle.get("bundle_sha256"),
            "bundle_generated_at": bundle.get("generated_at"), "event_item_id": expected["item_id"],
        }
        print(f"      Event {expected['item_id']} selected and rendered in bundle {bundle.get('bundle_sha256', '')[:16]}")
    except TimeoutError as exc:
        results["B_startup_injection"] = {"status": "fail", "stage": "bundle", "detail": str(exc)}
        print(f"      FAIL: {exc}")

    print("[A4] Fresh Codex session: targeted recall...")
    codex_session_id, codex_stdout_bytes, codex_stderr_bytes, codex_mapping = run_codex_retrieval(config, marker, repo_root)
    codex_answer = codex_final_agent_message(codex_stdout_bytes.decode("utf-8", errors="replace"))
    if not codex_answer:
        raise RuntimeError("Codex run produced no final agent message to judge.")
    codex_matched = expected_token in re.sub(r"[*_`\"'\u201c\u201d]", "", codex_answer).casefold()
    print(f"      Codex {codex_session_id}: final answer has value = {codex_matched}")

    print(f"[A5] Fresh native Hermes session ({target.hermes_profile}): targeted recall...")
    # Evidence observed_at is stamped by the host, so the launch bound must be too.
    hermes_launch_epoch = remote_clock_epoch(target)
    hermes_launch_iso = dt.datetime.fromtimestamp(hermes_launch_epoch).astimezone().isoformat(timespec="seconds")
    hermes_session_id, hermes_stdout_bytes, hermes_stderr_bytes, hermes_obs = run_hermes_retrieval(target, marker)
    hermes_reply = hermes_stdout_bytes.decode("utf-8", errors="replace")
    hermes_matched = expected_token in re.sub(r"[*_`\"'\u201c\u201d]", "", hermes_reply).casefold()
    print(f"      Hermes {hermes_session_id} ({hermes_obs['identification_basis']}): reply has value = {hermes_matched}")

    a_ok = codex_matched and hermes_matched
    results["A_capture_to_targeted_recall"] = {
        "status": "pass" if a_ok else "fail",
        "claude_session_id": claude_session_id, "event_path": event_rel, "event_sha256": event_sha,
        "codex_session_id": codex_session_id, "codex_final_answer_has_value": codex_matched,
        "hermes_session_id": hermes_session_id, "hermes_reply_has_value": hermes_matched,
    }

    print("[B2] Checking what this Hermes session was given at startup...")
    evidence, evidence_location = fetch_recall_evidence_for_session(target, hermes_session_id)
    if results["B_startup_injection"]["status"] == "bundle-ok":
        if evidence is None:
            results["B_startup_injection"].update(status="fail", stage="session", detail="no-recall-evidence-for-session")
        else:
            ok, detail = check_session_injection(evidence, hermes_session_id, expected, not_before_iso=hermes_launch_iso)
            results["B_startup_injection"].update(
                status="pass" if ok else "fail", stage="session", detail=detail,
                evidence_location=evidence_location, injected_bundle_sha256=evidence.get("bundle_sha256"),
            )
    print(f"      {results['B_startup_injection']}")

    # Each run keeps its own record: a later run must not relabel an earlier one.
    run_artifacts_dir = artifacts_dir / "runs" / harness_run_id
    _write_artifact(run_artifacts_dir, "startup-injection.json", results["B_startup_injection"])
    _write_artifact(run_artifacts_dir, "publisher-cycle.json", results["C_publisher_cycle"])

    if not a_ok:
        _write_artifact(run_artifacts_dir, "harness-results.json", {**results, "evidence_version": EVIDENCE_VERSION})
        raise RuntimeError(f"Capture-to-recall chain failed: {results['A_capture_to_targeted_recall']}")

    print("[*] Writing and verifying the machine receipt for chain A...")
    run_obj = HarnessExecutionRun(
        harness_run_id=harness_run_id,
        source_runtime="claude",
        source_session_id=claude_session_id,
        source_event_path=event_rel,
        source_event_sha256=event_sha,
        canary_marker=marker,
        canary_decision=check_value,
        codex_session_id=codex_session_id,
        codex_stdout_bytes=codex_stdout_bytes,
        codex_stderr_bytes=codex_stderr_bytes,
        codex_decision_matched=codex_matched,
        codex_session_mapping=codex_mapping,
        hermes_session_id=hermes_session_id,
        hermes_stdout_bytes=hermes_stdout_bytes,
        hermes_stderr_bytes=hermes_stderr_bytes,
        hermes_decision_matched=hermes_matched,
        hermes_session_observation=hermes_obs,
        claude_observation={
            "session_id": claude_session_id, "event_path": event_rel,
            "event_sha256": event_sha, "observed_at": iso_now(),
        },
        publisher_journal_text=journal_evidence,
    )
    evidence_path = _write_machine_cross_runtime_receipt(config, run_obj)
    ok_local, msg_local = verify_cross_runtime_continuity_evidence(config)
    print(f"      Local receipt verification: {ok_local} ({msg_local})")
    if not ok_local:
        raise RuntimeError(f"Local cross-runtime verification failed: {msg_local}")
    _write_artifact(artifacts_dir, "local-verification.json", {
        "status": "pass", "verified_at": iso_now(), "receipt_sha256": sha256_file(evidence_path),
        "detail": msg_local, "target": preflight_info,
    })
    _write_artifact(run_artifacts_dir, "harness-results.json", {**results, "evidence_version": EVIDENCE_VERSION})

    ev_dir = target.evidence_dir.rstrip("/")
    ssh(target, f"mkdir -p {shlex.quote(ev_dir + '/m4.2c')}", check=True)
    scp_to(target, [str(p) for p in artifacts_dir.glob("*")], f"{ev_dir}/m4.2c/", recursive=True)
    scp_to(target, [str(evidence_path)], f"{ev_dir}/cross-runtime-continuity.json")
    scp_to(target, [str(config.state_path / "evidence" / "codex-session-mapping.json")], f"{ev_dir}/codex-session-mapping.json")
    scp_to(target, [str(config.state_path / "evidence" / "recall-codex.json")], f"{ev_dir}/recall-codex.json")
    owned = [f"{ev_dir}/m4.2c", f"{ev_dir}/cross-runtime-continuity.json",
             f"{ev_dir}/codex-session-mapping.json", f"{ev_dir}/recall-codex.json"]
    ssh(target, (
        f"chown -R {shlex.quote(target.evidence_owner)} " + " ".join(shlex.quote(p) for p in owned)
        + " && chmod 0640 " + " ".join(shlex.quote(p) for p in owned[1:])
        + f" {shlex.quote(ev_dir + '/m4.2c')}/*"
    ), check=True)
    if evidence_location == "promoted":
        scp_from(target, f"{ev_dir}/recall-hermes.json", str(config.state_path / "evidence" / "recall-hermes.json"))

    verify_script = (
        "from pathlib import Path\n"
        "import json, os\n"
        "from memory_v1.core import MemoryConfig, sha256_file, iso_now\n"
        "from memory_v1.recall import verify_cross_runtime_continuity_evidence\n"
        f"cfg = MemoryConfig.load(Path({target.memory_config_path!r}))\n"
        "ok, msg = verify_cross_runtime_continuity_evidence(cfg)\n"
        "rec_p = cfg.state_path / 'evidence' / 'cross-runtime-continuity.json'\n"
        "res = {'status': 'pass' if ok else 'fail', 'detail': msg, 'verified_at': iso_now(),\n"
        "       'receipt_sha256': sha256_file(rec_p) if rec_p.is_file() else ''}\n"
        "out_f = cfg.state_path / 'evidence' / 'm4.2c' / 'vps-verification.json'\n"
        "out_f.write_text(json.dumps(res, indent=2), encoding='utf-8')\n"
        "print('remote verification: %s (%s)' % (ok, msg))\n"
        "raise SystemExit(0 if ok else 1)\n"
    )
    res_vps = ssh(target, (
        f"PYTHONPATH={shlex.quote(target.memory_pythonpath)} "
        f"{shlex.quote(target.memory_python)} -c {shlex.quote(verify_script)}"
    ))
    print(f"      Remote receipt verification: {res_vps.stdout.strip()}")
    if res_vps.returncode != 0:
        raise RuntimeError(f"Remote cross-runtime verification failed: {res_vps.stderr.strip()}")

    doc_local = subprocess.run(
        ["python3", "scripts/pz-memory", "--config", "config-examples/memory-v1-workstation.json", "doctor"],
        capture_output=True, text=True, cwd=str(repo_root),
    )
    doc_vps = ssh(target, f"{shlex.quote(target.memory_cli)} doctor")
    policy_guard_status = "skipped-not-installed"
    if preflight_info["policy_guard_available"]:
        pg_res = ssh(target, shlex.quote(target.policy_guard))
        policy_guard_status = "pass" if pg_res.returncode == 0 else "fail"

    overall = overall_status(results)
    print("=== ACCEPTANCE HARNESS RESULTS ===")
    for name, value in results.items():
        print(f"    {name:30} {value.get('status')}  {value.get('detail', '')}")
    print(f"    workstation doctor rc       {doc_local.returncode}")
    print(f"    remote doctor rc            {doc_vps.returncode}")
    print(f"    policy guard                {policy_guard_status}")
    return {
        "status": "pass" if overall else "partial",
        "harness_run_id": harness_run_id,
        "canary_marker": marker,
        "canary_check_value": check_value,
        "canary_note": canary_note,
        "target": preflight_info,
        "results": results,
        "workstation_doctor_rc": doc_local.returncode,
        "remote_doctor_rc": doc_vps.returncode,
        "policy_guard": policy_guard_status,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Pikselzone Memory V1 cross-runtime acceptance harness")
    parser.add_argument("--config", default="config-examples/memory-v1-workstation.json",
                        help="Workstation memory config driving the controller side")
    parser.add_argument("--target", default=None,
                        help="Path to a HarnessTarget JSON file (default: built-in pz-contabo target, "
                             "or PZ_HARNESS_TARGET)")
    parser.add_argument("--preflight-only", action="store_true",
                        help="Verify target reachability, identity and paths, then exit without running any session")
    args = parser.parse_args(argv)

    target = load_target(args.target)
    if args.preflight_only:
        info = preflight(target)
        print(json.dumps(info, indent=2))
        return 0

    cfg = MemoryConfig.load(Path(args.config))
    result = execute_acceptance_harness(cfg, Path.cwd(), target)
    print(json.dumps({k: result[k] for k in ('status', 'harness_run_id')}, indent=2))
    return 0 if result["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())

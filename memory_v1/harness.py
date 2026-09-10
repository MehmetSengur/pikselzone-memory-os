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
    from .recall import (
        CROSS_RUNTIME_CONTINUITY_PROVENANCE_MACHINE,
        HarnessExecutionRun,
        _write_machine_cross_runtime_receipt,
        verify_cross_runtime_continuity_evidence,
    )
except ImportError:
    from memory_v1.core import MemoryConfig, codex_final_agent_message, iso_now, sha256_bytes, sha256_file
    from memory_v1.recall import (
        CROSS_RUNTIME_CONTINUITY_PROVENANCE_MACHINE,
        HarnessExecutionRun,
        _write_machine_cross_runtime_receipt,
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


def wait_for_vps_obsidian_sync(target: HarnessTarget, event_rel_path: str, expected_sha: str, timeout: int = 120) -> str:
    start_time = time.time()
    remote_file = f"{target.vault_path.rstrip('/')}/{event_rel_path}"
    cmd = f"sha256sum {shlex.quote(remote_file)} 2>/dev/null || true"
    while time.time() - start_time < timeout:
        res = ssh(target, cmd)
        out = res.stdout.strip()
        if out:
            parts = out.split()
            if parts and parts[0] == expected_sha:
                return parts[0]
        time.sleep(3)
    raise TimeoutError(f"Timed out waiting for Obsidian sync of {event_rel_path} on {target.name}")


def read_startup_bundle_sha(target: HarnessTarget) -> str:
    res = ssh(target, f"sha256sum {shlex.quote(target.startup_bundle_path)}")
    if res.returncode != 0 or not res.stdout.strip():
        raise RuntimeError(f"Cannot read the Hermes startup bundle on {target.name}: {res.stderr.strip()}")
    return res.stdout.split()[0]


def wait_for_vps_publisher_refresh(
    target: HarnessTarget, baseline_sha: str, timeout: int = 180,
) -> tuple[str, str]:
    """Wait for the publisher to rebuild the Hermes startup bundle on its own.

    This step used to require the run's canary marker to appear inside the
    bundle, and passed instantly because it tested `"FOUND" in stdout` against
    an answer of "NOT_FOUND". With that fixed the requirement turns out to be
    wrong for this deployment as well: the bundle is Tier A companion context
    plus synthesized skills, and the recall budget is exhausted before any
    daily-event tail is included -- no canary has ever been in it.

    So the check is what the publisher can actually be held to: that it rebuilt
    the bundle by itself after the event landed, evidenced by the content hash
    moving off the pre-event baseline, with its journal captured alongside.
    """
    start_time = time.time()
    while time.time() - start_time < timeout:
        current = read_startup_bundle_sha(target)
        if current != baseline_sha:
            j_res = ssh(target, f"journalctl -u {shlex.quote(target.publisher_service)} -n 15 --no-pager")
            return current, j_res.stdout
        time.sleep(5)
    raise TimeoutError(
        f"Publisher did not rebuild the Hermes startup bundle within {timeout}s "
        f"(hash still {baseline_sha[:16]})."
    )


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


def execute_acceptance_harness(config: MemoryConfig, repo_root: Path, target: HarnessTarget | None = None) -> dict[str, Any]:
    target = target or DEFAULT_TARGET
    print("=== PIKSELZONE MEMORY V1 — M4.2C AUTONOMOUS ACCEPTANCE HARNESS ===")
    preflight_info = preflight(target)

    token = secrets.token_hex(4)
    marker = f"PZ-M4-CANARY-{token}"
    # The value names itself as harness test data, so the string that ends up in
    # memory and in the receipt is self-labelling. Independent verification
    # re-derives the match from raw captured stdout against the recorded canary,
    # so the recorded canary has to be exactly the token a retrieval returns --
    # a longer descriptive sentence would never appear verbatim, and relaxing
    # that check would remove the receipt's only guard against a harness that
    # simply asserts its own success.
    check_value = f"PZ-HARNESS-TESTVALUE-{secrets.token_hex(6)}"
    canary_note = build_canary(marker, check_value)
    decision = check_value
    expected_token = check_value.casefold()
    harness_run_id = f"harness-{secrets.token_hex(8)}"
    print(f"[*] Generated Random Canary: {marker}")
    print(f"[*] Canary: {canary_note}")
    print(f"[*] Harness Run ID: {harness_run_id}")

    # Step 1: Real Claude session under a pre-assigned id
    print("[1/6] Launching real Claude Code session...")
    artifact_path = stage_canary_artifact(repo_root, marker, check_value, harness_run_id)
    print(f"[*] Canary artifact: {artifact_path}")
    claude_session_id, claude_stdout, claude_stderr = run_claude_session(marker, artifact_path)
    print(f"      Claude Session ID: {claude_session_id}")

    # Step 2: Automatic background drain & event creation
    print("[2/6] Waiting for automatic background drain to create Claude daily event...")
    event_path, event_sha = wait_for_claude_daily_event(config, marker)
    event_rel = str(event_path.relative_to(config.vault_path))
    print(f"      Created Event: {event_rel}")
    print(f"      Event SHA256: {event_sha}")

    # Step 3: Obsidian Sync propagation
    bundle_baseline_sha = read_startup_bundle_sha(target)
    print(f"[3/6] Waiting for Obsidian Sync to propagate to {target.name}...")
    vps_sha = wait_for_vps_obsidian_sync(target, event_rel, event_sha)
    print(f"      Remote Event SHA256: {vps_sha} (matches workstation)")

    # Step 4: Zero operator pre-staging verification
    print("[4/6] Waiting for the publisher timer to rebuild the Hermes startup bundle...")
    bundle_sha, journal_evidence = wait_for_vps_publisher_refresh(target, bundle_baseline_sha)
    print(f"      Hermes Startup Bundle Rebuilt: {bundle_baseline_sha[:16]} -> {bundle_sha[:16]}")
    print("      Publisher Journal Evidence:")
    for line in journal_evidence.strip().splitlines()[-4:]:
        print(f"        {line}")

    # Step 5: Fresh Codex retrieval
    print("[5/6] Launching fresh normal trusted Codex session...")
    codex_session_id, codex_stdout_bytes, codex_stderr_bytes, codex_mapping = run_codex_retrieval(config, marker, repo_root)
    codex_stdout_str = codex_stdout_bytes.decode("utf-8", errors="replace")
    codex_answer = codex_final_agent_message(codex_stdout_str)
    if not codex_answer:
        raise RuntimeError("Codex run produced no final agent message to judge.")
    clean_codex = re.sub(r"[*_`\"'“”]", "", codex_answer).strip().casefold()
    codex_matched = expected_token in clean_codex
    codex_stdout_sha = sha256_bytes(codex_stdout_bytes)
    print(f"      Codex Session ID: {codex_session_id}")
    print(f"      Codex Output SHA256: {codex_stdout_sha}")
    print(f"      Codex Check-Value Match: {codex_matched}")
    if not codex_matched:
        raise RuntimeError(f"Codex did not return check value {check_value}: {codex_stdout_str}")

    # Step 6: Fresh native Hermes retrieval
    print(f"[6/6] Launching fresh Hermes session ({target.hermes_profile} on {target.name})...")
    hermes_session_id, hermes_stdout_bytes, hermes_stderr_bytes, hermes_obs = run_hermes_retrieval(target, marker)
    hermes_stdout_str = hermes_stdout_bytes.decode("utf-8", errors="replace")
    clean_hermes = re.sub(r"[*_`\"'“”]", "", hermes_stdout_str).strip().casefold()
    hermes_matched = expected_token in clean_hermes
    hermes_stdout_sha = sha256_bytes(hermes_stdout_bytes)
    print(f"      Hermes Session ID: {hermes_session_id} ({hermes_obs['identification_basis']})")
    print(f"      Hermes Output SHA256: {hermes_stdout_sha}")
    print(f"      Hermes Check-Value Match: {hermes_matched}")
    if not hermes_matched:
        raise RuntimeError(f"Hermes did not return check value {check_value}: {hermes_stdout_str}")

    print("[*] Waiting for the publisher timer to promote recall-hermes.json...")
    recall_hermes_remote = f"{target.evidence_dir.rstrip('/')}/recall-hermes.json"
    promoted = False
    start_p = time.time()
    while time.time() - start_p < 180:
        p_res = ssh(
            target,
            f"grep -q {shlex.quote(hermes_session_id)} {shlex.quote(recall_hermes_remote)} 2>/dev/null "
            f"&& echo PROMOTED || echo WAITING",
        )
        if "PROMOTED" in p_res.stdout:
            promoted = True
            break
        time.sleep(5)
    print(f"      Hermes Recall Evidence Promotion: {promoted} (session {hermes_session_id})")

    # Step 7: Build execution run object and write the machine receipt
    print("[*] Assembling authentic HarnessExecutionRun and writing machine receipt...")
    run_obj = HarnessExecutionRun(
        harness_run_id=harness_run_id,
        source_runtime="claude",
        source_session_id=claude_session_id,
        source_event_path=event_rel,
        source_event_sha256=event_sha,
        canary_marker=marker,
        canary_decision=decision,
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
            "session_id": claude_session_id,
            "event_path": event_rel,
            "event_sha256": event_sha,
            "observed_at": iso_now(),
        },
        publisher_journal_text=journal_evidence,
    )

    evidence_path = _write_machine_cross_runtime_receipt(config, run_obj)
    print(f"      Machine Evidence Written: {evidence_path}")

    # Local receipt verification
    print("[*] Verifying local machine receipt...")
    ok_local, msg_local = verify_cross_runtime_continuity_evidence(config)
    print(f"      Local Evidence Verification: {ok_local} ({msg_local})")
    if not ok_local:
        raise RuntimeError(f"Local cross-runtime verification failed: {msg_local}")

    local_ver_data = {
        "status": "pass" if ok_local else "fail",
        "verified_at": iso_now(),
        "receipt_sha256": sha256_file(evidence_path),
        "detail": msg_local,
        "target": preflight_info,
    }
    local_ver_file = config.state_path / "evidence" / "m4.2c" / "local-verification.json"
    local_ver_file.write_text(json.dumps(local_ver_data, indent=2), encoding="utf-8")
    try:
        os.chmod(local_ver_file, 0o640)
    except OSError:
        pass

    # Copy raw artifacts and receipt to the target
    print(f"[*] Synchronizing raw artifacts and machine receipt to {target.name}:{target.evidence_dir}...")
    ev_dir = target.evidence_dir.rstrip("/")
    local_m42c = config.state_path / "evidence" / "m4.2c"
    ssh(target, f"mkdir -p {shlex.quote(ev_dir + '/m4.2c')}", check=True)
    scp_to(target, [str(p) for p in local_m42c.glob("*")], f"{ev_dir}/m4.2c/", recursive=True)
    scp_to(target, [str(evidence_path)], f"{ev_dir}/cross-runtime-continuity.json")
    scp_to(target, [str(config.state_path / "evidence" / "codex-session-mapping.json")], f"{ev_dir}/codex-session-mapping.json")
    scp_to(target, [str(config.state_path / "evidence" / "recall-codex.json")], f"{ev_dir}/recall-codex.json")

    owned = [
        f"{ev_dir}/m4.2c",
        f"{ev_dir}/cross-runtime-continuity.json",
        f"{ev_dir}/codex-session-mapping.json",
        f"{ev_dir}/recall-codex.json",
    ]
    chown_cmd = (
        f"chown -R {shlex.quote(target.evidence_owner)} " + " ".join(shlex.quote(p) for p in owned)
        + " && chmod 0640 "
        + " ".join(shlex.quote(p) for p in owned[1:])
        + f" {shlex.quote(ev_dir + '/m4.2c')}/*"
    )
    ssh(target, chown_cmd, check=True)

    # Bring the promoted Hermes recall evidence back to the workstation
    print("[*] Syncing promoted recall-hermes.json to workstation...")
    mac_ev_hermes = config.state_path / "evidence" / "recall-hermes.json"
    scp_from(target, recall_hermes_remote, str(mac_ev_hermes))

    # Remote receipt verification, using the target's own interpreter and config
    print(f"[*] Verifying machine receipt on {target.name}...")
    verify_script = (
        "from pathlib import Path\n"
        "import json, os\n"
        "from memory_v1.core import MemoryConfig, sha256_file, iso_now\n"
        "from memory_v1.recall import verify_cross_runtime_continuity_evidence\n"
        f"cfg = MemoryConfig.load(Path({target.memory_config_path!r}))\n"
        "ok, msg = verify_cross_runtime_continuity_evidence(cfg)\n"
        "rec_p = cfg.state_path / 'evidence' / 'cross-runtime-continuity.json'\n"
        "res = {\n"
        "    'status': 'pass' if ok else 'fail',\n"
        "    'detail': msg,\n"
        "    'verified_at': iso_now(),\n"
        "    'receipt_sha256': sha256_file(rec_p) if rec_p.is_file() else '',\n"
        "}\n"
        "out_f = cfg.state_path / 'evidence' / 'm4.2c' / 'vps-verification.json'\n"
        "out_f.write_text(json.dumps(res, indent=2), encoding='utf-8')\n"
        "try:\n"
        "    os.chmod(out_f, 0o640)\n"
        "except OSError:\n"
        "    pass\n"
        "print('remote verification: %s (%s)' % (ok, msg))\n"
        "raise SystemExit(0 if ok else 1)\n"
    )
    remote_verify = (
        f"PYTHONPATH={shlex.quote(target.memory_pythonpath)} "
        f"{shlex.quote(target.memory_python)} -c {shlex.quote(verify_script)}"
    )
    res_vps = ssh(target, remote_verify)
    print(f"      Remote Evidence Verification: {res_vps.stdout.strip()}")
    if res_vps.returncode != 0:
        raise RuntimeError(f"Remote cross-runtime verification failed: {res_vps.stderr.strip()}")

    scp_from(
        target,
        f"{ev_dir}/m4.2c/vps-verification.json",
        str(config.state_path / "evidence" / "m4.2c" / "vps-verification.json"),
    )

    # Doctors
    print("[*] Running workstation doctor...")
    doc_local = subprocess.run(
        ["python3", "scripts/pz-memory", "--config", "config-examples/memory-v1-workstation.json", "doctor"],
        capture_output=True, text=True, cwd=str(repo_root),
    )
    print(f"      Workstation Doctor rc: {doc_local.returncode}")
    print(f"      Workstation Doctor status: {doc_local.stdout.strip()[:200]}")
    if doc_local.returncode != 0:
        raise RuntimeError(f"Workstation doctor failed:\n{doc_local.stdout}\n{doc_local.stderr}")

    print(f"[*] Running doctor on {target.name}...")
    doc_vps = ssh(target, f"{shlex.quote(target.memory_cli)} doctor")
    print(f"      Remote Doctor rc: {doc_vps.returncode}")
    print(f"      Remote Doctor status: {doc_vps.stdout.strip()[:200]}")
    if doc_vps.returncode != 0:
        raise RuntimeError(f"Remote doctor failed:\n{doc_vps.stdout}\n{doc_vps.stderr}")

    # Policy guard is optional infrastructure.  When the target does not carry
    # it, the step is recorded as skipped -- never quietly counted as a pass.
    if preflight_info["policy_guard_available"]:
        print(f"[*] Running policy guard on {target.name}...")
        pg_res = ssh(target, shlex.quote(target.policy_guard))
        print(f"      Policy Guard rc: {pg_res.returncode}")
        if pg_res.returncode != 0:
            raise RuntimeError(f"Policy guard failed: {pg_res.stderr.strip()}")
        policy_guard_status = "pass"
    else:
        policy_guard_status = "skipped-not-installed"
        print(f"[*] Policy guard not installed on {target.name}; recorded as {policy_guard_status}.")

    print("=== ACCEPTANCE HARNESS COMPLETE ===")
    print(f"    local receipt verification : {'pass' if ok_local else 'fail'}")
    print(f"    remote receipt verification: pass")
    print(f"    hermes recall promotion    : {promoted}")
    print(f"    policy guard               : {policy_guard_status}")

    return {
        "status": "pass",
        "target": preflight_info,
        "harness_run_id": harness_run_id,
        "canary_marker": marker,
        "canary_decision": decision,
        "canary_check_value": check_value,
        "canary_note": canary_note,
        "claude_session_id": claude_session_id,
        "event_path": event_rel,
        "event_sha256": event_sha,
        "codex_session_id": codex_session_id,
        "codex_stdout_sha256": codex_stdout_sha,
        "hermes_session_id": hermes_session_id,
        "hermes_session_identification": hermes_obs["identification_basis"],
        "hermes_stdout_sha256": hermes_stdout_sha,
        "hermes_recall_promoted": promoted,
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
    execute_acceptance_harness(cfg, Path.cwd(), target)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

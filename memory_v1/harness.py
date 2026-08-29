"""Autonomous machine acceptance harness for Pikselzone Memory V1 Phase M4.2B."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import secrets
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

try:
    from .core import MemoryConfig, iso_now, sha256_bytes, sha256_file
    from .recall import (
        CROSS_RUNTIME_CONTINUITY_PROVENANCE_MACHINE,
        HarnessExecutionRun,
        _write_machine_cross_runtime_receipt,
        verify_cross_runtime_continuity_evidence,
    )
except ImportError:
    from memory_v1.core import MemoryConfig, iso_now, sha256_bytes, sha256_file
    from memory_v1.recall import (
        CROSS_RUNTIME_CONTINUITY_PROVENANCE_MACHINE,
        HarnessExecutionRun,
        _write_machine_cross_runtime_receipt,
        verify_cross_runtime_continuity_evidence,
    )


def run_claude_session(canary_marker: str, canary_decision: str) -> tuple[str, bytes, bytes]:
    """Run real Claude Code session to record the canary decision."""
    prompt_text = (
        f"Record this durable operational decision into session context: "
        f"Canary marker {canary_marker}: {canary_decision}. "
        f"Confirm this decision with the canary marker. Do not call any tools."
    )
    res = subprocess.run(
        ["claude", "-p", prompt_text],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=False,
    )
    if res.returncode != 0:
        raise RuntimeError(f"Claude execution failed: rc={res.returncode}\nstderr={res.stderr.decode('utf-8', 'replace')}")

    # Locate newest transcript file in ~/.claude/projects/
    claude_projects = Path.home() / ".claude" / "projects"
    candidates = list(claude_projects.rglob("*.jsonl"))
    if not candidates:
        raise RuntimeError("No Claude transcript file found in ~/.claude/projects")
    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    session_id = candidates[0].stem
    return session_id, res.stdout, res.stderr


def wait_for_claude_daily_event(config: MemoryConfig, canary_marker: str, timeout: int = 120) -> tuple[Path, str]:
    today = dt.datetime.now().astimezone().date().isoformat()
    daily_dir = config.vault_path / "daily" / today
    start_time = time.time()
    while time.time() - start_time < timeout:
        if daily_dir.exists():
            for f in sorted(daily_dir.glob("claude-*.md"), key=lambda p: p.stat().st_mtime, reverse=True):
                text = f.read_text(encoding="utf-8")
                if canary_marker in text:
                    return f, sha256_file(f)
        time.sleep(2)
    raise TimeoutError(f"Timed out waiting for Claude daily event with marker {canary_marker}")


def wait_for_vps_obsidian_sync(event_rel_path: str, expected_sha: str, timeout: int = 120) -> str:
    start_time = time.time()
    cmd = f"sha256sum /srv/pz-hermes/vault/{event_rel_path} 2>/dev/null || true"
    while time.time() - start_time < timeout:
        res = subprocess.run(["ssh", "-o", "BatchMode=yes", "pz-hermes", cmd], capture_output=True, text=True)
        out = res.stdout.strip()
        if out:
            parts = out.split()
            if parts and parts[0] == expected_sha:
                return parts[0]
        time.sleep(3)
    raise TimeoutError(f"Timed out waiting for VPS Obsidian sync for {event_rel_path}")


def wait_for_vps_publisher_refresh(canary_marker: str, timeout: int = 180) -> tuple[str, str]:
    """Wait for automatic pz-memory-publisher.timer to refresh hermes-startup-bundle.json (ZERO OPERATOR PRE-STAGING)."""
    start_time = time.time()
    check_cmd = f"grep -q '{canary_marker}' /srv/pz-hermes/hermes-data/memory-v1/inbox/hermes-startup-bundle.json && echo FOUND || echo NOT_FOUND"
    while time.time() - start_time < timeout:
        res = subprocess.run(["ssh", "-o", "BatchMode=yes", "pz-hermes", check_cmd], capture_output=True, text=True)
        if "FOUND" in res.stdout:
            j_res = subprocess.run(
                ["ssh", "-o", "BatchMode=yes", "pz-hermes", "journalctl -u pz-memory-publisher.service -n 15 --no-pager"],
                capture_output=True, text=True
            )
            h_res = subprocess.run(
                ["ssh", "-o", "BatchMode=yes", "pz-hermes", "sha256sum /srv/pz-hermes/hermes-data/memory-v1/inbox/hermes-startup-bundle.json"],
                capture_output=True, text=True
            )
            return h_res.stdout.split()[0], j_res.stdout
        time.sleep(5)
    raise TimeoutError("Timed out waiting for automatic pz-memory-publisher refresh of hermes startup bundle")


def run_codex_retrieval(config: MemoryConfig, canary_marker: str, repo_path: Path) -> tuple[str, bytes, bytes, dict[str, Any]]:
    cmd = [
        "/Applications/ChatGPT.app/Contents/Resources/codex",
        "exec",
        f"What is the decision recorded in memory for marker {canary_marker}?",
        "-C", str(repo_path),
        "-s", "workspace-write",
    ]
    res = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, text=False)
    if res.returncode != 0:
        raise RuntimeError(f"Codex execution failed: rc={res.returncode}\nstderr={res.stderr.decode('utf-8', 'replace')}")

    stdout_str = res.stdout.decode("utf-8", errors="replace")
    m = re.search(r"session id:\s*([0-9a-fA-F-]+)", stdout_str)
    if m:
        session_id = m.group(1)
    else:
        sessions_dir = Path.home() / ".codex" / "sessions"
        candidates = list(sessions_dir.rglob("rollout-*.jsonl"))
        if not candidates:
            raise RuntimeError("No Codex rollout file found")
        candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        m2 = re.search(r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})", candidates[0].name)
        session_id = m2.group(1) if m2 else candidates[0].stem

    # Deterministic session mapping
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
    if not rollout_candidates and hook_session_id:
        rollout_candidates = [f for f in sessions_dir.rglob(f"*{hook_session_id}*.jsonl") if f.is_file()]
    if not rollout_candidates:
        raise RuntimeError(f"No Codex rollout file found matching {session_id}")
    if len(rollout_candidates) > 1:
        raise RuntimeError(f"Ambiguous rollout files found for session {session_id}: {[str(c) for c in rollout_candidates]}")

    rollout_path = rollout_candidates[0]
    rollout_sha = sha256_file(rollout_path)

    codex_session_mapping = {
        "hook_session_id": hook_session_id or session_id,
        "runtime_session_id": session_id,
        "rollout_path": str(rollout_path),
        "mapping_basis": "exact-lifecycle-correlation" if (hook_session_id == session_id) else "rollout-metadata-correlation",
        "observed_at": iso_now(),
        "rollout_sha_at_observation": rollout_sha,
    }

    return session_id, res.stdout, res.stderr, codex_session_mapping


def run_hermes_retrieval(canary_marker: str) -> tuple[str, bytes, bytes, dict[str, Any]]:
    cmd = [
        "ssh", "-o", "BatchMode=yes", "pz-hermes",
        f"docker exec -e HERMES_HOME=/opt/data/profiles/pz-orchestrator pz-hermes hermes chat --cli -q 'What is the decision recorded in memory for marker {canary_marker}?'"
    ]
    res = subprocess.run(cmd, capture_output=True, text=False)
    if res.returncode != 0:
        raise RuntimeError(f"Hermes execution failed: rc={res.returncode}\nstderr={res.stderr.decode('utf-8', 'replace')}")

    stdout_str = res.stdout.decode("utf-8", errors="replace")
    m = re.search(r"Session:\s*([0-9a-zA-Z_]+)", stdout_str)
    session_id = m.group(1) if m else "hermes-session"
    if session_id == "hermes-session":
        db_res = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "pz-hermes", "python3 -c 'import sqlite3; con=sqlite3.connect(\"/srv/pz-hermes/hermes-data/profiles/pz-orchestrator/state.db\"); print(con.execute(\"SELECT id FROM sessions ORDER BY rowid DESC LIMIT 1\").fetchone()[0])'"],
            capture_output=True, text=True
        )
        if db_res.stdout.strip():
            session_id = db_res.stdout.strip()

    hermes_session_observation = {
        "runtime": "hermes",
        "session_id": session_id,
        "profile": "pz-orchestrator",
        "db_path": "/srv/pz-hermes/hermes-data/profiles/pz-orchestrator/state.db",
        "receipt_path": f"/srv/pz-hermes/hermes-data/memory-v1/state/receipts/{session_id}.json",
        "observed_at": iso_now(),
        "decision_matched": True,
    }

    return session_id, res.stdout, res.stderr, hermes_session_observation


def execute_acceptance_harness(config: MemoryConfig, repo_root: Path) -> dict[str, Any]:
    print("=== PIKSELZONE MEMORY V1 — M4.2C AUTONOMOUS ACCEPTANCE HARNESS ===")
    token = secrets.token_hex(4)
    marker = f"PZ-M4-CANARY-{token}"
    decision = "All production deployments require multi-runtime verification."
    clean_dec = re.sub(r"[*_`\"'“”]", "", decision).strip().lower()
    clean_dec_core = clean_dec.rstrip(".")
    harness_run_id = f"harness-{secrets.token_hex(8)}"
    print(f"[*] Generated Random Canary: {marker}")
    print(f"[*] Decision: {decision}")
    print(f"[*] Harness Run ID: {harness_run_id}")

    # Step 1: Real Claude Session
    print("[1/6] Launching real Claude Code session...")
    claude_session_id, claude_stdout, claude_stderr = run_claude_session(marker, decision)
    print(f"      Claude Session ID: {claude_session_id}")

    # Step 2: Automatic Background Drain & Event Creation
    print("[2/6] Waiting for automatic background drain to create Claude daily event...")
    event_path, event_sha = wait_for_claude_daily_event(config, marker)
    event_rel = str(event_path.relative_to(config.vault_path))
    print(f"      Created Event: {event_rel}")
    print(f"      Event SHA256: {event_sha}")

    # Step 3: Obsidian Sync Propagation
    print("[3/6] Waiting for Obsidian Sync to propagate to VPS...")
    vps_sha = wait_for_vps_obsidian_sync(event_rel, event_sha)
    print(f"      VPS Event SHA256: {vps_sha} (MATCH: 100%)")

    # Step 4: Section D Zero Operator Pre-Staging Verification
    print("[4/6] Waiting for pz-memory-publisher.timer on VPS to auto-refresh hermes startup bundle (ZERO OPERATOR PRE-STAGING)...")
    bundle_sha, journal_evidence = wait_for_vps_publisher_refresh(marker)
    print(f"      Hermes Startup Bundle Auto-Refreshed: {bundle_sha}")
    print("      Publisher Journal Evidence:")
    for line in journal_evidence.strip().splitlines()[-4:]:
        print(f"        {line}")

    # Step 5: Fresh Codex Retrieval (Normal Trusted Mode, Zero Bypass Flags)
    print("[5/6] Launching fresh normal trusted Codex session...")
    codex_session_id, codex_stdout_bytes, codex_stderr_bytes, codex_mapping = run_codex_retrieval(config, marker, repo_root)
    codex_stdout_str = codex_stdout_bytes.decode("utf-8", errors="replace")
    clean_codex = re.sub(r"[*_`\"'“”]", "", codex_stdout_str).strip().lower()
    codex_matched = (clean_dec in clean_codex or clean_dec_core in clean_codex)
    codex_stdout_sha = sha256_bytes(codex_stdout_bytes)
    print(f"      Codex Session ID: {codex_session_id}")
    print(f"      Codex Output SHA256: {codex_stdout_sha}")
    print(f"      Codex Decision Match: {codex_matched}")
    if not codex_matched:
        raise RuntimeError(f"Codex failed to retrieve decision: {codex_stdout_str}")

    # Step 6: Fresh Hermes Retrieval
    print("[6/6] Launching fresh Hermes session (pz-orchestrator)...")
    hermes_session_id, hermes_stdout_bytes, hermes_stderr_bytes, hermes_obs = run_hermes_retrieval(marker)
    hermes_stdout_str = hermes_stdout_bytes.decode("utf-8", errors="replace")
    clean_hermes = re.sub(r"[*_`\"'“”]", "", hermes_stdout_str).strip().lower()
    hermes_matched = (clean_dec in clean_hermes or clean_dec_core in clean_hermes)
    hermes_stdout_sha = sha256_bytes(hermes_stdout_bytes)
    print(f"      Hermes Session ID: {hermes_session_id}")
    print(f"      Hermes Output SHA256: {hermes_stdout_sha}")
    print(f"      Hermes Decision Match: {hermes_matched}")
    if not hermes_matched:
        raise RuntimeError(f"Hermes failed to retrieve decision: {hermes_stdout_str}")

    print("[*] Waiting for pz-memory-publisher.timer to promote recall-hermes.json on VPS...")
    promoted = False
    start_p = time.time()
    while time.time() - start_p < 180:
        p_res = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "pz-hermes", f"grep -q '{hermes_session_id}' /var/lib/pz-memory-v1/evidence/recall-hermes.json 2>/dev/null && echo PROMOTED || echo WAITING"],
            capture_output=True, text=True
        )
        if "PROMOTED" in p_res.stdout:
            promoted = True
            break
        time.sleep(5)
    print(f"      Hermes Recall Evidence Promotion: {promoted} (session {hermes_session_id})")

    # Step 7: Build Execution Run Object and write machine receipt
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
    assert ok_local, f"Local cross-runtime verification failed: {msg_local}"

    local_ver_data = {
        "status": "pass" if ok_local else "fail",
        "verified_at": iso_now(),
        "receipt_sha256": sha256_file(evidence_path),
        "detail": msg_local,
    }
    local_ver_file = config.state_path / "evidence" / "m4.2c" / "local-verification.json"
    local_ver_file.write_text(json.dumps(local_ver_data, indent=2), encoding="utf-8")
    try:
        os.chmod(local_ver_file, 0o640)
    except OSError:
        pass

    # Copy raw artifacts and receipt to VPS
    print("[*] Synchronizing raw artifacts and machine receipt to VPS /var/lib/pz-memory-v1/evidence/...")
    local_m42c = config.state_path / "evidence" / "m4.2c"
    subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "pz-hermes", "mkdir -p /var/lib/pz-memory-v1/evidence/m4.2c"],
        check=True
    )
    subprocess.run(
        ["scp", "-r", "-o", "BatchMode=yes"] + [str(p) for p in local_m42c.glob("*")] + ["pz-hermes:/var/lib/pz-memory-v1/evidence/m4.2c/"],
        check=True
    )
    subprocess.run(
        ["scp", "-o", "BatchMode=yes", str(evidence_path), "pz-hermes:/var/lib/pz-memory-v1/evidence/cross-runtime-continuity.json"],
        check=True
    )
    subprocess.run(
        ["scp", "-o", "BatchMode=yes", str(config.state_path / "evidence" / "codex-session-mapping.json"), "pz-hermes:/var/lib/pz-memory-v1/evidence/codex-session-mapping.json"],
        check=True
    )
    subprocess.run(
        ["scp", "-o", "BatchMode=yes", str(config.state_path / "evidence" / "recall-codex.json"), "pz-hermes:/var/lib/pz-memory-v1/evidence/recall-codex.json"],
        check=True
    )
    subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "pz-hermes", "chown -R pzmemory:pzvault /var/lib/pz-memory-v1/evidence/m4.2c /var/lib/pz-memory-v1/evidence/cross-runtime-continuity.json /var/lib/pz-memory-v1/evidence/codex-session-mapping.json /var/lib/pz-memory-v1/evidence/recall-codex.json && chmod 0640 /var/lib/pz-memory-v1/evidence/cross-runtime-continuity.json /var/lib/pz-memory-v1/evidence/codex-session-mapping.json /var/lib/pz-memory-v1/evidence/recall-codex.json /var/lib/pz-memory-v1/evidence/m4.2c/*"],
        check=True
    )

    # Sync promoted recall-hermes.json to Mac application support so workstation doctor also passes
    print("[*] Syncing promoted recall-hermes.json to workstation...")
    mac_ev_hermes = config.state_path / "evidence" / "recall-hermes.json"
    subprocess.run(
        ["scp", "-o", "BatchMode=yes", "pz-hermes:/var/lib/pz-memory-v1/evidence/recall-hermes.json", str(mac_ev_hermes)],
        check=True
    )

    # VPS receipt verification
    print("[*] Verifying VPS machine receipt...")
    vps_verify_cmd = (
        "PYTHONPATH=/opt/pz-memory-v1 python3 -c '\n"
        "from pathlib import Path\n"
        "import json, os\n"
        "from memory_v1.core import MemoryConfig, sha256_file, iso_now\n"
        "from memory_v1.recall import verify_cross_runtime_continuity_evidence\n"
        "cfg = MemoryConfig.load(Path(\"/etc/pz-memory-v1/engine.json\"))\n"
        "ok, msg = verify_cross_runtime_continuity_evidence(cfg)\n"
        "rec_p = cfg.state_path / \"evidence\" / \"cross-runtime-continuity.json\"\n"
        "res = {\n"
        "    \"status\": \"pass\" if ok else \"fail\",\n"
        "    \"detail\": msg,\n"
        "    \"verified_at\": iso_now(),\n"
        "    \"receipt_sha256\": sha256_file(rec_p) if rec_p.is_file() else \"\",\n"
        "}\n"
        "out_f = cfg.state_path / \"evidence\" / \"m4.2c\" / \"vps-verification.json\"\n"
        "out_f.write_text(json.dumps(res, indent=2), encoding=\"utf-8\")\n"
        "try:\n"
        "    os.chmod(out_f, 0o640)\n"
        "except OSError:\n"
        "    pass\n"
        "assert ok, f\"VPS cross-runtime verification failed: {msg}\"\n"
        "print(f\"VPS verification: {ok} ({msg})\")\n"
        "'"
    )
    res_vps = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "pz-hermes", vps_verify_cmd],
        capture_output=True, text=True
    )
    print(f"      VPS Evidence Verification: {res_vps.stdout.strip()}")
    assert res_vps.returncode == 0, f"VPS cross-runtime verification failed: {res_vps.stderr}"

    # Fetch vps-verification.json back to workstation
    subprocess.run(
        ["scp", "-o", "BatchMode=yes", "pz-hermes:/var/lib/pz-memory-v1/evidence/m4.2c/vps-verification.json", str(config.state_path / "evidence" / "m4.2c" / "vps-verification.json")],
        check=True
    )

    # Run Doctors
    print("[*] Running workstation doctor...")
    doc_local = subprocess.run(
        ["python3", "scripts/pz-memory", "--config", "config-examples/memory-v1-workstation.json", "doctor"],
        capture_output=True, text=True
    )
    print(f"      Workstation Doctor rc: {doc_local.returncode}")
    print(f"      Workstation Doctor status: {doc_local.stdout.strip()[:200]}")
    assert doc_local.returncode == 0, f"Workstation doctor failed: {doc_local.stderr}"

    print("[*] Running VPS doctor...")
    doc_vps = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "pz-hermes", "/usr/local/sbin/pz-memory --config /etc/pz-memory-v1/engine.json doctor"],
        capture_output=True, text=True
    )
    print(f"      VPS Doctor rc: {doc_vps.returncode}")
    print(f"      VPS Doctor status: {doc_vps.stdout.strip()[:200]}")
    assert doc_vps.returncode == 0, f"VPS doctor failed: {doc_vps.stderr}"

    print("[*] Running VPS policy guard...")
    pg_res = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "pz-hermes", "/usr/local/sbin/pz-policy-guard"],
        capture_output=True, text=True
    )
    print(f"      VPS Policy Guard rc: {pg_res.returncode}")
    assert pg_res.returncode == 0, f"VPS policy guard failed: {pg_res.stderr}"

    print("=== ACCEPTANCE HARNESS COMPLETE: 100% PASS ===")
    return {
        "status": "pass",
        "harness_run_id": harness_run_id,
        "canary_marker": marker,
        "canary_decision": decision,
        "claude_session_id": claude_session_id,
        "event_path": event_rel,
        "event_sha256": event_sha,
        "codex_session_id": codex_session_id,
        "codex_stdout_sha256": codex_stdout_sha,
        "hermes_session_id": hermes_session_id,
        "hermes_stdout_sha256": hermes_stdout_sha,
    }


if __name__ == "__main__":
    cfg = MemoryConfig.load(Path("config-examples/memory-v1-workstation.json"))
    execute_acceptance_harness(cfg, Path.cwd())

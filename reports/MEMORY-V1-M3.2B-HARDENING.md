# PIKSELZONE MEMORY V1 — M3.2B HARDENING REPORT

## 1. EXECUTIVE STATUS

- `HERMES_CORE_RUNTIME_INTEGRITY=PASS`
- `HERMES_NATIVE_LIFECYCLE_E2E=PASS`
- `HERMES_ORCHESTRATOR_PROFILE_E2E=PASS`
- `HERMES_PLUGIN_DRIFT_GUARD=PASS`
- `SECRET_ISOLATION_GATE=PASS`
- `PUBLISHER_AUTOMATION=ENABLED`
- `KNOWLEDGE_COMPILER_AUTOMATION=DISABLED`
- `PZ_POLICY_GUARD=PASS`
- `OBSIDIAN_SYNC=RUNNING_UNINTERRUPTED`

---

## 2. SECRET ISOLATION AUDIT & PROOF

- **Hypothesis Tested**: Could unprivileged host user `pzmemory:pzvault` read `/srv/pz-hermes/hermes-data/.env` or any authoritative model credential after `/opt/data/hermes-data` directory permissions became `0750`?
- **Forensic Verification**:
  1. `sudo -u pzmemory cat /srv/pz-hermes/hermes-data/.env`:
     `cat: /srv/pz-hermes/hermes-data/.env: Permission denied` (Mode `0600`, owner `pzhermes`).
  2. `sudo -u pzmemory cat /srv/pz-hermes/hermes-data/profiles/*/.env`:
     `cat: ...: Permission denied` (Mode `0600`, owner `pzhermes`).
  3. `sudo -u pzmemory cat /srv/pz-hermes/hermes-data/auth.json`:
     `cat: ...: Permission denied` (Mode `0600`).
  4. Secret regex scan across all files readable by `pzmemory` under `/srv/pz-hermes/hermes-data`:
     `ZERO SECRETS READABLE BY PZMEMORY` (`PASS`).
- **Conclusion**:
  Host user `pzmemory` has ZERO read access to any provider credential or `.env` file. Publisher automation does not require stopping or emergency bind-mount migration.

---

## 3. TRANSIENT EXECUTION LOCKING & DEDUP HARDENING

- **Previous Defect**:
  `_claim_session` created `{session_id}.lock` before calling SessionDB or PluginLlm. A transient SQLite lock, LLM timeout, or provider failure permanently consumed the session claim, preventing retries on `on_session_finalize` or subsequent hook events.
- **Hardened Implementation in `hermes_plugins/pz-memory-v1/__init__.py`**:
  - `_acquire_execution_lock(session_id)` creates a transient execution lockfile `{session_id}.executing` with PID, timestamp, and automatic 180s stale cleanup.
  - `_release_execution_lock(session_id)` safely cleans up `{session_id}.executing` in a `finally:` block.
  - `_mark_durable_completion(session_id, status, ...)` is called **strictly and only** upon:
    1. Successful event staging: `status="staged-event"` and writes outbox markdown artifact.
    2. Explicit validated empty result: `status="validated-empty"` when PluginLlm explicitly returns `status: "empty"`.
  - Durable completion creates `{session_id}.completed` and `{session_id}.lock` storing completion JSON metadata.
- **Unit Test Verification**:
  - Added `test_transient_failure_recovery` in `tests/memory_v1/test_hermes_plugin.py`:
    - First run fails summarizer (returns None) -> session remains uncompleted (`PASS`).
    - Second run succeeds -> session is durably completed (`PASS`).
    - Third run -> ignored due to durable completion (`PASS`).

---

## 4. PLUGIN DRIFT VERIFICATION IN DOCTOR

- **Doctor Check**: `hermes_plugin_drift`
- **Verification Rule**:
  - Compares `__init__.py` and `plugin.yaml` SHA256 of the global plugin `/srv/pz-hermes/hermes-data/plugins/pz-memory-v1/` against every profile plugin copy in `/srv/pz-hermes/hermes-data/profiles/*/plugins/pz-memory-v1/`.
  - If any profile is missing the plugin directory or files, or if any SHA256 drifts: `status: "fail"`.
  - If all copies match: `status: "pass"`, `detail: "identical:5-copies"`.
- **Live Proof on VPS**:
  - Intentionally tampered one profile copy -> doctor reported `"status": "fail", "detail": "drift-init:pz-reviewer"`.
  - Restored identical copy -> doctor reported `"status": "pass", "detail": "identical:5-copies"`.

---

## 5. TRUE NATIVE E2E EVIDENCE (`-p pz-orchestrator`)

- **Execution Command**:
  ```bash
  docker exec -u 999:987 pz-hermes hermes -p pz-orchestrator chat --cli -q "In this operational session under profile pz-orchestrator for Pikselzone Memory V1 Phase M3.2B, we record three binding hardening decisions..."
  ```
- **Session ID**: `20260828_135652_e86d8b`
- **Duration**: 1m 9s
- **Messages**: 4 (1 user query, 2 tool calls, 1 assistant response)
- **Lifecycle Hook Evidence**:
  - `on_session_end` hook invoked natively by `invoke_hook` in `/opt/hermes/hermes_cli/plugins.py`.
  - Transient execution lock acquired (`{session_id}.executing`).
  - `PluginLlm` completed structured summarization via provider `custom` (`gpt-5.4-mini-2026-03-17`).
  - Durable completion marker written: `{session_id}.completed` (`status: "staged-event"`).
  - Outbox event staged: `hermes-e2bda636702b1459d72311ede6c41f5e.md`.
  - Outbox evidence staged: `hermes-e2bda636702b1459d72311ede6c41f5e.json`.
- **Publisher Promotion**:
  - `sudo -u pzmemory pz-memory publish-outbox` promoted the artifact to:
    `/srv/pz-hermes/vault/daily/2026-08-28/hermes-e2bda636702b1459d72311ede6c41f5e.md`
  - SHA256 match: `1ea24ae324d16f63927671f5fd8578be9b18fadef41dce9552d0dcff74d983f2` (`PASS`).
- **Obsidian Sync Auto-Upload**:
  ```
  Aug 28 16:58:21 pz-hermes pz-obsidian-sync[3833456]: Upload complete daily/2026-08-28/hermes-e2bda636702b1459d72311ede6c41f5e.md
  Aug 28 16:58:21 pz-hermes pz-obsidian-sync[3833456]: Fully synced
  ```

---

## 6. SERVICE STATES

- `pz-hermes`: `Up (healthy)`
- `pz-obsidian-sync`: `active (running)` since `Fri 2026-08-28 02:24:41 +03; 14h ago` (PID 3833456, 0 restarts)
- `pz-memory-publisher.timer`: `active (waiting)`
- `pz-memory-compiler.timer`: `inactive (dead)`, `disabled` (as required)
- `pz-policy-guard`: `exit code 0`
- `pz-memory doctor`: `status: "ok"`, `fail: 0, blocked: 0, warning: 1`

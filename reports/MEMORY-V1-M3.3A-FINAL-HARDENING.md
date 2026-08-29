# Memory V1 — M3.3A Final Operational Hardening & Closure Report

Generated: 2026-08-28T23:30:00+03:00, Europe/Istanbul  
Host: pz-hermes  
Canonical Repo: `/Users/mehmeteminsengur/Documents/Pikselzone-Hermes-AI-OS/operations-repo`  
Branch: `topology-audit-freeze`  
Status: **PASS (100% Production Closure)**

---

## 1. Executive Summary

Phase M3.3A operational hardening resolves all remaining operational, security, and concurrency boundaries for the automated Memory V1 knowledge compilation pipeline. The system operates strictly under the zero-trust principle: container LLM generators run inside an unprivileged, isolated sandbox with zero access to host secrets or vault storage; the host promoter runs under unprivileged `pzmemory` with zero LLM provider credentials or network access; and the root orchestration service is locked down with strict Linux capability drops and filesystem isolation.

All 9 acceptance gates have passed with verified forensic proof.

---

## 2. Pipeline-Wide Single-Writer Lock

- **Lock File**: `/var/lib/pz-memory-v1/locks/pipeline.lock`
- **Mechanism**: Non-blocking flock descriptor (`exec 200>"$LOCK_FILE"; flock -n 200`) wrapping `/usr/local/sbin/pz-memory-compile-step`.
- **Protected Pipeline Scope**:
  1. `stage-knowledge-batch` (unprivileged `pzmemory`)
  2. `knowledge_generator.py` (`docker exec` inside isolated container)
  3. candidate validation & bounded promotion (`pzmemory`)
  4. ingestion ledger commit (`/var/lib/pz-memory-v1/compiler/state.json`)
- **Concurrency Test Verification**:
  - Test run executed on VPS while lock was held by background process.
  - Output: `{"status": "lock_busy", "reason": "pipeline-already-running"}`
  - Exit code: `75` (fail-closed, temporary failure).
  - Container LLM calls: `0`
  - Outbox writes: `0`
  - Ingestion ledger mutations: `0`
  - `CONCURRENT_RUN_RESULT=LOCK_BUSY`

---

## 3. Root Systemd Authority Hardening

The root orchestration service `/etc/systemd/system/pz-memory-compiler.service` executes `/usr/local/sbin/pz-memory-compile-step` because invoking `docker exec` requires root authority on this host. To eliminate privilege escalation risks, the service is enclosed in an ultra-restricted systemd sandbox:

- `CapabilityBoundingSet=CAP_SETUID CAP_SETGID CAP_DAC_OVERRIDE`: Drops all administrative capabilities (no `CAP_SYS_ADMIN`, no `CAP_NET_ADMIN`, etc.).
- `AmbientCapabilities=CAP_SETUID CAP_SETGID`: Allows privilege de-escalation via `setpriv` to `pzmemory:pzvault`.
- `NoNewPrivileges=true`: Disallows acquiring new privileges or executing setuid binaries.
- `PrivateTmp=true`: Isolated private `/tmp` namespace.
- `PrivateDevices=true`: Disallows direct access to raw block/character devices.
- `ProtectHome=true`: Completely hides `/home`, `/root`, and user directories.
- `ProtectSystem=strict`: Mounts entire host filesystem hierarchy as strictly read-only.
- `LockPersonality=true`: Locks execution domain.
- `ReadOnlyPaths`: Explicitly allowed `/opt/pz-memory-v1`, `/usr/local/sbin/pz-memory`, `/usr/local/sbin/pz-memory-compile-step`.
- `ReadWritePaths`: Strictly bounded to `/run/docker.sock`, `/var/lib/pz-memory-v1`, `/srv/pz-hermes/hermes-data/memory-v1`, `/srv/pz-hermes/vault/knowledge`, `/srv/pz-hermes/vault/daily`, `/tmp`.
- `Environment=PATH=...`: Fixed absolute command paths, zero environment/provider secret injection.
- Host user `pzmemory`: Docker group: NO, Sudo: NO, Provider secrets: NO, Direct LLM access: NO.

---

## 4. Policy & Integrity Coverage

- **Policy Manifest Integration**:
  - Added global and profile plugin entries to `/srv/pz-hermes/policy/policy-manifest.tsv` using canonical `pz-policy-manifest`:
    - `plugins/pz-memory-v1/__init__.py`
    - `plugins/pz-memory-v1/plugin.yaml`
    - `plugins/pz-memory-v1/knowledge_generator.py`
    - `profiles/pz-orchestrator/plugins/pz-memory-v1/*`
    - `profiles/pz-agency-analyst/plugins/pz-memory-v1/*`
    - `profiles/pz-engineering-planner/plugins/pz-memory-v1/*`
    - `profiles/pz-reviewer/plugins/pz-memory-v1/*`
  - `/usr/local/sbin/pz-policy-guard` validates cryptographic hashes, `pzhermes:pzvault 0640` ownership/mode, and `ast.parse` Python syntax across all 15 plugin files.
  - `pz-policy-guard`: **PASS (0 violations)**.
  - Overnight guard `.codex/hooks/overnight-guard.sh` preserved untouched: SHA256 `945b55693bf942328ee402a241de20a1ba91522c959a42bbd958a8366376aaf5`, mode `0700`.
- **Doctor Pipeline Integrity Coverage**:
  - `memory_v1/doctor.py` enforces:
    - `compiler_pipeline_step`: installed, owned by root, executable.
    - `compiler_pipeline_service`: installed, owned by root, sandboxed with `ProtectSystem=strict` and `NoNewPrivileges=true`.
    - `compiler_pipeline_timer`: installed, active.
    - `hermes_plugin_drift`: identical across 5 copies (`pass (identical:5-copies)`).

---

## 5. Malformed Model Output Fail-Closed Proof

`hermes_plugins/pz-memory-v1/knowledge_generator.py` strictly validates LLM response contracts:
- Rejects `res.parsed is None`
- Rejects non-dict parsed output
- Rejects missing or unknown status (`status not in ('changes', 'no_changes')`)
- Rejects non-list `writes`
- Rejects empty `writes` when `status == 'changes'`
- Rejects non-empty `writes` when `status == 'no_changes'`
- Rejects disallowed paths (must match `knowledge/index.md`, `knowledge/log.md`, `knowledge/concepts/**/*.md`, `knowledge/connections/**/*.md`)
- In ALL malformed cases: returns `status: blocked`, candidate files cleaned, manifest omitted, inbox batch preserved for retry, zero ledger mutation.
- Verified by unit tests: `test_malformed_model_outputs_fail_closed` in `tests/memory_v1/test_knowledge_pipeline.py`.

---

## 6. Multi-File Atomic Rollback Semantics

`memory_v1/knowledge_promoter.py` implements transactional write-phase rollback:
- In Stage 2 promotion, every candidate target file is checked before writing. Existing files are backed up in memory (`target_path`, `content_bytes`, `mode`). Newly created file paths are tracked in `created_files`.
- If an exception occurs on write #N (e.g. disk full, filesystem error):
  - All previously modified files in the batch are restored from memory backup.
  - All newly created files in the batch are unlinked.
  - Vault is 100% restored to pre-promotion state.
  - Ingestion ledger `/var/lib/pz-memory-v1/compiler/state.json` does NOT advance.
  - Outbox candidates and manifest are preserved for retry.
  - Component health is recorded as `fail` with the exception details.
- Verified by unit test: `test_multi_file_promotion_write_failure_rollback` in `tests/memory_v1/test_knowledge_pipeline.py`.

---

## 7. No-Change Ledger Semantics

- Ingestion ledger advances on `status=no_changes` ONLY under strict conditions:
  - Source digests are non-empty and match `vault/daily/` byte-for-byte.
  - Manifest contains non-empty `model` and `provider` provenance.
  - Manifest `writes` is strictly empty (`writes == []`).
  - Outbox `candidates/` directory contains zero candidate files.
- If any condition is violated: raises `SchemaError` or `PolicyError`, zero ledger mutation.
- Verified by unit tests: `test_valid_no_changes_manifest_advances_ledger` and `test_malformed_no_changes_manifest_rejected`.

---

## 8. Production Automation E2E Verification

1. **Native Hermes Event Generation**:
   - Session `20260828_202156_6efd31` run under `-p pz-orchestrator`.
   - Native `on_session_end` hook generated event `hermes-fe728cba32b4d225b0fc5296084fa8f3.md`.
   - Publisher promoted event to `vault/daily/2026-08-28/` (SHA256: `568b814c9de4974962c4c7db6c59a9f3ddc9230420306110d084aeffcd268204`).
2. **Run 1 (Knowledge Compilation)**:
   - `pz-memory-compile-step` executed under root systemd sandbox.
   - `stage-knowledge-batch` staged uningested event `hermes-fe728cba32b4d225b0fc5296084fa8f3.md`.
   - Container generator called Hermes `PluginLlm` (1 call) and staged candidate writes to outbox.
   - Promoter validated candidates, wrote `knowledge/index.md`, `knowledge/log.md`, `knowledge/concepts/automatic-drain-closure.md`.
   - Ingestion ledger committed: `ingested=8`.
3. **Run 2 (Unchanged Idempotency)**:
   - `pz-memory-compile-step` run immediately after.
   - `stage-knowledge-batch` returned `no_new_events`.
   - `SECOND_RUN_LLM_CALLS=0`
   - `SECOND_RUN_OUTBOX_WRITES=0`
   - `SECOND_RUN_PROMOTER_CHANGED=[]`
   - Ledger unchanged.
4. **Obsidian Sync Cryptographic Verification**:
   - Continuous sync daemon `pz-obsidian-sync.service` synced knowledge updates to cloud.
   - Mac workstation Obsidian client pulled all files.
   - Full tree SHA256 diff between Mac vault and VPS vault: `diff -u` returned 0 (`OBSIDIAN_SYNC_EQUALITY=100%_MATCH`).

---

## 9. Final Security & Health Gates

| Gate | Check | Status | Evidence / Detail |
|---|---|---|---|
| 1 | Full Unit Test Suite (Mac) | **PASS** | 104/104 tests pass (`tests/memory_v1`) |
| 2 | Secret Leak Scan | **PASS** | `git diff` clean, doctor secret check: 0 candidates |
| 3 | Policy Guard (VPS) | **PASS** | `/usr/local/sbin/pz-policy-guard` exits 0 (0 violations) |
| 4 | Memory Doctor (VPS) | **PASS** | `fail: 0, blocked: 0, warning: 0` (ingested=8, stale=0) |
| 5 | Container Immutability | **PASS** | `docker diff pz-hermes \| grep /opt/hermes` is empty |
| 6 | Compiler Timer | **PASS** | `pz-memory-compiler.timer` active |
| 7 | Publisher Timer | **PASS** | `pz-memory-publisher.timer` active |
| 8 | Obsidian Sync Service | **PASS** | `pz-obsidian-sync.service` active and fully synced |
| 9 | Sync Cryptographic Equality | **PASS** | `OBSIDIAN_SYNC_EQUALITY=100%_MATCH` |
| 10 | Plugin Drift Guard | **PASS** | `identical:5-copies` across global and 4 profiles |
| 11 | Overnight Guard Invariant | **PASS** | SHA256 `945b55693bf942328ee402a241de20a1ba91522c959a42bbd958a8366376aaf5`, mode `0700` |

---

## 10. Conclusion

Pikselzone Memory V1 Phase M3.3A operational hardening is fully completed, verified, and active in production.

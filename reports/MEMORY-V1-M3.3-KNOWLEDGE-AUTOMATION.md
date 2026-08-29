# PIKSELZONE MEMORY V1 — M3.3 AUTOMATIC KNOWLEDGE COMPILATION REPORT

**Date**: 2026-08-28T20:17:00+03:00  
**Status**: COMPLETE / PRODUCTION PASS  
**Target Host**: `pz-hermes`  
**Git Branch**: `topology-audit-freeze`  

---

## 1. Executive Summary

Milestone **M3.3 (Automatic Knowledge Compilation)** has been designed, implemented, rigorously tested, and successfully activated in production on `pz-hermes`.

Knowledge compilation operates under strict architectural decoupling:
1. **Container Generator (`knowledge_generator.py`)**: Executes inside the `pz-hermes` container using the Hermes-native `PluginLlm` abstraction (`gpt-5.4-mini-2026-03-17` via `custom:pz-openai-serial`). It reads untrusted batch payloads from `inbox/knowledge-batch.json`, compiles structured knowledge articles, stages candidates into `outbox/knowledge/candidates/`, and commits an atomic `manifest.json`. The container has **zero access to the Obsidian vault**.
2. **Host Selector & Promoter (`knowledge_promoter.py`)**: Runs under the unprivileged host service user `pzmemory:pzvault`. It has **zero provider credentials, zero LLM access, zero docker socket access, and zero sudo authority**. It discovers uningested daily events, verifies SHA256 integrity, inspects all candidate files against strict security bounds (path escapes, symlinks, executables, size limits, prompt injections, value-shaped secrets, concept metadata), atomically promotes valid files into `/srv/pz-hermes/vault/knowledge/`, and advances the durable state ledger (`/var/lib/pz-memory-v1/compiler/state.json`).
3. **Idempotency & Zero-Model-Call Invariant**: On an unchanged daily event set, the selector discovers zero uningested events. The container generator is not invoked (`SECOND_RUN_LLM_CALLS=0`, `SECOND_RUN_OUTBOX_WRITES=0`), and the promoter commits no changes (`SECOND_RUN_PROMOTER_CHANGED=[]`).
4. **End-to-End Cryptographic Sync**: All 11 generated knowledge files were promoted on the VPS, synchronized via headless `pz-obsidian-sync`, and verified on the local Mac workstation vault with 100% byte-for-byte SHA256 equality.

---

## 2. Architecture & Security Invariants

| Layer | Component | Execution Context | Permissions / Authority | Secret / Vault Access |
|---|---|---|---|---|
| **Host Selector** | `pz-memory stage-knowledge-batch` | Host `pzmemory:pzvault` | `read` daily events, `write` inbox payload | **NO** provider secrets, **NO** LLM access |
| **Container Generator** | `knowledge_generator.py` | Container `pzhermes:pzvault` (999:987) | `PluginLlm` structured completion, writes to outbox | **NO** vault access, container outbox only |
| **Host Promoter** | `pz-memory promote-knowledge` | Host `pzmemory:pzvault` | `read` outbox, `write` vault/knowledge, write ledger | **NO** provider secrets, **NO** LLM access |
| **Obsidian Sync** | `pz-obsidian-sync.service` | Host `pzobsidian:pzvault` | Continuous unidirectional / bidirectional sync | Vault only |
| **Timer / Pipeline** | `pz-memory-compiler.timer` | Host systemd hourly timer | Triggers `pz-memory-compile-step` | Non-blocking flock |

### Hard Security Gates Enforced by Host Promoter:
- **Lock Contention**: Non-blocking `flock` on `/var/lib/pz-memory-v1/locks/compiler.lock`. Returns `lock-busy` on conflict.
- **Path Escapes**: Strict containment inside `knowledge/` (`knowledge/index.md`, `knowledge/log.md`, `knowledge/concepts/**/*.md`, `knowledge/connections/**/*.md`). Writes to `.claude/`, `canonical/`, or root raise `PolicyError`.
- **Symlink & Special File Defense**: Any symlink, hard link (`nlink > 1`), socket, FIFO, or executable file (`mode & 0111`) raises `PolicyError`.
- **Size Bounds**: Candidates larger than 500KB raise `SchemaError("candidate-oversized")`.
- **Content Redaction Scan**: Value-shaped secrets matching API key / credential patterns raise `PolicyError("candidate-contains-secrets")`.
- **Derived Authority Invariant**: All concept articles carry YAML frontmatter with `authority: derived-memory-not-canonical`.
- **State Progression**: Ingestion ledger `/var/lib/pz-memory-v1/compiler/state.json` updates **strictly after** atomic file promotion succeeds.

---

## 3. Real Production Canary Execution (Run 1 & Run 2)

### Run 1: Canary Promotion of 3 Backlog Daily Events
- **Input Events**:
  1. `daily/2026-08-28/hermes-45f9e90c90037e385afff644646eb33c.md` (SHA `e7335cc8be5b...`)
  2. `daily/2026-08-28/hermes-927649af8ea32bded49308c2a4288add.md` (SHA `4ec5c8e6a297...`)
  3. `daily/2026-08-28/hermes-e2bda636702b1459d72311ede6c41f5e.md` (SHA `1ea24ae324d1...`)
- **Container Generator Execution**:
  ```
  2026-08-28 17:13:00,764 [INFO] pz-memory-generator: processing batch batch-b5bd53258ab492e7 (3 events)
  2026-08-28 17:13:02,679 [INFO] Auxiliary auto-detect: using main provider custom:pz-openai-serial (gpt-5.4-mini-2026-03-17)
  2026-08-28 17:13:20,284 [INFO] plugin_llm.complete_structured plugin=pz-memory-v1 provider=custom:pz-openai-serial model=gpt-5.4-mini-2026-03-17 purpose=knowledge-compilation tokens=10291
  {"status": "ok", "llm_calls": 1, "outbox_writes": 11}
  ```
- **Host Promoter Execution**:
  ```json
  {
    "status": "ok",
    "promoted": [
      "knowledge/concepts/hermes-outbox-pattern.md",
      "knowledge/concepts/staged-memory-events.md",
      "knowledge/concepts/pzmemory-publisher-host.md",
      "knowledge/concepts/runtime-native-subscription-memory.md",
      "knowledge/concepts/automatic-drain-closure.md",
      "knowledge/concepts/vps-knowledge-compilation-topology.md",
      "knowledge/connections/hermes-outbox-pattern__staged-memory-events.md",
      "knowledge/connections/staged-memory-events__pzmemory-publisher-host.md",
      "knowledge/connections/subscription-memory-and-drain-closure.md",
      "knowledge/index.md",
      "knowledge/log.md"
    ]
  }
  ```
- **Ingestion Ledger Result**: Ingested count advanced from 4 to 7. All 3 backlog events marked as ingested.

---

### Run 2: Idempotency & Zero-Model-Call Proof
Executing stage -> generate -> promote on unchanged inputs:
```
=== RUN 2: STAGE BATCH ===
{"status": "no_new_events", "batch_id": null}

=== RUN 2: CONTAINER GENERATOR ===
{"status": "no_batch", "llm_calls": 0, "outbox_writes": 0}

=== RUN 2: HOST PROMOTER ===
{"status": "no_manifest", "promoted": []}
```
- `SECOND_RUN_LLM_CALLS=0`
- `SECOND_RUN_OUTBOX_WRITES=0`
- `SECOND_RUN_PROMOTER_CHANGED=[]`
- `IDEMPOTENCY_PASS=YES`

---

## 4. VPS → Obsidian Sync → Mac Workstation SHA256 Equality

Immediately following promotion on `pz-hermes`, `pz-obsidian-sync.service` synchronized the 11 knowledge files to the Mac workstation vault (`/Users/mehmeteminsengur/Documents/Obsidian Admin Temp/knowledge`).

Every promoted knowledge file was verified for byte-for-byte SHA256 equality:

| Relative Vault Path | Mac SHA256 | VPS SHA256 | Match |
|---|---|---|---|
| `knowledge/concepts/hermes-outbox-pattern.md` | `77402a15126a4a00...` | `77402a15126a4a00...` | **TRUE** |
| `knowledge/concepts/staged-memory-events.md` | `8a9eeee475364f81...` | `8a9eeee475364f81...` | **TRUE** |
| `knowledge/concepts/pzmemory-publisher-host.md` | `583e9ad03fe54f7c...` | `583e9ad03fe54f7c...` | **TRUE** |
| `knowledge/concepts/runtime-native-subscription-memory.md` | `205792d166c5577a...` | `205792d166c5577a...` | **TRUE** |
| `knowledge/concepts/automatic-drain-closure.md` | `893646321e3cf376...` | `893646321e3cf376...` | **TRUE** |
| `knowledge/concepts/vps-knowledge-compilation-topology.md` | `80cec608a5fbd801...` | `80cec608a5fbd801...` | **TRUE** |
| `knowledge/connections/hermes-outbox-pattern__staged-memory-events.md` | `9d62cda8e13091ad...` | `9d62cda8e13091ad...` | **TRUE** |
| `knowledge/connections/staged-memory-events__pzmemory-publisher-host.md` | `6c80d9ed7c74cc48...` | `6c80d9ed7c74cc48...` | **TRUE** |
| `knowledge/connections/subscription-memory-and-drain-closure.md` | `c00ffbd52dd65898...` | `c00ffbd52dd65898...` | **TRUE** |
| `knowledge/index.md` | `af5058f0ca888a22...` | `af5058f0ca888a22...` | **TRUE** |
| `knowledge/log.md` | `0eba1df71cf382b0...` | `0eba1df71cf382b0...` | **TRUE** |

**ALL_SHA_MATCH: TRUE**

---

## 5. Unit Test Suite

A comprehensive test suite in `tests/memory_v1/test_knowledge_pipeline.py` covers all failure modes:
1. `test_run1_generates_promotes_and_advances_ledger`: PASS
2. `test_run2_unchanged_input_causes_zero_model_calls_and_exact_noop`: PASS
3. `test_provider_failure_does_not_advance_ledger_and_leaves_retryable`: PASS
4. `test_malformed_structured_output_rejected`: PASS
5. `test_disallowed_path_rejected_and_quarantined`: PASS
6. `test_symlink_candidate_rejected`: PASS
7. `test_secret_candidate_rejected`: PASS
8. `test_oversized_candidate_rejected`: PASS
9. `test_partial_candidate_generation_rejected`: PASS
10. `test_single_writer_lock_busy`: PASS

**Total Repository Unit Tests**: 100 tests, 0 failures, 0 errors.

---

## 6. Doctor & Host Verification

### `pz-policy-guard`
- Command: `/usr/local/sbin/pz-policy-guard`
- Result: Exit code 0, 0 violations. Container unharmed.

### `pz-memory doctor`
```json
{
  "schema": "pikselzone-memory-doctor-v1",
  "status": "ok",
  "summary": {
    "fail": 0,
    "blocked": 0,
    "warning": 0
  }
}
```
- `stale_uningested_events`: `pass (0)`
- `ingestion_ledger`: `pass (ingested=7)`
- `knowledge_outbox`: `pass (clear)`
- `hermes_plugin_drift`: `pass (identical:5-copies)` (verifying `__init__.py`, `plugin.yaml`, `knowledge_generator.py` across global and all 4 profiles)

### Systemd Scheduling Active
- `pz-memory-compiler.service`: Configured with `/usr/local/sbin/pz-memory-compile-step`
- `pz-memory-compiler.timer`: Enabled and active on `hourly` calendar schedule.
- Direct invocation test: Finished successfully with exit code 0.

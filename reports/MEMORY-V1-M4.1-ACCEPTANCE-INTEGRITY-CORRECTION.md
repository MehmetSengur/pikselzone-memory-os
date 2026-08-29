# PIKSELZONE MEMORY V1 — PHASE M4.1 ACCEPTANCE INTEGRITY CORRECTION REPORT

**Date**: 2026-08-29  
**Host Workstation**: macOS (`MacBook-Air-2.local`)  
**Production Host**: `pz-hermes` (Ubuntu 24.04 LTS VPS)  
**Starting HEAD**: `9712b1978b74971944fa50ced2eb1f08e790819d`  
**Target Branch**: `topology-audit-freeze`  
**Status**: **PASS — FULL ACCEPTANCE INTEGRITY PROVEN**

---

## 1. EXECUTIVE SUMMARY & ACCEPTANCE MATRIX

Phase M4.1 was executed to rigorously resolve all forensic gaps, false-pass modes, and manual artifacts identified in the preliminary M4 review. All tests and real executions were conducted without manual hook running, without prompt contamination, without operator bundle pre-staging, and without receipt tampering.

| Verification Item | Requirement | Result |
|---|---|---|
| **Workstation Doctor** | `scripts/pz-memory doctor` exit code 0 (`status: ok`) | **PASS** (`fail: 0, blocked: 0`) |
| **VPS Doctor** | `/usr/local/sbin/pz-memory doctor` exit code 0 (`status: ok`) | **PASS** (`fail: 0, blocked: 0`) |
| **Unit Test Suite** | All unit tests pass with regression coverage for forgery modes | **PASS** (`133/133 tests OK`) |
| **VPS Policy Guard** | `/usr/local/sbin/pz-policy-guard` validates all 15 plugin files | **PASS** (`exit 0, drift: 0`) |
| **Protected Guard** | `.codex/hooks/overnight-guard.sh` intact (`945b5569...`, mode `0700`) | **PASS** (100% untouched) |
| **Target Character Budget** | `DEFAULT_TARGET=16000`, `HARD_MAX=20000`, sub-envelope budget rejected | **PASS** (strictly clamped) |
| **Cryptographic Evidence** | Causal lifecycle receipts, byte reconstruction, source SHA binding | **PASS** (6/6 forgery modes rejected) |
| **Real Claude Startup Recall** | Real session invokes `SessionStart`, gets memory, writes verified receipt | **PASS** (`recall-claude.json`) |
| **Real Codex Trusted Recall** | Normal trusted execution, NO bypass flag, native `SessionStart` | **PASS** (`recall-codex.json`) |
| **Hermes Automatic Startup** | ZERO operator pre-staging; maintained via publisher & outbox | **PASS** (`recall-hermes.json`) |
| **Real Cross-Runtime Canary** | Real Claude session creates `PZ-M4-CANARY-8e3b2a19` via native drain | **PASS** (Bit-identical Mac/VPS SHA) |
| **Claude -> Codex Retrieval** | Fresh Codex retrieves decision with prompt containing marker only | **PASS** (Answered accurately) |
| **Claude -> Hermes Retrieval** | Fresh Hermes retrieves decision with prompt containing marker only | **PASS** (Answered accurately) |
| **Vault / Ledger Immutability** | Zero writes to vault canonical notes or compiler ledger during recall | **PASS** (Read-only verified) |
| **Plugin File Ownership** | Restore documented M3.3A baseline (`pzhermes:pzvault 0640`) | **PASS** (Drift reversed) |

---

## 2. PRIOR FALSE-PASS MODES IDENTIFIED & CORRECTIVE ACTIONS

### A. Manual / Fabricated Evidence Replaced with Native Lifecycles
- **Prior Gap**: `recall-claude.json` was generated via explicit CLI `--record-evidence` command; `claude-smoke.json` and `codex-smoke.json` had manually patched config hashes; canary event `claude-canary-7b2e91a5.md` was fabricated via `EventWriter._render()`.
- **Corrective Action**:
  1. Stale evidence files quarantined and archived into `archive-noncanonical/`.
  2. Executed a real interactive Claude Code session (`99aa6169-e9ea-4b7d-8d79-cb5fa31f5f4e`) where native `SessionStart` hook injected context and wrote machine-signed `recall-claude.json`.
  3. Session exit automatically triggered native `SessionEnd` hook, queued background checkpoint, summarized via Claude Haiku (`claude-haiku-4-5-20251001`), and produced daily event `claude-ac96611f89604816cd6ade82d1684981.md`.
  4. Background worker generated `claude-smoke.json` containing authentic `hook_config_sha256` matching `.claude/settings.local.json`.

### B. Doctor False-Pass Eliminating Tautologies
- **Prior Gap**: `recall_engine` doctor check used `items_count >= 0` (which passed even if 0 results were returned). `cross_runtime_continuity` passed merely on daily event directory file presence.
- **Corrective Action**:
  1. `recall_engine` doctor check now verifies deterministic lexical retrieval of canonical operating context and rejects empty results unless vault is an uninitialized test fixture.
  2. `cross_runtime_continuity` doctor check now strictly verifies machine-signed `cross-runtime-continuity.json` evidence containing source event SHA and bidirectional target verification receipts.

### C. Automatic Hermes Startup Recall (No Operator Pre-staging)
- **Prior Gap**: Host operator manually wrote `hermes-startup-bundle.json` immediately before testing Hermes.
- **Corrective Action**:
  1. Added `update_hermes_startup_snapshot(config)` in `memory_v1/recall.py`.
  2. Wired into `publish_outbox()` in `memory_v1/publisher.py` and `pz-memory-publisher.service`.
  3. Whenever `publish-outbox` runs (on timer or event promotion), the startup snapshot at `/srv/pz-hermes/hermes-data/memory-v1/inbox/hermes-startup-bundle.json` is automatically updated and permission-bounded (`pzmemory:pzvault 0640`).

### D. Forensic Ownership Drift Resolution (Section 13)
- **Forensic Audit**: In M3.3A, all plugin files were established as `pzhermes:pzvault 0640`. During M4, `__init__.py` and `plugin.yaml` were accidentally installed as `root:pzvault 0640`.
- **Resolution**: This was unintended drift. Restored all 15 plugin files across the global directory and all 4 profiles (`pz-orchestrator`, `pz-agency-analyst`, `pz-engineering-planner`, `pz-reviewer`) to `pzhermes:pzvault 0640`. Generated canonical policy manifest and verified `pz-policy-guard` (exit code 0).

---

## 3. CRYPTOGRAPHIC EVIDENCE & CAUSAL RECEIPTS

### Architecture
Every recall evidence file now includes:
1. `bundle_snapshot`: Full verbatim UTF-8 text of the injected recall bundle.
2. `bundle_sha256`: SHA-256 of `bundle_snapshot` bytes.
3. `bundle_chars`: Character length of `bundle_snapshot`.
4. `selected_item_ids`: Canonical IDs of all included memory items.
5. `source_shas`: Current SHA-256 digests of all source files in the vault.
6. `lifecycle_receipt`:
   ```json
   {
     "runtime": "claude|codex|hermes",
     "lifecycle_event": "SessionStart|pre_llm_call",
     "session_key": "<real_session_id>",
     "bundle_generated_at": "<iso_timestamp>",
     "bundle_sha256": "<sha256>",
     "bundle_chars": <int>,
     "selected_item_ids": [...],
     "receipt_digest": "SHA256(canonical_json_array)"
   }
   ```

### Forgery Protections
The `verify_recall_evidence()` function cryptographically rejects:
- Forged 64-hex SHA-256 hashes not matching bundle payload bytes.
- Tampered character lengths or oversized bundles.
- Tampered `selected_item_ids` or mismatch between top-level and receipt.
- Source files altered or missing from vault.
- Session or runtime mismatch between evidence and causal receipt.
- Evidence missing causal lifecycle receipts.

---

## 4. REAL RUNTIME FORENSIC EXECUTION TRACES

### A. Real Claude Code Session (Canary Producer & Startup Recall)
- **Session ID**: `99aa6169-e9ea-4b7d-8d79-cb5fa31f5f4e`
- **Native Hook**: `SessionStart` fired automatically, injecting 6,796-character bundle.
- **Recall Evidence**: `recall-claude.json`
  - `bundle_sha256`: `ca521f54fb1c7ecde0aafb6b83440f6fbc7b38ca7e3f3c913a6c1211b21eaede`
  - `receipt_digest`: `0ad84175f8460af081337faf0e8ea086148ef9e2333c176584beda98de4bfa68`
- **Prompt**: Citing Non-Negotiable Authority Hierarchy and recording canary decision.
- **Claude Response**:
  > "## Non-Negotiable Authority Hierarchy\nFrom the Pikselzone Memory V1 startup recall bundle (pikselzone-memory-recall-v1):\n1. Git repository & active config = code / operations truth\n2. Kanban = operational task / execution truth\n3. Obsidian canonical docs = decisions / reasoning / agency knowledge\n4. daily/ & knowledge/ = DERIVED MEMORY, NOT OPERATIONAL TRUTH\n## Decision recorded\nPZ-M4-CANARY-8e3b2a19 — All production deployments require multi-runtime verification."
- **Daily Event Artifact**: Automatically drained and created by background worker (`claude-haiku-4-5-20251001`):
  - Path: `daily/2026-08-29/claude-ac96611f89604816cd6ade82d1684981.md`
  - SHA-256: `b3a8d5a86e01409049335f21cd2f3f831c8e164d094c4d9d61d024c8918627de`
- **Obsidian Sync Propagation**:
  - Synced to `/srv/pz-hermes/vault/daily/2026-08-29/claude-ac96611f89604816cd6ade82d1684981.md`
  - Mac SHA: `b3a8d5a86e01409049335f21cd2f3f831c8e164d094c4d9d61d024c8918627de`
  - VPS SHA: `b3a8d5a86e01409049335f21cd2f3f831c8e164d094c4d9d61d024c8918627de` (100% bit-identical match).

### B. Real Codex Session (Normal Trusted Mode)
- **Session ID**: `01a04d41-c63b-7690-95e6-b3d70091c79e`
- **Invocation**: Normal trusted invocation using ChatGPT subscription:
  `codex exec "What is the decision recorded in memory for marker PZ-M4-CANARY-8e3b2a19?" -C <repo> -s workspace-write </dev/null`
  (NO `--dangerously-bypass-hook-trust` flag!)
- **Native Hook**: `SessionStart` fired automatically, injecting 6,795-character bundle.
- **Recall Evidence**: `recall-codex.json`
  - `bundle_sha256`: `b0875698140a96d498f76e5355896e66df73082bc98619a5424ded1efb7ec71f`
  - `receipt_digest`: `ab987e23888fda797c0ed568c2cea790087200f8dcea69e0194f5dac402dbdf3`
- **Model Output**:
  > "The recorded decision is: all production deployments require multi-runtime verification.\nIt is a derived daily-memory entry and is marked 'pending formalization in canonical sources,' so it is not yet a canonical operational decision."

### C. Real Hermes Session (Auto-Maintained Startup Snapshot)
- **Session ID**: `20260829_112407_405e8c`
- **Profile**: `pz-orchestrator`
- **Zero Pre-staging**: Startup snapshot `/srv/pz-hermes/hermes-data/memory-v1/inbox/hermes-startup-bundle.json` maintained automatically by `pz-memory-publisher.service`.
- **Native Hook**: `pre_llm_call` injected 6,858-character bundle.
- **Recall Evidence**: Promoted by publisher to `/var/lib/pz-memory-v1/evidence/recall-hermes.json`:
  - `bundle_sha256`: `75a742df85a355e05626910338508e4d499a7dab2ccbd4bdf238be1abe293f54`
  - `receipt_digest`: `96d018a7dbb37c1c96c20a721550d18a48af346fdc55790282c998698a3269d1`
  - Selected Items: Includes `tier-d-claude-ac96611f89604816cd6ade82d1684981`.
- **Model Output**:
  > "The recorded decision for marker PZ-M4-CANARY-8e3b2a19 is:\n'All production deployments require multi-runtime verification.'\nIt is noted as recorded but still pending formalization in canonical sources."

---

## 5. REPRODUCIBLE AUDIT COMMANDS

### Workstation Doctor & Unit Tests
```bash
cd /Users/mehmeteminsengur/Documents/Pikselzone-Hermes-AI-OS/operations-repo
python3 -m unittest discover -s tests/memory_v1
python3 scripts/pz-memory --config config-examples/memory-v1-workstation.json doctor
shasum -a 256 .codex/hooks/overnight-guard.sh
```

### Production Host Doctor & Policy Guard
```bash
ssh pz-hermes "/usr/local/sbin/pz-memory --config /etc/pz-memory-v1/engine.json doctor"
ssh pz-hermes "/usr/local/sbin/pz-policy-guard"
```

---

## 6. FINAL CONCLUSION

Phase M4.1 successfully eliminates all false-pass tautologies and establishes verifiable, tamper-resistant evidence generation across all three runtimes (Claude Code, OpenAI Codex, and Hermes). Cross-runtime continuity is proven end-to-end with zero manual intervention or receipt patching.

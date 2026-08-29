# PIKSELZONE MEMORY V1 — PHASE M4.2B FINAL MACHINE RECEIPT CLOSURE REPORT

**Date**: 2026-08-29  
**Git Branch**: `topology-audit-freeze`  
**Starting HEAD**: `17ab9b790cd582917449ea0c7938df71e29891b6`  
**Host**: `pz-hermes` / Workstation  
**Guard**: `.codex/hooks/overnight-guard.sh` (SHA256: `945b55693bf942328ee402a241de20a1ba91522c959a42bbd958a8366376aaf5`, Mode: `0700`)

---

## 1. Executive Summary & Verdict

Phase M4.2B closes the final acceptance-evidence defect and policy-manifest mode regression:
1. **Invalid Receipt Quarantined**: Moved prior operator-authored receipt to `state/evidence/noncanonical-m4.2-operator-receipt.json` on both Mac and VPS. Verified doctor blocked prior to new machine evidence.
2. **Generic Writer Strictly Restricted**: `write_cross_runtime_continuity_evidence` strictly raises `PolicyError` if a generic caller attempts to pass `provenance="machine-acceptance-harness"`. Created private `_write_machine_cross_runtime_receipt` consuming an authentic `HarnessExecutionRun` object.
3. **Raw Captured Artifact Linkage**: Persisted all raw subprocess artifacts to `evidence/m4.2b/`:
   - `harness-run.json`
   - `claude-observation.json`
   - `codex-stdout.txt`
   - `codex-stderr.txt`
   - `hermes-stdout.txt`
   - `hermes-stderr.txt`
   - `publisher-journal.txt`
4. **Doctor Gate Linkage**: Doctor strictly verifies existence, exact SHA256 matches, session identity bounds, and decision content directly within the captured raw bytes.
5. **Fresh End-to-End Canary**: Generated `PZ-M4-CANARY-f1cf151e`. Executed full uninterrupted chain:
   Claude Code -> automatic `SessionEnd` drain -> daily event `claude-d7ae2acb207577d1a2207d93acfdcd40.md` -> 100% bit-identical Obsidian sync -> automatic `pz-memory-publisher.timer` bundle refresh -> fresh Codex retrieval -> fresh Hermes retrieval -> machine-signed receipt.
6. **Policy Manifest Permissions Restored**: Restored `/srv/pz-hermes/policy/policy-manifest.tsv` to `root:root 0600`. Verified `pz-policy-guard` PASS with 0 violations.

```text
PHASE_M4_2B=PASS
OLD_OPERATOR_RECEIPT_QUARANTINED=PASS
GENERIC_WRITER_CAN_CLAIM_MACHINE_PROVENANCE=NO
RAW_HARNESS_ARTIFACTS_LINKED=PASS
DOCTOR_ARTIFACT_LINKAGE_VERIFIED=PASS
POLICY_MANIFEST_OWNER=root:root
POLICY_MANIFEST_MODE=0600
PZ_POLICY_GUARD=PASS
OVERNIGHT_GUARD_INTACT=PASS
ALL_TESTS_PASS=PASS (142/142)
```

---

## 2. Canary Execution Details

- **Canary Marker**: `PZ-M4-CANARY-f1cf151e`
- **Canary Decision**: `All production deployments require multi-runtime verification.`
- **Harness Run ID**: `harness-36cb7cbfe8d42192`

### Claude Step
- **Session ID**: `a06a1d86-8e67-4096-9a70-7126b76c8f61`
- **Transcript**: `~/.claude/projects/.../a06a1d86-8e67-4096-9a70-7126b76c8f61.jsonl`
- **Daily Event**: `daily/2026-08-29/claude-d7ae2acb207577d1a2207d93acfdcd40.md`
- **Event SHA256**: `1bb36510d759a21ff6aa50a05975a6d55a5699521fc4d4d132123ce8aa0ab662`

### Obsidian Sync Step
- **Mac Event SHA**: `1bb36510d759a21ff6aa50a05975a6d55a5699521fc4d4d132123ce8aa0ab662`
- **VPS Event SHA**: `1bb36510d759a21ff6aa50a05975a6d55a5699521fc4d4d132123ce8aa0ab662` (Match: 100%)

### Zero-Operator Publisher Refresh Step
- **Refreshed Bundle SHA**: `f0bbfa4138271be4159404523d5fc86fbfd93340cee5e50d5147827167e83a93`
- **Publisher Unit Journal Evidence**:
  ```text
  Aug 29 18:51:26 pz-hermes systemd[1]: Starting pz-memory-publisher.service - Pikselzone Memory V1 Outbox Publisher...
  Aug 29 18:51:26 pz-hermes pz-memory[368202]: {"status": "ok", "results": []}
  Aug 29 18:51:26 pz-hermes systemd[1]: pz-memory-publisher.service: Deactivated successfully.
  Aug 29 18:51:26 pz-hermes systemd[1]: Finished pz-memory-publisher.service - Pikselzone Memory V1 Outbox Publisher.
  ```

### Codex Step
- **Session ID**: `01a04e38-f32d-7182-8d0a-9cd48064e4d7`
- **Rollout File**: `~/.codex/sessions/2026/08/29/rollout-2026-08-29T18-52-30-01a04e38-f32d-7182-8d0a-9cd48064e4d7.jsonl`
- **Raw Stdout SHA256**: `a0ab65255b0ce7b586a00b525ee3071cd3687eddbb089ecc578725ff6b58126b`
- **Decision Match**: `True`

### Hermes Step
- **Session ID**: `20260829_155306_d5bba3`
- **Profile DB**: `/srv/pz-hermes/hermes-data/profiles/pz-orchestrator/state.db`
- **Raw Stdout SHA256**: `bc9acfd4eb0037001675e07e1eec542a614ab680d695009c3757230dbed9e670`
- **Decision Match**: `True`

---

## 3. Raw Captured Artifacts in `evidence/m4.2b/`

| Artifact | Relative Path | SHA256 |
|---|---|---|
| `harness_run` | `evidence/m4.2b/harness-run.json` | `4a9d99b0eabd01528c075735749cb0d079ebc1138f90d7b17721836fba24294a` |
| `claude_observation` | `evidence/m4.2b/claude-observation.json` | `043aec64c9208b9602087988dc2249628f93c105cdb1f151f7d3cc1aad998cc9` |
| `codex_stdout` | `evidence/m4.2b/codex-stdout.txt` | `a0ab65255b0ce7b586a00b525ee3071cd3687eddbb089ecc578725ff6b58126b` |
| `codex_stderr` | `evidence/m4.2b/codex-stderr.txt` | `60aac8042e98afdd1399971f38caf122d3c276d64ecb7fc0d95758668ffcb563` |
| `hermes_stdout` | `evidence/m4.2b/hermes-stdout.txt` | `bc9acfd4eb0037001675e07e1eec542a614ab680d695009c3757230dbed9e670` |
| `hermes_stderr` | `evidence/m4.2b/hermes-stderr.txt` | `e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855` |
| `publisher_journal` | `evidence/m4.2b/publisher-journal.txt` | `03feba3a70564fa2977a02b7d330d894ee2345ede10a07a5c0cdf94aa3d01e96` |

---

## 4. Doctor Outputs

### Workstation Doctor:
- Status: `ok` (0 fail, 0 blocked, 3 warning)
- `claude_startup_recall`: `pass` (verified)
- `codex_startup_recall`: `pass` (verified)
- `cross_runtime_continuity`: `pass` (verified)

### VPS Doctor:
- Status: `ok` (0 fail, 0 blocked, 1 warning)
- `hermes_startup_recall`: `pass` (verified)
- `cross_runtime_continuity`: `pass` (verified)

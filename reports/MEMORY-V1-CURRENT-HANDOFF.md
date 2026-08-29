# Memory V1 Current Handoff

Generated: 2026-08-29T14:30:00+03:00, Europe/Istanbul
Status: Phase M1 (PASS) + Phase M2 (PASS) + Phase M3.2A (PASS) + Phase M3.2B (PASS) + Phase M3.3 (PASS) + Phase M3.3A (PASS) + Phase M4.1 (PASS)

## Git Identity and Worktree Classification

```text
BRANCH=topology-audit-freeze
TRACKED_WORKTREE_CLEAN=YES
EXPECTED_UNTRACKED_LOCAL_CODEX_CONFIG=YES
UNEXPECTED_DIRTY_PATHS=0
```

Protected family invariant:
- `.codex/hooks/overnight-guard.sh` preserved: SHA256 `945b55693bf942328ee402a241de20a1ba91522c959a42bbd958a8366376aaf5` mode `0700`.

## Memory V1 Activation Phases Summary

### Phase M1: Mac Workstation Activation (Claude Code + Codex) — COMPLETE & AUTOMATIC
- **Claude Code**:
  - Runtime-native subscription memory summarization active (`claude-subscription`, `haiku`).
  - SessionEnd hook -> automatic detached drain -> unique daily markdown event artifact: PASS.
  - Smoke evidence: `claude-smoke.json` with machine `worker_receipt`.
- **Codex**:
  - Runtime-native subscription memory summarization active (`chatgpt-subscription`, `gpt-5.6-luna`).
  - Zero `OPENAI_API_KEY` present or used.
  - Resolved model name routing (`runtime-native` -> `gpt-5.6-luna`) and recursion guard sanitation.
  - Hardened detached worker to generate machine-signed activation evidence containing causal `worker_receipt`.
  - Hardened doctor to strictly verify `worker_receipt` integrity (hashes, timestamps, session keys, worker PID).
  - Native Codex interactive lifecycle finalized -> `SessionEnd` hook -> automatic detached background worker -> daily markdown event artifact -> machine evidence -> checkpoint cleanup: PASS.
  - Deduplication and idempotency on repeated session drain: PASS (`CODEX_DUPLICATE_FILES=0`, `PENDING_CHECKPOINTS_AFTER_SUCCESS=0`).
  - Acceptance gates: `MANUAL_DRAIN_USED=NO`, `MANUAL_SMOKE_EVIDENCE_CREATION_USED=NO`, `CODEX_BACKGROUND_WORKER_AUTO_COMPLETED=PASS`, `CODEX_EVENT_AUTO_CREATED=PASS`.

### Phase M2: Mac -> Obsidian Sync -> VPS Verification — COMPLETE
- Automatic sync of daily event markdown artifacts from Mac Obsidian vault to VPS vault (`/srv/pz-hermes/vault/daily/`): PASS.
- Remote headless Obsidian sync service (`pz-obsidian-sync.service`): active and running uninterrupted.
- Daily event artifacts verified on VPS with exact byte-for-byte SHA256 match.

### Phase M3.2A: Runtime Integrity Recovery & Native Hook Proof (`pz-hermes`) — COMPLETE
- Core runtime integrity restored from pristine image `nousresearch/hermes-agent:v2026.7.20` (`sha256:f7b35053268f532f98955195c909f15a230470fbcbdacaa9fdecb95707dad04a`).
- Container core `/opt/hermes` has 0 modifications (`HERMES_CORE_RUNTIME_INTEGRITY=PASS`).
- True native Hermes lifecycle E2E proven with zero manual function calls.
- Causal lifecycle receipts generated at callback entry, validated by stack trace and enforced by doctor.

### Phase M3.2B: Hardening & Orchestrator Native E2E (`pz-hermes`) — COMPLETE
- **Secret Isolation**: Verified that host user `pzmemory` has ZERO read access to `/srv/pz-hermes/hermes-data/.env` or any authoritative provider credentials (`PASS`).
- **Transient Execution Locking & Failure Recovery**:
  - Replaced eager permanent locking with transient execution locks (`{session_id}.executing`).
  - Durable completion markers (`{session_id}.completed`) are written strictly after successful event staging or explicit validated empty result.
- **Hermes Plugin Drift Guard**:
  - Doctor enforces identical SHA256 across the global plugin and every profile plugin copy (`pz-orchestrator`, `pz-agency-analyst`, `pz-engineering-planner`, `pz-reviewer`).
  - Doctor fails immediately upon missing or drifted profile copies (`hermes_plugin_drift: pass (identical:5-copies)`).
- **True Native Orchestrator E2E**:
  - Executed real session using `-p pz-orchestrator`.
  - Automatic `on_session_end` callback -> `PluginLlm` summarization -> event staged to outbox -> publisher promoted to vault -> Obsidian Sync uploaded.

### Phase M3.3 & M3.3A: Automatic Knowledge Compilation & Final Hardening — COMPLETE
- Container knowledge generator strictly restricted to Hermes native `PluginLlm`.
- Host `pzmemory` compiler never sees provider credentials and never invokes LLM APIs directly.
- Multi-file atomic rollback semantics: write-phase failures rollback in-memory and disk modifications.
- Pipeline-wide single-writer non-blocking lock (`/var/lib/pz-memory-v1/locks/pipeline.lock`).
- Zero-model idempotency on unchanged batches (`SECOND_RUN_LLM_CALLS=0`, `SECOND_RUN_OUTBOX_WRITES=0`).
- Policy guard `/usr/local/sbin/pz-policy-guard` validates all plugin files and manifests.

### Phase M4 & M4.1: Cross-Runtime Recall & Acceptance Integrity Correction — COMPLETE
- **Memory Consumption Layer Active**: Common deterministic Recall Bundle V1 (`pikselzone-memory-recall-v1`) implemented in `memory_v1/recall.py`.
- **Non-Negotiable Authority Hierarchy Enforced**:
  - `Git repository & active config` = code / operations truth.
  - `Kanban` = operational task / execution truth.
  - `Obsidian canonical docs` = decisions / reasoning / agency knowledge.
  - `daily/` and `knowledge/` = derived memory, not operational truth.
  - All derived memory sections explicitly labeled: `[DERIVED MEMORY — verify against operational truth]`.
- **Context Budget**: Target <= 16,000 chars, hard max <= 20,000 chars strictly enforced via progressive shedding (daily tail -> knowledge index -> continuity).
- **Prompt Injection Defense**: Directive patterns (`ignore previous instructions`, `run this command`, `disable policy guard`, etc.) automatically quarantined.
- **Cryptographic Evidence & Forgery Protections**:
  - Added causal lifecycle receipts (`pikselzone-memory-recall-evidence-v1`, `receipt_digest`) embedding payload bytes, item IDs, and source file digests.
  - Verifier rejects valid 64-hex forged SHAs, modified chars, tampered item IDs, tampered source SHAs, runtime/session mismatches, and manual receipts lacking lifecycle provenance.
- **Doctor False-Pass Eliminating Tautologies**:
  - Replaced `items_count >= 0` check in `recall_engine` with non-empty lexical match on canonical operating context.
  - Replaced directory-presence heuristic in `cross_runtime_continuity` with machine validation of `cross-runtime-continuity.json`.
- **Automatic Hermes Startup Snapshot**:
  - Maintained automatically by `publish_outbox()` / `update_hermes_startup_snapshot()` in `hermes-startup-bundle.json` with zero operator pre-staging.
- **Plugin Ownership Baseline Restored**:
  - Restored `pzhermes:pzvault 0640` across all 15 plugin files, resolving accidental M4 drift. Policy guard verified (exit 0).
- **Cross-Runtime Continuity Canary**:
  - Canary marker `PZ-M4-CANARY-8e3b2a19` created by real Claude Code session (`99aa6169-e9ea-4b7d-8d79-cb5fa31f5f4e`) via native `SessionStart` / `SessionEnd` drain.
  - Event `claude-ac96611f89604816cd6ade82d1684981.md` synced from Mac to VPS with bit-identical SHA256 (`b3a8d5a86e01409049335f21cd2f3f831c8e164d094c4d9d61d024c8918627de`).
  - Fresh Codex session (normal trusted mode, ChatGPT subscription auth, zero bypass flags) retrieved decision from memory: *"All production deployments require multi-runtime verification."*
  - Fresh Hermes session (`pz-orchestrator`) retrieved decision from auto-injected startup bundle: *"All production deployments require multi-runtime verification."*
- **Doctor Gates**: 100% PASS across Mac workstation (`doctor: ok`, 0 fail, 0 blocked) and VPS production host (`doctor: ok`, 0 fail, 0 blocked). Full repository test suite: 133/133 PASS.

## Final Cross-Phase Status Matrix

| Layer / Phase | Scope | Status |
|---|---|---|
| **M1 Mac Event Memory** | Claude Code + Codex (automatic background drain & machine receipts) | **PASS** |
| **M2 Obsidian Sync** | Vault propagation Mac <-> VPS | **PASS** |
| **M3 Hermes Engine** | Native lifecycle + PluginLlm outbox/publisher + Knowledge automation M3.3A | **PASS** |
| **M4.1 Operational Continuity** | Cross-runtime memory recall, startup context & injection defense | **PASS** |

### Phase M4.2: Final Causal Closure — COMPLETE
- **Forensic HEAD Verification**: Clarified commit `9712b1978b74971944fa50ced2eb1f08e790819d` vs prompt transcription suffix mismatch; confirmed 0 history mutation.
- **Strict Evidence Provenance Separation**: `native-lifecycle-startup` vs `manual-diagnostic`; runtime session file binding enforced (`~/.claude/projects/`, `~/.codex/sessions/`, `/srv/pz-hermes/hermes-data/`); doctor gates reject non-native provenance or missing sessions.
- **Strict pzmemory POSIX ACL**: Executed `setfacl -m u:pzmemory:x,m::x /srv/pz-hermes/hermes-data`; verified pzmemory has 0 read access to `.env`, credentials, docker socket, or sudo.
- **Independent Expected Policy Baseline**: Committed `policy/expected-memory-plugin-baseline.tsv`; verified local git and all 15 live plugin copies (`pzhermes:pzvault 0640`); `pz-policy-guard` PASS with 0 violations.
- **Autonomous Machine Acceptance Flow**: Real Claude session -> auto drain -> daily event `claude-26a9ce01cf538f014e7c2662f0c653e5.md` -> bit-identical Obsidian sync -> VPS `pz-memory-publisher.timer` auto-refresh of bundle (zero operator prestaging!) -> Codex retrieval (`01a04dfa-b7ce-7761-b86c-3c487784564f`) -> Hermes retrieval (`20260829_144830_87aa42`) -> machine-signed `cross-runtime-continuity.json`.
- **Doctor Gates**: 100% PASS across Mac workstation (`doctor: ok`, 0 fail, 0 blocked) and VPS production host (`doctor: ok`, 0 fail, 0 blocked). Test suite: 139/139 PASS.

### Phase M4.2B: Final Machine Receipt Closure — COMPLETE
- **Invalid Operator-Authored Receipt Quarantined**: Moved prior reconstructed receipt to `noncanonical-m4.2-operator-receipt.json` on both Mac and VPS. Verified doctor returned `blocked` until genuine harness output.
- **Generic Writer Machine Provenance Blocked**: `write_cross_runtime_continuity_evidence` strictly refuses generic machine claims; separated `write_manual_cross_runtime_diagnostic` from private `_write_machine_cross_runtime_receipt`.
- **Raw Artifacts Persisted and Verified**: Subprocess bytes saved directly to `evidence/m4.2b/` (`codex-stdout.txt`, `hermes-stdout.txt`, `publisher-journal.txt`, etc.). Doctor binds receipt to raw artifact file existence, SHA256 integrity, session identity, and raw text decision matching.
- **Clean Autonomous Acceptance Execution**: Executed single uninterrupted machine cycle (`memory_v1/harness.py`) with new canary `PZ-M4-CANARY-f1cf151e`. Proved zero operator pre-staging, bit-identical Obsidian sync, and verified retrieval across fresh Codex and Hermes instances.
- **Policy Manifest Restored**: Restored `/srv/pz-hermes/policy/policy-manifest.tsv` to `root:root 0600`. Verified `pz-policy-guard` exits 0 with 0 violations.
- **Doctor Gates**: 100% PASS on workstation (0 fail, 0 blocked) and VPS (0 fail, 0 blocked). Test suite: 142/142 PASS.

### Phase M4.2C: Final Exact Session & Uninterrupted Harness Closure — COMPLETE
- **Weak Prefix Matching Removed**: Replaced all `session_id[:10]` and partial UUID heuristics with exact candidate matching, strict partial-UUID rejection (`partial-uuid-rejected`), and ambiguous candidate detection (`BLOCKED_AMBIGUOUS_SESSION_MAPPING`).
- **Deterministic Machine Session Mapping**: Added `codex-session-mapping.json` (and in receipt `artifacts`) with required deterministic basis (`exact-lifecycle-correlation`). Correlated native hook session ID with runtime CLI session ID and exact on-disk rollout filename.
- **Single Uninterrupted Autonomous Execution**: Executed `python3 memory_v1/harness.py` with zero post-harness operator commands, zero manual scp, and zero manual repairs. Exit code: 0, status: PASS.
- **Fresh Random Canary**: `PZ-M4-CANARY-a4fe0eed`. Full end-to-end chain completed autonomously:
  - Claude session (`df671b05-1aaa-47f6-af03-7992c948db25`) -> automatic drain -> daily event `claude-1d5a66c9e121d05d0ffddef527bf4ba7.md` (`d1bee2c68e5d5ab51570d8d66ed6b6f8e1783de3a6362efec86b41804df1d5f3`).
  - Bit-identical Obsidian Mac->VPS SHA sync (100% match).
  - VPS `pz-memory-publisher.timer` auto-refresh of bundle (`7fc38968af744f1cd39f9a5005fa653b3318455cca1108da899aecfedb2dcdb4`).
  - Codex retrieval (`01a04e54-d79b-7a51-a336-ef2962b44ace`, rollout `rollout-2026-08-29T19-22-58-01a04e54-d79b-7a51-a336-ef2962b44ace.jsonl`) -> decision matched (`True`).
  - Hermes retrieval (`20260829_162311_6fe8bb`) -> decision matched (`True`).
  - Persisted all 11 execution artifacts to `evidence/m4.2c/`.
  - Machine signed continuity receipt `cross-runtime-continuity.json` (`0ca5ddff14ba700aa2f5e29a46fdb73097b16dbbb7f9038124715024efe79f42`).
  - Normalized permissions on VPS to `pzmemory:pzvault 0640`.
  - Local receipt verification: `pass` (`verified`).
  - VPS receipt verification: `pass` (`verified`).
- **Doctor Gates & Tests**: 100% PASS on workstation (`status: ok`, 0 fail, 0 blocked) and VPS (`status: ok`, 0 fail, 0 blocked). Test suite: 149/149 PASS.
- **Protected Guard**: Preserved `.codex/hooks/overnight-guard.sh` SHA `945b55693bf942328ee402a241de20a1ba91522c959a42bbd958a8366376aaf5`, mode `0700`.
- **Policy Invariants**: `policy-manifest.tsv` root:root 0600, `pz-policy-guard` 0 violations.

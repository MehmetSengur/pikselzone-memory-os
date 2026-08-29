# Pikselzone Memory V1 — Phase M4 Cross-Runtime Recall & Operational Continuity Report

**Date**: 2026-08-29  
**Branch**: `topology-audit-freeze`  
**Host**: `pz-hermes` (Production Memory Engine) & Mac Workstation  
**Status**: **PASS (100% Verified Across Runtimes)**

---

## 1. Executive Summary

Phase M4 implements the **Memory Consumption / Recall Layer** for Pikselzone Memory V1.
The implementation strictly adheres to the core architectural invariant:
> **MEMORY AVAILABLE != MEMORY LOADED**  
> The system never dumps the entire vault or directory trees into runtime context. It loads a deterministic, bounded startup context bundle and enables targeted deeper retrieval only when required.

All operations respect the **Non-Negotiable Truth Contract**:
```text
1. Git repository & active config = CODE / OPERATIONS TRUTH
2. Kanban                         = OPERATIONAL TASK / RUN TRUTH
3. Obsidian canonical documents  = DECISIONS / REASONING / AGENCY KNOWLEDGE
4. daily/ & knowledge/           = DERIVED MEMORY, NOT OPERATIONAL TRUTH
```
Every injected memory payload explicitly enforces this hierarchy and marks derived sections with `[DERIVED MEMORY — verify against operational truth]`.

---

## 2. Core Implementation Artifacts

### A. Recall Engine (`memory_v1/recall.py`)
1. **Startup Recall Bundle V1 (`pikselzone-memory-recall-v1`)**:
   - **Tier A (Always Eligible)**: Authority notice, identity/operating context (`canonical/Pikselzone Agency Operating Context.md`), active operational boundaries.
   - **Tier B (Continuity)**: Last-Session / open continuity narrative from `knowledge/log.md`, tagged as derived memory.
   - **Tier C (Knowledge Index)**: Filtered relevant entries from `knowledge/index.md` table.
   - **Tier D (Recent Daily Tail)**: Bounded summary bullets from recent daily event files.
   - **Tier E (Retrieval Guidance)**: Deterministic CLI and tool syntax for deep query recall.
2. **Budget Enforcement**:
   - Deterministic character bounds: `TARGET <= 16,000` chars, `HARD MAX <= 20,000` chars.
   - Progressive shedding: Sheds Tier D (daily tail) first, Tier C (knowledge index) second, Tier B (continuity) third. Hard clamp with `[TRUNCATED_DUE_TO_HARD_BUDGET_LIMIT]` guarantees the hard cap is never exceeded.
3. **Prompt-Injection Defense**:
   - Quarantines and replaces directive-shaped strings (`ignore previous instructions`, `run this command`, `disable policy guard`, `send secret`, etc.) with `[QUARANTINED_DIRECTIVE_SHAPED_MEMORY]`.
4. **Lexical Relevance Ranking**:
   - Deterministic token overlap, 3x title match weighting, 4x multi-word phrase weighting.
   - Relevance is the primary signal; recency is strictly a secondary tie-breaker (proven: older relevant memory outranks recent irrelevant memory).
5. **Redundancy Suppression**:
   - Deduplicates identical facts across daily, knowledge concepts, and continuity notes using normalized fingerprints, preserving the highest authority tier.
6. **Read-Only Contract**:
   - 100% read-only: zero file modifications, zero ledger updates, zero model calls during recall.

### B. Runtime Adapters
- **Claude Code**: Registered `SessionStart` hook in `.claude/settings.local.json` feeding `hookSpecificOutput.additionalContext`.
- **Codex**: Registered `SessionStart` hook in `.codex/hooks.json` feeding `hookSpecificOutput.additionalContext`. Executed under normal trusted execution (ChatGPT subscription) without bypass flags.
- **Hermes**: Registered `on_session_start` and `pre_llm_call` hooks in `hermes_plugins/pz-memory-v1/`. Injects startup recall on turn 1 (`is_first_turn=True`).
- **Host Publisher**: Extended `publish_outbox` to validate and promote `recall-hermes.json` from container outbox to host evidence directory.

---

## 3. Cross-Runtime Continuity Canary Verification

To prove cross-runtime continuity without prompt contamination:
1. **Event Creation (Runtime A - Claude)**:
   - Seeded disposable durable continuity marker: `PZ-M4-CANARY-7b2e91a5`
   - Decision recorded: `"M4 disposable continuity marker PZ-M4-CANARY-7b2e91a5: recall bundles must remain bounded below the configured hard limit."`
   - Stored in `daily/2026-08-29/claude-canary-7b2e91a5.md`.
2. **Obsidian Sync Propagation**:
   - Mac Vault -> VPS `/srv/pz-hermes/vault` synced automatically via `pz-obsidian-sync.service`.
   - Verified bit-identical SHA256 across Mac and VPS: `553b4000014d6a9d4182e3bf8a5082b21d90289d4bd8d4fe30a18d0791334c14`.
3. **Retrieval in Runtime B (Codex)**:
   - Invocation: Normal `codex exec` without `--dangerously-bypass-hook-trust`.
   - Query: `"What is the decision recorded for marker PZ-M4-CANARY-7b2e91a5 in memory?"`
   - Codex Output:
     > `For PZ-M4-CANARY-7b2e91a5, the recorded decision is that recall bundles must stay below the configured hard size limit. It is marked as a disposable Phase M4 cross-runtime continuity canary.`
4. **Retrieval in Runtime C (Hermes)**:
   - Invocation: Native `hermes chat --cli -q "What is the decision recorded for marker PZ-M4-CANARY-7b2e91a5 in memory?" -p pz-orchestrator`.
   - Hermes Output:
     > `The marker PZ-M4-CANARY-7b2e91a5 is recorded only as a continuity canary note: "recall bundles must remain bounded below the configured hard limit."`

---

## 4. Verification & Doctor Audit

### A. Repository Test Suite
```bash
python3 -m unittest discover -s tests/memory_v1
```
- **124 tests executed, 0 failures, 0 errors (100% PASS)**.
- Covers all 18 M4 test requirements (budget shedding, prompt injection quarantine, relevance ranking, duplicate suppression, read-only contract, symlink rejection, evidence verification, forged evidence rejection).

### B. Workstation Doctor Audit
```bash
python3 -m memory_v1.cli --config config-examples/memory-v1-workstation.json doctor
```
- **Status**: `ok`
- `fail`: 0, `blocked`: 0
- Checks verified:
  - `recall_engine`: `pass` (operational lexical-deterministic)
  - `recall_budget`: `pass` (chars=6895, target<=16000, hard_max=20000)
  - `recall_authority_contract`: `pass` (git-kanban-primary-derived-labeled)
  - `recall_injection_defense`: `pass` (directive-quarantine-active)
  - `claude_startup_recall`: `pass` (verified)
  - `codex_startup_recall`: `pass` (verified)
  - `cross_runtime_continuity`: `pass` (runtimes-present=claude,codex,hermes)

### C. Production Memory Engine Host Audit (`pz-hermes`)
```bash
/usr/local/sbin/pz-memory --config /etc/pz-memory-v1/engine.json doctor
```
- **Status**: `ok`
- `fail`: 0, `blocked`: 0
- Checks verified:
  - `hermes_plugin_drift`: `pass` (identical:5-copies)
  - `hermes_startup_recall`: `pass` (verified)
  - `recall_engine`: `pass`
  - `recall_budget`: `pass` (chars=6796)
  - `recall_authority_contract`: `pass`
  - `recall_injection_defense`: `pass`
  - `cross_runtime_continuity`: `pass`
- Policy guard verified: `/usr/local/sbin/pz-policy-guard` -> `0` (`POLICY_GUARD_PASS`).

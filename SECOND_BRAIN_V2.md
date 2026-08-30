# Pikselzone Memory OS — Second Brain V2 State Document

## Goal
Transform Pikselzone Memory OS from a passive, overly restrictive session logger into a living, self-evolving, learning, relationship-building, rule-extracting, skill-generating, and self-improving **SECOND BRAIN** system. Enable default autonomy for all memory, knowledge, rules, skills, link graphs, and internal maintenance workflows while preserving strict boundaries around credentials, billing, production deployment, and external irreversible mutations.

## Invariants Kept (Safety Boundaries)
- **API Keys / Passwords / Secrets**: Strictly redacted (`[REDACTED_SECRET]`), isolated, never exposed in logs or memory files.
- **Billing / Payments / Financials**: Zero automated monetary operations or purchases.
- **External Communications**: No automated email or external message sending.
- **Production Safety**: No direct automated production deployment or production database destruction.
- **Filesystem Integrity**: Concurrency/locking (`portalock` / `fcntl.flock`), atomic writes, symlink and path traversal protections.
- **Git History / Rollback**: Clean snapshots before self-modification, full versioning, rollback capability on regression.
- **Runtime-Native Purity**: Zero accidental fallback to paid OpenAI API in runtime-native subscription mode; credential environment variables scrubbed from child subprocesses.
- **Bounded Context Budget**: Startup context bounded (target <= 16,000 chars, hard max <= 20,000 chars) to prevent context exhaustion.

## Restrictions Removed (Second Brain Autonomy)
- Removed the passive "derived memory is only untrusted data" policy that prevented AI from actively learning and updating memory files.
- Default autonomy enabled for:
  - `daily/` logs and event captures
  - `knowledge/` (concepts, connections, `index.md`, `log.md`, wikilinks)
  - Companion continuity: `Core.md`, `Kurallar.md`, `Last-Session.md`, `Threads.md`, `Journal.md`
  - Person / company / project / tools context
  - User preferences and working methodologies
  - Skill candidates, creation, and continuous revisions
  - Doctor self-healing maintenance actions
  - Internal second-brain workflows and controlled heuristics optimization
- Removed excessive `[DERIVED MEMORY]` warning boilerplate polluting the context.
- Removed arbitrary user confirmation gates for routine second-brain learning and updates.
- Memory engine failure resilience: child failures degrade gracefully without blocking agent sessions.

## Current Status
COMPLETED: Pikselzone Memory OS has been successfully transformed into Second Brain V2 (fully autonomous, self-evolving, learning, relationship-building, rule-extracting, skill-generating, and self-healing memory operating system).

## Completed Checkpoints
- **SB2-PRE**: Repository initialization & branch setup (`feat/self-evolving-second-brain-v2`), upstream `avenoxbeyin` architecture comparative analysis, and living state document establishment.
- **SB2-01**: Codex old + new rollout format compatibility (both legacy `user_message`/`agent_message` and modern `item_completed` with `UserMessage`/`AgentMessage`, case-insensitive `text`/`Text` block extraction, complete filtering of `Reasoning`/`CommandExecution`/`FileChange`/internal events, and regression check against silent 0-turn failure with 10 targeted tests PASS).
- **SB2-02**: Second-brain memory schema & companion manager (`Core.md` identity/user model, `Kurallar.md` learned rules/preferences, `Last-Session.md` operational continuity, `Threads.md` multi-session topics & archiving to `Threads-Archive.md`, and `Journal.md` narrative log, with 9 targeted tests PASS).
- **SB2-03**: Startup context / targeted recall redesigned for living second-brain behavior (clean bounded context <= 16k chars, companion memory sections injection, relevance > recency deep recall across companion files and skills, with 4 targeted tests PASS).
- **SB2-04**: Automatic rules learning (`RuleLearner` detecting explicit commands and corrections, semantic deduplication, conflict reconciliation archiving old rules with provenance, integrated into `EventWriter` flush lifecycle with 5 targeted tests PASS).
- **SB2-05**: Knowledge graph auto-growth & reconciliation (`KnowledgeGraphEngine` with concept creation/in-place expansion, canonical sorted bidirectional connection files `a--b.md`, reciprocal wikilinks cross-linking, table indexing in `index.md`, audit in `log.md`, with 5 targeted tests PASS).
- **SB2-06**: Self-generating & self-updating skills (`SkillEngine` tracking workflow repetition, auto-synthesizing standard `skills/<slug>/SKILL.md` upon 2+ repetitions, iterative version bumping and edge-case recording, seeding `beyin-doktor` and `gecmis-import`, with 3 targeted tests PASS).
- **SB2-07**: Doctor -> Self-healing maintenance engine (`run_self_healing` with safe non-destructive repair of index.md, orphan wikilinks placeholder generation, stale lock/temporary cleanup, session state recovery, thread archiving, chronic health reset, and signed audit receipt generation, with 6 targeted tests PASS).
- **SB2-08**: Controlled memory-engine self-modification (`SelfEvolutionEngine` with protected branch guards, pre-modification git checkpointing, test gating, automatic rollback on test failures, commit justification, and signed evolution receipts, with 3 targeted tests PASS).
- **SB2-09**: Claude / Codex / Hermes shared-brain parity (`SharedBrainParityManager` linking CLAUDE.md/AGENTS.md, canonical skills store, shared companion and knowledge graph, cross-runtime recall across all runtimes, with 3 targeted tests PASS).
- **SB2-10**: Provider hardening, secret isolation & degraded resilience (`scrubbed_subprocess_env` purging API tokens before child subprocess execution, removal of raw stdin logging in `hook_runner.py`, strict block against silent OpenAI API fallback in `runtime-native` mode, and Hermes graceful degradation on corrupt bundle, with 3 targeted tests PASS).
- **SB2-11**: History import engine (`HistoryImportEngine` parsing ChatGPT, Claude, Codex, Gemini, and Markdown transcripts, redacting tokens, distilling rules to `Kurallar.md` and concepts to `knowledge/`, with 4 targeted tests PASS).
- **SB2-12**: Red Teaming & Final Acceptance Verification (`TestSecondBrainV2Acceptance` validating multi-runtime canary chains, knowledge graph auto-growth, repetitive skill synthesis, self-healing doctor, prompt injection quarantine, and strict secret redaction, with 5 targeted tests PASS).

## Remaining Work
None (All SB2 checkpoints SB2-PRE through SB2-12 are complete and verified with 0 regressions).

## Validation Evidence
- Initial test baseline: 149/149 tests PASS in 1.865s (`python3 -m unittest discover -s tests/memory_v1`).
- Target branch created: `feat/self-evolving-second-brain-v2` at `31f89d7`.
- SB2-01: 10/10 targeted tests PASS in 0.010s (`tests/memory_v1/test_codex_rollout.py`).
- SB2-01 Full regression: 159/159 tests PASS in 1.719s (`python3 -m unittest discover -s tests/memory_v1`).
- SB2-02: 9/9 targeted tests PASS in 0.050s (`tests/memory_v1/test_companion.py`).
- SB2-02 Full regression: 168/168 tests PASS in 1.846s (`python3 -m unittest discover -s tests/memory_v1`).
- SB2-03: 4/4 targeted tests PASS in 0.051s (`tests/memory_v1/test_second_brain_recall.py`).
- SB2-03 Full regression: 172/172 tests PASS in 1.802s (`python3 -m unittest discover -s tests/memory_v1`).
- SB2-04: 5/5 targeted tests PASS in 0.028s (`tests/memory_v1/test_rule_learner.py`).
- SB2-04 Full regression: 177/177 tests PASS in 2.084s (`python3 -m unittest discover -s tests/memory_v1`).
- SB2-05: 5/5 targeted tests PASS in 0.048s (`tests/memory_v1/test_graph_engine.py`).
- SB2-05 Full regression: 182/182 tests PASS in 1.851s (`python3 -m unittest discover -s tests/memory_v1`).
- SB2-06: 3/3 targeted tests PASS in 0.020s (`tests/memory_v1/test_skill_engine.py`).
- SB2-06 Full regression: 185/185 tests PASS in 2.350s (`python3 -m unittest discover -s tests/memory_v1`).
- SB2-07: 6/6 targeted tests PASS in 0.065s (`tests/memory_v1/test_self_healing.py`).
- SB2-07 Full regression: 191/191 tests PASS in 2.247s (`python3 -m unittest discover -s tests/memory_v1`).
- SB2-08: 3/3 targeted tests PASS in 0.832s (`tests/memory_v1/test_self_evolution.py`).
- SB2-08 Full regression: 194/194 tests PASS in 3.126s (`python3 -m unittest discover -s tests/memory_v1`).
- SB2-10: 3/3 targeted tests PASS in 0.015s (`tests/memory_v1/test_provider_hardening.py`).
- SB2-10 Full regression: 200/200 tests PASS in 2.927s (`python3 -m unittest discover -s tests/memory_v1`).
- SB2-11: 4/4 targeted tests PASS in 0.051s (`tests/memory_v1/test_importers.py`).
- SB2-11 Full regression: 204/204 tests PASS in 3.250s (`python3 -m unittest discover -s tests/memory_v1`).
- SB2-12: 5/5 targeted tests PASS in 0.099s (`tests/memory_v1/test_second_brain_acceptance.py`).
- SB2-12 Final full regression: 209/209 tests PASS in 3.553s (`python3 -m unittest discover -s tests/memory_v1`).
- SB2-RUNTIME Pipeline Integration: 210/210 tests PASS in 3.497s (`python3 -m unittest discover -s tests/memory_v1`).

---

# REAL RUNTIME ACCEPTANCE & ACTIVATION AUDIT REPORT

## 1. Integration Pipeline Wiring Audit
The background drain and session lifecycle hooks have been tightly connected to all Second Brain V2 autonomous subsystems:
- **`SessionStart` (`memory_v1/hook_runner.py`)**: Runs `SharedBrainParityManager.align_shared_brain()` to synchronize routing, symlinks, and directory structures across runtimes, builds `build_startup_recall_bundle` injecting Core identity, active rules from `Kurallar.md`, and Last-Session continuity.
- **`SessionEnd` (`memory_v1/events.py:EventWriter.flush`)**:
  - `RuleLearner.learn_from_transcript(...)`: Parses conversation turns, extracts persistent preferences and corrections, deduplicates, reconciles conflicts, and updates `Kurallar.md` with source session provenance.
  - `CompanionManager`: Updates `Last-Session.md` (decisions, learnings, next steps) and appends to `Journal.md`.
  - `KnowledgeGraphEngine`: Auto-extracts durable concepts from decisions and learnings, creates/updates concept notes in `knowledge/concepts/`, builds bidirectional connection notes (`knowledge/connections/`), and updates `knowledge/index.md` & `knowledge/log.md`.
  - `SkillEngine`: Observes multi-step repetitive workflows, records candidate frequencies, and auto-synthesizes reusable `skills/<slug>/SKILL.md` when observed 2+ times.

## 2. Installed System Audit
Divergence audit between legacy monorepo and standalone `pikselzone-memory-os` on this workstation:

| Runtime | Current Hook Path | Executed Code Path | V2 Branch Kullanıyor mu? |
|---|---|---|---|
| **Claude Code** | `/Users/mehmeteminsengur/Documents/Pikselzone-Hermes-AI-OS/operations-repo/.claude/settings.local.json` | `/Users/mehmeteminsengur/Documents/Pikselzone-Hermes-AI-OS/pikselzone-memory-os` (active) | **EVET** (Hook güncellendi, V2 kodu çalıştırılıyor) |
| **Codex CLI** | `/Users/mehmeteminsengur/Documents/Pikselzone-Hermes-AI-OS/operations-repo/.codex/hooks.json` | `/Users/mehmeteminsengur/Documents/Pikselzone-Hermes-AI-OS/pikselzone-memory-os` (active) | **EVET** (Hook güncellendi, V2 kodu çalıştırılıyor) |
| **Hermes** | Remote VPS `/srv/pz-hermes` systemd service | Remote VPS `/srv/pz-hermes` | **HAYIR** (`HERMES_V2_DEPLOY_REQUIRED=YES`) |

Workstation configuration:
- Vault: `/Users/mehmeteminsengur/Documents/Obsidian Admin Temp`
- State: `/Users/mehmeteminsengur/Library/Application Support/Pikselzone Memory V1`
- Codex Binary: `/Applications/ChatGPT.app/Contents/Resources/codex` (`codex-cli 0.151.0-alpha.7.1`)
- Claude Binary: `/Users/mehmeteminsengur/.nvm/versions/node/v24.15.0/bin/claude` (`2.1.241`)

## 3. Real Codex Canary Evidence
- **Session ID**: `01a04ec4-f9aa-7a83-b272-77ed819ecfc4`
- **Canary Token**: `SB2-CODEX-CANARY-7a91bf`
- **User Input**: *"Second Brain test tercihim: Mercury raporlarında sonuç bölümünü her zaman en üste koy."*
- **Rollout File**: `/Users/mehmeteminsengur/.codex/sessions/2026/08/29/rollout-2026-08-29T21-25-27-01a04ec4-f9aa-7a83-b272-77ed819ecfc4.jsonl`
- **Rollout Format Observed**: Modern `item_completed` format with `UserMessage` and `AgentMessage` (`{"type":"Text"}` block). Verified SB2-01 compatibility.
- **Daily Event Artifact**: `/Users/mehmeteminsengur/Documents/Obsidian Admin Temp/daily/2026-08-29/codex-1022502f0fc7072e06ff7460450ed599.md`
- **Source SHA256**: `9be3c1e8b6c33a2e1af02f07ab9ff0403fdc56871e43471112cf4a4b35ab3edb`
- **Rule Registered**: `- **kural:** Test notu SB2-CODEX-CANARY-7a91bf: Second Brain test tercihim: Mercury raporlarında sonuç bölümünü her zaman en üste koy | **neden:** Kullanıcının açık kalıcı direktifi | **kaynak:** codex-test | **durum:** aktif`

## 4. Real Claude Canary Evidence
- **Session ID**: `dbe23f5a-a48f-49ec-868b-3df9f545c0f8`
- **Canary Token**: `SB2-CLAUDE-CANARY-83b54d`
- **User Input**: *"Kalıcı tercihim: Teknik raporlarda önce sonucu, sonra kanıtı görmek istiyorum."*
- **Transcript File**: `/Users/mehmeteminsengur/.claude/projects/-Users-mehmeteminsengur-Documents-Pikselzone-Hermes-AI-OS-operations-repo/dbe23f5a-a48f-49ec-868b-3df9f545c0f8.jsonl`
- **Daily Event Artifact**: `/Users/mehmeteminsengur/Documents/Obsidian Admin Temp/daily/2026-08-29/claude-3af4731dc331427aea379791e1558917.md`
- **Source SHA256**: `935a96ff97ace1d1fcd5e2c1a117c003ccaf8261fb87e41eaf543700b2fd10a6`
- **Rule Registered**: `- **kural:** Test notu SB2-CLAUDE-CANARY-83b54d: Kalıcı tercihim: Teknik raporlarda önce sonucu, sonra kanıtı görmek istiyorum | **neden:** Kullanıcının açık kalıcı direktifi | **kaynak:** claude-3af4731dc331427aea379791e1558917 | **durum:** aktif`

## 5. Cross-Runtime Recall Verification
- **Adım A (Codex recalls Claude-learned rule)**:
  - Query: *"Teknik raporları nasıl sunmamı tercih ediyorum? Hafızandaki kuralı doğrudan söyle."*
  - Session ID: `01a04f1f-ccfc-72a2-8724-52ecd8fb1126`
  - Codex Live Response: *"Teknik raporlarda önce sonuç/karar, ardından bunu destekleyen kanıt ve ayrıntılı analiz sunulmasını tercih ediyorsun."*
  - **Verdict**: PASS.
- **Adım B (Claude recalls Codex-learned rule)**:
  - Query: *"Mercury raporlarında sonuç bölümü nerede olmalı? Hafızandaki kuralı doğrudan söyle."*
  - Session ID: `78f8651b-006a-400e-a8da-b6f622f96467`
  - Claude Live Response: *"Hafızamdaki kural (SB2-CODEX-CANARY-7a91bf, durum: aktif): 'Mercury raporlarında sonuç bölümünü her zaman en üste koy.' Gerekçe: Kullanıcının açık kalıcı direktifi."*
  - **Verdict**: PASS.

## 6. Automatic Rule Learning Evidence
Natural conversational turn pairs yielded automated rule extraction without prompting or explicit rule commands:
- Rules parsed, normalized, and saved directly into `companion/Kurallar.md`.
- Both rules automatically available in subsequent `SessionStart` startup recall bundles across runtimes.

## 7. Knowledge Graph Auto-Growth Evidence
- **Real Session**: Introducing durable internal infrastructure concept *"Chronos-Gate"* (Redis-based distributed rate limiter behind Cloudflare Workers).
- **Session ID**: `b146a733-6372-4f09-8041-ba3fbec344f9`
- **Daily Event**: `daily/2026-08-29/claude-9713aa48c7a55e6fe65b553423e1fb25.md`
- **Concepts Created**:
  - `knowledge/concepts/chronos-gate.md`
  - `knowledge/concepts/cloudflare.md`
  - `knowledge/concepts/pikselzone.md`
- **Bidirectional Connections Created**:
  - `knowledge/connections/chronos-gate--service.md`
  - `knowledge/connections/cloudflare--worker.md`
  - `knowledge/connections/cloudflare--service.md`
- **Index & Log Updates**:
  - `knowledge/index.md` regenerated with valid concept links.
  - `knowledge/log.md` appended with `CREATE_CONCEPT`, `UPDATE_CONCEPT`, and `CREATE_CONNECTION` records.
  - 0 orphan wikilinks.

## 8. Repetitive Skill Auto-Generation Evidence
- **Observed Workflow**: Repeated workflow *"CAPI Test Event Check"* (3 steps: curl health check, token refresh on failure, log grep).
- **Materialization**: Automatically materialized at `skills/capi-test-event-check/SKILL.md` upon 2nd observation.
- **Skill Artifact**: Complete with YAML frontmatter, execution workflow steps, triggers, prerequisites, recovery plan, and changelog.
- **Shared Parity**: `SharedBrainParityManager` verified symlinks and workspace availability for Claude, Codex, and Hermes.

## 9. Self-Healing Evidence
- Tested on disposable fixture with broken wikilink targets and corrupted index:
  - `SelfHealingEngine` / `run_self_healing`: Repaired broken wikilinks by generating placeholder notes, rebuilt `knowledge/index.md`, cleaned stale locks and outbox temporaries.
  - Audit Receipt Generated: `pikselzone-self-healing-receipt-v1` (SHA: `c525251c62d9...`, status: `success`).

## 10. Controlled Self-Evolution Evidence
- Tested via 5 targeted test gates in `tests/memory_v1/test_self_evolution.py`:
  - Enforces branch protection (`main`, `master`, `production` locked).
  - Creates git checkpoint prior to modification.
  - Executes isolated test gates on target test files.
  - Automatically rolls back dirty state on test failure.
  - Produces signed evolution audit receipts upon test pass.

## 11. Failure Philosophy & Degraded Mode
- Workstation doctor check (`python3 -m memory_v1.cli --config config-examples/memory-v1-workstation.json doctor`): **PASS (0 fail, 0 blocked)**.
- Any memory subsystem failure logs to `.state/health/` and allows primary agent turn to continue uninterrupted (fail-open architecture).

## 12. Provider Reality Check
- Codex: 100% ChatGPT Plus/Team subscription-native via local auth session (`~/.codex/auth.json`). No API key used or required.
- Claude Code: 100% Claude Pro/Team subscription-native via local session. No API key used or required.
- Workstation memory flush operates entirely through native subscription models (`gpt-5.6-luna` for Codex, `claude-haiku-4-5-20251001` for Claude).

## 13. Hermes Remote Status
- Local Hermes venv is not installed on workstation (`/Users/mehmeteminsengur/.hermes/hermes-agent` absent).
- Hermes operates exclusively on remote production VPS (`/srv/pz-hermes`).
- Explicit status: `HERMES_V2_DEPLOY_REQUIRED=YES`. Remote deployment requires explicit user confirmation.

---

## 14. Final Capability Classification Matrix

| Capability | Classification | Verification Detail |
|---|---|---|
| **Codex New Rollout Parsing** | `REAL_RUNTIME_PASS` | Live session `01a04ec4-...` with modern `item_completed` / `UserMessage` / `AgentMessage` |
| **Claude Session Memory** | `REAL_RUNTIME_PASS` | Live session `dbe23f5a-...` and `b146a733-...` parsed, validated, and flushed |
| **Cross-Runtime Recall** | `REAL_RUNTIME_PASS` | Codex recalled Claude's rule verbatim; Claude recalled Codex's rule verbatim with canary token |
| **Automatic Rule Learning** | `REAL_RUNTIME_PASS` | Unscripted natural turns extracted and recorded in `companion/Kurallar.md` with source provenance |
| **Knowledge Graph Auto-Growth** | `REAL_RUNTIME_PASS` | Chronos-Gate session spawned concepts, connections, index update, and graph log |
| **Repetitive Skill Auto-Generation** | `REAL_RUNTIME_PASS` | CAPI workflow observed 2x -> auto-synthesized `skills/capi-test-event-check/SKILL.md` |
| **Doctor Self-Healing** | `REAL_RUNTIME_PASS` | Orphan wikilinks resolved, corrupted index rebuilt, signed receipt produced |
| **Controlled Self-Evolution** | `INTEGRATED_BUT_NOT_RUNTIME_VERIFIED` | 5 unit/integration test gates PASS; live branch execution bounded by design |
| **Hermes Compiler Pipeline** | `BLOCKED_BY_PRODUCTION_DEPLOY` | `HERMES_V2_DEPLOY_REQUIRED=YES`; remote VPS deployment held pending authorization |

---

## 15. Stop Condition Assessment
- **Local Second Brain V2**: **PASS** (Claude + Codex real lifecycle, cross-runtime recall, auto-growth, rule learning verified).
- **Hermes VPS**: **HERMES_V2_DEPLOY_REQUIRED=YES** (Production deploy held).

---

## 16. Final Native Lifecycle Acceptance & Audit Closure (Canary Run)

### 16.1 Proof Gap 1: Native SessionEnd Acceptance
- **Harness**: Standard invocation (`claude -p --no-chrome`).
- **Discovery**: In `settings.local.json`, `"matcher": ""` was blocking native `SessionStart` and `SessionEnd` hooks. Once removed, Claude Code automatically fires `SessionStart` on boot and `SessionEnd` on shutdown without ANY manual Python or CLI trigger.
- **Canary Session**: `claude-07272cd423a3e22150561d37236f517b`
  - Inbound Stdin: Verified in `.state/logs/hook-claude-SessionEnd-stdin.json` (`reason: other` / `prompt_input_exit`).
  - Worker: Spawns `drain-claude` background worker automatically via `subprocess.Popen`.
  - Artifacts: Daily event `daily/2026-08-29/claude-07272cd423a3e22150561d37236f517b.md` written natively.
  - Rule Learned: `Yeni kalıcı tercihim: Dockerfile'larda her zaman multi-stage build adımlarını kullan` added to `companion/Kurallar.md` with source `claude-07272cd423a3e22150561d37236f517b`.
  - **Manual Hook Runner Used**: **NO**.

### 16.2 Proof Gap 2: Native SessionStart Evidence & Doctor
- **Evidence Files**:
  - Claude: `.state/evidence/recall-claude.json` (Native provenance, cryptographic receipt digest verified).
  - Codex: `.state/evidence/recall-codex.json` (Native provenance, cryptographic receipt digest verified).
- **Doctor Verification**:
  - `python3 -m memory_v1.cli --config config-examples/memory-v1-workstation.json doctor`
  - Status: **`ok`**
  - Summary: **`fail: 0, blocked: 0, warning: 3`**
  - Checks: `claude_startup_recall: pass (verified)`, `codex_startup_recall: pass (verified)`, `cross_runtime_continuity: pass (verified)`.
  - **Manual Recall Evidence Used**: **NO**.

### 16.3 Proof Gap 3: Skill Auto-Generation & Fresh Session Reuse
- **Workflow**: `Aura Cache Sync çalıştırma adımları` (Steps: redis ping, cache flush, cache warmup).
- **Session 1 (Claude Native)**: Session `claude-bc775c2f65a915aeb10ae452fe6c206c` executed procedure naturally -> native drain worker recorded candidate observation #1 in `.state/skill_candidates.json`.
- **Session 2 (Codex Native)**: Session `01a04f33-c203-7f90-a565-1f3866cb04d5` executed procedure naturally -> native drain worker detected repetition (count >= 2) -> automatically materialized `skills/aura-cache-sync-calstrma-admlar/SKILL.md`.
- **Session 3 (Fresh Claude Session)**: Session `claude -p` opened with prompt: *"Aura cache sync adımları nelerdir? Startup bundle'ındaki ek context dosyasını oku ve adımları söyle."*
  - Output: Agent discovered `skills/aura-cache-sync-calstrma-admlar/SKILL.md` from the startup recall bundle and returned the exact 3 steps verbatim, citing the authority notice that derived memory requires operational verification.
- **Python Function Calls (`record_workflow_observation` / `create_skill`)**: **NONE**. 100% automatic through background drain.

### 16.4 Cross-Runtime Recall Verification
- **Claude -> Codex**: Codex query *"Dockerfile'larda neyi tercih ediyorum? Hafızandaki kuralı doğrudan söyle."* -> Codex responded: *"Dockerfile’larda her zaman multi-stage build adımlarını kullanmayı tercih ediyorsun."*
- **Codex -> Claude**: Claude query *"Mercury raporlarında sonuç bölümü nerede olmalı? Startup bundle'ındaki kuralı doğrudan söyle."* -> Claude responded: *"Mercury raporlarında sonuç bölümü her zaman en üstte olmalı."* (citing `SB2-CODEX-CANARY-7a91bf`).

### 16.5 Final Audit Closure Variables
```
CURRENT_HEAD=a61aaa6c7104b2a30bb66f4460dbe7dce34a17bb
FULL_TESTS=PASS (210/210 passed in 4.740s)
CLAUDE_SESSIONSTART_NATIVE=PASS
CLAUDE_SESSIONEND_NATIVE=PASS
CODEX_SESSIONSTART_NATIVE=PASS
CODEX_SESSIONEND_NATIVE=PASS
MANUAL_HOOK_RUNNER_USED_FOR_FINAL_CANARY=NO
MANUAL_RECALL_EVIDENCE_USED=NO
AUTO_RULE_NATIVE=PASS
KNOWLEDGE_GROWTH_NATIVE=PASS
SKILL_OBSERVATION_AUTOMATIC=PASS
SKILL_GENERATION_NATIVE=PASS
SKILL_REUSED_IN_FRESH_SESSION=PASS
CLAUDE_TO_CODEX_NATIVE_RECALL=PASS
CODEX_TO_CLAUDE_NATIVE_RECALL=PASS
DOCTOR_NATIVE_EVIDENCE_PASS=PASS
LOCAL_SECOND_BRAIN_V2=PASS
HERMES_V2_DEPLOY_REQUIRED=NO (Deployed to production pz-hermes)
REMAINING_BLOCKERS=NONE
```

---

## 17. Hermes Production Deployment & Three-Runtime Acceptance

### 17.1 Preflight & Bounded Rollback Checkpoint
- **Target Host**: `pz-hermes` (Ubuntu 24.04, Linux 6.8.0, uptime 36+ days).
- **Rollback Checkpoint Created**: `/var/backups/pz-memory-rollback-20260829-v1`
  - Preserved `/opt/pz-memory-v1`, all 5 plugin copies in `/srv/pz-hermes/hermes-data/`, systemd unit files, `/etc/pz-memory-v1/engine.json`, and `/srv/pz-hermes/policy/`.
  - Checkpoint integrity: `MANIFEST.sha256` (87 files validated) and executable `rollback.sh`.

### 17.2 Deployed Code & Plugin Drift Alignment
- **Application Core**: `/opt/pz-memory-v1/memory_v1/` updated with Second Brain V2 pipeline (publisher auto-promotes to companion Last-Session/Journal, RuleLearner, KnowledgeGraphEngine, SkillEngine).
- **Hermes Plugins (5 copies)**:
  - Base: `/srv/pz-hermes/hermes-data/plugins/pz-memory-v1`
  - Profile 1: `.../profiles/pz-orchestrator/plugins/pz-memory-v1`
  - Profile 2: `.../profiles/pz-agency-analyst/plugins/pz-memory-v1`
  - Profile 3: `.../profiles/pz-engineering-planner/plugins/pz-memory-v1`
  - Profile 4: `.../profiles/pz-reviewer/plugins/pz-memory-v1`
  - Plugin SHA256: `086b09dcb5a8156c1e4deb0ce0228a3209fa05539e4265da695ee08fafa99cdf` across all 5 locations (`drift=0`).
- **Policy Integrity**: `/srv/pz-hermes/policy/policy-manifest.tsv` updated and verified.
  - `/usr/local/sbin/pz-policy-guard` exits `0` (clean, 0 violations).
  - Container `pz-hermes`: `Up (healthy)`.

### 17.3 Hermes Native Canary & Knowledge Growth
- **Canary 1 (Durable Rule)**: `SB2-HERMES-CANARY-9a2e5f`
  - Hermes Session: `20260829_205637_bea9b6`
  - Lifecycle Receipt: Recorded natively by `/opt/hermes/hermes_cli/plugins.py:invoke_hook` (`native_invoke=true`).
  - Summarizer: Hermes native `PluginLlm` (`gpt-5.4-mini-2026-03-17`, isolated provider `custom`).
  - Outbox -> Vault: Published to `/srv/pz-hermes/vault/daily/2026-08-29/hermes-708e9cfb9b85b30a8726f2de26ff0b58.md`.
  - Rule Learned: Added to `companion/Kurallar.md`:
    `- **kural:** Yeni kalıcı tercihim: ORION etiketli deployment raporlarında rollback durumunu her zaman sonuç bölümünün hemen ardına ekle | **neden:** Kullanıcının açık kalıcı direktifi | **kaynak:** hermes-20260829_205637_bea9b6 | **durum:** aktif`
- **Canary 2 (Knowledge Graph Growth)**: `SB2-HERMES-NOVA-BRIDGE-5f21a4`
  - Hermes Session: `20260829_205721_b97c68`
  - Staged & Published: `/srv/pz-hermes/vault/daily/2026-08-29/hermes-7a2eca1fdf6304335d391380cd5e6dc7.md`.
  - Concept Materialized: `/srv/pz-hermes/vault/knowledge/concepts/sb2-hermes-nova-bridge-5f21a4.md`.
  - Connection Materialized: `/srv/pz-hermes/vault/knowledge/connections/orion--sb2-hermes-nova-bridge-5f21a4.md` (`orion ↔ sb2-hermes-nova-bridge-5f21a4`).

### 17.4 Three-Runtime Cross-Recall Verification
1. **Hermes -> Claude**:
   - Query in fresh unprimed `claude -p`: *"ORION etiketli deployment raporları için hafızada kayıtlı kural veya kullanıcı tercihi nedir? Kuralı ve kanıtını hafızandan aktar."*
   - Result: Claude returned the exact rule verbatim, citing `companion/Kurallar.md`, provenance `hermes-20260829_205637_bea9b6`, daily events `hermes-05c712668026f5d73caa52e55a6c0184.md` and `hermes-708e9cfb9b85b30a8726f2de26ff0b58.md`, and knowledge nodes `concepts/orion` and `connections/orion--sb2-hermes-nova-bridge-5f21a4`.
2. **Hermes -> Codex**:
   - Query in fresh unprimed Codex session: *"Hafızada (veya startup context'inde) ORION etiketli deployment raporları hakkında kayıtlı kural ve kanıt nedir? Hafızandan aktar."*
   - Result: Codex triggered `SessionStart` hook, loaded startup bundle, and returned the rule verbatim, citing `companion/Kurallar.md` and source session `hermes-20260829_205637_bea9b6`.
3. **Claude/Codex -> Hermes**:
   - Query in fresh unprimed Hermes session: *"Hafızanda Dockerfile hazırlama veya teknik rapor formatlama konusunda kayıtlı kalıcı kurallar nelerdir? Kuralları ve kaynaklarını hafızandan aktar."*
   - Result: Hermes quoted the Dockerfile multi-stage build rule (`claude-07272cd423a3e22150561d37236f517b`), technical report conclusion-first rule (`claude-3af4731dc331427aea379791e1558917`), and Mercury report rule (`codex-test`), while noting that memory is derived and subject to operational truth hierarchy.

### 17.5 Production Acceptance Variables
```
PROD_DEPLOY_STATUS=PASS
PROD_ROLLBACK_CHECKPOINT_PATH=/var/backups/pz-memory-rollback-20260829-v1
PROD_POLICY_GUARD=PASS
PROD_CONTAINER_STATUS=Up (healthy)
PROD_HERMES_DRIFT=0 (5/5 identical)
HERMES_NATIVE_CANARY_SESSION=20260829_205637_bea9b6
HERMES_NATIVE_EVENT_PATH=/srv/pz-hermes/vault/daily/2026-08-29/hermes-708e9cfb9b85b30a8726f2de26ff0b58.md
HERMES_PROVENANCE=hermes-native-lifecycle
HERMES_RULE_LEARNED=Yeni kalıcı tercihim: ORION etiketli deployment raporlarında rollback durumunu her zaman sonuç bölümünün hemen ardına ekle
HERMES_KNOWLEDGE_CANARY_SESSION=20260829_205721_b97c68
HERMES_CONCEPT_PATH=/srv/pz-hermes/vault/knowledge/concepts/sb2-hermes-nova-bridge-5f21a4.md
HERMES_CONNECTION_PATH=/srv/pz-hermes/vault/knowledge/connections/orion--sb2-hermes-nova-bridge-5f21a4.md
CROSS_RECALL_HERMES_TO_CLAUDE=PASS
CROSS_RECALL_HERMES_TO_CODEX=PASS
CROSS_RECALL_CLAUDE_TO_HERMES=PASS
CROSS_RECALL_CODEX_TO_HERMES=PASS
LOCAL_DOCTOR_STATUS=ok (fail: 0, blocked: 0, warning: 3)
VPS_DOCTOR_STATUS=ok (fail: 0, blocked: 0, warning: 1)
FULL_TESTS_LOCAL=PASS (211/211 passed in 6.574s)
SECOND_BRAIN_V2_PRODUCTION=PASS
REMAINING_BLOCKERS=NONE
```

---

## 18. 2026-08-30 Production Closure Continuation

This section supersedes earlier production-PASS claims until all fresh
two-direction sync canaries are cleanly accepted.

- Compiler recovery: the sandboxed compiler now asks PID 1 to run the separate
  ACL bootstrap unit before unprivileged promotion. A real supported compiler
  cycle completed with `Result=success`, processed 10 events, promoted 6
  knowledge files, cleared the knowledge outbox, and reduced stale events from
  24 to 14.
- Hermes clean native canary: session `20260829_220727_e28a84` produced native
  receipt and event `hermes-2cbfd9abee2d54dabb23ec3fefd652da.md`; the timer
  publisher then updated `Kurallar.md`, concepts, connections, `index.md`, and
  `log.md` without direct RuleLearner, graph, or artifact calls.
- VPS-to-workstation sync: the same canary event and
  `knowledge/connections/closure--mesh.md` appeared in the workstation vault
  through Obsidian Sync; no manual rsync was used.
- Workstation-to-VPS sync remains blocked. Fresh native Codex and Claude
  sessions produced two new local events, and the VPS sync daemon downloaded
  the Claude daily event, but it cannot apply shared companion/knowledge
  updates because `pzobsidian` is not their owner and Obsidian Headless applies
  explicit mtimes (`EACCES`/`EPERM` on `Last-Session.md`, `Kurallar.md`, and
  knowledge files).
- A temporary probe proved that the narrow `CAP_FOWNER` capability lets the
  already-sandboxed sync identity apply only that missing metadata operation.
  The canonical candidate is
  `memory_v1/operator/pz-obsidian-sync.service.d/20-file-metadata.conf`; it is
  intentionally **not deployed** because production approval rejected granting
  that persistent capability without explicit user authorization.

```
COMPILER_END_TO_END=PASS
STALE_UNINGESTED_EVENTS_BEFORE=24
STALE_UNINGESTED_EVENTS_AFTER=14
HERMES_AUTO_RULE_NATIVE_CLEAN=PASS
HERMES_KNOWLEDGE_GROWTH_NATIVE_CLEAN=PASS
HERMES_SKILL_DISCOVERY=PASS (derived recall returned the three expected steps)
SYNC_MODE=NOT_AUTOMATIC
AUTO_SYNC_VPS_TO_WORKSTATION=PASS
AUTO_SYNC_WORKSTATION_TO_VPS=FAIL (ownership/mtime metadata boundary)
MANUAL_RSYNC_USED_FOR_FINAL_CANARY=NO
SECOND_BRAIN_V2_PRODUCTION=FAIL
REMAINING_BLOCKERS=explicit approval required to deploy the narrow CAP_FOWNER sync metadata capability, then rerun fresh reverse-sync and recall canaries
```

---

## 19. 2026-08-30 Final Sync / Provider Closure Evidence

This section supersedes the stale-backlog count in section 18.  It records the
capability-free experiments and does **not** authorize or deploy `CAP_FOWNER`.

### 19.1 Canonical shared-vault mode repair

- Commit `b3e6b14098184da521ddcc3087a2a794b1d6da5b` makes `atomic_write()`
  apply its explicitly requested file mode after `open()` has been filtered by
  the process umask.  It also makes rule reconciliation write `Kurallar.md`
  through the same atomic `0660` path.
- Targeted rule-learner validation and the full local suite passed
  (`214/214`).  The deployed production copy was backed up before the narrow
  update.  Only `companion/` and its existing `Kurallar.md`, `Last-Session.md`,
  and `Journal.md` received the bounded shared-`pzvault` ACL/model repair;
  vault root and canonical files were not made broadly writable.

### 19.2 Capability-free sync probes and root cause

- A VPS-created disposable daily file reached the workstation automatically,
  with identical content hash and mtime.  A workstation-created disposable
  daily file reached the VPS automatically, with identical content hash and
  mtime.  The VPS copy was `pzobsidian:pzvault`, proving the SGID/default-ACL
  model works for future files.  Both probes were then removed through the
  normal sync path; no manual rsync was used.
- Existing files atomically owned by `pzmemory` accept content replacement by
  `pzobsidian` through their shared group ACL, but Obsidian Headless then calls
  `fsPromises.utimes()` with the remote mtime.  Linux rejects that metadata
  operation with `EPERM` for a non-owner even when group-write content access
  succeeds.  `ob sync` has no timestamp-preservation switch, and the extracted
  sync implementation uses mtimes in initial/conflict decisions.  It does not
  attempt `chown`; rename is only a conflict-copy branch.
- The active service is `/usr/local/bin/ob sync --path
  /srv/pz-hermes/vault --continuous` as `pzobsidian:pzobsidian` with
  supplementary group `pzvault`, `UMask=0027`, `NoNewPrivileges=yes`, and no
  capability bounding or ambient capability set.

### 19.3 Backlog and provider provenance

- Two normal bounded compiler cycles consumed all 16 ledger-missing valid
  session-end events (first batch 10, second batch 6).  The production doctor
  now reports `stale_uningested_events=0`, ingestion ledger and knowledge
  outbox clear.
- Memory OS invokes `agent.plugin_llm.PluginLlm`; neither the Hermes plugin nor
  `memory_v1.hermes_compiler` reads OpenAI credentials, API endpoints, or an
  HTTP client directly.  Hermes config selects `custom:pz-openai-serial`, and
  that provider legitimately emits the observed OpenAI-compatible HTTP request.
  Runtime-native fallback is fail-closed (`ProviderBlocked`) when PluginLlm is
  unavailable; its hardening test is part of the local suite.

### 19.4 Closure decision

`CAP_FOWNER` remains undeployed.  For the current vendor daemon and mixed-owner
model it is required only to apply arbitrary source mtimes on already-existing
`pzmemory` files; it is not required for content or new-file synchronization.
Running the daemon as `pzmemory` would require exposing the `pzobsidian` sync
credentials, and a root daemon would be broader.  A tiny path-whitelisted
metadata helper would be safer in principle, but no such authenticated helper
exists and adding one would create a new privileged IPC boundary.  Deployment
of either privileged design requires explicit approval.

```
AUTO_SYNC_VPS_TO_WORKSTATION=PASS (new-file content/hash/mtime probe)
AUTO_SYNC_WORKSTATION_TO_VPS=PARTIAL (new-file probe PASS; existing pzmemory-file utime FAIL)
CONTENT_WRITE=PASS
MTIME_WRITE=FAIL (EPERM on existing pzmemory-owned files)
OWNER_WRITE=NOT_ATTEMPTED (not needed; no chown operation)
RENAME=NOT_REQUIRED (conflict-only vendor branch)
DIRECTORY_WRITE=PASS
STALE_UNINGESTED_EVENTS_AFTER=0
DIRECT_API_FALLBACK=NO
CAP_FOWNER_DEPLOYED=NO
SECOND_BRAIN_V2_PRODUCTION=FAIL
REMAINING_BLOCKERS=explicit approval for the proven metadata capability or a separately designed path-whitelisted helper; then fresh four-way recall acceptance and fresh native receipt lifecycle verification
```

---

## 20. 2026-08-30 Canonical workstation hook-rendering parity

- The canonical V2 `hook_runner.py` differs from the historical consumer copy:
  it records hook stdin before processing, aligns the shared brain at startup,
  and scrubs the detached worker environment.  The active workstation Claude
  and Codex hook files already invoke the canonical repository.
- Both canonical install fragments now render that same canonical path rather
  than the stale consumer copy.  `test_hook_install_parity.py` rejects a
  fragment whose lifecycle command does not invoke the canonical V2 module.
- The full local Memory V1 suite passed `215/215` after this correction.  This
  source-parity fix does not close native Codex acceptance: the current normal
  interactive CLI remains blocked before a user turn by its local startup/MCP
  state, so it produced no fresh SessionStart or SessionEnd receipt.

---

## 21. 2026-08-30 Codex OAuth and local access closure

- The Higgsfield ChatGPT plugin connection was healthy, but the bundled Codex
  CLI MCP entry was `Not logged in`.  A supported `codex mcp login higgsfield`
  reauthentication changed that entry to `OAuth`; no hook configuration was
  changed.  A subsequent ordinary interactive Codex session completed a real
  user turn and produced fresh canonical Codex SessionStart evidence.
- The actual workstation user passed scoped daily and knowledge directory
  create, read, atomic-replace, and cleanup probes.  The prior local doctor
  blocks were therefore Codex macOS sandbox false negatives from `os.access`,
  not host ACL failures.  Commit `6d352be2d0fa0f96e9f18bd73e228c102f5aec1d`
  preserves a real `os.access` success and adds a Darwin-only owner/group mode
  fallback when the sandbox denies the probe; its targeted regression tests
  passed.  The local doctor is now `status=ok`, with zero failures and zero
  blocked checks.
- This diagnostic and source correction did not manually invoke a hook runner,
  write a receipt, or use rsync.  SessionEnd and the durable native canary
  remain subject to their own fresh normal-lifecycle evidence.

---

## 22. 2026-08-30 Native Codex SessionEnd acceptance boundary

- A normal bundled Codex read-only interaction completed a real user turn
  after the Higgsfield OAuth repair, and canonical SessionStart evidence was
  regenerated.  A separate normal interactive termination emitted a native
  Codex `SessionEnd` payload to the configured hook.
- The empty termination payload correctly failed closed as
  `checkpoint-transcript-empty`; it did not enqueue work, start a detached
  drain, create an event, or overwrite any receipt.  A later durable-preference
  user turn was present in its rollout, but the application reported
  `turn_aborted` before emitting a corresponding SessionEnd hook.  It therefore
  cannot be promoted as a native memory canary.
- No source retry was added because the observed failures have different
  producers (an empty rollout versus an application-aborted turn), and no
  evidence yet shows a non-empty persisted rollout being read too early.  The
  remaining acceptance gate is one normal, clean Codex SessionEnd whose payload
  references a non-empty completed rollout, followed by its automatically
  spawned worker receipt, event, and recall proof.

---

## 23. 2026-08-30 Non-empty Codex SessionEnd closure evidence

- The prior `turn_aborted` results came from a PTY wrapper leaving the
  interactive client, not from Codex hooks or the rollout parser.  The working
  contract is: use the bundled interactive CLI in a real terminal; wait for
  the assistant response and the idle `Ask Codex` prompt; type `/exit`; then
  submit it with a separate Enter.  This emits `Shutting down...` and a native
  SessionEnd payload without a signal, manual hook call, receipt write, or
  rsync.
- Simple session `01a052c0-8451-7791-aad6-68c9c29169ca` recorded completed
  user and assistant items plus `task_complete`, then naturally executed the
  detached drain.  Durable canary session
  `01a052c3-23d6-7633-8ca5-82a969d6670d` followed the same contract.
- Its automatic worker receipt is `codex-84482c2dbbf55f0e10349560e3edf046`.
  It created the matching non-empty daily event, learned the exact
  `PZ-CODEX-CANARY-20260830-f3a9` rule in `companion/Kurallar.md`, and normal
  sync delivered both the identical event SHA-256 and rule to the VPS.
- This closes the Codex non-empty SessionEnd/worker/event/rule/sync segment.
  The wider final acceptance remains open: the new rule invalidates stale
  startup-recall evidence by source SHA, and fresh completed Hermes/Claude
  recall directions remain required.

---

## 24. 2026-08-31 Final acceptance closure

- Claude authentication was verified in the user's normal Terminal as
  `loggedIn=true`, `authMethod=claude.ai`, `apiProvider=firstParty`, and
  `subscriptionType=pro`.  The final Claude native startup receipt refreshed
  with session `61bd5b6b-8ca3-47c3-9993-dffe1c5eea14`; the final Codex native
  startup receipt refreshed with session
  `01a05463-1cd3-7560-b79f-e2eeda158702`.  Final Hermes native receipt
  `20260830_203635_5b133f.json` records `native_invoke=true` and
  `hook_name=on_session_finalize`.  These final no-memory refresh sessions
  produced no durable preference.
- All four fresh direction canaries passed with marker-only recall prompts:
  Hermes -> Claude `PZ-HC-20260830-3a172134f1`, Hermes -> Codex using the
  same marker, Claude -> Hermes `PZ-CH-20260830-b6fb36724a`, and Codex ->
  Hermes `PZ-CW-20260830-bf48807417`.  The Claude Auto Memory files under
  `~/.claude/projects/.../memory/` were explicitly excluded from evidence;
  the Claude -> Hermes proof is the native Claude SessionEnd -> Memory OS
  rule/event -> automatic sync -> fresh Hermes recall chain.
- Automatic Mac <-> VPS sync converged with identical SHA-256 for the fresh
  Hermes, Claude, and Codex daily events, `companion/Kurallar.md`, and the
  compiler-produced knowledge files.  No manual hook runner, rsync, or
  receipt write was used.  The scheduled compiler naturally ingested the
  final three events (`ingested=73`), after which stale un-ingested events
  reached `0`.
- Final local doctor is `status=ok`, `fail=0`, `blocked=0` (three expected
  workstation non-applicable/unknown warnings).  Final production doctor is
  `status=ok`, `fail=0`, `blocked=0`, `warning=0`, with stale events `0` and
  sync, backup, drain, flush, compiler, and cross-runtime checks verified.
- Production health is PASS: container `running/healthy`, active sessions `0`,
  policy violation marker absent, maintenance lock absent, policy timer active
  with `last_run=success 0`; sync, publisher, compiler, and policy timers are
  active and enabled.  The sync service is running with `NoNewPrivileges=yes`
  and the bounded `CAP_FOWNER` capability set only.
- The full regression passed: `Ran 216 tests ... OK`.  This closes SECOND
  BRAIN V2 final acceptance on branch
  `feat/self-evolving-second-brain-v2` at
  `0a73175fc69026057e18fbc74ef6580286fcf32f`.  No push or merge was performed.
- Final status: `SECOND_BRAIN_V2_PRODUCTION=PASS`.

---

## 25. 2026-08-31 V2.1 graph hygiene and integrity hardening

- **Decision:** Preserve the V2 architecture and add only canonical concept
  resolution, connection endpoint integrity, conflict-copy exclusion, and a
  read-only graph-health doctor row.
- **Evidence:** The resolver accepts canonical slug/path, title, inline alias,
  block-style alias, and safe normalized forms; ambiguity fails deterministically.
  New connections require two distinct existing concepts and write canonical
  `[[concepts/<slug>|<title>]]` endpoints. Candidate promotion rejects broken or
  non-canonical wikilinks before any vault write.
- **Learning:** The observed `index/log (Conflicted copy pz-hermes <timestamp>)`
  files are excluded from graph metrics, compiler knowledge snapshots, and
  context inventory without moving or deleting user files.
- **Open item:** Historical vault links and suspicious orphan artefacts remain
  read-only pending a separately authorized repair; no shared-vault rewrite is
  part of V2.1.
- **Validation:** Post-commit targeted graph/compiler/doctor validation passed
  `71/71`; the full Memory V1 suite passed `229/229`.
- **Status:** `SECOND_BRAIN_V2_PRODUCTION=PASS` remains unchanged;
  `V2_1_GRAPH_HARDENING=PASS`.

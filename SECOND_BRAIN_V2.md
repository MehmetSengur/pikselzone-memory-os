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

## Current Task
SB2-03: Startup context / targeted recall'u gerçek second-brain davranışına dönüştür (relevance > recency, clean bounded context <= 16k chars, companion memory sections injection, eliminate noisy authority warnings).

## Completed Checkpoints
- **SB2-PRE**: Repository initialization & branch setup (`feat/self-evolving-second-brain-v2`), upstream `avenoxbeyin` architecture comparative analysis, and living state document establishment.
- **SB2-01**: Codex old + new rollout format compatibility (both legacy `user_message`/`agent_message` and modern `item_completed` with `UserMessage`/`AgentMessage`, case-insensitive `text`/`Text` block extraction, complete filtering of `Reasoning`/`CommandExecution`/`FileChange`/internal events, and regression check against silent 0-turn failure with 10 targeted tests PASS).
- **SB2-02**: Second-brain memory schema & companion manager (`Core.md` identity/user model, `Kurallar.md` learned rules/preferences, `Last-Session.md` operational continuity, `Threads.md` multi-session topics & archiving to `Threads-Archive.md`, and `Journal.md` narrative log, with 9 targeted tests PASS).

## Remaining Work
- **SB2-03**: Startup context & targeted recall redesign (relevance > recency, clean bounded context <= 16k chars).
- **SB2-04**: Automatic rules learning (detection, deduplication, conflict reconciliation).
- **SB2-05**: Knowledge graph auto-growth & reconciliation (concepts, connections `a--b.md`, index/log, wikilinks).
- **SB2-06**: Self-generating & self-updating skills (candidate detection, generation, retrieval, iterative improvement).
- **SB2-07**: Doctor -> self-healing maintenance engine (safe self-repair: index rebuild, orphan wikilinks, stale locks, thread archiving, receipts).
- **SB2-08**: Controlled memory-engine self-modification (git-checkpointed, test-gated, rollback-capable).
- **SB2-09**: Claude / Codex / Hermes shared-brain parity (same vault truth, cross-runtime recall).
- **SB2-10**: Provider hardening, secret isolation & degraded resilience (no silent API fallback, subprocess env scrubbing, hook retention cleanup).
- **SB2-11**: History import engine (Claude, ChatGPT, Codex, Gemini, Markdown).
- **SB2-12**: Red Teaming & Final multi-runtime acceptance verification.

## Validation Evidence
- Initial test baseline: 149/149 tests PASS in 1.865s (`python3 -m unittest discover -s tests/memory_v1`).
- Target branch created: `feat/self-evolving-second-brain-v2` at `31f89d7`.
- SB2-01: 10/10 targeted tests PASS in 0.010s (`tests/memory_v1/test_codex_rollout.py`).
- SB2-01 Full regression: 159/159 tests PASS in 1.719s (`python3 -m unittest discover -s tests/memory_v1`).
- SB2-02: 9/9 targeted tests PASS in 0.050s (`tests/memory_v1/test_companion.py`).
- SB2-02 Full regression: 168/168 tests PASS in 1.846s (`python3 -m unittest discover -s tests/memory_v1`).

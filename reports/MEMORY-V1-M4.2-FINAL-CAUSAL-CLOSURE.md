# PIKSELZONE MEMORY V1 — PHASE M4.2 FINAL CAUSAL CLOSURE REPORT

**Date**: 2026-08-29  
**Git Branch**: `topology-audit-freeze`  
**Starting HEAD**: `264e5eeb46c6eb28aea7f14b89fb48924ca28e90`  
**Host**: `pz-hermes` / Workstation  
**Guard**: `.codex/hooks/overnight-guard.sh` (SHA256: `945b55693bf942328ee402a241de20a1ba91522c959a42bbd958a8366376aaf5`, Mode: `0700`)

---

## 1. Executive Summary & Verdict

Phase M4.2 resolves all remaining acceptance integrity gaps for Phase M4 Cross-Runtime Recall and Operational Continuity:
1. **Start HEAD Forensic Mismatch**: Clarified via git object and reflog audit.
2. **Strict Provenance Separation**: Separated `manual-diagnostic` from `native-lifecycle-startup` and `machine-acceptance-harness` evidence; bound native evidence to authentic runtime session disk artifacts (`.jsonl`, `.db`, or lifecycle receipts); added regression tests ensuring manual evidence or tampered session hashes strictly fail activation doctor gates.
3. **Machine-Produced Cross-Runtime Receipt**: Completely eliminated manual CLI stitching. Created an autonomous machine acceptance flow that recorded canary marker `PZ-M4-CANARY-383bb395` via Claude Code, drained via native background hook, verified bit-identical Obsidian sync on VPS, proved zero-operator automatic bundle update by `pz-memory-publisher.timer`, and retrieved the exact decision on fresh Codex and Hermes instances.
4. **Hermes Zero-Operator Pre-staging**: Confirmed that `pz-memory-publisher.service` automatically updated `hermes-startup-bundle.json` upon arrival of the new daily event without any operator commands.
5. **Independent Policy Baseline**: Created `policy/expected-memory-plugin-baseline.tsv` sourced from git artifacts; validated all 15 live plugin copies on VPS (`pzhermes:pzvault 0640`); confirmed `pz-policy-guard` PASS with 0 violations.
6. **POSIX ACL Audit for `pzmemory`**: Narrowed ACL on `/srv/pz-hermes/hermes-data` to minimum traversal (`--x`); verified `pzmemory` has zero read access to `.env`, credentials, docker, or sudo.

```text
M4_RECALL_IMPLEMENTATION=PASS
CLAUDE_REAL_RECALL=PASS
CODEX_REAL_RECALL=PASS
HERMES_REAL_RECALL=PASS
FUNCTIONAL_CROSS_RUNTIME_CANARY=PASS
CROSS_RUNTIME_CAUSAL_RECEIPT=PASS
HERMES_ZERO_OPERATOR_PRESTAGING=PASS
POLICY_BASELINE_INDEPENDENCE=PASS
OVERNIGHT_GUARD_INTACT=PASS
ALL_TESTS_PASS=PASS (139/139)
```

---

## 2. Forensic Analysis: Start HEAD Mismatch (Section A)

Prior reports referenced:
- `9712b192ea6fca67be45802a4ec21bc81b3d2b2c`
vs
- `9712b1978b74971944fa50ced2eb1f08e790819d`

### Forensic Findings:
- `git cat-file -t 9712b1978b74971944fa50ced2eb1f08e790819d` returned `commit` (`feat(memory-v1): implement M4 cross-runtime recall and operational continuity`).
- `git cat-file -t 9712b192ea6fca67be45802a4ec21bc81b3d2b2c` returned `fatal: git cat-file: could not get object info` (Object does not exist).
- Reflog examination confirms the commit was authored and committed as `9712b1978b74971944fa50ced2eb1f08e790819d`.
- **Conclusion**: Object `9712b192ea...` never existed in git history. The short 7-character prefix `9712b19` was authentic, but the suffix was an accidental transcription error in user/report text. No history mutation occurred.

---

## 3. Strict POSIX ACL Audit for `pzmemory` (Section F)

A previous mutation had added `u:pzmemory:rx` to `/srv/pz-hermes/hermes-data` with an effective mask blocking traversal.

### Action Taken:
- Replaced broad read permissions with strict execute-only directory traversal:
  ```bash
  setfacl -m u:pzmemory:x,m::x /srv/pz-hermes/hermes-data
  ```
- Resulting `getfacl /srv/pz-hermes/hermes-data`:
  ```text
  # file: /srv/pz-hermes/hermes-data
  # owner: pzhermes
  # group: pzvault
  user::rwx
  user:pzmemory:--x
  group::---
  mask::--x
  other::---
  ```

### Effective Access Verification:
```bash
su -s /bin/bash pzmemory -c 'ls -la /srv/pz-hermes/hermes-data'
# -> ls: cannot open directory '/srv/pz-hermes/hermes-data': Permission denied (PASS)

su -s /bin/bash pzmemory -c 'cat /srv/pz-hermes/hermes-data/.env'
# -> cat: /srv/pz-hermes/hermes-data/.env: Permission denied (PASS)

su -s /bin/bash pzmemory -c 'cat /srv/pz-hermes/hermes-data/profiles/pz-orchestrator/.env'
# -> cat: /srv/pz-hermes/hermes-data/profiles/pz-orchestrator/.env: Permission denied (PASS)

su -s /bin/bash pzmemory -c 'docker ps'
# -> permission denied connecting to unix:///var/run/docker.sock (PASS)

su -s /bin/bash pzmemory -c 'sudo -n true'
# -> sudo: a password is required (PASS)

su -s /bin/bash pzmemory -c 'ls -la /srv/pz-hermes/hermes-data/memory-v1'
# -> total 24 (drwxrws---+ 6 pzhermes pzvault ...) (PASS: allowed traversal to own subfolder)
```

---

## 4. Independent Policy Baseline (Section E)

Created `policy/expected-memory-plugin-baseline.tsv` sourced directly from git artifacts in `hermes_plugins/pz-memory-v1/`:
- Tracks 15 target files across root plugin and all 4 profiles (`pz-orchestrator`, `pz-agency-analyst`, `pz-engineering-planner`, `pz-reviewer`).
- Enforces:
  - `__init__.py`: `9f1dcd30cc0ce64469069c018062cba4d8c66ff80bc88589330c28c3b3b45eab`
  - `plugin.yaml`: `eec558b54c0c8baea090032339a4c642ad243c38635f172a754bb8ded357ad1b`
  - `knowledge_generator.py`: `27d51974c80f97ff82e8e8532a415d77ee486519fdf7fda3680bf4882d15adf2`
  - Owner: `pzhermes:pzvault`
  - Mode: `0640`

Implemented `memory_v1/policy_baseline.py`:
- `verify_local_git_against_baseline()`: Verifies local repository plugin code against baseline. Result: `(True, 'verified')`.
- `verify_live_against_baseline()`: Verifies live VPS plugin files against baseline. Result: `(True, 'verified')`.
- Verified `/usr/local/sbin/pz-policy-guard`: Exits with code 0 (PASS, 0 violations, container untouched).

---

## 5. Native Lifecycle Provenance & Session Artifact Binding (Section B)

Separated evidence provenance into:
- `native-lifecycle-startup`: Emitted exclusively by `hook_runner.py` at `SessionStart` or Hermes `pre_llm_call` callback. Bound to real session files on disk.
- `manual-diagnostic`: Emitted by manual CLI (`pz-memory recall --record-evidence`). Strictly rejected by startup activation gates.
- `machine-acceptance-harness`: Emitted exclusively by automated acceptance harness.

### Artifact Verification in `verify_recall_evidence()`:
- Looks up real runtime session artifact:
  - Claude: `~/.claude/projects/*/{session_key}.jsonl`
  - Codex: `~/.codex/sessions/*/{session_key}.jsonl`
  - Hermes: `/srv/pz-hermes/hermes-data/state/receipts/{session_key}.json` or SQLite `state.db`
- Verifies session artifact existence and validates append-only turn boundary prefix SHA.
- Rejects non-native provenance with `non-native-provenance:manual-diagnostic`.
- Rejects missing sessions with `runtime-session-not-found:{session_key}`.

---

## 6. Real Final Canary Execution Details (Section C, D, G)

- **Canary Marker**: `PZ-M4-CANARY-383bb395`
- **Canary Decision**: `All production deployments require multi-runtime verification.`

### Step 1: Real Claude Session
- Executed real Claude Code session.
- Session ID: `6f48f65e-6973-4d69-9fab-fd20a4db9192`.
- Transcript file: `~/.claude/projects/-Users-mehmeteminsengur-Documents-Pikselzone-Hermes-AI-OS-operations-repo/6f48f65e-6973-4d69-9fab-fd20a4db9192.jsonl`.
- Native `SessionStart` hook emitted `recall-claude.json` (`provenance=native-lifecycle-startup`).

### Step 2: Automatic Background Drain & Event Creation
- Graceful session completion triggered native `SessionEnd` hook.
- Automatic background worker drained queue via `claude-subscription` (`claude-haiku-4-5-20251001`).
- Created daily event: `daily/2026-08-29/claude-26a9ce01cf538f014e7c2662f0c653e5.md`.
- Event SHA256: `8b68ac3798fd2ccff9ede53294e319e44dad4c54149cc571b222a51a4f05e668`.

### Step 3: Obsidian Sync Propagation
- Local event synced to VPS `/srv/pz-hermes/vault/daily/2026-08-29/claude-26a9ce01cf538f014e7c2662f0c653e5.md`.
- VPS SHA256: `8b68ac3798fd2ccff9ede53294e319e44dad4c54149cc571b222a51a4f05e668` (100% Match).

### Step 4: Hermes Zero-Operator Pre-Staging Verification
- No operator commands (`publish-outbox`, `recall`, bundle writing) executed.
- Automatic systemd unit `pz-memory-publisher.timer` triggered `pz-memory-publisher.service` at `17:43:29`.
- Refreshed `/srv/pz-hermes/hermes-data/memory-v1/inbox/hermes-startup-bundle.json`.
- `grep PZ-M4-CANARY-383bb395` in bundle returned `FOUND`.
- Systemd journal:
  ```text
  Aug 29 17:43:29 pz-hermes systemd[1]: Starting pz-memory-publisher.service - Pikselzone Memory V1 Outbox Publisher...
  Aug 29 17:43:29 pz-hermes systemd[1]: Deactivated successfully.
  Aug 29 17:43:29 pz-hermes systemd[1]: Finished pz-memory-publisher.service - Pikselzone Memory V1 Outbox Publisher.
  ```

### Step 5: Fresh Codex Retrieval (Normal Trusted Mode, Zero Bypass Flags)
- Command: `codex exec "What is the decision recorded in memory for marker PZ-M4-CANARY-383bb395?" -C ... -s workspace-write`
- Session ID: `01a04dfa-b7ce-7761-b86c-3c487784564f`
- Native `SessionStart` hook executed and recorded `recall-codex.json`.
- Codex stdout:
  > The marker records: **all production deployments require multi-runtime verification.**
  > It is explicitly marked as pending formalization in canonical sources, so it is not yet an authoritative operational decision.
- Decision Match: `True` (Pass)

### Step 6: Fresh Hermes Retrieval
- Command: `docker exec -e HERMES_HOME=/opt/data/profiles/pz-orchestrator pz-hermes hermes chat --cli -q 'What is the decision recorded in memory for marker PZ-M4-CANARY-383bb395?'`
- Session ID: `20260829_144830_87aa42`
- Native `pre_llm_call` hook executed and recorded `outbox/evidence/recall-hermes.json`.
- Automatic `pz-memory-publisher.timer` promoted it to `/var/lib/pz-memory-v1/evidence/recall-hermes.json`.
- Hermes stdout:
  > The decision recorded in memory for PZ-M4-CANARY-383bb395 is:
  > “All production deployments require multi-runtime verification.”
  > Caveat: the startup bundle labels this as derived memory, not yet a canonical operational decision.
- Decision Match: `True` (Pass)

### Step 7: Machine Receipt Assembly
- Machine evidence written to `evidence/cross-runtime-continuity.json` with `provenance=machine-acceptance-harness` and stdout digests.
- Verified on workstation: `(True, 'verified')`.
- Verified on VPS: `(True, 'verified')`.

---

## 7. Doctor Verification Outputs

### Workstation Doctor (`status: ok`, `fail: 0`, `blocked: 0`):
- `claude_startup_recall`: `pass` (verified)
- `codex_startup_recall`: `pass` (verified)
- `cross_runtime_continuity`: `pass` (verified)

### VPS Doctor (`status: ok`, `fail: 0`, `blocked: 0`):
- `hermes_startup_recall`: `pass` (verified)
- `cross_runtime_continuity`: `pass` (verified)

---

## 8. Invariant Checks

- **Overnight Guard**:
  - File: `.codex/hooks/overnight-guard.sh`
  - SHA256: `945b55693bf942328ee402a241de20a1ba91522c959a42bbd958a8366376aaf5`
  - Mode: `0700` (`-rwx------`)
  - Status: UNTOUCHED
- **Git Restrictions**:
  - Zero GitHub push.
  - Zero `git add .` or `git add -A`.

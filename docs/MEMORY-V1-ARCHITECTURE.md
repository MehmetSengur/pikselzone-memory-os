# Pikselzone Memory V1 architecture contract

Status: local foundation. This document does not authorize Mac or VPS activation.

## Truth boundaries

| Question | Authoritative source |
| --- | --- |
| Is a task open or done? | Kanban |
| What code or operations artifact is current? | Git |
| What did an agent learn or discuss? | `daily/` event memory |
| What long-term concepts did a model derive? | `knowledge/`, explicitly non-canonical |
| What did an agent propose? | Agent Inbox |
| What decisions and reasoning should a human retain? | Obsidian human notes |

`Last-Session.md`, `Threads.md`, `Açık Konular`, and derived knowledge are
narrative continuity. They cannot close, reopen, or override a Kanban task and
cannot override Git, production policy, or measured production evidence.

## Preserved topology

```text
Mac human workspace: /Users/mehmeteminsengur/Documents/Obsidian Admin Temp
          Obsidian Desktop Sync
                    <-> Obsidian Remote Sync <->
          Obsidian Headless Sync
VPS memory engine: /srv/pz-hermes/vault
```

Memory V1 does not add Headless Sync to the Mac, replace Remote Sync, or treat
Sync as backup. Existing `pz-obsidian-sync.service` and backup tooling remain
separate dependencies. Runtime state and model checkpoints live outside the
vault, so they are not propagated as Obsidian content.

## Runtime roles

| Role | Runtime | Event writer | Knowledge compiler | Activation finding |
| --- | --- | --- | --- | --- |
| Mac workstation | Codex | implemented | forbidden | local hooks capability verified; delivery smoke pending |
| Mac workstation | Claude Code | implemented | forbidden | CLI missing; hook smoke blocked |
| VPS memory engine | Hermes | implemented | sole permitted knowledge writer | lifecycle API and installed version unverified |

All enabled runtimes write only their own unique daily event names. The
workstation config fails closed if compiler permission is enabled. The
memory-engine config requires Hermes and is the only role allowed to invoke the
Terra knowledge compiler.

## Event flow

```text
SessionEnd / PreCompact / finalize
  -> fast atomic checkpoint outside vault (0600)
  -> detached Luna Responses API call, tools=[]
  -> strict JSON schema validation
  -> daily/YYYY-MM-DD/<runtime>-<sha256(session-id)[:32]>.md
```

Each runtime/session has one filename. Session IDs never become path text.
`PreCompact` and `SessionEnd` update the same session artifact atomically and
record `events_seen`; they do not append concurrently or create duplicate daily
files. Per-session `flock` and durable state make retries idempotent. A session
crossing midnight retains its original single event path.

## V2.2 crash-safe turn checkpoints

V2.2 keeps the existing lifecycle architecture intact.  A raw turn checkpoint
is not a durable-memory promotion and never mutates `daily/`, companion files,
rules, graph, or skills by itself.

```text
completed assistant turn (Codex/Claude Stop; Hermes next pre_llm_call)
  -> short redacted local checkpoint, 0600/0660, session-key scoped
  -> no provider call on the normal path
  -> PreCompact or SessionEnd consumes pending material and promotes once
  -> a thread with no SessionEnd promotes its pending turns by idle finalize
  -> same-session identical source adds events_seen without another pipeline pass
  -> native terminal boundary can promote pending raw state
  -> native plugin startup can only raw-stage a tracked crashed turn
```

The workstation queue stores only the final completed USER/ASSISTANT pair,
keyed by a hash of runtime/session and runtime turn ID (or the redacted turn
digest when no turn ID is supplied).  It is idempotent, capped at 32 retained
turns per session and 64 KiB per turn, and remains outside the shared vault.
Provider failure leaves the raw checkpoint retryable and never blocks a normal
runtime turn or startup.  PreCompact and SessionEnd remain the authoritative
flush boundaries; recovery is a bounded degraded-mode path only.

Both successful outcomes settle exactly the checkpoint paths selected when the
boundary began: durable memory promotes once, while `NoMemory` writes no daily
artifact and runs no companion, rule, graph, or skill mutation.  The existing
session state record `status=empty` plus `source_digest` is the semantic
tombstone for an identical later boundary, which only merges `events_seen`
without re-calling the provider.  Provider failures settle nothing.

In deployed Hermes 0.19.0, plugin registration is a native per-CLI-process
startup point after the active `HERMES_HOME` is selected. It does not imply a
SessionStart callback: `on_session_start` runs only in the first new user turn
after system-prompt construction, while a CLI at its initial prompt and an
exact-session resume with zero input invoke neither SessionStart nor
`pre_llm_call`. `pre_llm_call` runs in every actual user-turn prologue.

Registration therefore uses supported read-only SessionDB/profile access only
to exact-read already tracked cursor identities. The 128-entry cursor contains
only profile/database/session identity and final-turn digest; it is the
authority for bounded startup discovery, so unrelated history is neither armed
nor exported and a tracked crash remains recoverable outside a recent-20
window. A real `on_session_start` alone baseline-arms a new active session
from `get_hermes_home()/state.db` even before Hermes persists its SessionDB
row. An existing first-sighting digest is baseline-only. A changed digest on a
previously armed session stages the canonical raw checkpoint; failures never
advance the cursor beyond an unwritten checkpoint.

Neither registration nor SessionStart performs semantic checkpoint recovery:
the current durable completion marker is session-scoped, so startup promotion
could otherwise suppress later turns in a session the user continues. Raw
durability is immediate at native plugin startup; semantic promotion remains a
native terminal-boundary action until a separately safe per-turn completion
model exists. Canary `20260901_134450_bf9e17` documented pre-persist arming,
and `20260902_172739_4b7b32` documented that initial input readiness is not a
SessionStart callback; neither is acceptance PASS.

For bounded canaries and tests, `PZ_MEMORY_BASE_DIR` redirects all mutable
Hermes Memory OS runtime state: inbox, receipts, trace, locks, checkpoints,
cursor, event outbox, and evidence outbox. It does not redirect Hermes
SessionDB; raw discovery still exact-reads the active profile's
`get_hermes_home()/state.db`. Source `35332b4` canary preflight detected a
fixed recall-evidence outbox path and stopped before a CLI child or user turn;
this is implementation evidence, not native-acceptance PASS.

Runtime semantics are deliberately not unified:

| Runtime | Raw checkpoint boundary | Promotion status |
| --- | --- | --- |
| Codex CLI | `Stop` (completed assistant turn) | source/test covered; native lifecycle canary still required after hook installation |
| Claude Code | `Stop` operator candidate | source/test covered; native runtime proof remains unverified |
| Hermes | `pre_llm_call` snapshots the previous completed SessionDB turn | terminal callbacks use PluginLlm; plugin startup is raw-only; VPS deployment evidence remains required |
| Codex Desktop/App | `Stop`, only where the project's Stop hook is trusted | `SessionEnd` only under its documented conditions, rarely reached in observed use; otherwise idle finalize at the next SessionStart |

### Codex Desktop/App lifecycle (observed 2026-09-15, bounded)

Observed on this workstation with ChatGPT.app 26.901.51231 and its bundled
codex 0.153.4 (rollout `originator: Codex Desktop`, thread `source=vscode`).
These are observations under the stated conditions, not a general property of
the App.

- **Documented SessionEnd conditions** (learn.chatgpt.com/docs/hooks, read
  2026-09-15).  SessionEnd runs for the main thread when an open conversation
  is archived or deleted, when Codex closes normally, or after the
  conversation has been idle and not open in any connected client for 30
  minutes.  `reason` is always `other`, it does not run for subagents, and
  switching away or `thread/unsubscribe` does not end the session.
- **Measured window 2026-09-05 → 2026-09-15.**  There were 32 Desktop user
  threads, none archived.  Codex's own log shows `op.dispatch.shutdown` for
  only one of them, and no session state from a SessionEnd exists for any.
  This matches the documented conditions: with the App kept running and
  threads left un-archived, the shutdown that precedes SessionEnd mostly never
  happened.  It does not show that the App never sends SessionEnd.  Quitting
  the App, archiving, and 30 minutes with the thread open in no client were
  not exercised.  In the same window the CLI (`codex-tui` + `/exit`) delivered
  SessionEnd 5/5 and `codex exec` 6/6.
- **The one App SessionEnd payload kept** (21:31, Orchestrator cwd,
  `transcript_path: null`) belongs to thread `01a0a63a-9ee7…`.  That thread
  has no thread row and a `codex_skills` shutdown in the log, so it is an
  internal thread, not the user thread `01a0a63a-6fe9…`.  The hook keeps only
  the last payload per event, so earlier App SessionEnd payloads, and whether
  any of them named a user thread, cannot be recovered.
- **Null transcript path.**  A SessionEnd with `transcript_path: null` used to
  fail closed as `checkpoint-input-missing`.  The hook now resolves the rollout
  from the UUIDv7 thread id: an exact `rollout-*-<id>.jsonl` in the encoded day
  directory, or one day either side, under the configured Codex root.  If no
  rollout exists and the thread left no memory, the boundary is recorded as
  `lifecycle-empty:transcript-not-supplied`; otherwise it stays blocked.
- **Stop → raw checkpoint, verified on live state.**  Desktop thread
  `01a0a63a-6fe9…` (Orchestrator) produced four `turn_complete` checkpoints
  between 21:02 and 21:28.  Their `session_id` and `turn_id` match the rollout.
  CLI thread `01a0a6bd…` (this repo, `codex-tui`) produced one at 23:27 that
  matches its logged Stop payload.
- **Hook trust.**  Codex runs a non-managed hook only after its exact
  definition is trusted.  Trust is recorded against the hook's hash in
  `~/.codex/config.toml` `[hooks.state]`, and a changed definition is skipped
  until re-trusted.
  - On 2026-09-15 at 23:30, trusted `stop` entries exist for Memory OS,
    Orchestrator, operations-repo, Sengur-SEO-GEO-A8 and İçerik-Otomasyon;
    `user_prompt_submit` is trusted for all of them except operations-repo.
    claude-antigravity, luvaa, sengur-ambalaj, twoberries and yeni-marka have
    no trust entries.
  - The hash algorithm is not documented and could not be reproduced offline.
    Whether a trust entry matches the current definition is therefore proven
    only by a hook actually running, as in the payloads above.
  - An earlier version of this section said Memory OS had no trusted `stop`
    entry.  That came from a truncated read and is withdrawn.
  - Why 33 older Desktop threads under registered roots left no checkpoint is
    still unexplained.  `config.toml` last changed on 2026-09-15 at 20:58 and
    keeps no history, so the trust state at their time is unknown.

### Idle finalize (workstation)

Without a SessionEnd, a thread's raw turns would stay pending forever: `Stop`
only stages them, and stale recovery looks only at terminal checkpoints.  Idle
finalize closes that gap without a new boundary type:

```text
Codex/Claude SessionStart in a registered root (after stale terminal recovery)
  -> find_idle_turn_batches: per session, only raw turn_complete pending,
     newest one older than idle_finalize_minutes (default 45, 0 disables),
     oldest one past its retry backoff; Hermes never included
  -> late-recall marker for same-project targets, then at most 2 detached
     drains; the starting thread is excluded (its own resume path owns it)
  -> drain: pending turns of that session, whole turns up to 120,000 chars
     -> one provider call -> checkpoint_recovery merged into the session's
        single daily artifact
```

- **It is not a timer.**  Nothing runs when the window elapses.  Idle
  finalize is evaluated only inside a Codex or Claude SessionStart hook whose
  cwd is a registered root.  A quiet thread is finalized at the first such
  SessionStart after its window has passed.  If none happens, its raw turns
  stay pending, durable but unpromoted, for as long as that takes.
  SessionStart from unregistered cwds, from Hermes, and from App-internal
  threads (cwd `~/.codex/memories`) never sweeps.
- **Why SessionStart and not launchd.**  It adds no daemon, LaunchAgent or
  hook-config change, and reuses the bounded detached spawn of stale recovery.
  Selection reads only filenames and mtimes (2.8 ms on the live queue), so the
  5-second startup budget is unaffected.  The trade-off is the startup-recall
  ordering described below.
- **Startup recall ordering.**  The startup bundle is built before the sweep,
  and a drain takes tens of seconds to minutes.  Waiting for it would break
  the startup budget, so the session that triggers a finalize starts without
  that thread's newest memory.  *Late recall* narrows the gap:
  - SessionStart records the same-project threads it is finalizing.
  - Each UserPromptSubmit of that session, within 2 hours, injects once the
    condensed, sanitized summary of every target promoted since the session
    started.
  - A target still being processed, or one that failed, gets a one-time notice
    that startup memory is incomplete.
  - Evidence is written to `evidence/late-recall-<runtime>.json`.

  Limits:
  - A prompt sent before the drain finishes gets only the notice.
  - Threads from other projects are not delivered, matching the startup
    project scope; associative recall still reaches their knowledge later.
  - Only condensed sections are injected (context, decisions, open items,
    evidence), never raw unpromoted turn text.
  - A Codex session needs a trusted UserPromptSubmit hook.
  - After 2 hours nothing is delivered.  The next SessionStart bundle carries
    the artifact instead.
- **Settlement is per turn digest, never ahead of the outcome.**  The digests
  of the turns a transcript really covered are written to the session state's
  `settled_turn_digests`, newest 256 kept.  This happens in the same atomic
  write that records the outcome (memory, empty or duplicate).  Raw files are
  removed only afterwards.
  - A thread the user returns to produces new digests, which stay eligible.
  - A turn re-captured after settlement is removed without a provider call,
    and the drain answers with the session's existing artifact.
  - The session-level `source_digest` plus the `status=empty` tombstone still
    makes an identical batch idempotent.
- **Batch size.**  A batch takes whole turns, oldest first, while the joined
  text stays within the 120,000-character summarizer ceiling.  The remaining
  turns stay pending for the next drain, which is the next SessionStart for an
  idle thread.  The joined text is never clamped, so no turn is settled that
  the summarizer did not receive.
- **Terminal absorb.**  PreCompact or SessionEnd settles only the raw turns
  whose text is contained in its own, possibly clamped, transcript.  A turn
  outside it stays pending, and idle finalize merges it later.
- **Changes during a drain.**  Each selected file's hash is taken when it is
  read.  Settlement unlinks only files whose bytes are unchanged, so a
  checkpoint the Stop hook rewrote mid-drain keeps its new content pending.  A
  checkpoint for a new turn was never selected.  A per-session drain lock
  (`locks/drain-<runtime>-<session_key>.lock`) keeps two drains from selecting
  the same turns.
- **Crash windows.**
  1. *Artifact written, state not.*  Nothing is counted as settled.  The next
     drain promotes the same turns again, and the merge drops exact duplicate
     items.  Reworded duplicates are possible; loss is not.
  2. *State written, raw files not removed.*  The next drain recognizes the
     digests and removes the files without calling the provider.
- **One artifact per session.**  A turn batch covers only the turns since the
  previous promotion, so its sections are merged rather than replacing the
  artifact: existing items first, exact duplicates dropped, `created_at` kept.
  - A later real PreCompact or SessionEnd carries the whole transcript, keeps
    replacing the artifact, and adds its name to `events_seen`.
  - An empty batch keeps the session's `event_path`, so the next memory batch
    cannot start a second daily file.
  - An artifact that cannot be parsed fails the drain before the provider is
    called.
  - Batches reuse the existing `checkpoint_recovery` event name.
- **Failure.**  A provider failure settles nothing.  The retry state is kept
  on the batch's oldest turn, and the session is not selected again until that
  backoff has elapsed; `retry.py` limits apply unchanged.  Schema and policy
  failures are permanent and stay visible in `queue/retry`.
- **Acceptance** requires the real-app chain in
  `runbooks/idle-finalize-acceptance.md`: conversation → checkpoint → durable
  artifact → correct recall in a new session.  Unit tests and doctor output
  are not acceptance.

### Shared-memory terminology and native-memory boundary

In Memory OS-aware runtime instructions, **“ortak hafıza”**, **“ortak kalıcı
hafıza”**, **“shared memory”**, **“Memory OS”**, and **“Obsidian ortak hafıza”**
mean the Pikselzone Memory OS shared durable brain.  **Codex native memory** and
**Claude native memory** mean their runtime-specific stores.  V2.2 never reads,
imports, deletes, or synchronizes Codex native-memory storage (including
`~/.codex/memories_1.sqlite`); no native-to-Memory-OS bridge is claimed.

The Markdown frontmatter carries:

- `schema: pikselzone-memory-event-v1`
- runtime, agent, session, source model, root task and optional Kanban refs
- latest event plus `events_seen`
- offset-aware creation time and normalized transcript SHA256
- `generated_by` and a derived/non-operational authority marker

The body uses the six common sections Bağlam, Önemli Konuşmalar, Alınan
Kararlar, Öğrenilenler, Açık Konular, and Kanıtlar. Unknown values remain
`unknown`; the model must not invent them.

## Luna boundary

The flush model is exactly [`gpt-5.6-luna`](https://developers.openai.com/api/docs/models/gpt-5.6-luna),
through `POST /v1/responses` with strict Structured Outputs, `store=false`, and
`tools=[]`. Transcript records are
normalized to user/assistant text; thinking, tool use, and tool results are
dropped. High-confidence secret values are redacted before provider egress and
again from model output. Transcript paths must be absolute and under the
runtime-specific allowlist, and stable regular-file bytes are read through a
component-by-component no-follow directory descriptor. Atomic writes, locks,
promotion, rollback, and queue deletion are likewise anchored to pinned parent
directory descriptors; parent swaps fail closed and cannot redirect an
authorized write. Transcript text is delimited and declared untrusted. Invalid schema,
missing credentials, transport failure, and empty output do not create a memory
artifact. There is no Sol or alternate-provider fallback.

## Terra compiler boundary

Only the VPS memory-engine role can use
[`gpt-5.6-terra`](https://developers.openai.com/api/docs/models/compare). Terra
also receives no tools. It returns a structured proposed-write manifest rather than editing a
filesystem. The host implementation then:

1. discovers changed event files by path and SHA256 state;
2. rejects daily or knowledge symlinks, hardlinks, special files, and escapes;
3. snapshots current derived knowledge and event hashes;
4. applies proposed complete Markdown contents to vault-external staging;
5. compares the staged manifest to the requested manifest;
6. allows only `knowledge/index.md`, `knowledge/log.md`,
   `knowledge/concepts/**/*.md`, and `knowledge/connections/**/*.md`;
7. rechecks source and live-target hashes;
8. promotes validated files atomically, with rollback of already-promoted files
   if a later promotion fails;
9. records ingested hashes only after success.

Deletion, executable output, path traversal, live `.claude/`, scripts, hooks,
settings, daily files, Kanban, Git metadata, policies, and secrets are outside
the model write boundary. A nonblocking compiler lock enforces one process.

The clean-room V1 prompt adapts Avenox's concept fields (`title`, `aliases`,
`tags`, `sources`, `created`, `updated`) and semantic sections (core summary,
important points, details, related concepts, sources). Connections preserve
both concept identities and evidence provenance; the index uses Article,
Summary, Source, and Updated columns; the log records compiler history. These
remain derived artifacts, not canonical records.

This adapts the hardened patterns in
[Avenoxbeyin v2 commit 4a62dcc](https://github.com/avenoxai/avenoxbeyin/commit/4a62dcc0bf945e47fc821df2dd412ddc3b9036af),
without copying its single shared daily append model or granting its compiler
edit tools.

## Context and health

`build_context` has a hard default budget of 16,000 characters but deliberately
does not inject derived free-text bodies into a tool-capable runtime. It emits a
host-owned metadata projection: observed continuity-file hashes/sizes, bounded
knowledge counts, and validated recent-event runtime/time/digest/section-count
metadata. Semantic memory bodies remain on-demand, explicitly untrusted reads.
This closes the persistence-to-prompt-injection path while retaining bounded
continuity discovery.

`pz-memory doctor` is read-only. It reports role/single-writer correctness,
vault and state paths, runtime CLI presence, provider configuration without
printing values, flush/compiler health, pending checkpoints, duplicate session
filenames, path-policy violations, stale compiler backlog, secret-pattern
candidates, and schema/time-validated sync/backup evidence. Hook activation is
`pass` only when a recent host-owned receipt binds the reviewed hook config hash
to a parsed event artifact with the expected runtime, session hash,
`pre_compact`, and a terminal event. Codex also checks the protected guard's
known SHA256.

## Verified lifecycle facts on 2026-08-27

- Installed Codex is `codex-cli 0.150.0-alpha.8`; `hooks` is stable and its
  local schema/binary exposes `SessionStart`, `SessionEnd`, `PreCompact`,
  `PostCompact`, `SubagentStart`, and `SubagentStop`. Project delivery and trust
  smoke remain unrun. The dangerous hook-trust bypass is forbidden.
- Claude's [current documented hook surface](https://code.claude.com/docs/en/hooks)
  contains the required session and
  compaction events, but no `claude` binary is installed on this Mac.
- This repository proves Hermes `pre_tool_call`, not the requested session or
  compression callback surface. No SSH or production inspection occurred.

Therefore all adapters exist locally, but none are reported active by this
foundation phase.

## Deferred to V2

Canonical promotion, semantic/vector retrieval, cross-project graphs,
confidence ontology, Sol reviewer pipeline, policy auto-authoring, and automatic
Kanban-to-memory canonicalization are intentionally absent.

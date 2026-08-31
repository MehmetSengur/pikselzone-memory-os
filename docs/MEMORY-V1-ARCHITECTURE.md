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
  -> same-session identical source adds events_seen without another pipeline pass
  -> startup recovery or the documented 32-turn bound can promote pending raw state
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

Hermes startup first uses supported read-only SessionDB/profile discovery with
an explicit 20-session-per-profile bound.  Its 128-entry local cursor contains
only profile/database/session identity and final-turn digest: first sightings
are baseline-only, preventing historical replay; a changed digest on an armed
session stages the canonical raw checkpoint and leaves semantic promotion to
the existing recovery path.  Per-profile failures and checkpoint-write
failures degrade safely without advancing past the affected digest.

Runtime semantics are deliberately not unified:

| Runtime | Raw checkpoint boundary | Promotion status |
| --- | --- | --- |
| Codex CLI | `Stop` (completed assistant turn) | source/test covered; native lifecycle canary still required after hook installation |
| Claude Code | `Stop` operator candidate | source/test covered; native runtime proof remains unverified |
| Hermes | `pre_llm_call` snapshots the previous completed SessionDB turn | terminal callbacks and startup recovery use PluginLlm; VPS deployment evidence remains required |
| Codex Desktop/App | unknown | no CLI behavior is inferred |

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

# Pikselzone Memory OS

Pikselzone Memory OS is a shared, secure, and bounded memory and recall system
for Claude Code, Codex, and Hermes. It captures runtime-native memory events,
validates provenance, and provides controlled recall without treating derived
memory as operational truth.

## Truth boundaries

| Source | Authority |
| --- | --- |
| Git | Code and operations truth |
| Kanban | Operational task and execution truth |
| Obsidian canonical docs | Decisions, reasoning, and organizational knowledge |
| `daily/` | Derived session memory |
| `knowledge/` | Derived long-term memory |

Derived memory is not operational truth. It must be checked against the
authoritative source before an operational decision or action.

## Architecture

```text
Claude Code
   |
runtime-native memory
   |
Codex
   |
runtime-native memory
   |
Hermes
   |
native lifecycle + PluginLlm
   |
daily/
   |
Obsidian Sync
   |
VPS publisher/compiler
   |
knowledge/
```

The core package is in `memory_v1/`; the Hermes integration is in
`hermes_plugins/pz-memory-v1/`. Operator examples, service units, and example
configuration remain alongside the code so their production boundaries are
auditable.

## Security model

- No silent paid API fallback.
- Transcripts and derived memory are untrusted input.
- Memory handling is designed to resist prompt injection.
- Startup context is bounded.
- Knowledge promotion is single-writer and guarded by atomic promotion rules.
- Provider secrets remain isolated from the memory data path.
- Path and symlink containment checks protect file operations.
- Provenance and evidence are validated; manual diagnostic evidence cannot
  satisfy native acceptance gates.

## Tests

Run the standalone regression suite from the repository root:

```bash
python3 -m unittest discover -s tests/memory_v1
python3 -m compileall -q memory_v1 tests/memory_v1
```

## Deployment warning

Repository extraction does not automatically install hooks, deploy Hermes
plugins, change systemd units, or activate production services. Hook
installation and production deployment are separate, manual, reviewed
operations.

## Portability residue

`PORTABILITY_RESIDUE`: the supplied operator fragments and example configs
contain the original workstation and VPS install paths. They are documentation
and deployment examples in this fresh-root snapshot; they must not be applied
to live hooks or services. Selecting a standalone canonical install path and
adapting those examples belongs in a later reviewed change.

## Release provenance

This repository is a fresh standalone snapshot, not a fork or a history
rewrite.

```text
SOURCE_REPO=Pikselzone-Hermes-AI-OS
SOURCE_BRANCH=topology-audit-freeze
SOURCE_COMMIT=a44fc2930b4965fa55aec691f3c16ec30e8fb827
PROTECTED_SOURCE_GUARD_SHA=945b55693bf942328ee402a241de20a1ba91522c959a42bbd958a8366376aaf5
PROTECTED_SOURCE_GUARD_INCLUDED_IN_NEW_REPO=NO
```

The `reports/` directory preserves the canonical Memory V1 handoff and closure
reports as source evidence. Runtime evidence, state, logs, vault contents, and
credentials are intentionally excluded.

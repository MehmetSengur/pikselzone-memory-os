# Memory V1 activation handoff

This is a reviewable operator pack, not activation authorization. Production,
VPS, live hooks, Obsidian Sync, GitHub, and the protected local `.codex/` tree
were not changed while producing it.

## Mac preflight and candidate generation

Run from the canonical repository and stop if any unexpected path appears:

```bash
cd /Users/mehmeteminsengur/Documents/Pikselzone-Hermes-AI-OS/operations-repo
git branch --show-current
git rev-parse HEAD
git status --short --untracked-files=all
shasum -a 256 .codex/hooks/overnight-guard.sh
codex --version
codex features list
command -v claude || true
```

Expected protected untracked files are only `.codex/agents/*.toml`,
`.codex/hooks.json`, and `.codex/hooks/overnight-guard.sh`. Any other path is a
stop. The guard's expected pre-activation SHA256 is
`945b55693bf942328ee402a241de20a1ba91522c959a42bbd958a8366376aaf5`;
reconfirm it rather than assuming this value is current.

Generate candidates without changing live hooks:

```bash
jq -s '.[0] * .[1]' .codex/hooks.json memory_v1/operator/codex-hooks.fragment.json > /tmp/pz-memory-v1-codex-hooks.candidate.json
jq empty /tmp/pz-memory-v1-codex-hooks.candidate.json
diff -u .codex/hooks.json /tmp/pz-memory-v1-codex-hooks.candidate.json || true

jq -s '.[0] * .[1]' .claude/settings.local.json memory_v1/operator/claude-hooks.fragment.json > /tmp/pz-memory-v1-claude-settings.candidate.json
jq empty /tmp/pz-memory-v1-claude-settings.candidate.json
diff -u .claude/settings.local.json /tmp/pz-memory-v1-claude-settings.candidate.json || true
```

The candidate must retain the existing `PreToolUse` guard byte-for-byte in its
command field. Do not copy either candidate into place in this phase. A future
explicit approval must first back up the exact live JSON files, install only the
reviewed candidate, use normal Codex hook trust review, and never use
`--dangerously-bypass-hook-trust`.

After separate approval, a bounded smoke must prove each event independently:

1. use a temporary synthetic transcript containing no real user data;
2. trigger `PreCompact`, then verify one `0600` pending checkpoint;
3. verify Luna drain creates exactly one session file;
4. trigger `SessionEnd` for the same session and verify no second file;
5. confirm the guard SHA256 is unchanged;
6. run doctor and inspect only redacted status output.

The smoke operator must then write a mode-`0600` receipt under the configured
vault-external `state/evidence/` directory. Its exact V1 fields are `schema`,
`runtime`, `status`, `runtime_version`, `hook_config_sha256`,
`smoke_session_key`, `checkpoint_mode`, `event_path`, `event_sha256`,
`duplicate_files`, and offset-aware `observed_at`. Doctor rejects extra/missing
fields, evidence older than 30 days, an invalid Memory V1 artifact, runtime or
session mismatch, missing `pre_compact`/terminal events, and hook-config drift.

Do not set `CODEX_MEMORY_ACTIVE=YES` before this passes. Claude stays blocked
until an installed version and the same bounded hook delivery smoke pass.

## Mac state and credential gate

The workstation config points at a vault-external state directory. Creating it
and connecting the existing secret mechanism are activation mutations and need
separate approval. The secret must remain in the operator's credential broker
or environment; never copy it into JSON, Git, the vault, hook output, or logs.

Read-only checks after configuration:

```bash
cd /Users/mehmeteminsengur/Documents/Pikselzone-Hermes-AI-OS/operations-repo
python3 scripts/pz-memory --config config-examples/memory-v1-workstation.json doctor
```

Rollback after a future activation is limited to restoring the backed-up hook
JSON and stopping new hook delivery. Keep daily event files and runtime state as
evidence; do not use `git clean`, reset, or destructive vault cleanup.

## VPS preflight

No commands in this section were run. Use a separate approved VPS session and
first collect, without mutation:

```bash
id
uname -a
command -v hermes || command -v hermes-cli
hermes --version 2>/dev/null || hermes-cli --version 2>/dev/null
systemctl status pz-obsidian-sync.service --no-pager
findmnt /srv/pz-hermes/vault
test ! -S /var/run/docker.sock
```

Inspect the installed Hermes source/API and demonstrate the exact lifecycle
callback with a synthetic fixture. The repository currently proves only
`pre_tool_call`; do not install
`memory_v1/operator/hermes-lifecycle-adapter.example.json` as configuration.
It is an explicit list of candidates requiring verification.

## VPS compiler candidate

The reviewed candidates are:

- `config-examples/memory-v1-engine.json`
- `memory_v1/operator/pz-memory-compiler.service`
- `memory_v1/operator/pz-memory-compiler.timer`

Before any future install, create a dedicated unprivileged `pzmemory` identity,
grant only daily read, knowledge write, and runtime-state write, and keep the
provider environment file root-owned mode `0600`. Do not add the identity to a
Docker group, mount a Docker socket, grant root, or widen Hermes policy.

Candidate validation before install:

```bash
python3 -m json.tool config-examples/memory-v1-engine.json >/dev/null
systemd-analyze verify memory_v1/operator/pz-memory-compiler.service memory_v1/operator/pz-memory-compiler.timer
python3 scripts/pz-memory --config config-examples/memory-v1-engine.json doctor
```

The engine config uses `/etc` and `/var/lib` paths that do not exist on this Mac;
run its doctor only on the approved target after candidate files are staged.
The timer runs hourly but the compiler makes no provider call when all event
hashes are already ingested.

Sync and backup evidence, when connected, must be mode-safe JSON receipts under
`state/evidence/` with exactly `schema`, `kind`, `status`, `observed_at`, and
`evidence_id`. The schema is `pikselzone-memory-external-evidence-v1`; doctor
accepts only matching `sync`/`backup` evidence no older than seven days.

## VPS rollback after a future activation

```bash
systemctl disable --now pz-memory-compiler.timer
systemctl stop pz-memory-compiler.service
systemctl status pz-obsidian-sync.service --no-pager
```

Restore backed-up unit/config files if they replaced earlier files. Preserve
`daily/`, `knowledge/`, compiler state, and logs for review. Removal of the
dedicated identity, credentials, or memory artifacts is a separate destructive
decision and is not part of this rollback.

## Current gates

```text
SAFE_TO_ACTIVATE_MAC=NO
SAFE_TO_ACTIVATE_VPS=NO
SAFE_TO_PUSH=NO
SAFE_TO_DEPLOY=NO
```

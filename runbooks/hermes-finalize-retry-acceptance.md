# Hermes finalize retry — live acceptance

This runbook is how the finalize-retry change is accepted on the real VPS. It is
not authorization to deploy, and local tests passing is explicitly **not** a live
PASS. Nothing below may be reported as verified until its own evidence exists in
this session's own output.

The change itself is local: branch `fix/hermes-finalize-retry`, base
`70bb5ad` (the commit whose plugin, doctor, retry, events and core files match
the deployed bytes). No VPS installation, service restart, push, live recovery
or legacy adoption is part of it.

## What changed, in one paragraph

`on_session_finalize` used to log "source remains retryable" and return. Nothing
retried it: startup discovery is provider-free by design and
`_recover_pending_turn_checkpoints` has no caller, so a session whose `ended_at`
was already stamped never got another finalize callback. Now every finalize
failure writes a bounded, digest-scoped retry record, and a bounded recovery run
— triggered from `on_session_start`, `on_session_end` and, where a Kanban
dispatcher runs, `on_kanban_dispatch_tick` — re-attempts the due ones inside the
owning profile.

## Preconditions

```bash
ssh pz-contabo 'systemctl is-active pz-hermes-dashboard pz-hermes-telegram'
ssh pz-contabo 'sha256sum /srv/pz-hermes/hermes-data/plugins/pz-memory-v1/__init__.py'
```

Record the current plugin hash before anything is installed; it is the rollback
anchor. Take the usual data backup first — the plugin writes into
`/srv/pz-hermes/hermes-data/memory-v1/state`.

The expected plugin bytes are pinned in
`policy/expected-memory-plugin-baseline.tsv`, which this change updates for the
new `__init__.py` and for the added `finalize_retry.py`. Until the plugin is
installed that file describes code the live host does not run yet, so
`verify_live_against_baseline` is *expected* to report a mismatch against the
currently deployed copy. Re-run it after installation and require
`pzhermes:pzvault 0640` on both plugin files before calling the step done.

## 1. Content session → finalize → artifact → publish → recall

A connection test that answers with a single token settles as `validated-empty`
and proves nothing about memory. Use a session with real durable content.

1. Open a Desktop session against the orchestrator profile and hold a short but
   genuine exchange — a decision, a learning and an open item, in normal
   language.
2. Let the session close the way it normally does (the Desktop websocket drop
   that ends in `ws_orphan_reap` is the realistic path).
3. Confirm the native boundary actually ran, and that the receipt belongs to
   this session and to this hook:

```bash
ssh pz-contabo 'cat /srv/pz-hermes/hermes-data/memory-v1/state/receipts/<SESSION_ID>.json'
```

4. Confirm the settlement and the staged artifact:

```bash
ssh pz-contabo 'S=/srv/pz-hermes/hermes-data/memory-v1/state; \
  cat $S/settlements/hermes-$(printf %s <SESSION_ID> | sha256sum | cut -c1-32).json; \
  ls -la /srv/pz-hermes/hermes-data/memory-v1/outbox/events/'
```

5. Wait for the publisher timer, then confirm the event reached the vault daily
   note and that `health/flush-hermes` shows `ok` with this session's time.
6. Open a **new** session and confirm the startup recall bundle contains the
   content from step 1. Recall in a fresh session is the acceptance, not the
   artifact on disk.

## 2. The retry path itself

The honest way to see a retry is a genuine provider failure. Do not fabricate
one by editing state files or hand-writing a retry record.

* If a real quota or provider outage occurs, finalize records the failure.
  Confirm it, and note that the raw checkpoint is still present:

```bash
ssh pz-contabo 'S=/srv/pz-hermes/hermes-data/memory-v1/state; \
  ls -la $S/finalize-retry/ && cat $S/finalize-retry/*.json && ls -la $S/checkpoints/'
```

  Expected: `classification=retryable`, `status=retry-scheduled`, `attempts=1`,
  a `next_attempt_after` roughly five minutes out, `reason_code=usage-limit` for
  a quota error, and no raw provider message in the record.

* After the backoff has elapsed, the next session start or session end in that
  Hermes process runs the recovery. Confirm the outcome in the profile log:

```bash
ssh pz-contabo 'grep -E "finalize retry run" /srv/pz-hermes/hermes-data/profiles/pz-orchestrator/logs/agent.log | tail'
```

* Confirm the recovered artifact is marked as recovery, not as a native
  finalize: its outbox evidence carries `provenance=hermes-retry-recovery` and
  `status=unverified`. The original native finalize receipt is untouched, and no
  receipt is manufactured for the recovery.

### The gap this does not close

Recovery runs inside the Hermes process. If a retry becomes due and **no**
session starts or ends, and no Kanban dispatcher tick fires, the work waits. It
is not lost and not silently dropped: the record stays on disk and
`pz-memory doctor` reports it as `hermes_finalize_retry scheduled=N`. Closing
that gap needs an out-of-process trigger — a timer that drives a bounded Hermes
invocation — which is a deployment change and deliberately not part of this
work.

## 3. Doctor rows

```bash
ssh pz-contabo 'runuser -u pzhermes -- /srv/pz-hermes/memory-os/scripts/pz-memory doctor'
```

* `hermes_finalize_retry` — `scheduled/hold/permanent/exhausted`. Warn-only.
* `hermes_finalize_backlog` — sessions whose raw checkpoints have neither a
  settlement nor a retry record. Warn-only.
* `health_flush-hermes` — still the **last** flush result only. A healthy value
  here no longer means there is no backlog; that is what the two rows above are
  for.
* `project_registry` — `not-applicable` on a `role=memory-engine`,
  `runtimes=["hermes"]` deployment, decided from the active config. The
  workstation check is unchanged.

## 4. Legacy sessions

At the time of writing, 17 sessions from 9–15 September hold raw checkpoints
with no settlement, plus `20260915_105210_e3e7ad`. They are **not** adopted
automatically: a retry record is written by a failing finalize, never
retroactively, so these stay visible rather than being replayed behind the
operator's back.

Inspect them, read-only:

```bash
ssh pz-contabo 'runuser -u pzhermes -- env PYTHONPATH=/srv/pz-hermes/memory-os \
  /srv/pz-hermes/hermes-agent/venv/bin/python \
  /srv/pz-hermes/memory-os/scripts/hermes-finalize-backlog.py \
  --config /srv/pz-hermes/memory-config.json --json'
```

Selective adoption, when the user decides a specific session is worth
summarizing, is one explicit operator step per session: re-open that session in
its own profile so the runtime produces a genuine terminal boundary for its
current transcript. Each adopted session costs one summarizer call against the
subscription quota, which is why a bulk adoption is a separate decision and is
not scripted here.

## Rollback

The change is two plugin files, two engine files and the pinned baseline.

1. Restore the previous `__init__.py` (verify against the hash recorded in the
   preconditions) and remove `finalize_retry.py` from the plugin directory.
2. Restart `pz-hermes-dashboard` and `pz-hermes-telegram`.
3. Leave `state/finalize-retry/` in place. The old code ignores it, and the
   records are the only account of what failed; deleting them to make a doctor
   row green would destroy evidence.
4. Reverting the commit also restores the previous baseline rows, so the live
   parity check lines up with the restored plugin again.

A faster mitigation that needs no rollback: set
`PZ_MEMORY_FINALIZE_RETRY=0` in the service environment. Failures are still
recorded and still visible in doctor, but no automatic recovery run starts.

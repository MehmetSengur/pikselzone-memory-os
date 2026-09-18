# Hermes finalize retry — live acceptance

This runbook is how the finalize-retry change is accepted on the real VPS. It is
not authorization to deploy, and local tests passing is explicitly **not** a live
PASS. Nothing below may be reported as verified until its own evidence exists in
this session's own output.

The change itself is local: branch `fix/hermes-finalize-retry`, base
`70bb5ad` (the commit whose plugin, doctor, retry, events and core files match
the deployed bytes). No VPS installation, service restart, push, live recovery
or legacy adoption is part of it.

**Do not deploy this branch on its own.** The live host also runs the Bot Mode
guard from `1b1726f`, which `70bb5ad` predates: the deployed
`memory_v1/hermes_guards.py` hashes to `24cd6add…`, which is `1b1726f`'s
version, not the base's. Installing this branch alone would quietly revert that
guard. The deployable candidate is
`integration/hermes-finalize-retry-bot-guard`, a merge of this branch and
`1b1726f`; its `hermes_guards.py` matches the deployed file byte for byte, and
its combined suite — including the Bot Mode guard tests — is green.

Before and after any installation, confirm the guard file is the one the live
host already had:

```bash
ssh pz-contabo 'sha256sum /srv/pz-hermes/memory-os/memory_v1/hermes_guards.py'
# expected: 24cd6add45921aed685541424511df36692ab308cd836afd02ccffccdb3a4853
```

## What changed, in one paragraph

`on_session_finalize` used to log "source remains retryable" and return. Nothing
retried it: startup discovery is provider-free by design and
`_recover_pending_turn_checkpoints` has no caller, so a session whose `ended_at`
was already stamped never got another finalize callback. Now every finalize
failure writes a bounded, digest-scoped retry record, and a bounded recovery run
— triggered from `on_session_start`, `on_session_end`, a slow watchdog on
long-lived surfaces, and `on_kanban_dispatch_tick` where a Kanban dispatcher
runs — re-attempts the due ones inside the owning profile. A recovered session
is promoted as the contract's `checkpoint_recovery` event and stays
distinguishable through its evidence provenance. Records, locks and settlements
are scoped by the owning SessionDB, so two profiles holding the same session id
never share them.

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

  Expected: `classification=retryable`, `status=retry-scheduled`, `attempts=1`
  and no raw provider message in the record. A quota error
  (`reason_code=usage-limit`) is scheduled about an hour out and gets up to
  eight attempts across a day, because a plan limit routinely outlives the
  generic five-attempt horizon; other transient failures keep the shorter
  five-minute, five-attempt schedule.

* After the backoff has elapsed, the next session start or session end in that
  Hermes process runs the recovery. Confirm the outcome in the profile log:

```bash
ssh pz-contabo 'grep -E "finalize retry run" /srv/pz-hermes/hermes-data/profiles/pz-orchestrator/logs/agent.log | tail'
```

* Confirm the recovered artifact is marked as recovery, not as a native
  finalize: its outbox evidence carries `provenance=hermes-retry-recovery` and
  `status=unverified`, while the artifact itself declares the contract event
  `checkpoint_recovery` so the publisher can promote it. The original native
  finalize receipt is untouched, and no receipt is manufactured for the
  recovery. Confirm the publisher reports `published`, not `error`.

### Progress without a session, and what is still not covered

A quota can reset long after the last session of the day ended, so recovery no
longer depends on a session event arriving. In a long-lived process the plugin
runs a watchdog that wakes every 15 minutes and processes whatever is due. It
is enabled where the deployment already marks a lasting surface
(`PZ_HERMES_USER_SURFACE=1`, which the dashboard and Telegram units set), or
explicitly with `PZ_MEMORY_RETRY_WATCHDOG=1`. A one-shot CLI invocation never
starts it.

What remains outside this work: if every Hermes process is stopped, nothing
runs at all, and the due record simply waits for the next start. It is not lost
and not silently dropped — the record stays on disk and `pz-memory doctor`
reports it as `hermes_finalize_retry scheduled=N`. A trigger that survives the
runtime being down would be a systemd timer driving a bounded Hermes
invocation: a deployment change, deliberately not part of this commit.

## 3. Doctor rows

```bash
ssh pz-contabo 'runuser -u pzhermes -- /srv/pz-hermes/memory-os/scripts/pz-memory doctor'
```

* `hermes_finalize_retry` — `scheduled/hold/permanent/exhausted`. Warn-only.
* `hermes_finalize_backlog` — sessions whose raw checkpoints have neither a
  settlement nor a retry record, plus `stale_after_settlement`: sessions whose
  newest checkpoint is *newer* than their settlement, which therefore cannot
  account for it. Warn-only.
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
`PZ_MEMORY_FINALIZE_RETRY=0` in the service environment, which stops both the
lifecycle triggers and the watchdog. Failures are still recorded and still
visible in doctor, but no automatic recovery run starts. To keep the triggers
and drop only the timer, set `PZ_MEMORY_RETRY_WATCHDOG=0`.

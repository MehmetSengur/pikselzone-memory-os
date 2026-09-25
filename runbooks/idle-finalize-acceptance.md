# Idle finalize acceptance (real application)

Acceptance is the live chain **conversation → raw checkpoint → durable daily
artifact → correct recall in a new session**, proven in the real Codex Desktop
app. Unit tests and `pz-memory doctor` are not acceptance on their own.

Two promotion-to-recall paths must be proven separately, in the same run:

- the **idle path**: idle finalize, not SessionEnd or PreCompact, promotes the
  thread;
- **late recall**: the session whose SessionStart triggered the finalize
  receives the memory after it has started.

**Startup recall** in a later, different session is proven as well. A run
that proves only one of these is not accepted.

Evidence is gathered with `scripts/idle-finalize-evidence.py`. The script is
read-only: its only output is the `--out` JSON file.

## Preconditions (operator approval required)

1. The branch under test is active for the live hooks. `scripts/pz-memory-hook`
   imports the checkout it lives in, so this means checking the branch out in
   the live Memory OS tree. Record the commit SHA.
2. The target root is registered, and its Codex `stop` hook is trusted for the
   current `hooks.json` (check with `/hooks`). If the trigger or startup
   session is a Codex session, its `session_start` and `user_prompt_submit`
   hooks must be trusted too.
3. `idle_finalize_minutes` is known; the default is 45. Shortening it for the
   test is a config change and needs separate approval.
4. Choose a canary that is unique and neutral, for example
   `PZ-IDLE-CANARY-<8 hex>`. Phrase the decision descriptively, so a faithful
   summary contains no instruction-shaped wording the summary guard rejects.
5. No SessionEnd may promote the thread first. Keep the Desktop thread open in
   the running App, and do not archive it or quit the App until the promote
   phase has passed. A SessionEnd (App close, archive, or 30 minutes idle and
   open in no client) makes the promote phase fail with
   `terminal-event-present-idle-path-not-isolated`, and the run must be
   repeated with a new canary.

## Steps

Set `OUT=reports/idle-finalize-acceptance-<date>.json` and
`CFG=<workstation config>`. Every phase appends to the same `$OUT`.

1. **Conversation.** In Codex Desktop, in the registered root, start a new
   thread and state one durable project decision containing the canary. Wait
   for the answer.
2. **Capture.**
   `scripts/idle-finalize-evidence.py --config $CFG --out $OUT capture --thread <desktop thread id> --canary <canary>`
   PASS requires:
   - `originator=Codex Desktop`;
   - a pending `turn_complete` checkpoint holding the canary;
   - no terminal checkpoint pending for the thread.
3. **Idle.** Leave the thread untouched for longer than the idle window.
4. **Trigger session (session A).** Open a new Claude Code or Codex CLI session
   in the same registered root. Idle finalize is not a timer; this SessionStart
   runs it. Do not send a prompt yet. Wait for the drain:
   `logs/drain-<runtime>.log` gets a new line, and the thread's checkpoints
   disappear.
5. **Promote (idle path).**
   `scripts/idle-finalize-evidence.py --config $CFG --out $OUT promote`
   PASS requires:
   - every captured checkpoint removed, and its digest in the session's
     `settled_turn_digests`;
   - a parseable daily artifact containing the canary;
   - `checkpoint_recovery` in `events_seen`, and no terminal event.
6. **Late recall (session A).** In session A, ask for the latest recorded
   decision of this project, **without typing the canary**, and wait for the
   answer. Then run:
   `scripts/idle-finalize-evidence.py --config $CFG --out $OUT recall-late --runtime <claude|codex> --session <session A id>`
   PASS requires:
   - late-recall evidence for session A that delivered this artifact;
   - the promotion happened after session A started, so its startup bundle
     could not have held it;
   - the canary in the delivered text;
   - the assistant stating the canary, and the user never typing it.
7. **Startup recall (session B).** Open a different new session in the same
   root, ask the same question without the canary, and wait for the answer.
   Then run:
   `scripts/idle-finalize-evidence.py --config $CFG --out $OUT recall-startup --runtime <claude|codex> --session <session B id>`
   PASS requires:
   - startup evidence for session B, observed after the promotion, whose
     bundle contains the canary;
   - session B differs from session A;
   - the same statement rule as step 6.
8. **Verdict.**
   `scripts/idle-finalize-evidence.py --config $CFG --out $OUT verdict`
   The run is accepted only when capture, promote, recall_late and
   recall_startup all PASS. Commit `$OUT` together with the commit SHA.

## Known limits to record, not to hide

- A prompt sent in session A before the drain finishes gets only the "still
  processing" notice. Wait for step 5 before step 6.
- Late recall delivers a condensed block: at most one context item, two
  decisions, one open item and one evidence item, each cut to 220
  characters. The canary must sit in one of those items. If the summarizer put
  it elsewhere, recall-late fails with `canary-not-in-late-recall-text`; that
  is a real limit, not something to explain away.
- A thread in another project is finalized but not delivered to session A
  (startup project scope).
- A provider usage limit leaves the checkpoint pending with a scheduled retry.
  That is correct behavior, but it is not an acceptance PASS.

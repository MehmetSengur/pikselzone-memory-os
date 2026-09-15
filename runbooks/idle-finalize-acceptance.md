# Idle finalize acceptance (real application)

Acceptance is the live chain **conversation → raw checkpoint → durable daily
artifact → correct recall in a new session**, proven in the real Codex Desktop
app. Unit tests and `pz-memory doctor` are not acceptance on their own.

Evidence is gathered with `scripts/idle-finalize-evidence.py`. The script is
read-only: its only output is the `--out` JSON file.

## Preconditions (operator approval required)

1. The branch under test is active for the live hooks. `scripts/pz-memory-hook`
   imports the checkout it lives in, so this means checking the branch out in
   the live Memory OS tree. Record the commit SHA.
2. The target root is registered, and its Codex `stop` and
   `user_prompt_submit` hooks are trusted for the current `hooks.json` (check
   with `/hooks` in Codex).
3. `idle_finalize_minutes` is known; the default is 45. Shortening it for the
   test is a config change and needs separate approval.
4. Choose a canary that is unique and neutral, for example
   `PZ-IDLE-CANARY-<8 hex>`. The summary guard rejects summaries that contain
   `write to`, `execute this`, `run this command`, `system prompt`,
   `developer message` or `tool call`. Phrase the canary turn so a faithful
   summary does not need those words; see the retry diagnosis in the report.

## Steps

Set `OUT=reports/idle-finalize-acceptance-<date>.json` and
`CFG=<workstation config>`.

1. **Conversation.** In Codex Desktop, in the registered root, start a new
   thread. State one durable project decision containing the canary. Wait for
   the answer. Do not archive the thread, and do not quit the App.
2. **Capture.** Run this within the idle window:
   `scripts/idle-finalize-evidence.py --config $CFG --out $OUT capture --thread <desktop thread id> --canary <canary>`
   PASS requires `originator=Codex Desktop` and at least one pending checkpoint
   holding the canary.
3. **Idle.** Leave the thread untouched for longer than the idle window.

   If the App ends the session on its own (30 minutes idle and open in no
   client), a `session_end` checkpoint promotes the thread instead. The
   promote phase accepts that path; note which one happened.
4. **Trigger.** Open a new Claude Code or Codex CLI session in the same
   registered root. Idle finalize is not a timer; this SessionStart is what
   runs it. Wait for the drain; `logs/drain-<runtime>.log` gets a new line.
5. **Promote.**
   `scripts/idle-finalize-evidence.py --config $CFG --out $OUT promote`
   PASS requires:
   - every captured checkpoint removed;
   - every captured turn digest in the session's `settled_turn_digests`;
   - a parseable daily artifact containing the canary.
6. **Recall.** Use one of two channels.
   - *Late recall:* in the session from step 4, send a prompt that asks for
     the latest recorded decision of this project **without typing the
     canary**.
   - *Startup:* open another new session in the same root and ask the same
     question.

   Then run:
   `scripts/idle-finalize-evidence.py --config $CFG --out $OUT recall --runtime <claude|codex> --session <that session id>`
   PASS requires:
   - the canary present in that session's startup bundle or late-recall
     evidence;
   - the assistant stating the canary;
   - the user never typing it.

The run is accepted only if capture, promote and recall all PASS in one `$OUT`
file. Commit that file with the commit SHA and the trigger path used (idle
batch or App SessionEnd).

## Known limits to record, not to hide

- A prompt sent before the drain finishes gets only the "still processing"
  notice; ask again after the drain.
- A thread in another project is finalized but not delivered to this session
  (startup project scope).
- Provider usage limits leave the checkpoint pending with a scheduled retry.
  That is a correct outcome, but it is not an acceptance PASS.

# PIKSELZONE MEMORY V1 — M3.2A INTEGRITY RECOVERY REPORT

## 1. EXECUTIVE STATUS

- HERMES_CORE_RUNTIME_INTEGRITY=PASS
- HERMES_NATIVE_LIFECYCLE_E2E=PASS
- PUBLISHER_AUTOMATION=ENABLED
- KNOWLEDGE_COMPILER_AUTOMATION=DISABLED
- PZ_POLICY_GUARD=PASS
- OBSIDIAN_SYNC=RUNNING_UNINTERRUPTED

---

## 2. HERMES CORE RESTORATION FORENSIC

- **Pristine Image**: nousresearch/hermes-agent:v2026.7.20
- **Image Digest**: sha256:f7b35053268f532f98955195c909f15a230470fbcbdacaa9fdecb95707dad04a
- **Mutated Files Identified (via read-only diff prior to recreation)**:
  - `/opt/hermes/hermes_constants.py` (debug print at line 693, mode override to 0o750 at line 707)
  - `/opt/hermes/hermes_cli/config.py` (mode_str default override to 0o750 at line 841)
  - Python bytecode cache files for both
- **Container Recreation Evidence**:
  - Executed canonically via `/srv/pz-hermes/compose.yaml`:
    `cd /srv/pz-hermes && docker compose up -d --force-recreate`
  - Container recreate log:
    `Container pz-hermes Recreate` -> `Container pz-hermes Started` -> `healthy`
- **Post-Recreation Diff `/opt/hermes`**:
  - Clean: `docker diff pz-hermes | grep '/opt/hermes'` returns zero entries.
- **Core SHA Verification (matches pristine image byte-for-byte)**:
  - `/opt/hermes/hermes_constants.py`: `dacf56fbaf53871057044131922ba0040ce144d10c0ac19c843e091e2f4547b5` (`PASS`)
  - `/opt/hermes/hermes_cli/config.py`: `172b78ecb923048859ca177d96f5b010b44ec74bb1d13553577ff49bde1a071d` (`PASS`)

---

## 3. NATIVE HOOK ROOT CAUSE ANALYSIS

### Root Causes Identified
1. **Profile Isolation of Plugin Scopes**:
   - In Hermes, when `--profile <profile_name>` is supplied on the CLI (or defaulted by specific wrappers), `main.py` pre-parses `--profile` and sets `HERMES_HOME=/opt/data/profiles/<profile_name>`.
   - Plugin discovery (`_discover_and_load_inner`) scans `get_hermes_home() / "plugins"`.
   - Initially, `pz-memory-v1` was only installed in `/srv/pz-hermes/hermes-data/plugins/pz-memory-v1`, so profile runs (`-p pz-orchestrator`) did not find the plugin directory, discovering only 55 plugins and 48 enabled.
   - Resolution: Installed `pz-memory-v1` across all profile plugin directories (`/srv/pz-hermes/hermes-data/profiles/*/plugins/pz-memory-v1`) as well as the global plugins directory. Policy guard passed cleanly (guarded dirs only restrict `pz-safe-file`).
2. **Trivia Queries vs Architectural Durability**:
   - For trivial test inputs (e.g. asking for directory file listings), `PluginLlm` structured output returns `{"status": "empty", "context": [], ...}`.
   - By design, `pz-memory-v1` drops empty summaries to prevent vault pollution. In previous tests, the hook fired, but staged no artifact because `status == "empty"`.
   - Resolution: Real session prompts containing durable architectural decisions produce `status: "ok"`, generating full markdown events with context, decisions, and learnings.
3. **Outbox Traversal Permissions**:
   - Host `pzmemory` runs with `gid=987 (pzvault)`. When `/opt/data` had mode `0700`, ACL mask collapsed to `---`.
   - Rather than mutating `/opt/hermes` core code, `pz-memory-v1` runs inside the container as `uid=999 (pzhermes)`, which owns `/opt/data`. The plugin self-asserts `0750` permissions on `/opt/data` during startup and flush, cleanly allowing unprivileged host `pzmemory` to read the staged outbox without any core patching.

---

## 4. TRUE NATIVE E2E EVIDENCE

- **Session ID**: `20260828_120627_f163cb`
- **Execution Command**:
  `docker exec -u 999:987 pz-hermes hermes chat --cli -q "In this architectural session for Pikselzone Memory V1 Phase M3.2A, we have established three binding decisions..."`
- **Duration**: 8s
- **Messages**: 2 (1 user message, 1 assistant response, 0 tool calls)
- **Registered Callback Entry Evidence**:
  - `caller_module`: `/opt/hermes/hermes_cli/plugins.py`
  - `caller_function`: `invoke_hook`
  - `native_invoke`: `true`
  - `hook_name`: `on_session_end` (callback at `2026-08-28T12:06:32+00:00`)
  - `receipt_hash`: `134e372f3ae1da37cf1f8070d911763ed07fedad17d0c3b8e6ceac47dac19022`
  - Lifecycle trace logged to: `/opt/data/memory-v1/state/hook-trace.jsonl`
- **PluginLlm Invocation Evidence**:
  - Successfully called `PluginLlm.complete_structured` via `ctx.llm` provider `custom` (`gpt-5.4-mini-2026-03-17`).
- **Outbox Event File**: `hermes-927649af8ea32bded49308c2a4288add.md` (mode `0660`, group `pzvault`)
- **Outbox Evidence File**: `hermes-927649af8ea32bded49308c2a4288add.json` (mode `0660`, group `pzvault`)
- **Vault Promoted File**: `/srv/pz-hermes/vault/daily/2026-08-28/hermes-927649af8ea32bded49308c2a4288add.md`
- **Vault SHA256**: `4ec5c8e6a297210f06f7b0d672e8eb7208016218b87c2e58714be6d6fa20a1f1`
- **Evidence SHA256**: `4ec5c8e6a297210f06f7b0d672e8eb7208016218b87c2e58714be6d6fa20a1f1`
- **SHA Match**: `PASS` (Exact match)
- **Obsidian Sync Auto-Upload**:
  `pz-obsidian-sync[3833456]: Upload complete daily/2026-08-28/hermes-927649af8ea32bded49308c2a4288add.md`

---

## 5. EVIDENCE AUTHENTICITY HARDENING

- **Lifecycle Receipt Schema**: `pikselzone-memory-lifecycle-receipt-v1`
- **Receipt Fields**:
  - `session_id`: Unique Hermes session identifier
  - `hook_name`: Name of hook called (`on_session_end` or `on_session_finalize`)
  - `callback_at`: ISO timestamp at instant of callback entry
  - `plugin_version`: Plugin semantic version (`1.0.0`)
  - `plugin_hash`: SHA256 of `__init__.py`
  - `caller_module`: Module filename from caller frame stack
  - `caller_function`: Function name (`invoke_hook`)
  - `native_invoke`: Boolean verifying call originated from `hermes_cli.plugins:invoke_hook`
  - `receipt_hash`: SHA256 cryptographic digest binding session, hook, timestamp, plugin hash, and invoke status
- **Doctor Validation Rules**:
  - `_activation_evidence_valid` enforces `provenance == "hermes-native-lifecycle"`.
  - Verifies presence of embedded `lifecycle_receipt` with `native_invoke == True`.
  - Validates causal timeline order: `callback_at <= observed_at <= promoted_at`.
  - Validates `event_sha256` matches byte-for-byte with the promoted file in `/srv/pz-hermes/vault/daily/`.
- **Unit Test Coverage**:
  - `tests/memory_v1/test_hermes_plugin.py`:
    - `test_lifecycle_receipt_and_doctor_causal_chain`: Validates end-to-end receipt generation, promotion, and doctor pass.
    - `test_doctor_rejects_unverified_operator_call`: Proves operator calls without `invoke_hook` produce `native_invoke=False` and are rejected by doctor.
  - Test suite status: 88 passed, 0 failed.

---

## 6. SERVICE STATES

- `pz-hermes`: `Up 1 hour (healthy)`
- `pz-obsidian-sync`: `active (running)` since `Fri 2026-08-28 02:24:41 +03; 12h ago` (PID 3833456, 0 restarts)
- `pz-memory-publisher.timer`: `active (running)`, enabled
- `pz-memory-publisher.service`: Triggered automatically every 2 minutes
- `pz-memory-compiler.timer`: `inactive (dead)`, `disabled` (as required)

---

## 7. NEXT STEP: KNOWLEDGE AUTOMATION DESIGN

To enable `pz-memory-compiler.timer` in a future phase:
1. An automatic knowledge generation trigger inside the Hermes container or scheduled task must write candidate knowledge trees to `/opt/data/memory-v1/outbox/knowledge/`.
2. The host compiler will validate and promote candidate files under the existing single-writer lock into `/srv/pz-hermes/vault/knowledge/`.
3. Until that automatic generator is operational, the compiler timer remains disabled.

"""Single-writer learning for the shared companion files.

``companion/Kurallar.md`` (active rules and candidates) and
``companion/Journal.md`` live in the synced vault, and both hosts used to
read, modify and rewrite them. An atomic write only guarantees a whole file;
it does not stop one host from writing a version built on a copy that had not
yet received the other host's change, so learned rules could silently vanish.

Now only the memory-engine host (the Contabo publisher) writes those files.
Every other host records what it learned as a write-once observation file
under ``companion/learning-inbox/<host>/``. Each file has a content-derived
id and is created exactly once, so two hosts never write the same path. The
files travel with normal Obsidian Sync (markdown, because the sync carries no
JSON), survive a disconnected host, and are merged by the publisher cycle:

- each merge is a two-phase ledger record under one lock: ``intent`` before
  anything is written, ``committed`` after. Every effect is idempotent for its
  observation (journal entries carry the id; rules and candidates are keyed by
  text and source session; a promotion is a single write), so a merge that
  stopped at any point is completed by re-running it, never applied twice;
- the id is recorded before the inbox file is removed, so a redelivered
  observation is a duplicate, never a second session;
- the merger re-checks provenance and intent on the sentence itself and keeps
  the weaker of the two classifications, so a forged or quoted record cannot
  become an active rule;
- the ledger keeps the origin host, session and outcome of every merge.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import fcntl
import hashlib
import json
import logging
import contextlib
import os
import re
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

from . import provenance as pv
from .companion import CompanionManager
from .core import (
    MemoryConfig, PolicyError, atomic_write, ensure_safe_directory, iso_now, redact_sensitive_text, safe_unlink,
)
from .rule_learner import ExtractedRule, RuleLearner

logger = logging.getLogger("memory_v1.learning_inbox")

SCHEMA = "pikselzone-learning-observation-v1"
LEDGER_SCHEMA = "pikselzone-learning-merge-v1"
INBOX_REL = Path("companion") / "learning-inbox"
REJECTED_DIRNAME = "_rejected"
MERGER_ROLE = "memory-engine"
KINDS = {"rule", "journal"}
RULE_INTENTS = {pv.DURABLE_DIRECTIVE, pv.PREFERENCE_CANDIDATE}
MAX_TEXT_CHARS = 4000
MAX_RULE_CHARS = 600
APPLIED_OUTCOMES = {
    "added-active", "reconciled", "candidate-added", "candidate-observed", "candidate-promoted",
    "journal-appended",
}
_FIELDS = (
    "schema", "obs_id", "kind", "origin_host", "runtime", "source_session",
    "observed_at", "intent", "evidence", "reason",
)
_HOST_RE = re.compile(r"[^A-Za-z0-9._-]+")


@dataclasses.dataclass
class Observation:
    kind: str
    text: str
    source_session: str
    runtime: str
    origin_host: str
    observed_at: str
    intent: str = ""
    evidence: str = ""
    reason: str = ""
    obs_id: str = ""

    def __post_init__(self) -> None:
        if not self.obs_id:
            self.obs_id = observation_id(self.kind, self.text, self.source_session)


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def observation_id(kind: str, text: str, source_session: str) -> str:
    material = f"{SCHEMA}\n{kind}\n{_normalize(text)}\n{source_session}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]


def _default_host_label() -> str:
    name = ""
    if sys.platform == "darwin":
        # socket.gethostname() on macOS follows the network (a reverse-DNS name),
        # so it cannot identify the host across Wi-Fi changes.
        try:
            name = subprocess.run(
                ["scutil", "--get", "LocalHostName"], capture_output=True, text=True, timeout=5,
            ).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            name = ""
    name = name or socket.gethostname().split(".")[0]
    return _HOST_RE.sub("-", name).strip("-") or "unknown-host"


def host_label(config: MemoryConfig | None = None) -> str:
    """A stable name for this host: chosen once and kept in its state directory."""
    if config is None:
        return _default_host_label()
    path = config.state_path / "host-id"
    try:
        value = _HOST_RE.sub("-", path.read_text(encoding="utf-8").strip())
    except OSError:
        value = ""
    if not value:
        value = _default_host_label()
        ensure_safe_directory(path.parent, create=True)
        atomic_write(path, value.encode("utf-8"), mode=0o600)
    return value


def is_merger(config: MemoryConfig) -> bool:
    return config.role == MERGER_ROLE


def inbox_root(config: MemoryConfig) -> Path:
    return config.vault_path / INBOX_REL


def ledger_path(config: MemoryConfig) -> Path:
    return config.state_path / "learning" / "merge-ledger.jsonl"


# --- observation files ------------------------------------------------------

def render_observation(obs: Observation) -> str:
    lines = ["---"]
    values = dataclasses.asdict(obs)
    values["schema"] = SCHEMA
    for key in _FIELDS:
        lines.append(f"{key}: {json.dumps(values[key], ensure_ascii=False)}")
    lines += ["---", "", _normalize(obs.text), ""]
    return "\n".join(lines)


def parse_observation(text: str) -> Observation:
    lines = text.splitlines()
    if not lines or lines[0] != "---" or "---" not in lines[1:]:
        raise ValueError("observation-frontmatter-missing")
    boundary = lines.index("---", 1)
    meta: dict[str, Any] = {}
    for line in lines[1:boundary]:
        key, sep, raw = line.partition(":")
        if not sep:
            raise ValueError("observation-frontmatter-line-invalid")
        meta[key.strip()] = json.loads(raw.strip())
    if set(meta) != set(_FIELDS) or meta["schema"] != SCHEMA:
        raise ValueError("observation-fields-invalid")
    if meta["kind"] not in KINDS:
        raise ValueError("observation-kind-invalid")
    if not all(isinstance(meta[k], str) for k in _FIELDS):
        raise ValueError("observation-field-type-invalid")
    body = _normalize("\n".join(lines[boundary + 1:]))
    if not body or len(body) > MAX_TEXT_CHARS or not meta["source_session"]:
        raise ValueError("observation-body-invalid")
    obs = Observation(
        kind=meta["kind"], text=body, source_session=meta["source_session"], runtime=meta["runtime"],
        origin_host=meta["origin_host"], observed_at=meta["observed_at"], intent=meta["intent"],
        evidence=meta["evidence"], reason=meta["reason"], obs_id=meta["obs_id"],
    )
    if obs.obs_id != observation_id(obs.kind, obs.text, obs.source_session):
        raise ValueError("observation-id-mismatch")
    return obs


def write_observation(config: MemoryConfig, obs: Observation) -> bool:
    """Create the observation file once. Returns False when it already exists."""
    directory = inbox_root(config) / _HOST_RE.sub("-", obs.origin_host)
    ensure_safe_directory(directory, create=True)
    path = directory / f"{obs.obs_id}.md"
    if path.exists():
        return False
    atomic_write(path, render_observation(obs).encode("utf-8"), mode=0o660)
    return True


# --- sinks used by RuleLearner and the journal ----------------------------

def _rule_observation(config: MemoryConfig, item: ExtractedRule, source: str, runtime: str) -> Observation:
    clean, _ = redact_sensitive_text(item.rule_text.strip())
    return Observation(
        kind="rule", text=clean, source_session=source, runtime=runtime, origin_host=host_label(config),
        observed_at=iso_now(), intent=item.intent, evidence=item.evidence, reason=item.reason,
    )


def learning_sink(config: MemoryConfig, runtime: str) -> Callable[[ExtractedRule, str], int]:
    """Where a learner's rules go on this host: merged now, or queued for the merger."""
    if is_merger(config):
        def merge_now(item: ExtractedRule, source: str) -> int:
            outcome = merge_observation(config, _rule_observation(config, item, source, runtime))
            return 1 if outcome in APPLIED_OUTCOMES else 0
        return merge_now

    def queue(item: ExtractedRule, source: str) -> int:
        return 1 if write_observation(config, _rule_observation(config, item, source, runtime)) else 0
    return queue


def record_journal(
    config: MemoryConfig, companion: CompanionManager, *, title: str, narrative: str,
    runtime: str, source_session: str,
) -> None:
    clean_narrative, _ = redact_sensitive_text(narrative.strip())
    if not clean_narrative:
        return
    obs = Observation(
        kind="journal", text=clean_narrative, source_session=source_session, runtime=runtime,
        origin_host=host_label(config), observed_at=iso_now(), reason=title,
    )
    if is_merger(config):
        merge_observation(config, obs, companion=companion)
    else:
        write_observation(config, obs)


# --- merge --------------------------------------------------------------------

def _ledger_state(config: MemoryConfig) -> tuple[set[str], set[str]]:
    """(committed ids, ids with an intent but no commit). Entries from before the
    two-phase format have no ``phase`` and count as committed."""
    path = ledger_path(config)
    committed: set[str] = set()
    intents: set[str] = set()
    if not path.is_file():
        return committed, intents
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue  # a torn last line from an interrupted append
        if not (isinstance(entry, dict) and entry.get("schema") == LEDGER_SCHEMA and entry.get("obs_id")):
            continue
        if entry.get("phase") == "intent":
            intents.add(str(entry["obs_id"]))
        else:
            committed.add(str(entry["obs_id"]))
    return committed, intents - committed


def _read_ledger_ids(config: MemoryConfig) -> set[str]:
    return _ledger_state(config)[0]


def _append_ledger(config: MemoryConfig, obs: Observation, outcome: str, *, phase: str = "committed") -> None:
    path = ledger_path(config)
    ensure_safe_directory(path.parent, create=True)
    entry = {
        "schema": LEDGER_SCHEMA, "obs_id": obs.obs_id, "phase": phase, "kind": obs.kind,
        "origin_host": obs.origin_host, "runtime": obs.runtime, "source_session": obs.source_session,
        "observed_at": obs.observed_at, "claimed_intent": obs.intent, "merged_at": iso_now(),
        "outcome": outcome, "excerpt": obs.text[:160],
    }
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    path.chmod(0o640)


@contextlib.contextmanager
def merge_lock(config: MemoryConfig):
    """The one lock every writer of the shared companion files takes on the engine."""
    lock_path = config.state_path / "learning" / "merge.lock"
    ensure_safe_directory(lock_path.parent, create=True)
    with lock_path.open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _effective_rule_intent(obs: Observation) -> tuple[str, str]:
    """(intent, reason). The merger never trusts a stronger claim than the text supports."""
    if obs.intent not in RULE_INTENTS:
        return "", f"rejected:claimed-intent-{obs.intent or 'missing'}"
    if pv.find_test_marker(obs.text):
        return "", "rejected:test-data"
    if len(obs.text) > MAX_RULE_CHARS:
        return "", "rejected:not-a-single-rule"
    turn = pv.analyze_user_turn(obs.text)
    foreign = [b.provenance for b in turn.blocks if b.provenance != pv.AUTHORED]
    if foreign:
        return "", f"rejected:provenance-{foreign[0]}"
    now_intent, _ = pv.classify_sentence(obs.text)
    if now_intent not in RULE_INTENTS:
        return "", f"rejected:reclassified-{now_intent}"
    if obs.intent == pv.DURABLE_DIRECTIVE and now_intent == pv.DURABLE_DIRECTIVE:
        return pv.DURABLE_DIRECTIVE, ""
    return pv.PREFERENCE_CANDIDATE, ""


def _apply(config: MemoryConfig, obs: Observation, companion: CompanionManager) -> str:
    if obs.kind == "journal":
        appended = companion.append_journal_entry(
            title=obs.reason or "Oturum Özeti", narrative=obs.text, runtime=obs.runtime, marker=obs.obs_id,
        )
        return "journal-appended" if appended else "journal-already-present"
    intent, rejection = _effective_rule_intent(obs)
    if rejection:
        return rejection
    from .rule_learner import CANDIDATE_REASON, DURABLE_REASON
    durable = intent == pv.DURABLE_DIRECTIVE
    item = ExtractedRule(
        rule_text=obs.text, reason=obs.reason or (DURABLE_REASON if durable else CANDIDATE_REASON),
        is_explicit=durable, confidence=0.95 if durable else 0.6, source_turn=obs.text,
        intent=intent, evidence=obs.evidence,
    )
    return RuleLearner(companion).apply_rule(item, obs.source_session)


def merge_observation(
    config: MemoryConfig, obs: Observation, *, companion: CompanionManager | None = None,
) -> str:
    """Apply one observation exactly once. Only the merger host may call this.

    Recovery by stopping point:
    - before the intent record: nothing happened; the next run applies it;
    - after the intent, before or during the effect write: the next run finds
      the intent and applies again; effects are idempotent, and the commit is
      recorded as ``recovered:<outcome>``;
    - after the commit, before the inbox file is removed: the next run sees a
      committed id and only removes the file (``duplicate-delivery``).
    The ledger is read under the lock on every call, so a merge running in
    another process is never missed.
    """
    if not is_merger(config):
        raise RuntimeError("learning-merge-refused:not-the-merger-host")
    with merge_lock(config):
        committed, intents = _ledger_state(config)
        if obs.obs_id in committed:
            return "duplicate-delivery"
        recovering = obs.obs_id in intents
        if not recovering:
            _append_ledger(config, obs, "applying", phase="intent")
        outcome = _apply(config, obs, companion or CompanionManager(config.vault_path))
        _append_ledger(config, obs, f"recovered:{outcome}" if recovering else outcome)
        return outcome


def _pending_files(root: Path) -> list[Path]:
    if not root.is_dir() or root.is_symlink():
        return []
    files = []
    for path in sorted(root.glob("*/*.md")):
        if path.parent.name == REJECTED_DIRNAME or path.is_symlink() or not path.is_file():
            continue
        files.append(path)
    return files


def pending_observations(config: MemoryConfig) -> list[Path]:
    return _pending_files(inbox_root(config))


def merge_learning_inbox(config: MemoryConfig) -> dict[str, Any]:
    """Merge every synced observation file. A no-op on hosts that are not the merger."""
    root = inbox_root(config)
    if not is_merger(config):
        return {"status": "skipped", "reason": "not-the-merger-host", "pending": len(_pending_files(root))}
    counts: dict[str, int] = {}
    companion = CompanionManager(config.vault_path)
    for path in _pending_files(root):
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            continue  # another merge already took and removed this file
        try:
            obs = parse_observation(text)
        except (ValueError, json.JSONDecodeError) as exc:
            rejected = root / REJECTED_DIRNAME
            ensure_safe_directory(rejected, create=True)
            try:
                path.replace(rejected / f"{path.parent.name}-{path.name}")
            except FileNotFoundError:
                continue
            logger.warning("Rejected malformed learning observation %s: %s", path.name, exc)
            counts["rejected-malformed"] = counts.get("rejected-malformed", 0) + 1
            continue
        outcome = merge_observation(config, obs, companion=companion)
        key = outcome.split(":", 1)[0] if outcome.startswith("rejected") else outcome
        counts[key] = counts.get(key, 0) + 1
        if path.exists():  # a concurrent merge may have removed it after committing
            try:
                safe_unlink(path, root=root)
            except PolicyError:
                if path.exists():
                    raise
    return {"status": "ok", "merged_at": iso_now(), "counts": counts}


def inbox_status(config: MemoryConfig) -> dict[str, Any]:
    """Pending observation files and the age of the oldest (this host's clock)."""
    files = pending_observations(config)
    oldest = None
    for path in files:
        mtime = path.stat().st_mtime
        oldest = mtime if oldest is None else min(oldest, mtime)
    age = None
    if oldest is not None:
        age = int(dt.datetime.now().timestamp() - oldest)
    return {"pending": len(files), "oldest_age_seconds": age}

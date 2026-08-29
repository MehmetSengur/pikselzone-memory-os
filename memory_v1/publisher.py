"""Deterministic host outbox publisher for Pikselzone Memory V1.

Runs under unprivileged user pzmemory:pzvault.
Zero LLM calls, zero provider secrets, zero Docker socket access, zero root/sudo.

Scans the container outbox (/srv/pz-hermes/hermes-data/memory-v1/outbox/events/),
validates each staged event artifact against strict Memory V1 schema and policy boundaries,
and atomically promotes valid artifacts into the Obsidian vault (/srv/pz-hermes/vault/daily/).
"""
from __future__ import annotations

import json
import logging
import os
import re
import stat
from pathlib import Path
from typing import Any

from .core import (
    MemoryConfig, PolicyError, SchemaError,
    atomic_write, iso_now, path_within, reject_symlink_chain,
    safe_unlink, secure_read_text, sha256_bytes, sha256_file, write_health,
)
from .events import parse_event_artifact

logger = logging.getLogger(__name__)

HERMES_EVENT_FILENAME_RE = re.compile(r"^hermes-[0-9a-f]{32}\.md$")
HERMES_EVIDENCE_FILENAME_RE = re.compile(r"^hermes-[0-9a-f]{32}\.json$")


def publish_outbox(
    config: MemoryConfig,
    outbox_root: Path | None = None,
) -> list[dict[str, Any]]:
    if outbox_root:
        root = Path(outbox_root)
    else:
        hermes_roots = config.transcript_roots.get("hermes", [])
        if hermes_roots:
            root = Path(hermes_roots[0]) / "memory-v1"
        else:
            root = config.state_path.parent / "hermes-data" / "memory-v1"
    events_dir = root / "outbox" / "events"
    evidence_dir = root / "outbox" / "evidence"
    quarantine_dir = root / "quarantine"

    if not events_dir.exists() or not events_dir.is_dir():
        logger.debug("Outbox events directory does not exist: %s", events_dir)
        return []

    results: list[dict[str, Any]] = []

    # Sort files deterministically
    candidates = sorted([p for p in events_dir.iterdir() if p.is_file() and not p.name.startswith(".")])

    for event_path in candidates:
        if not HERMES_EVENT_FILENAME_RE.match(event_path.name):
            logger.warning("Skipping non-matching outbox filename: %s", event_path.name)
            continue

        try:
            reject_symlink_chain(event_path)
            info = event_path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise PolicyError("outbox-event-file-unsafe")

            event_text, event_digest = secure_read_text(
                event_path, root=events_dir, max_bytes=2 * 1024 * 1024
            )
            event = parse_event_artifact(event_text)

            if event.get("runtime") != "hermes":
                raise PolicyError("outbox-event-runtime-not-hermes")

            created_at = str(event.get("created_at", ""))
            if len(created_at) < 10 or not re.match(r"^\d{4}-\d{2}-\d{2}", created_at[:10]):
                raise SchemaError("outbox-event-created-at-invalid")

            date_str = created_at[:10]
            target_daily_dir = config.vault_path / "daily" / date_str
            target_file = target_daily_dir / event_path.name

            if not path_within(target_file, config.vault_path / "daily"):
                raise PolicyError("target-daily-path-outside-vault")

            session_hash = event_path.name.replace("hermes-", "").replace(".md", "")

            # Deduplication check
            if target_file.exists():
                reject_symlink_chain(target_file)
                target_text, target_digest = secure_read_text(
                    target_file, root=config.vault_path / "daily", max_bytes=2 * 1024 * 1024
                )
                if target_digest == event_digest:
                    # Content matches: safely clean up outbox duplicate
                    safe_unlink(event_path, root=events_dir)
                    results.append({
                        "status": "deduplicated",
                        "file": event_path.name,
                        "target": str(target_file),
                        "sha256": event_digest,
                    })
                else:
                    # Content mismatch for same filename: quarantine
                    quarantine_dir.mkdir(parents=True, exist_ok=True)
                    quarantine_path = quarantine_dir / f"{event_path.name}.{event_digest[:8]}.conflict"
                    event_path.rename(quarantine_path)
                    results.append({
                        "status": "quarantined",
                        "file": event_path.name,
                        "reason": "hash-conflict",
                        "quarantine_path": str(quarantine_path),
                    })
                    continue
            else:
                # Promotion: atomically write target daily file
                target_daily_dir.mkdir(parents=True, exist_ok=True)
                atomic_write(target_file, event_text.encode("utf-8"), mode=0o640)
                safe_unlink(event_path, root=events_dir)
                results.append({
                    "status": "published",
                    "file": event_path.name,
                    "target": str(target_file),
                    "sha256": event_digest,
                })

                # Second Brain Pipeline for promoted Hermes event
                try:
                    from .companion import CompanionManager, LastSessionData
                    from .graph_engine import KnowledgeGraphEngine, ConceptData
                    from .rule_learner import RuleLearner
                    from .skill_engine import SkillEngine, WorkflowObservation

                    companion_mgr = CompanionManager(config.vault_path)
                    rule_learner = RuleLearner(companion_mgr)
                    graph_engine = KnowledgeGraphEngine(config.vault_path)
                    skill_engine = SkillEngine(config.vault_path)

                    # 1. Learn rules from event summary & text
                    rule_candidates = (
                        event.get("important_conversations", [])
                        + event.get("decisions", [])
                        + event.get("learnings", [])
                    )
                    turn_pairs = [("user", cand) for cand in rule_candidates]
                    if turn_pairs:
                        rule_learner.learn_from_transcript(turn_pairs, source_session=f"hermes-{session_hash}")

                    # 2. Update Last-Session continuity & Journal
                    summary_context = event.get("context", []) or event.get("important_conversations", [])
                    decisions = event.get("decisions", [])
                    learnings = event.get("learnings", [])
                    open_items = event.get("open_items", [])
                    ls_data = LastSessionData(
                        runtime="hermes",
                        session_id=str(event.get("session_id", session_hash)),
                        completed_items=summary_context[:5],
                        decisions=decisions[:5],
                        pending_items=open_items[:5],
                        next_steps=open_items[:3],
                        active_project=str(event.get("root_task_id", config.vault_path.name)),
                        updated_at=created_at,
                    )
                    companion_mgr.write_last_session(ls_data)

                    if decisions or learnings:
                        narrative = " ".join(decisions[:2] + learnings[:2])
                        companion_mgr.append_journal_entry(
                            title=f"{str(event.get('event', 'session_end')).replace('_', ' ').capitalize()} Özeti",
                            narrative=narrative,
                            runtime="hermes",
                        )

                    # 3. Knowledge Graph concepts & connections
                    created_slugs: list[str] = []
                    for item_text in decisions + learnings:
                        terms = re.findall(r"\b[A-Z][a-zA-Z0-9_\-\.]{2,}\b", item_text)
                        for term in terms:
                            if term.lower() not in {
                                "the", "this", "that", "with", "from", "when", "then",
                                "true", "false", "none", "null", "user", "assistant"
                            }:
                                c_path = graph_engine.add_or_update_concept(ConceptData(
                                    title=term,
                                    summary=item_text[:140],
                                    details=[f"{event.get('event', 'session_end')} (hermes): {item_text}"],
                                    sources=[f"hermes:{session_hash}"],
                                ))
                                if c_path.stem not in created_slugs:
                                    created_slugs.append(c_path.stem)
                    if len(created_slugs) >= 2:
                        for i in range(len(created_slugs) - 1):
                            sa, sb = created_slugs[i], created_slugs[i + 1]
                            if sa != sb:
                                graph_engine.add_or_update_connection(
                                    concept_a=sa,
                                    concept_b=sb,
                                    relationship="aynı oturumda birlikte kararlaştırıldı",
                                    evidence=[f"hermes:{session_hash}"],
                                    source=f"hermes:{session_hash}",
                                )

                    # 4. Skill candidate observation
                    workflow_candidates = []
                    for item in event.get("important_conversations", []) + decisions:
                        if any(marker in item.lower() for marker in ("adımlar", "komut", "workflow", "prosedür", "deploy", "build", "test", "kontrol", "kurulum", "ayarla", "görev")):
                            workflow_candidates.append(item)
                    for cand in workflow_candidates:
                        w_name = cand.split(":", 1)[0].strip(" -:\n") if ":" in cand else cand[:40].strip(" -:\n")
                        if ":" in w_name:
                            w_name = w_name.split(":")[-1].strip()
                        body = cand.split(":", 1)[1] if ":" in cand else cand
                        w_steps = [s.strip() for s in re.split(r"(?:[0-9]+\.|\n|;|,)+", body) if len(s.strip()) > 5][:6]
                        if len(w_steps) >= 2:
                            skill_engine.record_workflow_observation(WorkflowObservation(
                                workflow_name=w_name[:50],
                                goal=cand[:120].strip(),
                                steps=w_steps,
                                session_id=f"hermes-{session_hash}",
                            ))
                except Exception as sb_exc:
                    logger.warning("Second brain pipeline warning during event publish: %s", sb_exc)

            # Check and promote corresponding evidence receipt if available
            evidence_file = evidence_dir / f"hermes-{session_hash}.json"
            if evidence_file.exists():
                try:
                    reject_symlink_chain(evidence_file)
                    ev_text, _ = secure_read_text(evidence_file, root=evidence_dir, max_bytes=64 * 1024)
                    ev_data = json.loads(ev_text)
                    if (
                        isinstance(ev_data, dict)
                        and ev_data.get("schema") == "pikselzone-memory-activation-evidence-v1"
                        and ev_data.get("runtime") == "hermes"
                        and ev_data.get("provenance") in {"hermes-native-lifecycle", "automatic-lifecycle-drain"}
                        and ev_data.get("source_provider") == event.get("source_provider")
                    ):
                        receipt = ev_data.get("lifecycle_receipt")
                        if ev_data.get("provenance") == "hermes-native-lifecycle":
                            if not isinstance(receipt, dict) or not receipt.get("native_invoke"):
                                raise PolicyError("evidence-lifecycle-receipt-invalid")
                        ev_data["promoted_at"] = iso_now()
                        ev_data["promotion_status"] = "promoted"
                        promoted_bytes = json.dumps(ev_data, indent=2).encode("utf-8")
                        dest_evidence_dir = config.state_path / "evidence"
                        dest_evidence_dir.mkdir(parents=True, exist_ok=True)
                        dest_evidence_file = dest_evidence_dir / "hermes-lifecycle-smoke.json"
                        atomic_write(dest_evidence_file, promoted_bytes, mode=0o600)
                    safe_unlink(evidence_file, root=evidence_dir)
                except Exception as ev_exc:
                    logger.warning("Failed to process evidence file %s: %s", evidence_file, ev_exc)

        except Exception as exc:
            logger.error("Error publishing outbox event %s: %s", event_path, exc)
            results.append({
                "status": "error",
                "file": event_path.name,
                "error": str(exc),
            })

    # Check and promote recall evidence if present
    recall_evidence = evidence_dir / "recall-hermes.json"
    if recall_evidence.exists():
        try:
            reject_symlink_chain(recall_evidence)
            rec_text, _ = secure_read_text(recall_evidence, root=evidence_dir, max_bytes=512 * 1024)
            rec_data = json.loads(rec_text)
            if (
                isinstance(rec_data, dict)
                and rec_data.get("schema") == "pikselzone-memory-recall-evidence-v1"
                and rec_data.get("runtime") == "hermes"
                and rec_data.get("status") == "pass"
            ):
                dest_evidence_dir = config.state_path / "evidence"
                dest_evidence_dir.mkdir(parents=True, exist_ok=True)
                dest_evidence_file = dest_evidence_dir / "recall-hermes.json"
                atomic_write(dest_evidence_file, rec_text.encode("utf-8"), mode=0o600)
                safe_unlink(recall_evidence, root=evidence_dir)
        except Exception as r_exc:
            logger.warning("Failed to promote recall evidence: %s", r_exc)

    try:
        from .recall import update_hermes_startup_snapshot
        update_hermes_startup_snapshot(config)
    except Exception as s_exc:
        logger.debug("Failed to auto-update Hermes startup snapshot: %s", s_exc)

    try:
        write_health(config.state_path, "drain", "ok")
    except Exception as h_exc:
        logger.debug("Failed to write drain health: %s", h_exc)

    return results

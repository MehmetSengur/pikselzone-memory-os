"""Deterministic host-side knowledge selector, validator, and outbox promoter."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import os
import re
import shutil
import stat
from pathlib import Path
from typing import Any

from .core import (
    BARE_CONCEPT_DENYLIST, MemoryConfig, MemoryError, PolicyError, SchemaError,
    atomic_json, atomic_write, directive_shaped, ensure_safe_directory,
    compiler_write_relative_path, exclusive_lock, iso_now, knowledge_relative_path,
    path_within,
    redact_sensitive_text, reject_symlink_chain, safe_unlink,
    secure_read_file, secure_read_text, sha256_file, write_health,
)
from .events import parse_event_artifact
from .graph_engine import is_conflicted_copy_path

logger = logging.getLogger("pz-memory-promoter")
CANONICAL_WIKILINK_RE = re.compile(r"(?<!!)\[\[([^\]|#]+)(?:#[^\]|]*)?(?:\|[^\]]*)?\]\]")


def load_compiler_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {
            "schema": "pikselzone-memory-compiler-state-v1",
            "ingested": {},
            "last_success": None,
            "last_changed": [],
        }
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SchemaError("compiler-state-corrupt") from exc
    if not isinstance(data, dict) or not isinstance(data.get("ingested"), dict):
        raise SchemaError("compiler-state-invalid")
    return data


def select_and_stage_batch(
    config: MemoryConfig,
    outbox_root: Path | None = None,
    max_events: int = 10,
    max_input_chars: int = 300000,
) -> dict[str, Any] | None:
    """Discover uningested daily events and stage an untrusted batch payload into the inbox."""
    daily_root = config.vault_path / "daily"
    if not daily_root.exists() or not daily_root.is_dir():
        return None
    reject_symlink_chain(daily_root)

    state_path = config.state_path / "compiler" / "state.json"
    state = load_compiler_state(state_path)
    ingested = state.get("ingested", {})

    candidates: list[Path] = []
    for current, _, files in os.walk(daily_root, followlinks=False):
        curr_p = Path(current)
        for fname in sorted(files):
            if not fname.endswith(".md"):
                continue
            fpath = curr_p / fname
            info = fpath.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                continue
            rel_path = fpath.relative_to(config.vault_path).as_posix()
            _, digest = secure_read_file(fpath, root=daily_root, max_bytes=2 * 1024 * 1024)
            if ingested.get(rel_path) != digest:
                candidates.append(fpath)

    if not candidates:
        logger.debug("pz-memory-promoter: zero uningested daily events found")
        return None

    selected = sorted(candidates)[:max_events]
    source_digests: dict[str, str] = {}
    event_chunks: list[str] = []
    used_chars = 0
    char_limit = max_input_chars // 2

    for path in selected:
        text, digest = secure_read_text(path, root=daily_root, max_bytes=2 * 1024 * 1024)
        parse_event_artifact(text)
        text_clean, _ = redact_sensitive_text(text)
        rel_name = path.relative_to(config.vault_path).as_posix()
        source_digests[rel_name] = digest
        block = f"\n### EVENT {path.name}\n{text_clean}"
        if used_chars + len(block) > char_limit:
            remaining = max(0, char_limit - used_chars)
            if remaining:
                event_chunks.append(block[:remaining])
            break
        event_chunks.append(block)
        used_chars += len(block)

    # Existing knowledge snapshot
    knowledge_root = config.vault_path / "knowledge"
    knowledge_chunks: list[str] = []
    used_k_chars = 0
    if knowledge_root.exists() and knowledge_root.is_dir():
        for path in sorted(knowledge_root.rglob("*.md")):
            if is_conflicted_copy_path(path):
                continue
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                continue
            rel_name = path.relative_to(config.vault_path).as_posix()
            try:
                knowledge_relative_path(rel_name)
            except PolicyError:
                continue
            text, _ = secure_read_text(path, root=knowledge_root, max_bytes=2 * 1024 * 1024)
            text_clean, _ = redact_sensitive_text(text)
            block = f"\n### FILE {rel_name}\n{text_clean}"
            if used_k_chars + len(block) <= char_limit:
                knowledge_chunks.append(block)
                used_k_chars += len(block)

    untrusted_events = "".join(event_chunks)
    untrusted_knowledge = "".join(knowledge_chunks) or "(none)"

    # Determine outbox / inbox base directory
    base = outbox_root
    if base is None:
        roots = config.transcript_roots.get("hermes", [])
        base = Path(roots[0]) / "memory-v1" if roots else Path("/srv/pz-hermes/hermes-data/memory-v1")

    inbox_dir = base / "inbox"
    inbox_dir.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(inbox_dir, 0o770)
    except OSError:
        pass

    batch_key = hashlib.sha256(
        (json.dumps(sorted(source_digests.items())) + iso_now()).encode("utf-8")
    ).hexdigest()[:16]

    payload = {
        "schema": "pikselzone-knowledge-batch-v1",
        "batch_id": f"batch-{batch_key}",
        "created_at": iso_now(),
        "event_digests": source_digests,
        "untrusted_events": untrusted_events,
        "untrusted_existing_knowledge": untrusted_knowledge,
    }

    batch_file = inbox_dir / "knowledge-batch.json"
    tmp_file = f"{batch_file}.{os.getpid()}.tmp"
    with open(tmp_file, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)
        fh.flush()
        os.fsync(fh.fileno())
    os.chmod(tmp_file, 0o660)
    os.replace(tmp_file, batch_file)

    logger.info("pz-memory-promoter: staged batch %s with %d events", payload["batch_id"], len(selected))
    return payload


def _verify_sources(vault_path: Path, expected: dict[str, str]) -> None:
    for rel_path, digest in expected.items():
        full_path = vault_path / rel_path
        if not full_path.is_file():
            raise PolicyError(f"event-source-missing:{rel_path}")
        _, current_digest = secure_read_file(
            full_path, root=vault_path / "daily", max_bytes=2 * 1024 * 1024
        )
        if current_digest != digest:
            raise PolicyError(f"event-source-changed:{rel_path}")


def _ensure_concept_derived_frontmatter(content: str, rel_path: str) -> str:
    lines = content.splitlines()
    if not lines or lines[0].strip() != "---":
        raise PolicyError(f"concept-missing-frontmatter:{rel_path}")
    fm_lines = []
    body_idx = -1
    for idx, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            body_idx = idx + 1
            break
        fm_lines.append(line)
    if body_idx == -1:
        raise PolicyError(f"concept-unclosed-frontmatter:{rel_path}")
    fm_text = "\n".join(fm_lines)
    if "title:" not in fm_text:
        raise PolicyError(f"concept-missing-title:{rel_path}")
    if "authority:" not in fm_text:
        fm_lines.append("authority: derived-memory-not-canonical")
    body_text = "\n".join(lines[body_idx:])
    return "---\n" + "\n".join(fm_lines) + "\n---\n\n" + body_text.lstrip()


def _validate_graph_candidate_integrity(
    config: MemoryConfig, validated_payloads: list[tuple[str, Path, bytes]],
) -> None:
    """Reject staged graph writes that could create unresolved or orphan links."""
    knowledge = config.vault_path / "knowledge"
    candidate_concepts = {
        rel.removeprefix("knowledge/concepts/").removesuffix(".md")
        for rel, _, _ in validated_payloads if rel.startswith("knowledge/concepts/")
    }
    candidate_connections = {
        rel.removeprefix("knowledge/connections/").removesuffix(".md")
        for rel, _, _ in validated_payloads if rel.startswith("knowledge/connections/")
    }
    live_concepts = {
        path.stem for path in (knowledge / "concepts").glob("*.md")
        if path.is_file() and not path.is_symlink()
    }
    live_connections = {
        path.stem for path in (knowledge / "connections").glob("*.md")
        if path.is_file() and not path.is_symlink()
    }
    valid_concepts = live_concepts | candidate_concepts
    valid_connections = live_connections | candidate_connections

    # A bare status/artefact word is never a durable concept; refuse the batch
    # rather than promoting it into the shared graph.
    for slug in sorted(candidate_concepts):
        if slug in BARE_CONCEPT_DENYLIST:
            raise PolicyError(f"candidate-generic-bare-concept:{slug}")

    for rel, _, content_bytes in validated_payloads:
        content = content_bytes.decode("utf-8", errors="replace")
        targets = [match.group(1).strip().removesuffix(".md") for match in CANONICAL_WIKILINK_RE.finditer(content)]
        for target in targets:
            if target.startswith("concepts/") and target.removeprefix("concepts/") in valid_concepts:
                continue
            if target.startswith("connections/") and target.removeprefix("connections/") in valid_connections:
                continue
            raise PolicyError(f"candidate-broken-or-noncanonical-wikilink:{rel}:{target}")

        if rel.startswith("knowledge/connections/"):
            endpoints = {
                target.removeprefix("concepts/") for target in targets
                if target.startswith("concepts/") and target.removeprefix("concepts/") in valid_concepts
            }
            expected_name = "--".join(sorted(endpoints)) if len(endpoints) == 2 else ""
            actual_name = rel.removeprefix("knowledge/connections/").removesuffix(".md")
            if not expected_name or actual_name != expected_name:
                raise PolicyError(f"candidate-connection-endpoint-integrity:{rel}")


def _promote_direct_outbox_files(config: MemoryConfig, root: Path) -> list[Path]:
    live_root = config.vault_path / "knowledge"
    if not live_root.exists() or not live_root.is_dir():
        raise PolicyError("knowledge-root-missing")

    candidates: list[Path] = []
    for cur, dirnames, filenames in os.walk(root, followlinks=False):
        for name in filenames:
            if name.startswith("."):
                continue
            p = Path(cur) / name
            candidates.append(p)

    candidates.sort()
    changed: list[Path] = []

    for cand in candidates:
        reject_symlink_chain(cand)
        info = cand.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise PolicyError(f"candidate-unsafe:{cand.name}")

        rel = cand.relative_to(root).as_posix()
        knowledge_relative_path(f"knowledge/{rel}")

        dest = live_root / rel
        if not path_within(dest, live_root):
            raise PolicyError(f"candidate-path-escape:{rel}")

        cand_bytes, cand_digest = secure_read_file(cand, root=root, max_bytes=2 * 1024 * 1024)
        if directive_shaped(cand_bytes.decode("utf-8", errors="replace")):
            raise PolicyError(f"candidate-directive-shaped:{rel}")

        dest_mode = 0o640
        if dest.exists():
            reject_symlink_chain(dest)
            dest_info = dest.lstat()
            if not stat.S_ISREG(dest_info.st_mode) or dest_info.st_nlink != 1:
                raise PolicyError(f"destination-unsafe:{dest.name}")
            _, live_digest = secure_read_file(dest, root=live_root, max_bytes=2 * 1024 * 1024)
            if live_digest == cand_digest:
                safe_unlink(cand, root=root)
                continue
            dest_mode = stat.S_IMODE(dest_info.st_mode)

        dest.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(dest, cand_bytes, mode=dest_mode)
        safe_unlink(cand, root=root)
        changed.append(dest)

    return changed


def promote_knowledge_outbox(
    config: MemoryConfig,
    outbox_root: Path | None = None,
    outbox_knowledge_root: Path | None = None,
) -> Any:
    """Validate candidate files in outbox and promote atomically into vault/knowledge/."""
    lock_path = config.state_path / "locks" / "compiler.lock"
    with exclusive_lock(lock_path, nonblocking=True):
        if outbox_knowledge_root is not None:
            knowledge_outbox = Path(outbox_knowledge_root)
        elif outbox_root is not None:
            knowledge_outbox = Path(outbox_root) / "outbox" / "knowledge"
        else:
            roots = config.transcript_roots.get("hermes", [])
            base = Path(roots[0]) / "memory-v1" if roots else Path("/srv/pz-hermes/hermes-data/memory-v1")
            knowledge_outbox = base / "outbox" / "knowledge"

        manifest_file = knowledge_outbox / "manifest.json"
        candidates_dir = knowledge_outbox / "candidates"

        if not manifest_file.is_file():
            if outbox_knowledge_root is not None and knowledge_outbox.is_dir():
                return _promote_direct_outbox_files(config, knowledge_outbox)
            return {"status": "no_manifest", "promoted": []}

        try:
            manifest_text = manifest_file.read_text(encoding="utf-8")
            manifest = json.loads(manifest_text)
        except (OSError, json.JSONDecodeError) as exc:
            write_health(config.state_path, "compiler", "blocked", f"corrupt-manifest:{exc}")
            raise SchemaError(f"manifest-corrupt:{exc}")

        if manifest.get("schema") != "pikselzone-knowledge-outbox-manifest-v1":
            write_health(config.state_path, "compiler", "blocked", "invalid-manifest-schema")
            raise SchemaError("manifest-schema-invalid")

        source_digests = manifest.get("source_digests", {})
        _verify_sources(config.vault_path, source_digests)

        state_path = config.state_path / "compiler" / "state.json"
        state = load_compiler_state(state_path)

        m_status = manifest.get("status")

        if m_status == "no_changes":
            if not isinstance(source_digests, dict) or not source_digests:
                write_health(config.state_path, "compiler", "blocked", "no-changes-missing-sources")
                raise SchemaError("manifest-no-changes-missing-sources")
            model = manifest.get("model")
            provider = manifest.get("provider")
            if not model or not provider or not isinstance(model, str) or not isinstance(provider, str):
                write_health(config.state_path, "compiler", "blocked", "no-changes-missing-provenance")
                raise SchemaError("manifest-no-changes-missing-provenance")
            writes = manifest.get("writes")
            if writes != []:
                write_health(config.state_path, "compiler", "blocked", "no-changes-has-writes")
                raise SchemaError("manifest-no-changes-has-writes")
            if candidates_dir.is_dir() and any(candidates_dir.iterdir()):
                write_health(config.state_path, "compiler", "fail", "no-changes-with-staged-candidates")
                raise PolicyError("no-changes-with-staged-candidates")

            state["ingested"].update(source_digests)
            state["last_success"] = iso_now()
            atomic_json(state_path, state)
            safe_unlink(manifest_file, root=knowledge_outbox)
            write_health(config.state_path, "compiler", "ok", "no-changes")
            logger.info("pz-memory-promoter: completed with valid no changes for batch %s", manifest.get("batch_id"))
            return {"status": "no_changes", "promoted": []}

        if m_status != "candidates-staged":
            write_health(config.state_path, "compiler", "blocked", f"unknown-manifest-status:{m_status}")
            raise SchemaError(f"unknown-manifest-status:{m_status}")

        writes = manifest.get("writes", [])
        if not writes or not isinstance(writes, list):
            write_health(config.state_path, "compiler", "blocked", "empty-candidates-writes")
            raise SchemaError("empty-candidates-writes")

        # Stage 1: Validate all candidates before promoting any
        validated_payloads: list[tuple[str, Path, bytes]] = []

        for item in writes:
            rel_path = str(item.get("path", "")).strip().lstrip("/")
            expected_sha = item.get("sha256")
            if not rel_path:
                raise SchemaError("candidate-path-empty")

            # Path validation & traversal prevention.  The *write* policy is
            # narrower than the read allowlist: knowledge/index.md and
            # knowledge/log.md are deterministic single-writer artifacts
            # rebuilt after promotion, never promoted model output.
            try:
                compiler_write_relative_path(rel_path)
            except PolicyError as exc:
                write_health(config.state_path, "compiler", "fail", str(exc))
                raise

            candidate_file = candidates_dir / rel_path
            if not candidate_file.is_file():
                write_health(config.state_path, "compiler", "fail", f"missing-candidate:{rel_path}")
                raise PolicyError(f"missing-candidate-file:{rel_path}")

            info = candidate_file.lstat()
            if stat.S_ISLNK(info.st_mode):
                write_health(config.state_path, "compiler", "fail", f"candidate-symlink:{rel_path}")
                raise PolicyError(f"candidate-symlink:{rel_path}")
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                write_health(config.state_path, "compiler", "fail", f"candidate-special:{rel_path}")
                raise PolicyError(f"candidate-special:{rel_path}")
            if stat.S_IMODE(info.st_mode) & 0o111:
                write_health(config.state_path, "compiler", "fail", f"candidate-executable:{rel_path}")
                raise PolicyError(f"candidate-executable:{rel_path}")

            # Size limit check (max 500KB per file)
            if info.st_size > 500 * 1024:
                write_health(config.state_path, "compiler", "fail", f"candidate-oversized:{rel_path}")
                raise SchemaError(f"candidate-oversized:{rel_path}")

            content_bytes, file_digest = secure_read_file(
                candidate_file, root=candidates_dir, max_bytes=500 * 1024
            )
            if file_digest != expected_sha:
                write_health(config.state_path, "compiler", "fail", f"candidate-sha-mismatch:{rel_path}")
                raise PolicyError(f"candidate-sha-mismatch:{rel_path}")

            content_text = content_bytes.decode("utf-8", errors="replace")
            if directive_shaped(content_text):
                write_health(config.state_path, "compiler", "fail", f"candidate-directive-shaped:{rel_path}")
                raise PolicyError(f"candidate-directive-shaped:{rel_path}")

            _, secrets_found = redact_sensitive_text(content_text)
            if secrets_found > 0:
                write_health(config.state_path, "compiler", "fail", f"candidate-contains-secrets:{rel_path}")
                raise PolicyError(f"candidate-contains-secrets:{rel_path}")

            if rel_path.startswith("knowledge/concepts/"):
                content_text = _ensure_concept_derived_frontmatter(content_text, rel_path)
                content_bytes = content_text.encode("utf-8")

            target_path = config.vault_path / rel_path
            if not path_within(target_path, config.vault_path / "knowledge"):
                raise PolicyError(f"target-path-escape:{rel_path}")

            validated_payloads.append((rel_path, target_path, content_bytes))

        try:
            _validate_graph_candidate_integrity(config, validated_payloads)
        except PolicyError as exc:
            write_health(config.state_path, "compiler", "blocked", str(exc))
            raise

        # Stage 2: Atomically promote validated files with bounded rollback
        backups: list[tuple[Path, bytes, int]] = []
        created_files: list[Path] = []
        promoted_paths: list[str] = []

        try:
            for rel_path, target_path, data in validated_payloads:
                if target_path.exists():
                    target_info = target_path.stat()
                    backups.append((target_path, target_path.read_bytes(), target_info.st_mode & 0o777))
                else:
                    created_files.append(target_path)
                ensure_safe_directory(target_path.parent, create=True)
                atomic_write(target_path, data, mode=0o640)
                promoted_paths.append(rel_path)
        except Exception as exc:
            # Rollback all applied writes to preserve pre-promotion vault integrity
            for b_path, b_data, b_mode in backups:
                try:
                    atomic_write(b_path, b_data, mode=b_mode)
                except Exception:
                    pass
            for c_path in created_files:
                try:
                    if c_path.is_file():
                        c_path.unlink()
                except Exception:
                    pass
            write_health(config.state_path, "compiler", "fail", f"promotion-write-failed:{exc}")
            logger.error("pz-memory-promoter: write failure during promotion, rolled back: %s", exc)
            raise

        # Stage 3: Clean up outbox candidates and manifest
        shutil.rmtree(candidates_dir, ignore_errors=True)
        safe_unlink(manifest_file, root=knowledge_outbox)

        # Stage 4: Commit ingestion ledger strictly after successful writes
        state["ingested"].update(source_digests)
        state["last_success"] = iso_now()
        state["last_changed"] = promoted_paths
        atomic_json(state_path, state)

        # Stage 5: deterministic, single-writer rebuild of the two shared files.
        # The model never authors these; they are derived from the canonical
        # concept/connection files that were just promoted.
        from .knowledge_index import rebuild_after_promotion
        rebuilt = rebuild_after_promotion(
            config.vault_path,
            batch_id=str(manifest.get("batch_id") or "batch-unknown"),
            promoted=promoted_paths,
        )

        write_health(config.state_path, "compiler", "ok")
        logger.info(
            "pz-memory-promoter: successfully promoted %d knowledge files, "
            "index rebuilt deterministically (%d rows)",
            len(promoted_paths), rebuilt["index_rows"],
        )
        return {"status": "ok", "promoted": promoted_paths, "index_rows": rebuilt["index_rows"]}

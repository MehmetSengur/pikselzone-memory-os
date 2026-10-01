"""Command-line entry point used by hooks and bounded operator packs."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .adapters import checkpoint_hook, drain_checkpoint, flush_hook, load_hook_input
from .compiler import TerraCompiler
from .context import build_context
from .core import (
    DuplicateEvent, MemoryConfig, MemoryError, NoMemory, PolicyError, write_health,
)
from .doctor import run_doctor
from .provider import StructuredResponsesProvider, create_provider


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pz-memory")
    parser.add_argument("--config", required=True, type=Path)
    commands = parser.add_subparsers(dest="command", required=True)
    trace = commands.add_parser('trace', help='Trace a memory using safe references and reason codes')
    trace.add_argument('--query', required=True)
    trace.add_argument('--session-id', default='')
    profiles = commands.add_parser('profiles', help='Configured versus native-observed profile integration')
    profiles.add_argument('--base', type=Path)
    profiles.add_argument('--reconcile', action='store_true', help='Bounded native profile inventory; never edits profile settings')
    parser.add_argument('--manual', action='store_true', help='Explicit processing/recall in manual mode; never overrides no-memory')
    flush = commands.add_parser("flush")
    flush.add_argument("--runtime", required=True, choices=("codex", "claude", "hermes"))
    flush.add_argument("--event")
    flush.add_argument("--hook-input", type=Path)
    checkpoint = commands.add_parser("checkpoint")
    checkpoint.add_argument("--runtime", required=True, choices=("codex", "claude", "hermes"))
    checkpoint.add_argument("--event")
    checkpoint.add_argument("--hook-input", type=Path)
    drain = commands.add_parser("drain")
    drain.add_argument("--queue", required=True, type=Path)
    quarantine_cmd = commands.add_parser(
        "quarantine-checkpoint",
        help="Set a raw checkpoint aside so its session can drain again",
    )
    quarantine_cmd.add_argument("--queue", required=True, type=Path)
    quarantine_cmd.add_argument("--reason", default="operator")
    restore_cmd = commands.add_parser(
        "restore-checkpoint", help="Return a quarantined checkpoint to the queue",
    )
    restore_cmd.add_argument("--name", required=True)
    commands.add_parser(
        "list-quarantine", help="List quarantined checkpoints and why they were set aside",
    )
    reset_retry_cmd = commands.add_parser(
        "reset-retry",
        help="Clear a stale drain verdict so the bounded retry path tries again",
    )
    reset_retry_cmd.add_argument("--queue", required=True, type=Path)
    doc_cmd = commands.add_parser("doctor")
    doc_cmd.add_argument("--heal", "--fix", action="store_true", dest="heal", help="Run self-healing maintenance and produce audit receipt")
    compile_cmd = commands.add_parser("compile")
    compile_cmd.add_argument("--max-events", type=int, default=50)
    compile_cmd.add_argument("--outbox", type=Path)
    context = commands.add_parser("context")
    context.add_argument("--budget", type=int, default=16000)
    pub_outbox = commands.add_parser("publish-outbox")
    pub_outbox.add_argument("--outbox", type=Path)
    stage_batch = commands.add_parser("stage-knowledge-batch")
    stage_batch.add_argument("--outbox", type=Path)
    stage_batch.add_argument("--max-events", type=int, default=10)
    promote_k = commands.add_parser("promote-knowledge")
    promote_k.add_argument("--outbox", type=Path)
    recall_cmd = commands.add_parser("recall")
    recall_cmd.add_argument("--runtime", choices=("codex", "claude", "hermes"), default="claude")
    recall_cmd.add_argument("--startup", action="store_true")
    recall_cmd.add_argument("--query", type=str)
    recall_cmd.add_argument("--budget", type=int, default=16000)
    recall_cmd.add_argument("--format", choices=("wire", "markdown", "json"), default="wire")
    recall_cmd.add_argument("--record-evidence", action="store_true")
    recall_cmd.add_argument("--session-key", type=str, default="cli-recall")
    import_cmd = commands.add_parser("import-history")
    import_cmd.add_argument("--file", required=True, type=Path)
    import_cmd.add_argument(
        "--format",
        choices=("claude", "claude-code", "chatgpt", "codex", "gemini", "markdown"),
        default=None,
    )
    import_cmd.add_argument(
        "--project", default=None,
        help="registered project slug the imported history belongs to",
    )
    import_cmd.add_argument(
        "--dry-run", dest="dry_run", action="store_true",
        help="report what would be extracted without writing rules or skills",
    )
    parity_cmd = commands.add_parser("parity")
    parity_cmd.add_argument("--align", action="store_true", default=True)
    register_cmd = commands.add_parser("register")
    register_cmd.add_argument("root", type=Path)
    register_cmd.add_argument("--project", required=True)
    unregister_cmd = commands.add_parser("unregister")
    unregister_cmd.add_argument("root", type=Path)
    requeue_cmd = commands.add_parser(
        "requeue-hermes-finalize",
        help="Put permanent Hermes finalize verdicts back on the bounded retry path",
    )
    requeue_cmd.add_argument("--plan-out", type=Path, help="Write a reviewable plan here; changes nothing.")
    requeue_cmd.add_argument("--apply", type=Path, help="Apply a previously reviewed plan file.")
    requeue_cmd.add_argument("--revert", help="Restore the records of this requeue id.")
    requeue_cmd.add_argument("--reason", action="append", default=[],
                             help="Reason code to include (default: schema and trust-denied).")
    requeue_cmd.add_argument("--force", action="store_true", help="With --revert: restore even changed records.")
    repair_cmd = commands.add_parser(
        "repair-memory",
        help="Source-verified maintenance of learned rules and skills (plan, apply, revert).",
    )
    repair_cmd.add_argument("--plan-out", type=Path, help="Write a reviewable plan here; changes nothing.")
    repair_cmd.add_argument("--apply", type=Path, help="Apply a previously reviewed plan file.")
    repair_cmd.add_argument("--revert", help="Restore the backup of this repair id.")
    repair_cmd.add_argument("--retire-candidate", dest="retire_candidate",
                            help="Retire the rule candidate with exactly this text (memory-engine host only).")
    repair_cmd.add_argument("--classification", default="", help="With --retire-candidate: why it is not a preference.")
    repair_cmd.add_argument("--evidence", default="", help="With --retire-candidate: where that was established.")
    repair_cmd.add_argument("--force", action="store_true", help="With --revert: allow even if files changed.")
    repair_cmd.add_argument("--history", type=Path, action="append", default=[],
                            help="Earlier Kurallar.md snapshot(s) to search for lost entries.")
    repair_cmd.add_argument("--claude-root", type=Path, default=Path.home() / ".claude" / "projects")
    repair_cmd.add_argument("--codex-root", type=Path, default=Path.home() / ".codex" / "sessions")
    return parser


COMPILER_COMMANDS = frozenset({"compile", "stage-knowledge-batch", "promote-knowledge"})


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if os.environ.get("PZ_MEMORY_INVOKED_BY") == "memory-v1" and args.command in {"flush", "checkpoint", "drain"}:
        print(json.dumps({"status": "noop", "reason": "recursion-guard"}))
        return 0
    try:
        config = MemoryConfig.load(args.config)
        from .profile_integration import active_settings
        # The compiler is one system job, not a chat profile. Applying the
        # profile of whatever HERMES_HOME it runs under replaced the operator's
        # project grants in memory-config.json with that profile's, which are
        # empty for the root home.
        settings = None if args.command in COMPILER_COMMANDS else active_settings()
        if settings:
            import dataclasses
            config = dataclasses.replace(config, memory={**config.memory,
                'owner':settings['owner'], 'project':settings['project'], 'projects':settings['projects'],
                'shared':settings['shared'], 'mode':settings['mode'], 'no_memory':settings['status']=='opt-out'})
        if args.manual:
            import dataclasses
            config = dataclasses.replace(config, memory={**config.memory, 'mode':'normal'})
        if args.command == 'trace':
            from .trace import trace_memory
            print(json.dumps(trace_memory(config, args.query, session_id=args.session_id), ensure_ascii=False))
            return 0
        if args.command == 'profiles':
            from .profile_integration import profile_report, reconcile_profiles
            roots = config.transcript_roots.get('hermes', ())
            base = args.base or (Path(settings['base_dir']) if settings else (roots[0] / 'memory-v1' if roots else config.state_path.parent))
            print(json.dumps({'profiles':reconcile_profiles() if args.reconcile else profile_report(base)}, ensure_ascii=False))
            return 0

        if args.command == "flush":
            stdin_text = "" if args.hook_input else sys.stdin.read()
            payload = load_hook_input(args.hook_input, stdin_text)
            provider = create_provider(config)
            path = flush_hook(
                config, runtime=args.runtime, payload=payload,
                event_override=args.event, provider=provider,
            )
            print(json.dumps({"status": "ok", "event_path": str(path)}))
            return 0
        if args.command == "compile":
            if config.provider_mode == "runtime-native":
                from .compiler import promote_knowledge_outbox
                res = promote_knowledge_outbox(config, outbox_root=args.outbox)
                print(json.dumps(res, ensure_ascii=False))
                return 0
            provider = create_provider(config)
            paths = TerraCompiler(config, provider).compile(max_events=args.max_events)
            print(json.dumps({"status": "ok", "changed": [str(path) for path in paths]}))
            return 0
        if args.command == "stage-knowledge-batch":
            from .compiler import select_and_stage_batch
            res = select_and_stage_batch(config, outbox_root=args.outbox, max_events=args.max_events)
            if res is None:
                print(json.dumps({"status": "no_new_events", "batch_id": None}))
            else:
                print(json.dumps({
                    "status": "staged",
                    "batch_id": res["batch_id"],
                    "events": len(res["event_digests"]),
                }, ensure_ascii=False))
            return 0
        if args.command == "checkpoint":
            stdin_text = "" if args.hook_input else sys.stdin.read()
            payload = load_hook_input(args.hook_input, stdin_text)
            path = checkpoint_hook(
                config, runtime=args.runtime, payload=payload, event_override=args.event
            )
            print(json.dumps({"status": "checkpointed", "queue_path": str(path)}))
            return 0
        if args.command == "drain":
            provider = create_provider(config)
            path = drain_checkpoint(config, args.queue, provider=provider)
            try:
                write_health(config.state_path, "drain", "ok")
            except OSError:
                pass
            print(json.dumps({"status": "ok", "event_path": str(path)}))
            return 0
        if args.command == "reset-retry":
            from .retry import clear_retry_state, load_retry_state
            target = args.queue.resolve()
            previous = load_retry_state(config, target)
            clear_retry_state(config, target)
            print(json.dumps({
                "status": "reset",
                "checkpoint": target.name,
                "previous_status": previous.get("status"),
                "previous_reason": previous.get("last_reason"),
            }, ensure_ascii=False))
            return 0
        if args.command == "quarantine-checkpoint":
            from .retry import quarantine_checkpoint
            target = quarantine_checkpoint(config, args.queue.resolve(), reason=args.reason)
            print(json.dumps({"status": "quarantined", "path": str(target)}))
            return 0
        if args.command == "restore-checkpoint":
            from .retry import restore_quarantined_checkpoint
            target = restore_quarantined_checkpoint(config, args.name)
            print(json.dumps({"status": "restored", "path": str(target)}))
            return 0
        if args.command == "list-quarantine":
            from .retry import quarantined_checkpoints
            print(json.dumps(
                {"status": "ok", "quarantined": quarantined_checkpoints(config)},
                ensure_ascii=False, indent=2,
            ))
            return 0
        if args.command == "recall":
            from .recall import build_startup_recall_bundle, targeted_recall, write_recall_evidence, RECALL_EVIDENCE_PROVENANCE_MANUAL
            if args.query:
                result = targeted_recall(config, query=args.query, budget_chars=args.budget)
                if args.format == "json":
                    print(json.dumps(result, ensure_ascii=False, indent=2))
                else:
                    print(result["markdown"])
                return 0
            else:
                bundle = build_startup_recall_bundle(
                    config, runtime=args.runtime, session_key=args.session_key, budget_chars=args.budget
                )
                if args.record_evidence:
                    write_recall_evidence(config, bundle, lifecycle_event="manual-recall", provenance=RECALL_EVIDENCE_PROVENANCE_MANUAL)
                if args.format == "markdown":
                    print(bundle.text)
                elif args.format == "json":
                    print(json.dumps(bundle.to_dict(), ensure_ascii=False, indent=2))
                else:
                    if args.runtime == "hermes":
                        print(json.dumps({"context": bundle.text}, ensure_ascii=False))
                    else:
                        wire = {
                            "continue": True,
                            "hookSpecificOutput": {
                                "hookEventName": "SessionStart",
                                "additionalContext": bundle.text,
                            },
                        }
                        print(json.dumps(wire, ensure_ascii=False))
                return 0
        if args.command == "doctor":
            if getattr(args, "heal", False):
                from .doctor import run_self_healing
                result = run_self_healing(config)
                print(json.dumps(result, ensure_ascii=False, indent=2))
                return 0
            result = run_doctor(config)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if result["status"] == "ok" else 2
        if args.command == "context":
            sys.stdout.write(build_context(config, budget=args.budget))
            return 0
        if args.command == "publish-outbox":
            from .publisher import publish_outbox
            results = publish_outbox(config, outbox_root=args.outbox)
            from .learning_inbox import merge_learning_inbox
            try:
                learning = merge_learning_inbox(config)
            except Exception as exc:  # a merge problem must not stop event publication
                learning = {"status": "error", "error": str(exc)[:300]}
            try:
                from .sync_heartbeat import acknowledge_heartbeats
                learning["sync_ack"] = acknowledge_heartbeats(config)
            except Exception as exc:
                learning["sync_ack"] = {"status": "error", "error": str(exc)[:300]}
            print(json.dumps({"status": "ok", "results": results, "learning_merge": learning}, ensure_ascii=False))
            return 0
        if args.command == "promote-knowledge":
            from .compiler import promote_knowledge_outbox
            res = promote_knowledge_outbox(config, outbox_root=args.outbox)
            print(json.dumps(res, ensure_ascii=False))
            return 0
        if args.command == "import-history":
            import dataclasses
            from .importers import HistoryImportEngine
            if args.project:
                from .project_registry import lookup, validate_slug
                slug = validate_slug(args.project)
                if not lookup(config.state_path, slug):
                    # A typo here would attribute the backfill to a project
                    # that does not exist, which no later query could correct.
                    raise PolicyError(f"import-project-not-registered:{slug}")
            engine = HistoryImportEngine(config)
            receipt = engine.import_file(
                args.file, source_format=args.format,
                project=args.project, dry_run=args.dry_run,
            )
            print(json.dumps(dataclasses.asdict(receipt), ensure_ascii=False, indent=2))
            return 0
        if args.command == "parity":
            import dataclasses
            from .parity import SharedBrainParityManager
            mgr = SharedBrainParityManager(config.vault_path)
            report = mgr.align_shared_brain()
            print(json.dumps(dataclasses.asdict(report), ensure_ascii=False, indent=2))
            return 0
        if args.command == "requeue-hermes-finalize":
            from .hermes_requeue import (
                DEFAULT_REASONS, apply_requeue_plan, build_requeue_plan, revert_requeue,
            )
            if args.revert:
                result = revert_requeue(config, args.revert, force=args.force)
            elif args.apply:
                result = apply_requeue_plan(config, json.loads(args.apply.read_text(encoding="utf-8")))
            else:
                result = build_requeue_plan(config, reasons=tuple(args.reason) or DEFAULT_REASONS)
                if args.plan_out:
                    args.plan_out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
                    result = {"status": "planned", "plan": str(args.plan_out), "summary": result["summary"]}
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        if args.command == "repair-memory":
            from .memory_repair import apply_repair_plan, build_repair_plan, retire_rule_candidate, revert_repair
            if args.retire_candidate:
                result = retire_rule_candidate(
                    config, args.retire_candidate, classification=args.classification, evidence=args.evidence,
                )
            elif args.revert:
                result = revert_repair(config, args.revert, force=args.force)
            elif args.apply:
                plan = json.loads(args.apply.read_text(encoding="utf-8"))
                result = apply_repair_plan(config, plan)
            else:
                result = build_repair_plan(
                    config, claude_root=args.claude_root, codex_root=args.codex_root,
                    history_files=args.history,
                )
                if args.plan_out:
                    args.plan_out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
                    result = {"status": "planned", "plan": str(args.plan_out), "summary": result["summary"]}
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        if args.command == "register":
            from .hook_install import gitignore_unignored, install as hook_install
            from .project_registry import register as registry_register
            memory_os_root = Path(__file__).resolve().parents[1]
            cfg_abs = args.config.resolve()
            entry = registry_register(config.state_path, args.root, args.project)
            root_path = Path(entry.root)
            hooks_changed = {
                rt: hook_install(
                    root_path, runtime=rt, memory_os_root=memory_os_root,
                    config_path=cfg_abs, project=entry.project,
                )
                for rt in ("claude", "codex")
            }
            print(json.dumps({
                "status": "registered",
                "project": entry.project,
                "root": entry.root,
                "hooks_changed": hooks_changed,
                "gitignore_not_excluding": gitignore_unignored(root_path),
            }, ensure_ascii=False))
            return 0
        if args.command == "unregister":
            from .hook_install import uninstall as hook_uninstall
            from .project_registry import unregister as registry_unregister
            root_path = args.root.resolve()
            registry_removed = registry_unregister(config.state_path, root_path)
            hooks_changed = {
                rt: hook_uninstall(root_path, runtime=rt) for rt in ("claude", "codex")
            }
            print(json.dumps({
                "status": "unregistered",
                "root": str(root_path),
                "registry_removed": registry_removed,
                "hooks_changed": hooks_changed,
            }, ensure_ascii=False))
            return 0
    except DuplicateEvent as exc:
        print(json.dumps({"status": "duplicate", "event_path": str(exc)}))
        return 0
    except NoMemory as exc:
        if "config" in locals() and config is not None:
            try:
                write_health(config.state_path, "drain", "ok", "no-memory")
            except OSError:
                pass
        print(json.dumps({"status": "no_memory", "reason": str(exc)}))
        return 0
    except MemoryError as exc:
        if "config" in locals() and config is not None:
            try:
                write_health(config.state_path, "drain", "blocked", str(exc))
            except OSError:
                pass
        print(json.dumps({"status": "blocked", "reason": str(exc)}), file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

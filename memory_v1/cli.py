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
from .core import DuplicateEvent, MemoryConfig, MemoryError, NoMemory, write_health
from .doctor import run_doctor
from .provider import StructuredResponsesProvider, create_provider


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pz-memory")
    parser.add_argument("--config", required=True, type=Path)
    commands = parser.add_subparsers(dest="command", required=True)
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
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if os.environ.get("PZ_MEMORY_INVOKED_BY") == "memory-v1" and args.command in {"flush", "checkpoint", "drain"}:
        print(json.dumps({"status": "noop", "reason": "recursion-guard"}))
        return 0
    try:
        config = MemoryConfig.load(args.config)
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
            print(json.dumps({"status": "ok", "results": results}, ensure_ascii=False))
            return 0
        if args.command == "promote-knowledge":
            from .compiler import promote_knowledge_outbox
            res = promote_knowledge_outbox(config, outbox_root=args.outbox)
            print(json.dumps(res, ensure_ascii=False))
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

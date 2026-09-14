"""Guards for the user-facing Hermes processes (Desktop dashboard, Telegram gateway).

Service profiles are not chat surfaces
    ``pz-memory-compiler`` is the single knowledge-compiler owner. It has to
    stay under ``profiles/`` because it borrows the root openai-codex OAuth
    grant through Hermes' root fallback; a home outside would have no grant.
    Hermes 0.21.1 has no way to hide a profile, so Desktop listed it next to
    Astra. A profile is a service profile when its home carries
    ``service-profile.json`` (schema below). In a guarded process such a
    profile is left out of every profile listing and cannot be resolved by
    name, so the dashboard's profile routes and the chat gateway's
    ``session.create``/prompt paths refuse it. The compiler's own process is
    not guarded and runs as before.

Backend updates follow the runbook
    Desktop offers "Update" for the backend checkout, which would pull
    upstream into the running Contabo install past the memory plugin's tested
    version. In the dashboard process the updater reports itself as managed
    outside the dashboard, so the check shows no update and the apply call is
    refused; updates go through the documented backup-and-verify procedure.

Kanban worker protocol only for dispatched workers
    Hermes 0.21.1 injects its Kanban *worker* protocol ("You have been assigned
    ONE task ... Call ``kanban_show()`` first (no args)") into every agent that
    has the ``kanban_show`` tool. pz-orchestrator enables the ``kanban``
    toolset, so ordinary conversations (Desktop, Telegram, terminal) received
    it without any task and opened with an argument-less ``kanban_show`` that
    failed with "task_id is required". In agents that are not dispatched
    workers the protocol is replaced by conversation guidance: no task call
    without an explicit id, find or create a task only for real work, ids only
    from tool results. Dispatched workers (``HERMES_KANBAN_TASK`` set by the
    dispatcher) keep the original protocol; the tools' own task_id checks are
    unchanged.

The Kanban guidance guard runs for every ``pz-hermes`` invocation; the profile
and update guards only with ``PZ_HERMES_USER_SURFACE=1`` (dashboard, Telegram).
"""
from __future__ import annotations

import functools
import json
import os
import sys
from pathlib import Path
from typing import Any

SERVICE_MARKER = "service-profile.json"
SERVICE_SCHEMA = "pikselzone-service-profile-v1"
UPDATE_MESSAGE = (
    "Hermes updates on this host are applied with the Pikselzone runbook "
    "(backup, memory plugin compatibility check, restart), not from the dashboard."
)


class ServiceProfileRefused(FileNotFoundError):
    """A service profile was requested from a user-facing surface."""


def service_profile_marker(home: Path) -> dict[str, Any] | None:
    try:
        value = json.loads((Path(home) / SERVICE_MARKER).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if isinstance(value, dict) and value.get("schema") == SERVICE_SCHEMA and value.get("service"):
        return value
    return None


def is_service_profile_home(home: Path) -> bool:
    return service_profile_marker(home) is not None


def install_profile_guards(profiles_mod: Any, *, process_home: Path | None) -> None:
    """Hide and refuse service profiles in this process. Idempotent."""
    if getattr(profiles_mod, "__pz_service_profile_guard__", False):
        return
    own_home = Path(process_home).resolve() if process_home else None

    def foreign_service(home: Path) -> bool:
        return is_service_profile_home(home) and Path(home).resolve() != own_home

    original_iter = profiles_mod._iter_named_profile_dirs

    @functools.wraps(original_iter)
    def iter_named_profile_dirs(*args: Any, **kwargs: Any):
        return [entry for entry in original_iter(*args, **kwargs) if not foreign_service(entry)]

    original_get = profiles_mod.get_profile_dir

    @functools.wraps(original_get)
    def get_profile_dir(name: Any, *args: Any, **kwargs: Any):
        home = original_get(name, *args, **kwargs)
        if foreign_service(Path(home)):
            raise ServiceProfileRefused(
                f"Profile '{name}' is a service profile and is not available for chats."
            )
        return home

    original_exists = profiles_mod.profile_exists

    @functools.wraps(original_exists)
    def profile_exists(name: Any, *args: Any, **kwargs: Any):
        # Hermes' own profile_exists resolves through the module's get_profile_dir,
        # so a refusal can surface from inside the original call as well.
        try:
            if not original_exists(name, *args, **kwargs):
                return False
            get_profile_dir(name)
        except ServiceProfileRefused:
            return False
        return True

    profiles_mod._iter_named_profile_dirs = iter_named_profile_dirs
    profiles_mod.get_profile_dir = get_profile_dir
    profiles_mod.profile_exists = profile_exists
    profiles_mod.__pz_service_profile_guard__ = True


def install_update_guard() -> None:
    import hermes_cli.web_server_files as files

    files._dashboard_local_update_managed_externally = lambda: True
    try:
        import hermes_cli.web_routers.actions as actions
    except ImportError:
        return
    actions._MANAGED_EXTERNALLY_MESSAGE = UPDATE_MESSAGE


KANBAN_CHAT_GUIDANCE = (
    "# Kanban in conversations\n"
    "This is a conversation, not a dispatched Kanban task: you have no current task id and "
    "`$HERMES_KANBAN_TASK` is not set. Never call `kanban_show` or any other task tool without an "
    "explicit `task_id`. Greetings, questions and explanations need no Kanban call and no new task.\n"
    "When the user gives real work that should be tracked: look for an existing matching task with "
    "`kanban_list`; if exactly one fits, continue with its id; if several plausibly fit, ask the user "
    "the smallest question needed to pick one; if none fits, create one with `kanban_create` scoped to "
    "what the user asked. Take task ids only from tool results, pass `task_id=` explicitly on every later "
    "Kanban call in this conversation, and never reuse an id from another conversation or profile. When "
    "part of the work goes to another profile or a delegated agent, create a child task with "
    "`parents=[<task id>]` and a real profile as assignee, or pass the task id in the delegation goal."
)


def is_dispatched_kanban_worker() -> bool:
    """A worker the Kanban dispatcher started for one task (not a delegate child)."""
    if not os.environ.get("HERMES_KANBAN_TASK"):
        return False
    try:
        from agent import delegation_context
        return bool(delegation_context.is_dispatcher_owned_worker_context())
    except Exception:
        return True


def install_kanban_guidance_guard(system_prompt_mod: Any, worker_guidance: str) -> bool:
    """Give non-worker agents conversation guidance instead of the worker protocol. Idempotent."""
    if getattr(system_prompt_mod, "__pz_kanban_guidance_guard__", False):
        return True
    original = getattr(system_prompt_mod, "_tool_guidance_block", None)
    if original is None or not worker_guidance:
        return False

    @functools.wraps(original)
    def _tool_guidance_block(agent: Any):
        text = original(agent)
        if text and worker_guidance in text and not is_dispatched_kanban_worker():
            return text.replace(worker_guidance, KANBAN_CHAT_GUIDANCE)
        return text

    system_prompt_mod._tool_guidance_block = _tool_guidance_block
    system_prompt_mod.__pz_kanban_guidance_guard__ = True
    return True


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    # Hermes applies ``-p <profile>`` from sys.argv while hermes_cli.main is
    # imported, so the argv it expects must be in place before any Hermes import.
    sys.argv = ["hermes", *args]
    from hermes_cli.main import main as hermes_main

    if os.environ.get("PZ_HERMES_USER_SURFACE") == "1":
        from hermes_cli import profiles
        from hermes_constants import get_hermes_home

        # The process home after the profile override: a service profile may run
        # its own process, and only other service profiles are refused.
        install_profile_guards(profiles, process_home=Path(get_hermes_home()))
        if "dashboard" in args:
            install_update_guard()
    try:
        from agent import system_prompt
        from agent.prompt_builder import KANBAN_GUIDANCE

        if not install_kanban_guidance_guard(system_prompt, KANBAN_GUIDANCE):
            print("pz-hermes: Kanban guidance guard not installed (Hermes seam changed)", file=sys.stderr)
    except ImportError as exc:
        print(f"pz-hermes: Kanban guidance guard unavailable: {exc}", file=sys.stderr)
    return hermes_main()


if __name__ == "__main__":
    raise SystemExit(main())

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

Enable with ``PZ_HERMES_USER_SURFACE=1`` (see ``/usr/local/bin/pz-hermes``).
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


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    from hermes_cli import profiles

    home = os.environ.get("HERMES_HOME", "").strip()
    install_profile_guards(profiles, process_home=Path(home) if home else None)
    if "dashboard" in args:
        install_update_guard()
    from hermes_cli.main import main as hermes_main

    sys.argv = ["hermes", *args]
    return hermes_main()


if __name__ == "__main__":
    raise SystemExit(main())

"""Service profiles stay out of user surfaces; user profiles are untouched."""
from __future__ import annotations

import json
import tempfile
import types
import unittest
from pathlib import Path

from memory_v1 import hermes_guards as guards


def _fake_profiles(root: Path) -> types.ModuleType:
    """The three hermes_cli.profiles functions the guard wraps, over a real directory tree."""
    module = types.ModuleType("hermes_cli.profiles")
    profiles_root = root / "profiles"

    def _iter_named_profile_dirs(*, live_only=True):
        return [p for p in sorted(profiles_root.iterdir()) if p.is_dir()]

    def get_profile_dir(name):
        return root if name == "default" else profiles_root / name

    def profile_exists(name):
        # Like Hermes 0.21.1: resolves through the module-level get_profile_dir.
        return Path(module.get_profile_dir(name)).is_dir()

    def list_profiles():  # resolves the helper through module globals, like Hermes does
        return ["default", *(p.name for p in module._iter_named_profile_dirs())]

    module._iter_named_profile_dirs = _iter_named_profile_dirs
    module.get_profile_dir = get_profile_dir
    module.profile_exists = profile_exists
    module.list_profiles = list_profiles
    return module


class ServiceProfileGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        for name in ("pz-orchestrator", "pz-sengur", "pz-memory-compiler"):
            (self.root / "profiles" / name).mkdir(parents=True)
        self.compiler = self.root / "profiles" / "pz-memory-compiler"
        (self.compiler / guards.SERVICE_MARKER).write_text(json.dumps({
            "schema": guards.SERVICE_SCHEMA, "service": "knowledge-compiler",
        }), encoding="utf-8")
        self.profiles = _fake_profiles(self.root)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _guard(self, process_home=None):
        guards.install_profile_guards(self.profiles, process_home=process_home or self.root)

    def test_service_profile_is_left_out_of_listings(self):
        self._guard()
        self.assertEqual(["default", "pz-orchestrator", "pz-sengur"], self.profiles.list_profiles())

    def test_service_profile_cannot_be_resolved_for_a_chat(self):
        self._guard()
        with self.assertRaises(guards.ServiceProfileRefused):
            self.profiles.get_profile_dir("pz-memory-compiler")
        self.assertFalse(self.profiles.profile_exists("pz-memory-compiler"))

    def test_user_profiles_and_switching_are_unchanged(self):
        self._guard()
        for name in ("pz-orchestrator", "pz-sengur", "default"):
            self.assertTrue(self.profiles.profile_exists(name))
            self.assertTrue(Path(self.profiles.get_profile_dir(name)).is_dir())

    def test_compiler_process_keeps_its_own_home(self):
        self._guard(process_home=self.compiler)
        self.assertEqual(self.compiler, self.profiles.get_profile_dir("pz-memory-compiler"))

    def test_marker_needs_the_schema_not_just_a_file(self):
        (self.root / "profiles" / "pz-sengur" / guards.SERVICE_MARKER).write_text("{}", encoding="utf-8")
        self._guard()
        self.assertTrue(self.profiles.profile_exists("pz-sengur"))

    def test_guard_is_idempotent(self):
        self._guard()
        wrapped = self.profiles.get_profile_dir
        self._guard()
        self.assertIs(wrapped, self.profiles.get_profile_dir)

    def test_refusal_is_a_file_not_found_error(self):
        # Hermes' tui gateway turns FileNotFoundError from profile resolution into
        # a normal "profile does not exist" reply rather than a server error.
        self.assertTrue(issubclass(guards.ServiceProfileRefused, FileNotFoundError))


def _fake_bot_mode(root: Path):
    """tools.bot_mode_probe / bot_relay / bot_mode_dm seams, with consumers resolving them
    through module globals the way Hermes 0.21.3 does."""
    import shlex

    probe = types.ModuleType("tools.bot_mode_probe")
    probe._roster = lambda r: [("default", r), *((p.name, p) for p in sorted((r / "profiles").iterdir()))]
    probe.teammates = lambda: [name for name, _home in probe._roster(root)]

    relay = types.ModuleType("tools.bot_relay")
    relay._hermes_cli = lambda: "/venv/bin/hermes"
    relay.local_delivery_command = lambda profile, qf: [relay._hermes_cli(), "-p", profile, "chat", "--query-file", qf]

    dm = types.ModuleType("tools.bot_mode_dm")
    dm._delivery_command = lambda argv, dm_file, *, stdin_file, profile_home=None, author=None: shlex.join(
        ["python", "bot_mode_dm.py", "--run-delivery", dm_file, *argv])
    return probe, relay, dm


class BotModeGuardTests(unittest.TestCase):
    """Bot Mode rosters and DM children honour the service-profile boundary."""

    def setUp(self) -> None:
        from unittest import mock
        self.mock = mock
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        for name in ("pz-orchestrator", "pz-sengur", "pz-memory-compiler"):
            (self.root / "profiles" / name).mkdir(parents=True)
        self.compiler = self.root / "profiles" / "pz-memory-compiler"
        (self.compiler / guards.SERVICE_MARKER).write_text(json.dumps({
            "schema": guards.SERVICE_SCHEMA, "service": "knowledge-compiler",
        }), encoding="utf-8")
        self.cli = self.root / "bin" / "hermes"
        self.cli.parent.mkdir()
        self.cli.write_text("#!/bin/sh\n", encoding="utf-8")
        self.cli.chmod(0o755)
        self.probe, self.relay, self.dm = _fake_bot_mode(self.root)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _guard(self, process_home=None, cli=None):
        return guards.install_bot_mode_guards(
            self.probe, self.relay, self.dm, process_home=process_home or self.root,
            cli=str(self.cli) if cli is None else cli)

    def test_service_profile_is_not_a_teammate(self):
        self.assertTrue(self._guard())
        self.assertEqual(["default", "pz-orchestrator", "pz-sengur"], self.probe.teammates())

    def test_compiler_process_keeps_its_own_roster_entry(self):
        self._guard(process_home=self.compiler)
        self.assertIn("pz-memory-compiler", self.probe.teammates())

    def test_dm_child_starts_through_the_guarded_cli(self):
        self._guard()
        command = self.dm._delivery_command(["hermes", "-p", "pz-sengur", "chat", "-c", "Bot Chat"], "/tmp/dm",
                                            stdin_file=False)
        self.assertIn(f"{self.cli} -p pz-sengur chat", command)
        self.assertNotIn(" hermes -p", command)
        self.assertEqual(str(self.cli), self.relay.local_delivery_command("pz-sengur", "/tmp/q")[0])

    def test_guarded_cli_keeps_the_basename_hermes_routing_needs(self):
        self.assertTrue(guards._is_hermes_cli(str(self.cli)))
        with self.mock.patch.dict("os.environ", {guards.BOT_CLI_ENV: str(self.cli)}):
            self.assertEqual(str(self.cli), guards.guarded_bot_cli())
        wrong_name = self.root / "bin" / "pz-hermes"
        wrong_name.write_text("#!/bin/sh\n", encoding="utf-8")
        wrong_name.chmod(0o755)
        for value in (str(wrong_name), str(self.root / "missing" / "hermes"), ""):
            with self.mock.patch.dict("os.environ", {guards.BOT_CLI_ENV: value}):
                self.assertIsNone(guards.guarded_bot_cli())

    def test_without_a_guarded_cli_roster_is_still_filtered_and_install_reports_it(self):
        self.assertFalse(self._guard(cli=""))
        self.assertNotIn("pz-memory-compiler", self.probe.teammates())
        self.assertEqual("/venv/bin/hermes", self.relay._hermes_cli())

    def test_install_is_idempotent_and_reports_a_missing_seam(self):
        self._guard()
        wrapped = self.probe._roster
        self.assertTrue(self._guard())
        self.assertIs(wrapped, self.probe._roster)
        self.assertFalse(guards.install_bot_mode_guards(types.ModuleType("empty"), self.relay, self.dm,
                                                        process_home=self.root, cli=str(self.cli)))


class LauncherOrderTests(unittest.TestCase):
    def test_hermes_sees_its_own_argv_before_any_hermes_import(self):
        # The Telegram unit runs `pz-hermes -p pz-orchestrator gateway run`; Hermes
        # strips -p while hermes_cli.main is imported, so argv must already be set.
        import builtins
        import sys
        from unittest import mock

        seen = {}
        real_import = builtins.__import__

        def recording_import(name, *args, **kwargs):
            if name.startswith("hermes_cli") and "argv" not in seen:
                seen["argv"] = list(sys.argv)
            if name in {"hermes_cli.main", "hermes_cli", "hermes_constants"}:
                raise ImportError("stop before running Hermes")
            return real_import(name, *args, **kwargs)

        with mock.patch.object(sys, "argv", ["guards.py"]), mock.patch("builtins.__import__", side_effect=recording_import):
            with self.assertRaises(ImportError):
                guards.main(["-p", "pz-orchestrator", "gateway", "run"])
        self.assertEqual(["hermes", "-p", "pz-orchestrator", "gateway", "run"], seen["argv"])


WORKER_PROTOCOL = "# Kanban task execution protocol\nYou have been assigned ONE task. 1. **Orient.** Call `kanban_show()` first (no args)."


def _fake_system_prompt():
    module = types.ModuleType("agent.system_prompt")

    def _tool_guidance_block(agent):
        return " ".join(g for g in ("MEMORY GUIDANCE", agent.kanban) if g) or None

    module._tool_guidance_block = _tool_guidance_block
    return module


class KanbanGuidanceGuardTests(unittest.TestCase):
    """Only a dispatched Kanban worker gets the no-arguments kanban_show protocol."""

    def setUp(self):
        from unittest import mock
        self.mock = mock
        self.module = _fake_system_prompt()
        self.assertTrue(guards.install_kanban_guidance_guard(self.module, WORKER_PROTOCOL))

    def _agent(self, kanban=WORKER_PROTOCOL):
        return types.SimpleNamespace(kanban=kanban)

    def _env(self, task=None, dispatcher_owned=True):
        env = {"HERMES_KANBAN_TASK": task} if task else {}
        ctx = types.ModuleType("agent.delegation_context")
        ctx.is_dispatcher_owned_worker_context = lambda: dispatcher_owned
        agent_pkg = types.ModuleType("agent")
        agent_pkg.delegation_context = ctx
        return (self.mock.patch.dict("os.environ", env, clear=False),
                self.mock.patch.dict("sys.modules", {"agent": agent_pkg, "agent.delegation_context": ctx}))

    def test_conversation_without_task_gets_conversation_guidance(self):
        env, mods = self._env()
        with env, mods, self.mock.patch.dict("os.environ", {}, clear=False):
            import os
            os.environ.pop("HERMES_KANBAN_TASK", None)
            text = self.module._tool_guidance_block(self._agent())
        self.assertNotIn("(no args)", text)
        self.assertIn("explicit `task_id`", text)
        self.assertIn("MEMORY GUIDANCE", text)  # the rest of the block is untouched

    def test_dispatched_worker_keeps_the_worker_protocol(self):
        env, mods = self._env(task="t_1234", dispatcher_owned=True)
        with env, mods:
            self.assertIn("(no args)", self.module._tool_guidance_block(self._agent()))

    def test_delegated_child_of_a_worker_is_not_treated_as_the_worker(self):
        env, mods = self._env(task="t_1234", dispatcher_owned=False)
        with env, mods:
            self.assertNotIn("(no args)", self.module._tool_guidance_block(self._agent()))

    def test_agent_without_kanban_tools_is_unchanged(self):
        self.assertEqual("MEMORY GUIDANCE", self.module._tool_guidance_block(self._agent(kanban="")))

    def test_install_is_idempotent_and_reports_a_missing_seam(self):
        wrapped = self.module._tool_guidance_block
        self.assertTrue(guards.install_kanban_guidance_guard(self.module, WORKER_PROTOCOL))
        self.assertIs(wrapped, self.module._tool_guidance_block)
        self.assertFalse(guards.install_kanban_guidance_guard(types.ModuleType("empty"), WORKER_PROTOCOL))


if __name__ == "__main__":
    unittest.main()

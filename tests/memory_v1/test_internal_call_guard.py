"""The re-entrancy flag must survive concurrent provider calls.

Codex reproduced the interleaving locally against the current plugin: call A
finds no previous value and sets ``PZ_MEMORY_INTERNAL_CALL=1``; call B, already
inside A's window, records "1" as the value it must restore; A finishes and
removes the variable; B finishes and puts "1" back. Both calls have returned,
yet the flag stays on, so every later lifecycle callback in that process is
dropped as an internal recursive call.

These tests pin the fixed behaviour. They say nothing about the 16 September VPS
incident, where lifecycle callbacks stopped after a multi-session finalize: that
still has no evidence tying it to this race, and this file must not be cited as
if it had.
"""
from __future__ import annotations

import importlib.util
import os
import sys
import threading
import types
import unittest
from pathlib import Path
from unittest import mock

ENV = "PZ_MEMORY_INTERNAL_CALL"
STATE_KEY = "_pz_memory_internal_call_state_v1"


def load_hermes_plugin():
    p = Path(__file__).resolve().parent.parent.parent / "hermes_plugins" / "pz-memory-v1" / "__init__.py"
    spec = importlib.util.spec_from_file_location("pz_memory_v1_guard_test", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _BlockingFacade:
    """A provider facade that parks inside the guarded window until released."""

    def __init__(self, entered: threading.Event, release: threading.Event):
        self.entered = entered
        self.release = release

    def complete_structured(self, **_kwargs):
        self.entered.set()
        if not self.release.wait(timeout=10):
            raise AssertionError("blocked provider call was never released")
        return types.SimpleNamespace(parsed={"status": "empty"}, provider="custom", model="m")


class InternalCallGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        sys.modules.pop(STATE_KEY, None)
        self.addCleanup(sys.modules.pop, STATE_KEY, None)
        self.plugin = load_hermes_plugin()

    def _plugin_llm_module(self, facade):
        module = types.ModuleType("agent.plugin_llm")
        module.PluginLlm = mock.Mock(return_value=facade)
        module.PluginLlmTextInput = lambda **kw: kw
        return module

    def test_interleaved_summaries_leave_the_flag_clear(self):
        a_in, a_go = threading.Event(), threading.Event()
        b_in, b_go = threading.Event(), threading.Event()
        errors: list[BaseException] = []

        def run(facade):
            module = self._plugin_llm_module(facade)
            try:
                with mock.patch.dict(sys.modules, {"agent.plugin_llm": module}):
                    self.plugin._summarize_with_hermes("transcript")
            except BaseException as exc:  # pragma: no cover - surfaced by the assertion below
                errors.append(exc)

        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(ENV, None)
            a = threading.Thread(target=run, args=(_BlockingFacade(a_in, a_go),))
            b = threading.Thread(target=run, args=(_BlockingFacade(b_in, b_go),))
            a.start()
            self.assertTrue(a_in.wait(timeout=10), "first call never reached the provider")
            b.start()
            self.assertTrue(b_in.wait(timeout=10), "second call never reached the provider")
            # Both are inside the guarded window: the flag is on for either of them.
            self.assertEqual("1", os.environ.get(ENV))
            a_go.set()
            a.join(timeout=10)
            # A has returned while B is still inside: the flag must stay on for B.
            self.assertEqual("1", os.environ.get(ENV))
            b_go.set()
            b.join(timeout=10)

            self.assertEqual([], errors)
            self.assertFalse(a.is_alive() or b.is_alive())
            self.assertIsNone(os.environ.get(ENV), "the flag stayed on after both calls returned")
            self.assertFalse(self.plugin._is_internal_call())

    def test_nesting_restores_a_pre_existing_value(self):
        with mock.patch.dict(os.environ, {ENV: "previous"}):
            with self.plugin.internal_call_guard():
                self.assertEqual("1", os.environ[ENV])
                with self.plugin.internal_call_guard():
                    self.assertEqual("1", os.environ[ENV])
                self.assertEqual("1", os.environ[ENV], "an inner exit must not release the flag")
            self.assertEqual("previous", os.environ[ENV])

    def test_the_guard_still_blocks_a_recursive_lifecycle_call(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(ENV, None)
            with self.plugin.internal_call_guard():
                self.assertTrue(self.plugin._is_internal_call())
            self.assertFalse(self.plugin._is_internal_call())

    def test_a_failing_provider_call_releases_the_flag(self):
        facade = mock.Mock()
        facade.complete_structured.side_effect = RuntimeError("provider down")
        module = self._plugin_llm_module(facade)
        with mock.patch.dict(os.environ, {}, clear=False), \
             mock.patch.dict(sys.modules, {"agent.plugin_llm": module}):
            os.environ.pop(ENV, None)
            self.assertEqual((None, "", ""), self.plugin._summarize_with_hermes("transcript"))
            self.assertIsNone(os.environ.get(ENV))

    def test_plugin_and_engine_share_one_counter(self):
        # The plugin is loaded by path and the compiler worker by import, so the
        # shared state has to live in sys.modules or the two would clear each
        # other's flag. An engine-side exit while the plugin is still inside must
        # not release the flag.
        from memory_v1 import hermes_compiler

        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(ENV, None)
            with self.plugin.internal_call_guard():
                with hermes_compiler.internal_call_guard():
                    self.assertEqual("1", os.environ[ENV])
                self.assertEqual("1", os.environ[ENV])
            self.assertIsNone(os.environ.get(ENV))
            self.assertIs(sys.modules[STATE_KEY], hermes_compiler._internal_call_state())


if __name__ == "__main__":
    unittest.main()

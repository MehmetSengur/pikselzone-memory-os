"""Which summarizer and compiler models a memory config may name."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from memory_v1.core import ConfigError, MemoryConfig


class ModelRoutingTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        (self.root / "vault").mkdir()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _config(self, flush: str, compiler: str, mode: str = "runtime-native") -> MemoryConfig:
        return MemoryConfig.from_dict({
            "role": "memory-engine", "vault_path": str(self.root / "vault"),
            "state_path": str(self.root / "state"), "runtimes": ["hermes"],
            "transcript_roots": {"hermes": [str(self.root)]},
            "can_write_event_memory": True, "can_run_compiler": True,
            "models": {"flush": flush, "compiler": compiler}, "provider": {"mode": mode},
        })

    def test_gpt6_luna_summarizer_and_sol_compiler_are_accepted(self):
        for mode in ("runtime-native", "external-openai-api"):
            config = self._config("gpt-6-luna", "gpt-6-sol", mode)
            self.assertEqual(("gpt-6-luna", "gpt-6-sol"), (config.flush_model, config.compiler_model))

    def test_gpt56_pair_still_loads_so_a_rollback_keeps_memory_running(self):
        config = self._config("gpt-5.6-luna", "gpt-5.6-terra")
        self.assertEqual("gpt-5.6-terra", config.compiler_model)

    def test_unnamed_models_default_to_gpt6_luna_and_sol(self):
        config = MemoryConfig.from_dict({
            "role": "memory-engine", "vault_path": str(self.root / "vault"),
            "state_path": str(self.root / "state"), "runtimes": ["hermes"],
            "transcript_roots": {"hermes": [str(self.root)]},
            "can_write_event_memory": True, "can_run_compiler": True,
            "provider": {"mode": "external-openai-api"},
        })
        self.assertEqual(("gpt-6-luna", "gpt-6-sol"), (config.flush_model, config.compiler_model))

    def test_unknown_model_is_refused(self):
        with self.assertRaisesRegex(ConfigError, "memory-model-routing-forbidden"):
            self._config("gpt-6-astra", "gpt-6-sol")


if __name__ == "__main__":
    unittest.main()

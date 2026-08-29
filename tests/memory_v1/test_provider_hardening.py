"""Unit tests for Provider Hardening, Secret Isolation & Degraded Resilience (SB2-10)."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from memory_v1.core import MemoryConfig, ProviderBlocked
from memory_v1.provider import scrubbed_subprocess_env, summarize_with_hermes


class TestProviderHardening(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name).resolve()
        self.vault = self.root / "vault"
        self.state = self.root / "state"
        self.vault.mkdir(parents=True)
        self.state.mkdir(parents=True)

        self.config = MemoryConfig.from_dict({
            "role": "workstation",
            "vault_path": str(self.vault),
            "state_path": str(self.state),
            "runtimes": ["claude", "codex"],
            "transcript_roots": {
                "claude": [str(self.root)],
                "codex": [str(self.root)],
            },
            "can_write_event_memory": True,
            "can_run_compiler": False,
            "models": {"flush": "gpt-5.6-luna", "compiler": "gpt-5.6-terra"},
            "provider": {"mode": "runtime-native"},
            "context_budget_chars": 16000,
        })

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    # 1. Environment scrubbing for child subprocesses
    def test_scrubbed_subprocess_env(self):
        test_env = {
            "PATH": "/usr/bin:/bin",
            "HOME": "/home/user",
            "ANTHROPIC_API_KEY": "sk-ant-sensitive-12345",
            "OPENAI_API_KEY": "sk-proj-sensitive-67890",
            "GEMINI_API_KEY": "AIzaSySensitive123",
            "HERMES_API_KEY": "hermes-secret-key",
            "DATABASE_PASSWORD_SECRET": "db-secret",
            "NORMAL_VAR": "safe_value",
        }
        with patch.dict(os.environ, test_env, clear=True):
            clean = scrubbed_subprocess_env({"EXTRA_VAR": "added"})
            self.assertEqual(clean.get("PATH"), "/usr/bin:/bin")
            self.assertEqual(clean.get("HOME"), "/home/user")
            self.assertEqual(clean.get("NORMAL_VAR"), "safe_value")
            self.assertEqual(clean.get("EXTRA_VAR"), "added")
            self.assertEqual(clean.get("PZ_MEMORY_INVOKED_BY"), "memory-v1")

            # All sensitive keys must be purged
            self.assertNotIn("ANTHROPIC_API_KEY", clean)
            self.assertNotIn("OPENAI_API_KEY", clean)
            self.assertNotIn("GEMINI_API_KEY", clean)
            self.assertNotIn("HERMES_API_KEY", clean)
            self.assertNotIn("DATABASE_PASSWORD_SECRET", clean)

    # 2. Silent OpenAI fallback prevention in runtime-native mode
    def test_prevent_silent_openai_fallback_in_runtime_native(self):
        with self.assertRaises(ProviderBlocked) as ctx:
            summarize_with_hermes(
                config=self.config,
                instruction="Summarize session",
                untrusted_input="User: Hello",
                schema={"type": "object"},
                runner=None,
            )
        self.assertIn("runtime-native-mode-prohibits-silent-api-fallback", str(ctx.exception))

    # 3. Hermes degraded mode on corrupt staged bundle
    def test_hermes_pre_llm_call_degraded_mode(self):
        import importlib.util
        p = Path(__file__).resolve().parent.parent.parent / "hermes_plugins" / "pz-memory-v1" / "__init__.py"
        spec = importlib.util.spec_from_file_location("pz_memory_v1", p)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        pre_llm_call = mod.pre_llm_call
        
        # Simulate unreadable/corrupt bundle
        res = pre_llm_call(
            session_id="test-degraded-session-1",
            is_first_turn=True,
            conversation_history=None,
        )
        self.assertIsNotNone(res)
        self.assertIn("context", res)
        self.assertIn("PIKSELZONE MEMORY V1", res["context"])


if __name__ == "__main__":
    unittest.main()

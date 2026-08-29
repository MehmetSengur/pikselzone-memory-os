"""Zero-tool OpenAI Responses API adapter with strict structured output."""
from __future__ import annotations

import json
import os
import platform
import subprocess
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Optional

from .core import MemoryConfig, ProviderBlocked, SchemaError, discover_codex_binary


Transport = Callable[[str, dict[str, Any], dict[str, str]], dict[str, Any]]
KeychainReader = Callable[[str, Optional[str]], str]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _default_transport(
    url: str, payload: dict[str, Any], headers: dict[str, str]
) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", **headers},
        method="POST",
    )
    try:
        opener = urllib.request.build_opener(_NoRedirect)
        with opener.open(request, timeout=240) as response:
            value = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise ProviderBlocked(f"provider-http-{exc.code}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise ProviderBlocked(f"provider-transport:{exc.__class__.__name__}") from exc
    if not isinstance(value, dict):
        raise ProviderBlocked("provider-response-not-object")
    return value


def _read_macos_keychain(service: str, account: str | None = None) -> str:
    cmd = ["security", "find-generic-password", "-s", service, "-w"]
    if account:
        cmd.extend(["-a", account])
    try:
        res = subprocess.run(
            cmd, capture_output=True, text=True, timeout=5, check=False
        )
        if res.returncode != 0:
            return ""
        return res.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return ""


def check_macos_keychain_presence(service: str, account: str | None = None) -> bool:
    cmd = ["security", "find-generic-password", "-s", service]
    if account:
        cmd.extend(["-a", account])
    try:
        res = subprocess.run(
            cmd, capture_output=True, text=True, timeout=5, check=False
        )
        return res.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def resolve_credential(
    *,
    key_env: str = "OPENAI_API_KEY",
    credential_source: str = "env",
    keychain_service: str | None = None,
    keychain_account: str | None = None,
    keychain_reader: KeychainReader | None = None,
) -> tuple[str, str]:
    """Resolve API key per priority order:
    1. Process environment (key_env)
    2. Configured macOS Keychain service (if Darwin)
    3. Otherwise raise ProviderBlocked (missing)
    Never exposes key value in exceptions.
    """
    env_val = os.environ.get(key_env, "").strip()
    if env_val:
        return env_val, "env"

    if platform.system() == "Darwin" and keychain_service:
        reader = keychain_reader or _read_macos_keychain
        try:
            val = reader(keychain_service, keychain_account)
            if isinstance(val, str) and val.strip():
                return val.strip(), "macos-keychain"
        except ProviderBlocked:
            raise
        except Exception:
            raise ProviderBlocked("keychain-read-failed") from None

    raise ProviderBlocked(f"credential-missing:{key_env}")


class StructuredResponsesProvider:
    """Responses client that never exposes tools and never falls back models."""

    def __init__(
        self,
        *,
        api_base: str,
        key_env: str = "OPENAI_API_KEY",
        credential_source: str = "env",
        keychain_service: str | None = None,
        keychain_account: str | None = None,
        keychain_reader: KeychainReader | None = None,
        transport: Transport | None = None,
    ) -> None:
        self.api_base = api_base.rstrip("/")
        if self.api_base != "https://api.openai.com/v1":
            raise ProviderBlocked("provider-api-base-forbidden")
        self.key_env = key_env
        self.credential_source = credential_source
        self.keychain_service = keychain_service
        self.keychain_account = keychain_account
        self.keychain_reader = keychain_reader
        self.transport = transport or _default_transport

    @classmethod
    def from_config(
        cls,
        config: MemoryConfig,
        *,
        transport: Transport | None = None,
        keychain_reader: KeychainReader | None = None,
    ) -> "StructuredResponsesProvider":
        return cls(
            api_base=config.provider_api_base,
            key_env=config.provider_key_env,
            credential_source=config.provider_credential_source,
            keychain_service=config.provider_keychain_service,
            keychain_account=config.provider_keychain_account,
            keychain_reader=keychain_reader,
            transport=transport,
        )

    def request(
        self,
        *,
        model: str,
        instruction: str,
        untrusted_input: str,
        schema_name: str,
        schema: dict[str, Any],
        runtime: str | None = None,
    ) -> dict[str, Any]:
        key = ""
        try:
            key, _ = resolve_credential(
                key_env=self.key_env,
                credential_source=self.credential_source,
                keychain_service=self.keychain_service,
                keychain_account=self.keychain_account,
                keychain_reader=self.keychain_reader,
            )
        except ProviderBlocked:
            if self.transport is _default_transport:
                raise

        if self.transport is _default_transport and not key:
            raise ProviderBlocked(f"credential-missing:{self.key_env}")

        payload = {
            "model": model,
            "store": False,
            "input": [
                {"role": "system", "content": [{"type": "input_text", "text": instruction}]},
                {"role": "user", "content": [{"type": "input_text", "text": untrusted_input}]},
            ],
            "tools": [],
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": schema_name,
                    "strict": True,
                    "schema": schema,
                }
            },
        }
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        response = self.transport(f"{self.api_base}/responses", payload, headers)
        if not isinstance(response, dict):
            raise ProviderBlocked("provider-response-not-object")
        text = response.get("output_text")
        if not isinstance(text, str):
            parts: list[str] = []
            for item in response.get("output", []):
                if not isinstance(item, dict) or item.get("type") != "message":
                    continue
                for content in item.get("content", []):
                    if isinstance(content, dict) and content.get("type") == "output_text":
                        if isinstance(content.get("text"), str):
                            parts.append(content["text"])
            text = "".join(parts)
        if not text:
            raise ProviderBlocked("provider-output-empty")
        try:
            value = json.loads(text)
        except json.JSONDecodeError as exc:
            raise SchemaError("provider-output-not-json") from exc
        if not isinstance(value, dict):
            raise SchemaError("provider-output-not-object")
        return value


def _extract_json_block(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    return text


SENSITIVE_ENV_KEYS = {
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "GEMINI_API_KEY",
    "HERMES_API_KEY",
    "CLAUDE_API_KEY",
    "PZ_OPENAI_API_KEY",
    "OPENAI_KEY",
    "AWS_SECRET_ACCESS_KEY",
    "GITHUB_TOKEN",
    "GH_TOKEN",
}


def scrubbed_subprocess_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """Scrub sensitive API tokens and credentials from environment before passing to child subprocesses."""
    clean = {}
    for k, v in os.environ.items():
        k_upper = k.upper()
        if k in SENSITIVE_ENV_KEYS or k_upper in SENSITIVE_ENV_KEYS:
            continue
        if k_upper.endswith("_API_KEY") or k_upper.endswith("_SECRET") or k_upper.endswith("_TOKEN"):
            continue
        clean[k] = v
    clean["PZ_MEMORY_INVOKED_BY"] = "memory-v1"
    if extra:
        clean.update(extra)
    return clean


def summarize_with_claude(
    *,
    instruction: str,
    untrusted_input: str,
    schema: dict[str, Any],
    model: str | None = None,
    timeout: int = 60,
    runner: Callable[..., Any] | None = None,
) -> tuple[dict[str, Any], str, str]:
    if os.environ.get("PZ_MEMORY_INVOKED_BY") == "memory-v1":
        raise ProviderBlocked("claude-recursion-detected")

    prompt = (
        f"{instruction}\n\n"
        f"You must return ONLY a single valid JSON object strictly matching this schema:\n"
        f"{json.dumps(schema, ensure_ascii=False)}\n\n"
        f"Do not output markdown text, comments, or explanations outside the JSON object.\n\n"
        f"{untrusted_input}"
    )
    claude_model = model if (model and model != "runtime-native") else "haiku"
    cmd = [
        "claude", "-p",
        "--output-format", "json",
        "--no-session-persistence",
        "--safe-mode",
        "--tools", "",
        "--model", claude_model,
        prompt,
    ]
    env = scrubbed_subprocess_env()
    run_func = runner or subprocess.run
    try:
        res = run_func(
            cmd,
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=timeout,
            env=env,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise ProviderBlocked("claude-timeout") from None
    except OSError as exc:
        raise ProviderBlocked(f"claude-exec-error:{exc.__class__.__name__}") from None

    if res.returncode != 0:
        raise ProviderBlocked(f"claude-process-failed:{res.returncode}")

    try:
        data = json.loads(res.stdout)
    except json.JSONDecodeError as exc:
        raise ProviderBlocked("claude-output-not-json") from exc

    if not isinstance(data, dict):
        raise ProviderBlocked("claude-output-not-object")

    if data.get("is_error"):
        raise ProviderBlocked("claude-error-response")

    raw_text = data.get("result")
    if not isinstance(raw_text, str) or not raw_text.strip():
        raise ProviderBlocked("claude-result-empty")

    cleaned = _extract_json_block(raw_text)
    try:
        summary_obj = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise SchemaError("claude-result-not-json") from exc

    if not isinstance(summary_obj, dict):
        raise SchemaError("claude-result-not-object")

    used_model = model if (model and model != "runtime-native") else "haiku"
    model_usage = data.get("modelUsage")
    if isinstance(model_usage, dict) and model_usage:
        used_model = next(iter(model_usage.keys()))

    return summary_obj, used_model, "claude-subscription"


def summarize_with_codex(
    *,
    config: MemoryConfig | None = None,
    instruction: str,
    untrusted_input: str,
    schema: dict[str, Any],
    model: str | None = None,
    timeout: int = 60,
    runner: Callable[..., Any] | None = None,
) -> tuple[dict[str, Any], str, str]:
    if os.environ.get("PZ_MEMORY_INVOKED_BY") == "memory-v1":
        raise ProviderBlocked("codex-recursion-detected")

    codex_binary = discover_codex_binary(config) or "codex"
    prompt = f"{instruction}\n\n{untrusted_input}"

    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as schema_file:
        schema_path = Path(schema_file.name)
        try:
            json.dump(schema, schema_file)
            schema_file.flush()
            schema_file.close()

            cmd = [
                codex_binary, "exec",
                "--ephemeral",
                "-s", "read-only",
                "--skip-git-repo-check",
                "--ignore-rules",
                "--json",
                "--output-schema", str(schema_path),
            ]
            codex_model = model if (model and model != "runtime-native") else "gpt-5.6-luna"
            cmd.extend(["-m", codex_model])
            cmd.append(prompt)

            env = scrubbed_subprocess_env()
            run_func = runner or subprocess.run
            try:
                res = run_func(
                    cmd,
                    capture_output=True,
                    text=True,
                    stdin=subprocess.DEVNULL,
                    timeout=timeout,
                    env=env,
                    check=False,
                )
            except subprocess.TimeoutExpired:
                raise ProviderBlocked("codex-timeout") from None
            except OSError as exc:
                raise ProviderBlocked(f"codex-exec-error:{exc.__class__.__name__}") from None
        finally:
            schema_path.unlink(missing_ok=True)

    if res.returncode != 0:
        err_msg = res.stderr.strip()[:200] if res.stderr else "no-stderr"
        out_msg = res.stdout.strip()[:200] if res.stdout else "no-stdout"
        raise ProviderBlocked(f"codex-process-failed:{res.returncode}: err={err_msg} out={out_msg}")

    message_text: str | None = None
    turn_failed = False
    for line in res.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            evt = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(evt, dict):
            continue
        if evt.get("type") in {"turn.failed", "error"}:
            turn_failed = True
        if evt.get("type") == "item.completed":
            item = evt.get("item")
            if isinstance(item, dict) and item.get("type") == "agent_message":
                text = item.get("text")
                if isinstance(text, str):
                    message_text = text

    if turn_failed and not message_text:
        raise ProviderBlocked("codex-turn-failed")

    if not message_text:
        raise ProviderBlocked("codex-no-message")

    cleaned = _extract_json_block(message_text)
    try:
        summary_obj = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise SchemaError("codex-result-not-json") from exc

    if not isinstance(summary_obj, dict):
        raise SchemaError("codex-result-not-object")

    return summary_obj, codex_model, "chatgpt-subscription"


def summarize_with_hermes(
    *,
    config: MemoryConfig | None = None,
    instruction: str,
    untrusted_input: str,
    schema: dict[str, Any],
    model: str | None = None,
    runner: Callable[..., Any] | None = None,
) -> tuple[dict[str, Any], str, str]:
    if os.environ.get("PZ_MEMORY_INVOKED_BY") == "memory-v1":
        raise ProviderBlocked("hermes-recursion-detected")
    if runner:
        return runner(instruction=instruction, untrusted_input=untrusted_input, schema=schema)
    if config is not None:
        if config.provider_mode == "runtime-native":
            raise ProviderBlocked("runtime-native-mode-prohibits-silent-api-fallback")
        try:
            actual_model = model or (
                config.luna_model
                if config.luna_model not in {"runtime-native", "vps-hermes-runtime"}
                else "gpt-5.4-mini-2026-03-17"
            )
            provider = StructuredResponsesProvider.from_config(config)
            res = provider.request(
                model=actual_model,
                instruction=instruction,
                untrusted_input=untrusted_input,
                schema_name="pikselzone_memory_hermes_v1",
                schema=schema,
            )
            return res, actual_model, "pz-openai-serial"
        except (ProviderBlocked, SchemaError):
            raise
        except Exception as exc:
            raise ProviderBlocked(f"hermes-provider-error:{exc}") from exc
    raise ProviderBlocked("hermes-auxiliary-unconfigured")


class RuntimeNativeProvider:
    """Runtime-native subscription-backed memory summarization provider."""

    def __init__(
        self,
        config: MemoryConfig,
        *,
        claude_runner: Callable[..., Any] | None = None,
        codex_runner: Callable[..., Any] | None = None,
        hermes_runner: Callable[..., Any] | None = None,
    ) -> None:
        self.config = config
        self.claude_runner = claude_runner
        self.codex_runner = codex_runner
        self.hermes_runner = hermes_runner
        self.last_source_model: str | None = None
        self.last_source_provider: str | None = None

    def request(
        self,
        *,
        model: str,
        instruction: str,
        untrusted_input: str,
        schema_name: str,
        schema: dict[str, Any],
        runtime: str | None = None,
    ) -> dict[str, Any]:
        target_runtime = runtime or ("hermes" if self.config.role == "memory-engine" else "codex")
        if target_runtime == "claude":
            summary, actual_model, actual_provider = summarize_with_claude(
                instruction=instruction,
                untrusted_input=untrusted_input,
                schema=schema,
                model="haiku" if model == self.config.luna_model else model,
                runner=self.claude_runner,
            )
        elif target_runtime == "codex":
            summary, actual_model, actual_provider = summarize_with_codex(
                config=self.config,
                instruction=instruction,
                untrusted_input=untrusted_input,
                schema=schema,
                model=model,
                runner=self.codex_runner,
            )
        elif target_runtime == "hermes":
            summary, actual_model, actual_provider = summarize_with_hermes(
                config=self.config,
                instruction=instruction,
                untrusted_input=untrusted_input,
                schema=schema,
                model=model,
                runner=self.hermes_runner,
            )
        else:
            raise ProviderBlocked(f"unsupported-runtime:{target_runtime}")

        self.last_source_model = actual_model
        self.last_source_provider = actual_provider
        return summary


def create_provider(
    config: MemoryConfig,
    *,
    transport: Transport | None = None,
    claude_runner: Callable[..., Any] | None = None,
    codex_runner: Callable[..., Any] | None = None,
    hermes_runner: Callable[..., Any] | None = None,
) -> Any:
    if config.provider_mode == "runtime-native":
        return RuntimeNativeProvider(
            config,
            claude_runner=claude_runner,
            codex_runner=codex_runner,
            hermes_runner=hermes_runner,
        )
    if config.provider_mode == "external-openai-api":
        return StructuredResponsesProvider.from_config(config, transport=transport)
    raise ProviderBlocked(f"unknown-provider-mode:{config.provider_mode}")

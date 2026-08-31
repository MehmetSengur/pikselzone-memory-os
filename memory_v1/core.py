"""Common schema, configuration, transcript, state, and path primitives."""
from __future__ import annotations

import dataclasses
import datetime as dt
import fcntl
import hashlib
import json
import os
import re
import secrets
import stat
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
import platform
import shutil
import subprocess
from typing import Any, Iterator, Sequence


SCHEMA_NAME = "pikselzone-memory-event-v1"
RUNTIMES = {"codex", "claude", "hermes"}
EVENTS = {
    "session_start", "session_end", "pre_compact", "post_compact",
    "session_finalize", "session_reset", "subagent_start", "subagent_stop",
    # A turn checkpoint is raw, local state.  It becomes an event only when a
    # bounded recovery drain has to promote it after a crash.
    "turn_complete", "checkpoint_recovery",
}
SUMMARY_FIELDS = (
    "context", "important_conversations", "decisions", "learnings",
    "open_items", "evidence",
)
ALLOWED_KNOWLEDGE_ROOTS = {"concepts", "connections"}
SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(api[_-]?key|access[_-]?token|auth[_-]?token|client[_-]?secret|"
    r"private[_-]?key|password|passwd|credential)\b(\s*[:=]\s*[\"']?)"
    r"([^\s\"']{8,})"
)
SECRET_TOKEN = re.compile(
    r"(sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|"
    r"xox[baprs]-[A-Za-z0-9-]{10,}|AIza[A-Za-z0-9_-]{20,}|"
    r"Bearer\s+[A-Za-z0-9._-]{16,}|"
    r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{2,}\.[A-Za-z0-9_-]{2,})"
)
PRIVATE_KEY_BLOCK = re.compile(
    r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----.*?"
    r"-----END (?:RSA |EC |OPENSSH )?PRIVATE KEY-----",
    re.DOTALL,
)
DIRECTIVE_SHAPED = re.compile(
    r"(?i)(ignore (all|any|the) previous|system prompt|developer message|"
    r"run this command|execute this|write to|edit .*hook|tool[_ -]?call)"
)


class MemoryError(RuntimeError):
    """Base fail-closed error."""


class ConfigError(MemoryError):
    pass


class PolicyError(MemoryError):
    pass


class ProviderBlocked(MemoryError):
    pass


class SchemaError(MemoryError):
    pass


class NoMemory(MemoryError):
    pass


class DuplicateEvent(MemoryError):
    pass


@dataclasses.dataclass(frozen=True)
class NormalizedTranscript:
    text: str
    turn_count: int
    sha256: str

    @classmethod
    def from_checkpoint(cls, text: Any, digest: Any) -> "NormalizedTranscript":
        if not isinstance(text, str) or not text.strip():
            raise SchemaError("checkpoint-normalized-transcript-invalid")
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise SchemaError("checkpoint-source-digest-invalid")
        if sha256_bytes(text.encode("utf-8")) != digest:
            raise SchemaError("checkpoint-source-digest-mismatch")
        turns = sum(
            line.startswith(("USER: ", "ASSISTANT: ")) for line in text.splitlines()
        )
        if turns < 1:
            raise SchemaError("checkpoint-normalized-turns-invalid")
        return cls(text=text, turn_count=turns, sha256=digest)


def iso_now() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def redact_sensitive_text(text: str) -> tuple[str, int]:
    """Redact high-confidence secret values without exposing them in telemetry."""
    count = 0

    def assignment(match: re.Match[str]) -> str:
        nonlocal count
        count += 1
        return f"{match.group(1)}{match.group(2)}[REDACTED_SECRET]"

    def token(_: re.Match[str]) -> str:
        nonlocal count
        count += 1
        return "[REDACTED_SECRET]"

    redacted = PRIVATE_KEY_BLOCK.sub(token, text)
    redacted = SECRET_ASSIGNMENT.sub(assignment, redacted)
    redacted = SECRET_TOKEN.sub(token, redacted)
    return redacted, count


def quarantine_directives(text: str) -> tuple[str, int]:
    count = 0
    output: list[str] = []
    for line in text.splitlines():
        if DIRECTIVE_SHAPED.search(line):
            output.append("[QUARANTINED_DIRECTIVE_SHAPED_MEMORY]")
            count += 1
        else:
            output.append(line)
    return "\n".join(output), count


def directive_shaped(text: str) -> bool:
    """Detect directive-shaped model text across whitespace and line boundaries."""
    return bool(DIRECTIVE_SHAPED.search(re.sub(r"\s+", " ", text)))


def _open_directory_nofollow(path: Path, *, create: bool = False) -> int:
    if not path.is_absolute():
        raise PolicyError("secure-root-not-absolute")
    flags = (
        os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor: int | None = None
    try:
        descriptor = os.open(path.anchor, flags)
    except OSError:
        pass

    start_idx = 1
    if descriptor is not None:
        try:
            for idx, part in enumerate(path.parts[1:], start=1):
                if part in {"", ".", ".."}:
                    raise PolicyError("secure-root-component-invalid")
                try:
                    next_descriptor = os.open(part, flags, dir_fd=descriptor)
                except FileNotFoundError:
                    if not create:
                        raise
                    try:
                        os.mkdir(part, mode=0o750, dir_fd=descriptor)
                    except FileExistsError:
                        pass
                    next_descriptor = os.open(part, flags, dir_fd=descriptor)
                except PermissionError:
                    os.close(descriptor)
                    descriptor = None
                    break
                os.close(descriptor)
                descriptor = next_descriptor
            if descriptor is not None:
                return descriptor
        except Exception:
            if descriptor is not None:
                os.close(descriptor)
            raise

    for i in range(len(path.parts), 0, -1):
        ancestor = Path(*path.parts[:i])
        try:
            descriptor = os.open(str(ancestor), flags)
            start_idx = i
            break
        except (PermissionError, FileNotFoundError, NotADirectoryError):
            continue

    if descriptor is None:
        raise PolicyError(f"secure-root-cannot-open:{path}")

    try:
        for part in path.parts[start_idx:]:
            if part in {"", ".", ".."}:
                raise PolicyError("secure-root-component-invalid")
            try:
                next_descriptor = os.open(part, flags, dir_fd=descriptor)
            except FileNotFoundError:
                if not create:
                    raise
                try:
                    os.mkdir(part, mode=0o750, dir_fd=descriptor)
                except FileExistsError:
                    pass
                next_descriptor = os.open(part, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_descriptor
        return descriptor
    except Exception:
        if descriptor is not None:
            os.close(descriptor)
        raise


def secure_read_file(
    path: Path, *, root: Path | None = None, max_bytes: int | None = None
) -> tuple[bytes, str]:
    """Read stable bytes below a pinned, no-follow directory descriptor."""
    if not path.is_absolute():
        raise PolicyError("secure-read-path-not-absolute")
    active_root = root or Path(path.anchor)
    if not active_root.is_absolute():
        raise PolicyError("secure-read-root-not-absolute")
    try:
        relative = path.relative_to(active_root)
    except ValueError as exc:
        raise PolicyError("secure-read-outside-root") from exc
    if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
        raise PolicyError("secure-read-relative-invalid")
    directory_descriptor: int | None = None
    descriptor: int | None = None
    try:
        directory_descriptor = _open_directory_nofollow(active_root)
        directory_flags = (
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        )
        for part in relative.parts[:-1]:
            next_descriptor = os.open(part, directory_flags, dir_fd=directory_descriptor)
            os.close(directory_descriptor)
            directory_descriptor = next_descriptor
        file_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(relative.parts[-1], file_flags, dir_fd=directory_descriptor)
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise PolicyError("secure-read-not-single-regular-file")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if max_bytes is not None and total > max_bytes:
                raise PolicyError("secure-read-too-large")
            chunks.append(chunk)
        after = os.fstat(descriptor)
        if (
            before.st_dev != after.st_dev or before.st_ino != after.st_ino
            or before.st_size != after.st_size
            or before.st_mtime_ns != after.st_mtime_ns
        ):
            raise PolicyError("secure-read-changed")
    except OSError as exc:
        raise PolicyError(f"secure-read-open:{exc.__class__.__name__}") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if directory_descriptor is not None:
            os.close(directory_descriptor)
    data = b"".join(chunks)
    return data, sha256_bytes(data)


def secure_read_text(
    path: Path, *, root: Path | None = None, max_bytes: int | None = None
) -> tuple[str, str]:
    data, digest = secure_read_file(path, root=root, max_bytes=max_bytes)
    try:
        return data.decode("utf-8"), digest
    except UnicodeDecodeError as exc:
        raise SchemaError("secure-read-not-utf8") from exc


def session_key(session_id: str) -> str:
    if not isinstance(session_id, str) or not session_id.strip():
        raise SchemaError("session-id-missing")
    return hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:32]


def _require_absolute(path: Path, label: str) -> Path:
    if not path.is_absolute():
        raise ConfigError(f"{label}-must-be-absolute")
    return path


@dataclasses.dataclass(frozen=True)
class MemoryConfig:
    role: str
    vault_path: Path
    state_path: Path
    runtimes: tuple[str, ...]
    can_write_event_memory: bool
    can_run_compiler: bool
    flush_model: str = "runtime-native"
    compiler_model: str = "vps-hermes-runtime"
    luna_model: str = "gpt-5.6-luna"
    terra_model: str = "gpt-5.6-terra"
    provider_mode: str = "runtime-native"
    provider_api_base: str = "https://api.openai.com/v1"
    provider_key_env: str = "OPENAI_API_KEY"
    provider_credential_source: str = "env"
    provider_keychain_service: str | None = None
    provider_keychain_account: str | None = None
    context_budget_chars: int = 16000
    backup_evidence_path: Path | None = None
    sync_evidence_path: Path | None = None
    codex_hooks_path: Path | None = None
    claude_settings_path: Path | None = None
    hermes_lifecycle_evidence_path: Path | None = None
    codex_smoke_evidence_path: Path | None = None
    claude_smoke_evidence_path: Path | None = None
    codex_binary_path: Path | None = None
    transcript_roots: dict[str, tuple[Path, ...]] = dataclasses.field(default_factory=dict)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "MemoryConfig":
        if not isinstance(raw, dict):
            raise ConfigError("config-not-object")
        allowed_top_level = {
            "role", "vault_path", "state_path", "runtimes", "transcript_roots",
            "can_write_event_memory", "can_run_compiler", "models", "provider",
            "context_budget_chars", "backup_evidence_path", "sync_evidence_path",
            "activation",
        }
        if not set(raw).issubset(allowed_top_level):
            raise ConfigError("config-fields-invalid")
        role = raw.get("role")
        if role not in {"workstation", "memory-engine"}:
            raise ConfigError("role-invalid")
        vault = _require_absolute(Path(str(raw.get("vault_path", ""))), "vault")
        state = _require_absolute(Path(str(raw.get("state_path", ""))), "state")
        runtimes_raw = raw.get("runtimes")
        if not isinstance(runtimes_raw, list) or not runtimes_raw:
            raise ConfigError("runtimes-invalid")
        runtimes = tuple(str(item) for item in runtimes_raw)
        if any(item not in RUNTIMES for item in runtimes):
            raise ConfigError("runtime-invalid")
        if len(set(runtimes)) != len(runtimes):
            raise ConfigError("runtime-duplicate")
        writer = raw.get("can_write_event_memory") is True
        compiler = raw.get("can_run_compiler") is True
        if role == "workstation" and set(runtimes) != {"codex", "claude"}:
            raise ConfigError("workstation-runtimes-must-be-codex-claude")
        if role == "memory-engine" and set(runtimes) != {"hermes"}:
            raise ConfigError("memory-engine-runtime-must-be-hermes")
        if role == "workstation" and compiler:
            raise ConfigError("workstation-compiler-forbidden")
        if path_within(state, vault):
            raise ConfigError("state-path-inside-vault")
        models = raw.get("models") or {}
        provider = raw.get("provider") or {}
        if not isinstance(models, dict) or not set(models).issubset({"flush", "compiler"}):
            raise ConfigError("model-fields-invalid")
        allowed_provider_fields = {
            "mode", "api_base", "key_env", "credential_source", "keychain_service", "keychain_account"
        }
        if not isinstance(provider, dict) or not set(provider).issubset(allowed_provider_fields):
            raise ConfigError("provider-fields-invalid")
        provider_mode = str(provider.get("mode", "runtime-native"))
        if provider_mode not in {"runtime-native", "external-openai-api"}:
            raise ConfigError("provider-mode-invalid")
        ALLOWED_MODELS = {
            "runtime-native", "vps-hermes-runtime",
            "gpt-5.6-luna", "gpt-5.6-terra", "gpt-5.6-sol",
            "gpt-5.4-mini-2026-03-17", "gpt-5.4-nano-2026-03-17", "gpt-5.4-2026-03-05",
        }
        if provider_mode == "runtime-native":
            flush_model = str(models.get("flush", "runtime-native"))
            compiler_model = str(models.get("compiler", "vps-hermes-runtime"))
            if flush_model not in ALLOWED_MODELS or compiler_model not in ALLOWED_MODELS:
                raise ConfigError("memory-model-routing-forbidden")
        else:
            flush_model = str(models.get("flush", "gpt-5.6-luna"))
            compiler_model = str(models.get("compiler", "gpt-5.6-terra"))
            if flush_model not in ALLOWED_MODELS or compiler_model not in ALLOWED_MODELS:
                raise ConfigError("memory-model-routing-forbidden")
        luna_model = flush_model
        terra_model = compiler_model
        budget = int(raw.get("context_budget_chars", 16000))
        if budget < 1000 or budget > 100000:
            raise ConfigError("context-budget-invalid")
        backup = raw.get("backup_evidence_path")
        sync = raw.get("sync_evidence_path")
        activation = raw.get("activation") or {}
        activation_fields = {
            "codex_hooks_path", "claude_settings_path",
            "hermes_lifecycle_evidence_path", "codex_smoke_evidence_path",
            "claude_smoke_evidence_path", "codex_binary_path",
        }
        if not isinstance(activation, dict) or not set(activation).issubset(activation_fields):
            raise ConfigError("activation-fields-invalid")
        transcript_roots_raw = raw.get("transcript_roots") or {}
        if not isinstance(transcript_roots_raw, dict):
            raise ConfigError("transcript-roots-invalid")
        transcript_roots: dict[str, tuple[Path, ...]] = {}
        for runtime, roots in transcript_roots_raw.items():
            if runtime not in RUNTIMES or not isinstance(roots, list) or not roots:
                raise ConfigError("transcript-roots-invalid")
            transcript_roots[runtime] = tuple(
                _require_absolute(Path(str(root)), "transcript-root") for root in roots
            )
        if set(transcript_roots) != set(runtimes):
            raise ConfigError("transcript-roots-must-match-runtimes")
        api_base = str(provider.get("api_base", "https://api.openai.com/v1")).rstrip("/")
        if api_base != "https://api.openai.com/v1":
            raise ConfigError("provider-api-base-forbidden")
        key_env = str(provider.get("key_env", "OPENAI_API_KEY"))
        if not re.fullmatch(r"[A-Z][A-Z0-9_]{1,127}", key_env):
            raise ConfigError("provider-key-env-invalid")
        credential_source = str(provider.get("credential_source", "env"))
        if credential_source not in {"env", "macos-keychain"}:
            raise ConfigError("provider-credential-source-invalid")
        keychain_service = (
            str(provider["keychain_service"]).strip()
            if "keychain_service" in provider else None
        )
        if "keychain_service" in provider and not keychain_service:
            raise ConfigError("provider-keychain-service-invalid")
        keychain_account = (
            str(provider["keychain_account"]).strip()
            if "keychain_account" in provider else None
        )
        if "keychain_account" in provider and not keychain_account:
            raise ConfigError("provider-keychain-account-invalid")
        optional_paths = [
            (backup, "backup-evidence"), (sync, "sync-evidence"),
            *((activation.get(field), field) for field in activation_fields),
        ]
        for value, label in optional_paths:
            if value:
                _require_absolute(Path(str(value)), label)
        return cls(
            role=role,
            vault_path=vault,
            state_path=state,
            runtimes=runtimes,
            can_write_event_memory=writer,
            can_run_compiler=compiler,
            flush_model=flush_model,
            compiler_model=compiler_model,
            luna_model=luna_model,
            terra_model=terra_model,
            provider_mode=provider_mode,
            provider_api_base=api_base,
            provider_key_env=key_env,
            provider_credential_source=credential_source,
            provider_keychain_service=keychain_service,
            provider_keychain_account=keychain_account,
            context_budget_chars=budget,
            backup_evidence_path=Path(backup) if backup else None,
            sync_evidence_path=Path(sync) if sync else None,
            codex_hooks_path=(
                Path(activation["codex_hooks_path"])
                if activation.get("codex_hooks_path") else None
            ),
            claude_settings_path=(
                Path(activation["claude_settings_path"])
                if activation.get("claude_settings_path") else None
            ),
            hermes_lifecycle_evidence_path=(
                Path(activation["hermes_lifecycle_evidence_path"])
                if activation.get("hermes_lifecycle_evidence_path") else None
            ),
            codex_smoke_evidence_path=(
                Path(activation["codex_smoke_evidence_path"])
                if activation.get("codex_smoke_evidence_path") else None
            ),
            claude_smoke_evidence_path=(
                Path(activation["claude_smoke_evidence_path"])
                if activation.get("claude_smoke_evidence_path") else None
            ),
            codex_binary_path=(
                Path(activation["codex_binary_path"])
                if activation.get("codex_binary_path") else None
            ),
            transcript_roots=transcript_roots,
        )

    @classmethod
    def load(cls, path: Path) -> "MemoryConfig":
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ConfigError(f"config-read:{exc.__class__.__name__}") from exc
        return cls.from_dict(raw)


def discover_codex_binary(config: MemoryConfig | None = None) -> str | None:
    """Resolve Codex binary according to preferred priority:
    1. Real codex in PATH
    2. On macOS bundled path: /Applications/ChatGPT.app/Contents/Resources/codex
    3. Explicit configured path
    4. Otherwise BLOCKED (None)
    """
    binary = shutil.which("codex")
    if binary and os.path.isfile(binary) and os.access(binary, os.X_OK):
        return binary
    if platform.system() == "Darwin":
        bundled = Path("/Applications/ChatGPT.app/Contents/Resources/codex")
        if bundled.is_file() and os.access(bundled, os.X_OK):
            return str(bundled)
    if config and getattr(config, "codex_binary_path", None):
        configured = Path(config.codex_binary_path)
        if configured.is_file() and os.access(configured, os.X_OK):
            return str(configured)
    return None


def path_within(candidate: Path, root: Path) -> bool:
    try:
        common = os.path.commonpath(
            [str(candidate.resolve(strict=False)), str(root.resolve(strict=False))]
        )
    except ValueError:
        return False
    return common == str(root.resolve(strict=False))


def reject_symlink_chain(path: Path, *, allow_missing_leaf: bool = False) -> None:
    """Reject symlinks or special nodes in every existing path component."""
    current = Path(path.anchor)
    parts = path.parts[1:] if path.is_absolute() else path.parts
    for index, part in enumerate(parts):
        current = current / part
        try:
            info = current.lstat()
        except FileNotFoundError:
            if allow_missing_leaf:
                return
            raise PolicyError(f"path-missing:{current.name}")
        if stat.S_ISLNK(info.st_mode):
            raise PolicyError(f"path-symlink:{current.name}")
        if index < len(parts) - 1 and not stat.S_ISDIR(info.st_mode):
            raise PolicyError(f"path-parent-not-directory:{current.name}")


def ensure_safe_directory(path: Path, *, create: bool = False) -> None:
    try:
        descriptor = _open_directory_nofollow(path, create=create)
    except OSError as exc:
        raise PolicyError(f"directory-open:{exc.__class__.__name__}") from exc
    os.close(descriptor)


def atomic_write(path: Path, data: bytes | str, mode: int = 0o600) -> None:
    if not path.is_absolute() or path.name in {"", ".", ".."}:
        raise PolicyError("atomic-write-path-invalid")
    payload = data if isinstance(data, (bytes, bytearray)) else data.encode("utf-8")
    try:
        parent_descriptor = _open_directory_nofollow(path.parent, create=True)
    except OSError as exc:
        raise PolicyError(f"atomic-write-parent:{exc.__class__.__name__}") from exc
    parent_identity = os.fstat(parent_descriptor)
    temporary_name = f".{path.name}.{secrets.token_hex(16)}.tmp"
    descriptor: int | None = None
    try:
        descriptor = os.open(
            temporary_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            mode,
            dir_fd=parent_descriptor,
        )
        # `open(..., mode)` is filtered by the process umask.  Callers use
        # explicit modes to preserve bounded shared-vault group access, so
        # apply the requested mode to the newly-created, still-private FD.
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as output:
            descriptor = None
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        try:
            existing = os.stat(path.name, dir_fd=parent_descriptor, follow_symlinks=False)
        except FileNotFoundError:
            existing = None
        if existing is not None:
            if stat.S_ISLNK(existing.st_mode) or not stat.S_ISREG(existing.st_mode):
                raise PolicyError(f"unsafe-target:{path.name}")
            if existing.st_nlink != 1:
                raise PolicyError(f"hardlink-target:{path.name}")
        os.replace(
            temporary_name, path.name,
            src_dir_fd=parent_descriptor, dst_dir_fd=parent_descriptor,
        )
        os.fsync(parent_descriptor)
        current_parent = _open_directory_nofollow(path.parent)
        try:
            current_identity = os.fstat(current_parent)
            if (
                current_identity.st_dev != parent_identity.st_dev
                or current_identity.st_ino != parent_identity.st_ino
            ):
                raise PolicyError("atomic-write-parent-changed")
        finally:
            os.close(current_parent)
    except OSError as exc:
        raise PolicyError(f"atomic-write:{exc.__class__.__name__}") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            os.unlink(temporary_name, dir_fd=parent_descriptor)
        except FileNotFoundError:
            pass
        os.close(parent_descriptor)


def safe_unlink(path: Path, *, root: Path) -> None:
    if not path.is_absolute() or not root.is_absolute():
        raise PolicyError("unlink-path-invalid")
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise PolicyError("unlink-outside-root") from exc
    if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
        raise PolicyError("unlink-relative-invalid")
    root_descriptor = _open_directory_nofollow(root)
    directory_descriptor = root_descriptor
    try:
        flags = (
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        )
        for part in relative.parts[:-1]:
            next_descriptor = os.open(part, flags, dir_fd=directory_descriptor)
            os.close(directory_descriptor)
            directory_descriptor = next_descriptor
        info = os.stat(
            relative.parts[-1], dir_fd=directory_descriptor, follow_symlinks=False
        )
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise PolicyError("unlink-target-unsafe")
        os.unlink(relative.parts[-1], dir_fd=directory_descriptor)
        os.fsync(directory_descriptor)
    except OSError as exc:
        raise PolicyError(f"unlink:{exc.__class__.__name__}") from exc
    finally:
        os.close(directory_descriptor)


def atomic_json(path: Path, payload: dict[str, Any]) -> None:
    atomic_write(
        path,
        (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(),
    )


@contextmanager
def exclusive_lock(path: Path, *, nonblocking: bool = False) -> Iterator[None]:
    if not path.is_absolute():
        raise PolicyError("lock-path-not-absolute")
    descriptor: int | None = None
    last_error: OSError | None = None
    for _ in range(2):
        parent_descriptor = _open_directory_nofollow(path.parent, create=True)
        try:
            descriptor = os.open(
                path.name,
                os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                0o600,
                dir_fd=parent_descriptor,
            )
            break
        except FileNotFoundError as exc:
            last_error = exc
        except OSError as exc:
            os.close(parent_descriptor)
            raise PolicyError(f"lock-open:{exc.__class__.__name__}") from exc
        os.close(parent_descriptor)
    if descriptor is None:
        assert last_error is not None
        raise PolicyError("lock-open:FileNotFoundError") from last_error
    try:
        os.close(parent_descriptor)
    except OSError:
        os.close(descriptor)
        raise
    info = os.fstat(descriptor)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        os.close(descriptor)
        raise PolicyError("lock-target-unsafe")
    with os.fdopen(descriptor, "a+", encoding="utf-8") as handle:
        flags = fcntl.LOCK_EX | (fcntl.LOCK_NB if nonblocking else 0)
        try:
            fcntl.flock(handle.fileno(), flags)
        except BlockingIOError as exc:
            raise MemoryError("lock-busy") from exc
        yield


def write_health(state_path: Path, component: str, status: str, detail: str = "") -> None:
    payload = {
        "schema": "pikselzone-memory-health-v1",
        "component": component,
        "status": status,
        "updated_at": iso_now(),
    }
    if detail:
        payload["detail"] = detail[:500]
    try:
        atomic_json(state_path / "health" / f"{component}.json", payload)
    except (MemoryError, OSError):
        pass


INTERNAL_ITEM_TYPES = {
    "reasoning",
    "commandexecution",
    "command_execution",
    "filechange",
    "file_change",
    "tool",
    "tool_output",
    "tool_use",
    "tool_result",
    "token_count",
    "task_started",
    "task_complete",
    "task_update",
    "internal",
    "internal_state",
    "thought",
    "thinking",
}

USER_ROLES = {"user", "usermessage", "user_message"}
AGENT_ROLES = {"assistant", "agent", "agentmessage", "agent_message"}


def _normalize_role(raw_role: Any) -> str:
    r = str(raw_role or "").strip().lower()
    if r in USER_ROLES:
        return "user"
    if r in AGENT_ROLES:
        return "assistant"
    return ""


def _text_blocks(content: Any) -> list[str]:
    if isinstance(content, str):
        return [content] if content.strip() else []
    if isinstance(content, dict):
        item_type = str(content.get("type", "")).strip().lower()
        if item_type in {"text", "input_text", "output_text", ""}:
            text = content.get("text")
            if isinstance(text, str) and text.strip():
                return [text]
            val = content.get("value")
            if isinstance(val, str) and val.strip():
                return [val]
            cnt = content.get("content")
            if cnt and cnt is not content:
                return _text_blocks(cnt)
        return []
    if not isinstance(content, list):
        return []
    blocks: list[str] = []
    for item in content:
        if isinstance(item, str):
            if item.strip():
                blocks.append(item)
            continue
        if not isinstance(item, dict):
            continue
        item_type = str(item.get("type", "")).strip().lower()
        if item_type in {"text", "input_text", "output_text", ""}:
            text = item.get("text")
            if isinstance(text, str) and text.strip():
                blocks.append(text)
            else:
                val = item.get("value")
                if isinstance(val, str) and val.strip():
                    blocks.append(val)
                elif "content" in item:
                    sub = _text_blocks(item.get("content"))
                    blocks.extend(sub)
    return blocks


def _message_from_record(record: dict[str, Any]) -> tuple[str, Any, bool]:
    """Extract conversational role and content from transcript record.
    Returns: (role, content, is_candidate)
    - role: 'user', 'assistant', or ''
    - content: text or list of content blocks
    - is_candidate: True if this record was shaped like a conversational message
    """
    rec_type = str(record.get("type", "")).strip()
    rec_type_lower = rec_type.lower()

    # 1. Codex event_msg wrapper
    if rec_type_lower == "event_msg":
        payload = record.get("payload")
        if not isinstance(payload, dict):
            return "", None, False
        ptype = str(payload.get("type", "")).strip().lower()

        # Old format: payload.type == "user_message" / "agent_message"
        if ptype in {"user_message", "agent_message", "usermessage", "agentmessage"}:
            role = "user" if "user" in ptype else "assistant"
            content = payload.get("message")
            if content is None:
                content = payload.get("content")
            return role, content, True

        # New format: payload.type == "item_completed" / "item.completed"
        if ptype in {"item_completed", "item.completed"}:
            item = payload.get("item")
            if isinstance(item, dict):
                itype = str(item.get("type", "")).strip()
                itype_lower = itype.lower()
                if itype_lower in INTERNAL_ITEM_TYPES:
                    return "", None, False
                norm_role = _normalize_role(itype_lower) or _normalize_role(item.get("role"))
                if norm_role:
                    content = (
                        item.get("content")
                        if item.get("content") is not None
                        else (item.get("text") if item.get("text") is not None else item.get("message"))
                    )
                    return norm_role, content, True
                if any(k in itype_lower for k in ("message", "user", "agent", "assistant")):
                    return "", None, True
            return "", None, False

        # Nested role/message inside payload
        msg = payload.get("message")
        if isinstance(msg, dict):
            norm_role = _normalize_role(msg.get("role"))
            if norm_role:
                return norm_role, msg.get("content"), True
        if "role" in payload:
            norm_role = _normalize_role(payload.get("role"))
            if norm_role:
                return norm_role, payload.get("content"), True
        return "", None, False

    # 2. Direct item_completed (unwrapped)
    if rec_type_lower in {"item_completed", "item.completed"}:
        item = record.get("item")
        if isinstance(item, dict):
            itype_lower = str(item.get("type", "")).strip().lower()
            if itype_lower in INTERNAL_ITEM_TYPES:
                return "", None, False
            norm_role = _normalize_role(itype_lower) or _normalize_role(item.get("role"))
            if norm_role:
                content = (
                    item.get("content")
                    if item.get("content") is not None
                    else (item.get("text") if item.get("text") is not None else item.get("message"))
                )
                return norm_role, content, True
            if any(k in itype_lower for k in ("message", "user", "agent", "assistant")):
                return "", None, True
        return "", None, False

    # 3. Standard Claude / Hermes / generic record
    message = record.get("message")
    if isinstance(message, dict):
        role_key = message.get("role") or record.get("type")
        norm_role = _normalize_role(role_key)
        if norm_role:
            return norm_role, message.get("content"), True

    role_key = record.get("role") or record.get("type")
    norm_role = _normalize_role(role_key)
    if norm_role:
        return norm_role, record.get("content"), True

    raw_check = f"{rec_type_lower} {str(record.get('role', '')).lower()}"
    if any(k in raw_check for k in ("user_message", "agent_message", "usermessage", "agentmessage")):
        return "", None, True

    return "", None, False


def normalize_transcript(
    source: Path | str | Sequence[dict[str, Any]], *,
    max_turns: int = 200, max_chars: int = 120000,
    allowed_roots: Sequence[Path] | None = None,
    state_path: Path | None = None,
) -> tuple[str, int, str]:
    """Extract user/assistant prose only; tool results and reasoning are ignored."""
    records: list[dict[str, Any]] = []
    if isinstance(source, Path):
        roots = tuple(allowed_roots or ())
        matching_root = next(
            (root for root in roots if _path_lexically_within(source, root)), None
        )
        if matching_root is None:
            raise PolicyError("transcript-path-outside-allowed-roots")
        source_text, _ = secure_read_text(
            source, root=matching_root, max_bytes=20 * 1024 * 1024
        )
        for line_number, raw_line in enumerate(
            source_text.splitlines(), start=1
        ):
            if not raw_line.strip():
                continue
            try:
                value = json.loads(raw_line)
            except json.JSONDecodeError as exc:
                raise SchemaError(f"transcript-jsonl-invalid:{line_number}") from exc
            if isinstance(value, dict):
                if "messages" in value and isinstance(value["messages"], list):
                    records.extend(item for item in value["messages"] if isinstance(item, dict))
                else:
                    records.append(value)
    elif isinstance(source, str):
        try:
            value = json.loads(source)
            if isinstance(value, dict) and "messages" in value and isinstance(value["messages"], list):
                records = [item for item in value["messages"] if isinstance(item, dict)]
            elif not isinstance(value, list):
                raise SchemaError("transcript-not-list")
            else:
                records = [item for item in value if isinstance(item, dict)]
        except json.JSONDecodeError:
            records = []
            for raw_line in source.splitlines():
                if not raw_line.strip():
                    continue
                try:
                    val = json.loads(raw_line)
                    if isinstance(val, dict):
                        if "messages" in val and isinstance(val["messages"], list):
                            records.extend(item for item in val["messages"] if isinstance(item, dict))
                        else:
                            records.append(val)
                except json.JSONDecodeError as exc:
                    raise SchemaError("transcript-jsonl-invalid") from exc
    else:
        records = [item for item in source if isinstance(item, dict)]

    turns: list[tuple[str, str]] = []
    candidates_count = 0
    for record in records:
        role, content, is_candidate = _message_from_record(record)
        if is_candidate:
            candidates_count += 1
        if role not in {"user", "assistant"}:
            continue
        text = " ".join(_text_blocks(content))
        redacted, _ = redact_sensitive_text(text)
        flattened = re.sub(r"\s+", " ", redacted).strip()
        if flattened:
            turns.append((role, flattened))

    if candidates_count > 0 and not turns:
        if state_path:
            write_health(
                state_path,
                "codex-parser",
                "warn",
                f"0 turns extracted from {candidates_count} conversation-shaped candidate records",
            )
        raise SchemaError(f"transcript-zero-turns-from-{candidates_count}-candidates")

    turns = turns[-max_turns:]
    rendered = "\n".join(f"{role.upper()}: {text}" for role, text in turns)
    if len(rendered) > max_chars:
        rendered = rendered[-max_chars:]
        boundary = rendered.find("\n")
        if boundary >= 0:
            rendered = rendered[boundary + 1:]
    digest = sha256_bytes(rendered.encode("utf-8"))
    return rendered, len(turns), digest


def validate_summary(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise SchemaError("summary-not-object")
    if set(value) != {"status", *SUMMARY_FIELDS}:
        raise SchemaError("summary-fields-invalid")
    if value["status"] not in {"memory", "empty"}:
        raise SchemaError("summary-status-invalid")
    normalized: dict[str, Any] = {"status": value["status"]}
    for field in SUMMARY_FIELDS:
        entries = value[field]
        if not isinstance(entries, list) or any(
            not isinstance(item, str) or not item.strip() for item in entries
        ):
            raise SchemaError(f"summary-{field}-invalid")
        if any(directive_shaped(item) for item in entries):
            raise SchemaError(f"summary-{field}-directive-shaped")
        normalized[field] = [
            re.sub(
                r"\s+", " ", redact_sensitive_text(item.strip()[:4000])[0]
            ).strip()
            for item in entries[:100]
        ]
    if value["status"] == "empty" and any(normalized[field] for field in SUMMARY_FIELDS):
        raise SchemaError("empty-summary-has-content")
    if value["status"] == "memory" and not any(
        normalized[field] for field in SUMMARY_FIELDS
    ):
        raise SchemaError("memory-summary-empty")
    return normalized


def _path_lexically_within(candidate: Path, root: Path) -> bool:
    try:
        candidate.relative_to(root)
    except ValueError:
        return False
    return candidate.is_absolute() and root.is_absolute()


def knowledge_relative_path(value: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or "." in path.parts:
        raise PolicyError("knowledge-path-traversal")
    if str(path) in {"knowledge/index.md", "knowledge/log.md"}:
        return path
    if (
        len(path.parts) >= 3
        and path.parts[0] == "knowledge"
        and path.parts[1] in ALLOWED_KNOWLEDGE_ROOTS
        and path.suffix == ".md"
    ):
        return path
    raise PolicyError(f"knowledge-path-forbidden:{value[:80]}")


def summary_json_schema() -> dict[str, Any]:
    properties: dict[str, Any] = {
        "status": {"type": "string", "enum": ["memory", "empty"]}
    }
    properties.update({
        field: {"type": "array", "items": {"type": "string"}, "maxItems": 100}
        for field in SUMMARY_FIELDS
    })
    return {
        "type": "object",
        "properties": properties,
        "required": ["status", *SUMMARY_FIELDS],
        "additionalProperties": False,
    }


def compiler_json_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "status": {"type": "string", "enum": ["changes", "no_changes"]},
            "writes": {
                "type": "array",
                "maxItems": 100,
                "items": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "content": {"type": "string"},
                    },
                    "required": ["path", "content"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["status", "writes"],
        "additionalProperties": False,
    }

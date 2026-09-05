"""Secret resolution and handling.

Resolution order: process environment → local ``.env`` (data dir first, then
the working directory) → OS keyring. Everything here talks about secret
*names*; values never appear in logs, errors, events or session files. When a
value must be compared (e.g. the "same account" Judge warning), we compare
hashes, not strings.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

from .paths import data_dir

__all__ = [
    "SecretError",
    "delete_secret",
    "have_keyring",
    "redact",
    "resolve",
    "secret_fingerprint",
    "secret_status",
    "set_secret",
]

_KEYRING_SERVICE = "ai-council"


class SecretError(RuntimeError):
    """A secret could not be read or stored. Never carries the value itself."""


def _read_env_file(name: str, env_file: Path | None) -> str | None:
    candidates = [env_file] if env_file else [data_dir() / ".env", Path(".env")]
    for path in candidates:
        if path is None or not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, _, value = stripped.partition("=")
            if key.strip() != name:
                continue
            value = value.strip().strip('"').strip("'")
            return value or None
    return None


def have_keyring() -> bool:
    try:
        import keyring  # noqa: F401
    except ImportError:
        return False
    return True


def _keyring_get(name: str) -> str | None:
    try:
        import keyring
    except ImportError:
        return None
    try:
        return keyring.get_password(_KEYRING_SERVICE, name)
    except Exception:  # keyring backends raise their own zoo of errors
        return None


def resolve(name: str, *, env_file: Path | None = None) -> str | None:
    """Resolve a secret by *name*. Returns ``None`` when not found."""
    value = os.environ.get(name)
    if value:
        return value
    value = _read_env_file(name, env_file)
    if value:
        return value
    return _keyring_get(name)


def secret_status(name: str, *, env_file: Path | None = None) -> str:
    """Where a secret currently lives: ``env`` | ``dotenv`` | ``keyring`` | ``unset``.

    Mirrors :func:`resolve`'s precedence so the web UI can say "this variable
    is set in your environment and would shadow the keyring value".
    """
    if os.environ.get(name):
        return "env"
    if _read_env_file(name, env_file) is not None:
        return "dotenv"
    if _keyring_get(name) is not None:
        return "keyring"
    return "unset"


def require(name: str, *, env_file: Path | None = None) -> str:
    value = resolve(name, env_file=env_file)
    if value is None:
        raise SecretError(
            f"缺少密钥 {name}：请写入环境变量、数据目录的 .env，"
            f"或用 `council key set {name}` 存入系统钥匙串"
        )
    return value


def set_secret(name: str, value: str) -> None:
    try:
        import keyring
    except ImportError as err:
        raise SecretError("keyring 不可用，请把密钥写入数据目录的 .env") from err
    try:
        keyring.set_password(_KEYRING_SERVICE, name, value)
    except Exception as err:
        raise SecretError(f"无法把 {name} 写入系统钥匙串：{type(err).__name__}") from err


def delete_secret(name: str) -> None:
    try:
        import keyring
    except ImportError as err:
        raise SecretError("keyring 不可用") from err
    try:
        keyring.delete_password(_KEYRING_SERVICE, name)
    except Exception as err:
        raise SecretError(f"无法从系统钥匙串删除 {name}") from err


def secret_fingerprint(name: str, *, env_file: Path | None = None) -> str | None:
    """Short fingerprint of a secret *name's* value, for "same account" checks.

    Used to warn when the Judge shares an account with a participant without
    ever putting the secret anywhere.
    """
    value = resolve(name, env_file=env_file)
    if value is None:
        return None
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def redact(text: str, *secrets_: str) -> str:
    """Replace any occurrence of a known secret value with ``***``.

    Longest-first so overlapping values cannot leak a prefix.
    """
    for value in sorted((s for s in secrets_ if s), key=len, reverse=True):
        text = text.replace(value, "***")
    return text

"""Secret resolution and mandatory recursive redaction boundaries."""

import os
import re
import threading
from typing import Iterable, Optional

REDACTED = "[REDACTED]"
_SENSITIVE_KEYS = re.compile(r"(api[_-]?key|password|passwd|secret|token|authorization|cookie)", re.I)
_TOKEN_PATTERNS = [
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+"),
    re.compile(r"\b(?:sk|sk-ant)-[A-Za-z0-9_-]{12,}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b"),
]
_KNOWN_SECRETS: set[str] = set()
_LOCK = threading.RLock()


def register_secret(value: Optional[str]) -> Optional[str]:
    if value and len(value) >= 6:
        with _LOCK:
            _KNOWN_SECRETS.add(value)
    return value


def redact_text(value: str) -> str:
    result = value
    with _LOCK:
        known = sorted(_KNOWN_SECRETS, key=len, reverse=True)
    for secret in known:
        result = result.replace(secret, REDACTED)
    for pattern in _TOKEN_PATTERNS:
        result = pattern.sub(lambda match: (match.group(1) if match.lastindex else "") + REDACTED, result)
    return result


def redact(value):
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return {
            key: REDACTED if _SENSITIVE_KEYS.search(str(key)) else redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact(item) for item in value)
    return value


class SecretResolver:
    """Resolve from Windows/macOS/Linux keyring locally or environment in CI."""

    def __init__(self, service: str = "intent-workflow-runtime-v3", keyring_backend=None):
        self.service = service
        self._backend = keyring_backend

    def get(self, name: str, env_names: Iterable[str] = (), required: bool = True) -> Optional[str]:
        source = os.environ.get("IW_SECRET_SOURCE", "auto").lower()
        if source not in {"auto", "keyring", "environment"}:
            raise RuntimeError("IW_SECRET_SOURCE must be auto, keyring, or environment")
        env_names = tuple(env_names)
        prefer_environment = source == "environment" or (
            source == "auto" and os.environ.get("CI", "").lower() in {"1", "true", "yes"}
        )
        value = None
        if prefer_environment:
            value = self._from_environment(env_names)
        if value is None and source != "environment":
            value = self._from_keyring(name)
        if value is None and source == "auto" and not prefer_environment:
            value = self._from_environment(env_names)
        if value is None and required:
            raise RuntimeError(
                f"secret {name} not found in configured OS keyring/CI environment source"
            )
        return register_secret(value)

    def set(self, name: str, value: str) -> None:
        backend = self._keyring()
        backend.set_password(self.service, name, value)
        register_secret(value)

    def _from_environment(self, names: Iterable[str]) -> Optional[str]:
        return next((os.environ[name] for name in names if os.environ.get(name)), None)

    def _keyring(self):
        if self._backend is None:
            import keyring
            self._backend = keyring
        return self._backend

    def _from_keyring(self, name: str) -> Optional[str]:
        try:
            return self._keyring().get_password(self.service, name)
        except Exception as exc:
            if os.environ.get("IW_SECRET_SOURCE", "auto").lower() == "keyring":
                raise RuntimeError("configured OS keyring is unavailable") from exc
            return None


default_resolver = SecretResolver()

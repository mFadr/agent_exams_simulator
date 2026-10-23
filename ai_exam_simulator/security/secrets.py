"""Secure credential storage.

API keys are stored in the Windows Credential Manager through ``keyring``.
If no secure backend is available the key is kept in memory for the current
session only - it is never written to the config file, the database or logs.
"""

from __future__ import annotations

import logging
from typing import Protocol

from .redaction import register_secret, unregister_secret

log = logging.getLogger(__name__)

SERVICE_NAME = "AIExamSimulator"


class SecretBackend(Protocol):
    def get_password(self, service: str, name: str) -> str | None: ...
    def set_password(self, service: str, name: str, value: str) -> None: ...
    def delete_password(self, service: str, name: str) -> None: ...


class MemoryBackend:
    """Session-only backend (used for tests and when no secure store exists)."""

    def __init__(self) -> None:
        self._data: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, name: str) -> str | None:
        return self._data.get((service, name))

    def set_password(self, service: str, name: str, value: str) -> None:
        self._data[(service, name)] = value

    def delete_password(self, service: str, name: str) -> None:
        self._data.pop((service, name), None)


def _detect_keyring() -> SecretBackend | None:
    try:
        import keyring
        from keyring.backends import fail, null

        backend = keyring.get_keyring()
        if isinstance(backend, (fail.Keyring, null.Keyring)):
            return None
        return keyring  # module exposes get/set/delete_password
    except Exception as exc:  # pragma: no cover - environment dependent
        log.warning("Secure credential storage unavailable: %s", type(exc).__name__)
        return None


class SecretStore:
    """Facade over the secure credential backend."""

    def __init__(self, backend: SecretBackend | None = None, *, detect: bool = True) -> None:
        if backend is None and detect:
            backend = _detect_keyring()
        self._persistent = backend is not None and not isinstance(backend, MemoryBackend)
        self._backend: SecretBackend = backend or MemoryBackend()

    @property
    def is_persistent(self) -> bool:
        """True when secrets survive restarts (Windows Credential Manager)."""
        return self._persistent

    @property
    def backend_description(self) -> str:
        if self._persistent:
            return "Windows Credential Manager (secure, persistent)"
        return "Session memory only (no secure store available - key is forgotten on exit)"

    def get(self, name: str) -> str | None:
        try:
            value = self._backend.get_password(SERVICE_NAME, name)
        except Exception as exc:
            log.error("Could not read credential '%s': %s", name, type(exc).__name__)
            return None
        register_secret(value)
        return value

    def set(self, name: str, value: str) -> None:
        value = value.strip()
        if not value:
            self.delete(name)
            return
        old = self.get(name)
        self._backend.set_password(SERVICE_NAME, name, value)
        register_secret(value)
        if old and old != value:
            unregister_secret(old)
        log.info("Credential '%s' stored (%s)", name, "persistent" if self._persistent else "session")

    def delete(self, name: str) -> None:
        old = None
        try:
            old = self._backend.get_password(SERVICE_NAME, name)
            if old is not None:
                self._backend.delete_password(SERVICE_NAME, name)
        except Exception as exc:
            log.warning("Could not delete credential '%s': %s", name, type(exc).__name__)
        unregister_secret(old)

    def has(self, name: str) -> bool:
        return bool(self.get(name))


def mask_key(value: str | None) -> str:
    """Display-safe hint for a stored key, e.g. '••••••••3f9a'."""
    if not value:
        return "(not set)"
    return "•" * 8 + value[-4:] if len(value) > 8 else "•" * len(value)

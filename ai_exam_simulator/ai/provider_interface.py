"""Provider abstraction shared by local (Ollama) and cloud (OpenAI-compatible) models."""

from __future__ import annotations

import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import TypedDict


class ProviderKind(str, Enum):
    LOCAL = "local"
    CLOUD = "cloud"


class ChatMessage(TypedDict):
    role: str  # "system" | "user" | "assistant"
    content: str


class CancelToken:
    """Cooperative cancellation flag passed to long-running AI operations."""

    def __init__(self) -> None:
        self._event = threading.Event()

    def cancel(self) -> None:
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def raise_if_cancelled(self) -> None:
        if self._event.is_set():
            raise OperationCancelled("Operation cancelled by the user.")


@dataclass
class AIResponse:
    text: str
    provider_name: str
    kind: ProviderKind
    model: str
    duration_s: float = 0.0
    usage: dict[str, int] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)  # routing notes (fallbacks etc.)


@dataclass
class ProviderStatus:
    available: bool
    message: str
    models: list[str] = field(default_factory=list)
    installed: bool | None = None  # local only: is the runtime installed


# ----------------------------------------------------------------------------- errors


class AIError(Exception):
    """Base class. ``str(exc)`` is a user-facing, secret-free message."""

    retriable = False
    kind = "error"


class ProviderNotConfiguredError(AIError):
    kind = "not_configured"


class ProviderUnavailableError(AIError):
    retriable = True
    kind = "unavailable"


class ProviderTimeoutError(AIError):
    retriable = True
    kind = "timeout"


class RateLimitError(AIError):
    retriable = True
    kind = "rate_limit"

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class ProviderAuthError(AIError):
    kind = "auth"


class ModelNotFoundError(AIError):
    kind = "model_not_found"


class MalformedResponseError(AIError):
    retriable = True
    kind = "malformed"


class OperationCancelled(AIError):
    kind = "cancelled"


class CloudConsentRequired(AIError):
    kind = "consent_required"


class AIProvider(ABC):
    """A chat-completion capable model provider."""

    kind: ProviderKind
    name: str

    @property
    @abstractmethod
    def model(self) -> str: ...

    @abstractmethod
    def is_configured(self) -> bool:
        """Cheap check: is enough configuration present to attempt a call?"""

    @abstractmethod
    def check_status(self) -> ProviderStatus:
        """Network check: reachable, authenticated, model available."""

    @abstractmethod
    def list_models(self) -> list[str]: ...

    @abstractmethod
    def chat(
        self,
        messages: list[ChatMessage],
        *,
        json_mode: bool = False,
        temperature: float = 0.4,
        cancel: CancelToken | None = None,
    ) -> AIResponse: ...

    def describe(self) -> str:
        return f"{self.name} ({self.model or 'no model selected'})"

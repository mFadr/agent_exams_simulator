"""Scriptable provider used by the automated test-suite (never used by the app UI)."""

from __future__ import annotations

from typing import Callable

from .provider_interface import (
    AIError,
    AIProvider,
    AIResponse,
    CancelToken,
    ChatMessage,
    ProviderKind,
    ProviderStatus,
)

Responder = Callable[[list[ChatMessage]], str]


class MockProvider(AIProvider):
    def __init__(
        self,
        kind: ProviderKind = ProviderKind.LOCAL,
        responses: list[str | AIError] | None = None,
        responder: Responder | None = None,
        model: str = "mock-model",
        configured: bool = True,
        name: str | None = None,
    ) -> None:
        self.kind = kind
        self.name = name or f"Mock {kind.value}"
        self._responses = list(responses or [])
        self._responder = responder
        self._model_name = model
        self._configured = configured
        self.calls: list[list[ChatMessage]] = []

    @property
    def model(self) -> str:
        return self._model_name

    def is_configured(self) -> bool:
        return self._configured

    def check_status(self) -> ProviderStatus:
        return ProviderStatus(self._configured, "mock", [self._model_name])

    def list_models(self) -> list[str]:
        return [self._model_name]

    def chat(self, messages: list[ChatMessage], *, json_mode: bool = False, temperature: float = 0.4,
             cancel: CancelToken | None = None) -> AIResponse:
        self.calls.append(messages)
        if cancel:
            cancel.raise_if_cancelled()
        if self._responses:
            item = self._responses.pop(0)
            if isinstance(item, AIError):
                raise item
            text = item
        elif self._responder:
            text = self._responder(messages)
        else:
            raise AIError("MockProvider has no scripted response left")
        return AIResponse(text=text, provider_name=self.name, kind=self.kind, model=self._model_name)

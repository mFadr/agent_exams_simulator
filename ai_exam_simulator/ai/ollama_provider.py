"""Local inference through an Ollama server (https://ollama.com)."""

from __future__ import annotations

import json
import logging
import os
import shutil
import time
from pathlib import Path
from typing import Callable

import httpx

from .provider_interface import (
    AIProvider,
    AIResponse,
    CancelToken,
    ChatMessage,
    ModelNotFoundError,
    OperationCancelled,
    ProviderKind,
    ProviderNotConfiguredError,
    ProviderStatus,
    ProviderTimeoutError,
    ProviderUnavailableError,
)

log = logging.getLogger(__name__)

NOT_RUNNING_HELP = (
    "Ollama is not reachable at {url}. Install it from https://ollama.com/download, "
    "start the Ollama app (it runs in the system tray), then press 'Check Ollama'."
)
NO_MODEL_HELP = (
    "No local model is installed. Open a terminal and run e.g. 'ollama pull llama3.2:3b' "
    "(small) or 'ollama pull qwen2.5:7b' (better quality), then refresh the model list."
)


def ollama_installed() -> bool:
    if shutil.which("ollama"):
        return True
    local = os.environ.get("LOCALAPPDATA")
    return bool(local) and Path(local, "Programs", "Ollama", "ollama.exe").exists()


class OllamaProvider(AIProvider):
    kind = ProviderKind.LOCAL
    name = "Ollama (local)"

    def __init__(
        self,
        base_url_getter: Callable[[], str],
        model_getter: Callable[[], str],
        timeout_getter: Callable[[], int] = lambda: 180,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._url = base_url_getter
        self._model = model_getter
        self._timeout = timeout_getter
        self._transport = transport

    @property
    def model(self) -> str:
        return self._model()

    def _client(self, timeout: float) -> httpx.Client:
        return httpx.Client(base_url=self._url(), timeout=httpx.Timeout(timeout, connect=5.0), transport=self._transport)

    def is_configured(self) -> bool:
        return bool(self._model())

    def list_models(self) -> list[str]:
        try:
            with self._client(10) as c:
                r = c.get("/api/tags")
                r.raise_for_status()
                return sorted(m["name"] for m in r.json().get("models", []))
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            raise ProviderUnavailableError(NOT_RUNNING_HELP.format(url=self._url())) from exc

    def check_status(self) -> ProviderStatus:
        installed = ollama_installed()
        try:
            models = self.list_models()
        except ProviderUnavailableError as exc:
            msg = str(exc) if installed or self._transport else (
                "Ollama does not appear to be installed. Download it from https://ollama.com/download."
            )
            return ProviderStatus(False, msg, [], installed)
        if not models:
            return ProviderStatus(False, "Ollama is running. " + NO_MODEL_HELP, [], installed)
        model = self._model()
        if not model:
            return ProviderStatus(False, f"Ollama is running with {len(models)} model(s). Select one to use.", models, installed)
        if model not in models and f"{model}:latest" not in models:
            return ProviderStatus(False, f"Model '{model}' is not installed. Run 'ollama pull {model}' or pick another.", models, installed)
        return ProviderStatus(True, f"Ollama is running; model '{model}' is available.", models, installed)

    def chat(
        self,
        messages: list[ChatMessage],
        *,
        json_mode: bool = False,
        temperature: float = 0.4,
        cancel: CancelToken | None = None,
    ) -> AIResponse:
        model = self._model()
        if not model:
            raise ProviderNotConfiguredError("No local model selected. Choose one in AI Settings.")
        body: dict = {
            "model": model,
            "messages": messages,
            "stream": True,
            "options": {"temperature": temperature},
        }
        if json_mode:
            body["format"] = "json"
        start = time.monotonic()
        chunks: list[str] = []
        usage: dict[str, int] = {}
        timeout = float(self._timeout())
        try:
            with self._client(timeout) as c, c.stream("POST", "/api/chat", json=body) as r:
                if r.status_code == 404:
                    r.read()
                    raise ModelNotFoundError(f"Local model '{model}' was not found. Run 'ollama pull {model}' or choose another model.")
                if r.status_code >= 400:
                    r.read()
                    detail = _safe_error_text(r)
                    raise ProviderUnavailableError(f"Ollama returned HTTP {r.status_code}: {detail}")
                for line in r.iter_lines():
                    if cancel and cancel.cancelled:
                        raise OperationCancelled("Operation cancelled by the user.")
                    if time.monotonic() - start > timeout:
                        raise ProviderTimeoutError(f"Local model did not finish within {timeout:.0f}s.")
                    if not line.strip():
                        continue
                    try:
                        data = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if "error" in data:
                        raise ProviderUnavailableError(f"Ollama error: {str(data['error'])[:300]}")
                    chunks.append(data.get("message", {}).get("content", ""))
                    if data.get("done"):
                        usage = {
                            "prompt_tokens": int(data.get("prompt_eval_count", 0) or 0),
                            "completion_tokens": int(data.get("eval_count", 0) or 0),
                        }
                        break
        except httpx.ConnectError as exc:
            raise ProviderUnavailableError(NOT_RUNNING_HELP.format(url=self._url())) from exc
        except httpx.TimeoutException as exc:
            raise ProviderTimeoutError(
                f"The local model did not respond within {timeout:.0f}s. Large models on modest hardware can be slow; "
                "try a smaller model or raise the timeout in AI Settings."
            ) from exc
        except httpx.HTTPError as exc:
            raise ProviderUnavailableError(f"Communication with Ollama failed: {type(exc).__name__}") from exc
        return AIResponse(
            text="".join(chunks), provider_name=self.name, kind=self.kind, model=model,
            duration_s=time.monotonic() - start, usage=usage,
        )


def _safe_error_text(r: httpx.Response) -> str:
    try:
        data = r.json()
        return str(data.get("error") or data)[:300]
    except Exception:
        return r.text[:300]

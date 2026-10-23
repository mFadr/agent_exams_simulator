"""Cloud inference through any OpenAI-compatible Chat Completions API.

Works with OpenAI, OpenRouter, Groq, Mistral, Anthropic's OpenAI-compatible
endpoint, self-hosted gateways, etc. The endpoint, model and key are user
configuration; the key comes from :class:`SecretStore` at call time and is
never logged.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Callable

import httpx

from ..security.redaction import redact
from .provider_interface import (
    AIProvider,
    AIResponse,
    CancelToken,
    ChatMessage,
    MalformedResponseError,
    ModelNotFoundError,
    OperationCancelled,
    ProviderAuthError,
    ProviderKind,
    ProviderNotConfiguredError,
    ProviderStatus,
    ProviderTimeoutError,
    ProviderUnavailableError,
    RateLimitError,
)

log = logging.getLogger(__name__)

API_KEY_SECRET_NAME = "cloud_api_key"


class CloudProvider(AIProvider):
    kind = ProviderKind.CLOUD

    def __init__(
        self,
        base_url_getter: Callable[[], str],
        model_getter: Callable[[], str],
        api_key_getter: Callable[[], str | None],
        name_getter: Callable[[], str] = lambda: "Cloud",
        timeout_getter: Callable[[], int] = lambda: 90,
        json_mode_getter: Callable[[], bool] = lambda: True,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._url = base_url_getter
        self._model = model_getter
        self._key = api_key_getter
        self._name = name_getter
        self._timeout = timeout_getter
        self._json_mode = json_mode_getter
        self._transport = transport
        self.last_status: str = "Not tested yet"

    @property
    def name(self) -> str:  # type: ignore[override]
        return f"{self._name()} (cloud)"

    @property
    def model(self) -> str:
        return self._model()

    def is_configured(self) -> bool:
        return bool(self._url() and self._model() and self._key())

    def _headers(self) -> dict[str, str]:
        key = self._key()
        if not key:
            raise ProviderNotConfiguredError("No API key configured for the cloud provider. Add one in AI Settings.")
        return {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}

    def _client(self, timeout: float) -> httpx.Client:
        url = self._url()
        if not url:
            raise ProviderNotConfiguredError("No cloud API endpoint configured.")
        return httpx.Client(base_url=url.rstrip("/") + "/", timeout=httpx.Timeout(timeout, connect=10.0), transport=self._transport)

    def _raise_for_status(self, r: httpx.Response) -> None:
        if r.status_code < 400:
            return
        try:
            r.read()
            body = r.json()
            detail = body.get("error", {}).get("message") if isinstance(body.get("error"), dict) else body.get("error") or body
        except Exception:
            detail = r.text[:300] if hasattr(r, "text") else ""
        detail = redact(str(detail))[:300]
        if r.status_code in (401, 403):
            raise ProviderAuthError(f"The cloud provider rejected the API key (HTTP {r.status_code}). Check the key in AI Settings.")
        if r.status_code == 404:
            raise ModelNotFoundError(f"Endpoint or model not found (HTTP 404). Check the endpoint URL and model name. {detail}")
        if r.status_code == 429:
            ra = r.headers.get("retry-after")
            try:
                retry_after = float(ra) if ra else None
            except ValueError:
                retry_after = None
            raise RateLimitError(f"Rate limit or quota exceeded at the cloud provider. {detail}", retry_after)
        if r.status_code >= 500:
            raise ProviderUnavailableError(f"Cloud provider error (HTTP {r.status_code}). The service may be down. {detail}")
        raise ProviderUnavailableError(f"Cloud request failed (HTTP {r.status_code}): {detail}")

    def list_models(self) -> list[str]:
        try:
            with self._client(20) as c:
                r = c.get("models", headers=self._headers())
                self._raise_for_status(r)
                data = r.json().get("data", [])
                return sorted(str(m.get("id")) for m in data if m.get("id"))
        except httpx.ConnectError as exc:
            raise ProviderUnavailableError("Cannot reach the cloud API endpoint (network or DNS problem).") from exc
        except httpx.TimeoutException as exc:
            raise ProviderTimeoutError("The cloud API did not respond in time.") from exc
        except httpx.HTTPError as exc:
            raise ProviderUnavailableError(f"Cloud request failed: {type(exc).__name__}") from exc
        except ValueError as exc:
            raise MalformedResponseError("The provider returned an unexpected model list.") from exc

    def check_status(self) -> ProviderStatus:
        if not self._key():
            self.last_status = "No API key configured"
            return ProviderStatus(False, "No API key configured.")
        if not self._model():
            return ProviderStatus(False, "No cloud model selected.")
        try:
            resp = self.chat([{"role": "user", "content": "Reply with the single word OK."}], json_mode=False, temperature=0)
        except Exception as exc:
            self.last_status = f"Failed: {exc}"
            return ProviderStatus(False, str(exc))
        self.last_status = f"OK ({resp.duration_s:.1f}s)"
        return ProviderStatus(True, f"Connected to {self.name}, model '{self.model}' answered in {resp.duration_s:.1f}s.")

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
            raise ProviderNotConfiguredError("No cloud model configured. Enter a model name in AI Settings.")
        body: dict = {"model": model, "messages": messages, "temperature": temperature, "stream": True}
        if json_mode and self._json_mode():
            body["response_format"] = {"type": "json_object"}
        timeout = float(self._timeout())
        start = time.monotonic()
        parts: list[str] = []
        usage: dict[str, int] = {}
        try:
            with self._client(timeout) as c, c.stream("POST", "chat/completions", json=body, headers=self._headers()) as r:
                self._raise_for_status(r)
                ctype = r.headers.get("content-type", "")
                if "text/event-stream" not in ctype:
                    # Provider ignored stream=true: parse a normal JSON response.
                    r.read()
                    data = r.json()
                    parts.append(_extract_message(data))
                    usage = _usage(data)
                else:
                    for line in r.iter_lines():
                        if cancel and cancel.cancelled:
                            raise OperationCancelled("Operation cancelled by the user.")
                        if time.monotonic() - start > timeout:
                            raise ProviderTimeoutError(f"The cloud model did not finish within {timeout:.0f}s.")
                        if not line.startswith("data:"):
                            continue
                        payload = line[5:].strip()
                        if payload == "[DONE]":
                            break
                        try:
                            data = json.loads(payload)
                        except json.JSONDecodeError:
                            continue
                        if data.get("error"):
                            raise ProviderUnavailableError(redact(str(data["error"]))[:300])
                        for ch in data.get("choices") or []:
                            parts.append((ch.get("delta") or {}).get("content") or "")
                        if data.get("usage"):
                            usage = _usage(data)
        except httpx.ConnectError as exc:
            raise ProviderUnavailableError("Cannot reach the cloud API (network interruption or wrong endpoint).") from exc
        except httpx.TimeoutException as exc:
            raise ProviderTimeoutError(f"The cloud API did not respond within {timeout:.0f}s.") from exc
        except httpx.HTTPError as exc:
            raise ProviderUnavailableError(f"Cloud request failed: {type(exc).__name__}") from exc
        except (ValueError, KeyError) as exc:
            raise MalformedResponseError("The cloud provider returned an unexpected response format.") from exc
        text = "".join(parts)
        if not text.strip():
            raise MalformedResponseError("The cloud model returned an empty response.")
        self.last_status = f"Last call OK ({time.monotonic() - start:.1f}s)"
        return AIResponse(text=text, provider_name=self.name, kind=self.kind, model=model,
                          duration_s=time.monotonic() - start, usage=usage)


def _extract_message(data: dict) -> str:
    choices = data.get("choices") or []
    if not choices:
        raise MalformedResponseError("The cloud provider returned no choices.")
    return str((choices[0].get("message") or {}).get("content") or "")


def _usage(data: dict) -> dict[str, int]:
    u = data.get("usage") or {}
    return {k: int(u.get(k, 0) or 0) for k in ("prompt_tokens", "completion_tokens") if k in u}

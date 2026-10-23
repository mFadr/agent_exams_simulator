"""Routing of AI operations between the local and cloud providers.

Rules
-----
* ``LOCAL_ONLY`` / ``CLOUD_ONLY`` use exactly one provider; no silent switching.
* ``MANUAL`` uses the provider the user selected (or a per-operation override).
* ``HYBRID_AUTO`` prefers the local model for routine work and the cloud model
  for nuanced grading or high-complexity requests, and falls back to the other
  provider when the first fails.
* The cloud is only used for operations the user has permitted, never for
  study-material text unless explicitly allowed, and only after consent for
  the current operation (``cloud_consent=True``). The UI asks for consent
  before calling :meth:`HybridRouter.execute` whenever
  :attr:`RouteDecision.may_use_cloud` is true.
* Every fallback is recorded in :attr:`AIResponse.notes` and shown to the user.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Callable, Literal, Protocol

from ..config.settings import AIOperation, AISettings, OPERATION_LABELS, RoutingMode
from .provider_interface import (
    AIError,
    AIProvider,
    AIResponse,
    CancelToken,
    ChatMessage,
    CloudConsentRequired,
    OperationCancelled,
    ProviderKind,
    ProviderNotConfiguredError,
    RateLimitError,
)

log = logging.getLogger(__name__)

Complexity = Literal["low", "normal", "high"]
CLOUD_PREFERRED_OPS = {AIOperation.EVALUATE_WRITTEN, AIOperation.EVALUATE_CODE}
LOCAL_FEASIBLE_OPS = set(AIOperation)  # every operation can reasonably run locally (quality may differ)


class UsageLogger(Protocol):
    def __call__(self, *, operation: str, provider: str, model: str, kind: str, success: bool,
                 error_kind: str, chars_sent: int, duration_s: float) -> None: ...


@dataclass
class RouteDecision:
    operation: AIOperation
    chain: list[AIProvider] = field(default_factory=list)
    reason: str = ""
    excluded: list[str] = field(default_factory=list)

    @property
    def possible(self) -> bool:
        return bool(self.chain)

    @property
    def primary(self) -> AIProvider | None:
        return self.chain[0] if self.chain else None

    @property
    def may_use_cloud(self) -> bool:
        return any(p.kind == ProviderKind.CLOUD for p in self.chain)

    @property
    def cloud_is_primary(self) -> bool:
        return bool(self.chain) and self.chain[0].kind == ProviderKind.CLOUD

    def explain(self) -> str:
        if not self.chain:
            return "No AI provider is available for this operation. " + " ".join(self.excluded)
        names = " → then ".join(p.describe() for p in self.chain)
        text = f"{OPERATION_LABELS[self.operation]}: {names}. {self.reason}"
        if self.excluded:
            text += " (" + " ".join(self.excluded) + ")"
        return text


class HybridRouter:
    def __init__(
        self,
        settings_getter: Callable[[], AISettings],
        local: AIProvider,
        cloud: AIProvider,
        usage_logger: UsageLogger | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._settings = settings_getter
        self.local = local
        self.cloud = cloud
        self._log_usage = usage_logger
        self._sleep = sleep

    # ------------------------------------------------------------------ planning
    def plan(
        self,
        operation: AIOperation,
        *,
        complexity: Complexity = "normal",
        includes_source_material: bool = False,
        override: Literal["local", "cloud"] | None = None,
    ) -> RouteDecision:
        s = self._settings()
        d = RouteDecision(operation)
        mode = s.routing_mode
        if override:
            order = [self.local if override == "local" else self.cloud]
            d.reason = f"User override: {override} provider."
        elif mode == RoutingMode.LOCAL_ONLY:
            order = [self.local]
            d.reason = "Local-only mode."
        elif mode == RoutingMode.CLOUD_ONLY:
            order = [self.cloud]
            d.reason = "Cloud-only mode."
        elif mode == RoutingMode.MANUAL:
            order = [self.cloud if s.manual_provider == "cloud" else self.local]
            d.reason = f"Manual selection: {s.manual_provider}."
        else:
            prefer_cloud = operation in CLOUD_PREFERRED_OPS or complexity == "high"
            order = [self.cloud, self.local] if prefer_cloud else [self.local, self.cloud]
            d.reason = (
                "Hybrid: cloud preferred for nuanced grading / complex requests, local as fallback."
                if prefer_cloud else "Hybrid: local model preferred for routine work, cloud as fallback."
            )

        for p in order:
            if p.kind == ProviderKind.CLOUD:
                if operation not in s.cloud_allowed_operations:
                    d.excluded.append(f"Cloud not permitted for '{OPERATION_LABELS[operation]}' in AI Settings.")
                    continue
                if includes_source_material and not s.allow_source_material_to_cloud:
                    d.excluded.append("Study materials are not allowed to be sent to the cloud (privacy setting).")
                    continue
                if not p.is_configured():
                    d.excluded.append("Cloud provider is not configured (endpoint, model and API key needed).")
                    continue
            else:
                if not p.is_configured():
                    d.excluded.append("No local Ollama model selected.")
                    continue
            d.chain.append(p)
        return d

    # ------------------------------------------------------------------ execution
    def execute(
        self,
        operation: AIOperation,
        messages: list[ChatMessage],
        *,
        decision: RouteDecision | None = None,
        cloud_consent: bool = False,
        json_mode: bool = False,
        cancel: CancelToken | None = None,
        complexity: Complexity = "normal",
        includes_source_material: bool = False,
        override: Literal["local", "cloud"] | None = None,
    ) -> AIResponse:
        decision = decision or self.plan(
            operation, complexity=complexity, includes_source_material=includes_source_material, override=override
        )
        if not decision.chain:
            raise ProviderNotConfiguredError(decision.explain())
        s = self._settings()
        notes: list[str] = []
        errors: list[str] = []
        chars = sum(len(m["content"]) for m in messages)
        for idx, provider in enumerate(decision.chain):
            if provider.kind == ProviderKind.CLOUD and not cloud_consent:
                if len(decision.chain) == 1:
                    raise CloudConsentRequired("This operation would send data to a cloud provider and was not approved.")
                notes.append(f"Skipped {provider.describe()}: sending data to the cloud was not approved.")
                continue
            if idx > 0 and errors:
                notes.append(f"Fell back to {provider.describe()} after: {errors[-1]}")
            attempts = s.max_retries + 1
            for attempt in range(attempts):
                if cancel:
                    cancel.raise_if_cancelled()
                t0 = time.monotonic()
                try:
                    resp = provider.chat(messages, json_mode=json_mode, temperature=s.temperature, cancel=cancel)
                except OperationCancelled:
                    raise
                except AIError as exc:
                    self._record(operation, provider, False, exc.kind, chars, time.monotonic() - t0)
                    log.warning("AI %s via %s failed (attempt %d/%d): %s", operation.value, provider.name, attempt + 1, attempts, exc)
                    if exc.retriable and attempt + 1 < attempts:
                        delay = getattr(exc, "retry_after", None) if isinstance(exc, RateLimitError) else None
                        self._sleep(min(delay or (1.5 * (attempt + 1)), 20))
                        continue
                    errors.append(str(exc))
                    break
                self._record(operation, provider, True, "", chars, resp.duration_s)
                resp.notes = notes + resp.notes
                return resp
        if not errors and notes:
            raise CloudConsentRequired(" ".join(notes))
        raise AIError("All AI providers failed for this operation. " + " | ".join(errors))

    def _record(self, op: AIOperation, p: AIProvider, ok: bool, err: str, chars: int, dur: float) -> None:
        if self._log_usage is None:
            return
        try:
            self._log_usage(
                operation=op.value, provider=p.name, model=p.model, kind=p.kind.value, success=ok,
                error_kind=err, chars_sent=chars if p.kind == ProviderKind.CLOUD else 0, duration_s=dur,
            )
        except Exception:  # usage logging must never break an operation
            log.exception("Usage logging failed")

    def status_line(self) -> str:
        s = self._settings()
        parts = {
            RoutingMode.LOCAL_ONLY: f"Local only · {self.local.describe()}",
            RoutingMode.CLOUD_ONLY: f"Cloud only · {self.cloud.describe()}",
            RoutingMode.HYBRID_AUTO: f"Hybrid · local {self.local.model or '—'} / cloud {self.cloud.model or '—'}",
            RoutingMode.MANUAL: f"Manual · {(self.cloud if s.manual_provider == 'cloud' else self.local).describe()}",
        }
        return parts[s.routing_mode]

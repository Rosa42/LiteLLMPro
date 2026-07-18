"""Failure/success callbacks for shared-quota routing (phase 8).

Duck-types LiteLLM CustomLogger hooks so unit tests run without litellm installed.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from shared_quota_router.classifiers.base import (
    FailureClassification,
    FailureKind,
    UpstreamError,
)
from shared_quota_router.classifiers.generic_openai import (
    GenericOpenAIClassifier,
    is_high_confidence_quota_exhaust,
)
from shared_quota_router.lease import LeaseManager
from shared_quota_router.metrics import inc, set_gauge
from shared_quota_router.models import (
    DeploymentRuntimeState,
    ProviderStatus,
    QuotaGroup,
    QuotaGroupStatus,
    RequestRoutingContext,
)
from shared_quota_router.state_store import StateStore, StateStoreError
from shared_quota_router.strategy import (
    context_from_request_kwargs,
    save_request_context,
)

logger = logging.getLogger(__name__)

AlertHook = Callable[[str, dict[str, Any]], None]


def _default_alert(event: str, payload: dict[str, Any]) -> None:
    logger.error("ALERT %s %s", event, payload)


class SharedQuotaCallback:
    """Process success/failure events and update Redis quota state."""

    def __init__(
        self,
        store: StateStore,
        lease_manager: LeaseManager | None = None,
        classifier: GenericOpenAIClassifier | None = None,
        alert_hook: AlertHook | None = None,
        short_cooldown_seconds: int = 30,
        reqctx_ttl_seconds: int = 360,
    ) -> None:
        self.store = store
        self.lease_manager = lease_manager
        self.classifier = classifier or GenericOpenAIClassifier()
        self.alert_hook = alert_hook or _default_alert
        self.short_cooldown_seconds = short_cooldown_seconds
        self.reqctx_ttl_seconds = reqctx_ttl_seconds

    def _ctx(self, kwargs: dict[str, Any]) -> RequestRoutingContext:
        return context_from_request_kwargs(
            kwargs,
            store=self.store,
            ttl_seconds=self.reqctx_ttl_seconds,
        )

    def _save_ctx(self, ctx: RequestRoutingContext) -> None:
        save_request_context(ctx, self.store, ttl_seconds=self.reqctx_ttl_seconds)

    # --- LiteLLM CustomLogger-compatible hooks ---

    async def async_log_success_event(
        self, kwargs: dict, response_obj: Any, start_time: Any, end_time: Any
    ) -> None:
        self.on_success(kwargs)

    async def async_log_failure_event(
        self, kwargs: dict, response_obj: Any, start_time: Any, end_time: Any
    ) -> None:
        self.on_failure(kwargs, response_obj)

    async def async_log_stream_event(
        self, kwargs: dict, response_obj: Any, start_time: Any, end_time: Any
    ) -> None:
        # First stream chunk ⇒ mark first byte (hard switch gate)
        self.mark_first_byte(kwargs)

    async def async_post_call_streaming_hook(
        self, kwargs: dict | None = None, **_extra: Any
    ) -> None:
        if kwargs:
            self.mark_first_byte(kwargs)

    # --- Core logic ---

    def mark_first_byte(self, kwargs: dict[str, Any]) -> None:
        ctx = self._ctx(kwargs)
        if not ctx.first_byte_sent:
            ctx.mark_first_byte_sent()
            self._save_ctx(ctx)  # P0-3: immediately durable for strategy reloads
            inc("shared_quota_stream_first_byte_total")
            logger.info(
                "first_byte_sent request_id=%s — cross-deployment retry forbidden",
                ctx.request_id,
            )

    def should_allow_retry(self, kwargs: dict[str, Any]) -> bool:
        """Gate for cross-deployment retry (strategy also enforces via Redis)."""
        ctx = self._ctx(kwargs)
        if ctx.first_byte_sent:
            inc("shared_quota_stream_failure_after_first_byte_total")
            return False
        if len(ctx.tried_quota_groups) >= ctx.max_quota_groups:
            return False
        return True

    def on_success(self, kwargs: dict[str, Any]) -> None:
        meta = self._extract_deployment_meta(kwargs)
        request_id = str(kwargs.get("litellm_call_id") or meta.get("request_id") or "unknown")
        qg = meta.get("quota_group_id")
        dep_id = meta.get("deployment_id")
        now = datetime.now(timezone.utc)

        if self.lease_manager and qg:
            try:
                self.lease_manager.release(quota_group_id=qg, request_id=request_id)
            except StateStoreError as exc:
                logger.warning("lease release failed: %s", exc)

        if dep_id:
            try:
                st = self.store.get_deployment_state(dep_id) or DeploymentRuntimeState(
                    deployment_id=dep_id
                )
                st.last_success_at = now
                st.is_in_cooldown = False
                self.store.put_deployment_state(st)
            except StateStoreError as exc:
                logger.warning("deployment success update failed: %s", exc)

        if qg:
            try:
                group = self.store.get_quota_group(qg)
                if group is None:
                    group = QuotaGroup(
                        quota_group_id=qg,
                        provider_id=str(meta.get("provider_id") or "unknown"),
                        account_id=qg,
                        display_name=qg,
                    )
                group.last_success_at = now
                group.consecutive_failures = 0
                if group.status == QuotaGroupStatus.DEGRADED:
                    group.status = QuotaGroupStatus.AVAILABLE
                self.store.put_quota_group(group)
                set_gauge(
                    "shared_quota_group_status",
                    1.0,
                    quota_group_id=qg,
                    status=group.status.value,
                )
            except StateStoreError as exc:
                logger.warning("quota success update failed: %s", exc)

        inc(
            "shared_quota_route_selection_total",
            result="success",
            quota_group_id=qg or "unknown",
            deployment_id=dep_id or "unknown",
        )

    def on_failure(self, kwargs: dict[str, Any], response_obj: Any = None) -> None:
        meta = self._extract_deployment_meta(kwargs)
        ctx = self._ctx(kwargs)
        request_id = str(kwargs.get("litellm_call_id") or ctx.request_id)
        qg = meta.get("quota_group_id")
        dep_id = meta.get("deployment_id")
        provider_id = meta.get("provider_id")

        if self.lease_manager and qg:
            try:
                self.lease_manager.release(quota_group_id=qg, request_id=request_id)
            except StateStoreError as exc:
                logger.warning("lease release failed: %s", exc)

        if qg:
            ctx.mark_tried(qg)
        self._save_ctx(ctx)

        classification = self._classify(kwargs, response_obj)
        inc(
            "shared_quota_classifier_total",
            kind=classification.kind.value,
            scope=classification.scope,
        )
        if classification.confidence < 0.85 and classification.kind == FailureKind.SHARED_QUOTA_EXHAUSTED:
            # Safety: never exhaust on low confidence
            classification = FailureClassification(
                kind=FailureKind.SHORT_RATE_LIMIT,
                retryable=True,
                scope="deployment",
                confidence=classification.confidence,
                normalized_message="downgraded_low_confidence",
                retry_after_seconds=classification.retry_after_seconds,
                reset_at=classification.reset_at,
            )
            inc("shared_quota_classifier_low_confidence_total")

        logger.info(
            "failure kind=%s scope=%s conf=%.2f qg=%s dep=%s request_id=%s first_byte=%s",
            classification.kind.value,
            classification.scope,
            classification.confidence,
            qg,
            dep_id,
            request_id,
            ctx.first_byte_sent,
        )

        try:
            self._apply_classification(
                classification,
                quota_group_id=qg,
                deployment_id=dep_id,
                provider_id=provider_id,
            )
        except StateStoreError as exc:
            logger.error("fail-closed store update on failure: %s", exc)

        if classification.kind in {
            FailureKind.SHARED_QUOTA_EXHAUSTED,
            FailureKind.AUTH_INVALID,
            FailureKind.ACCOUNT_DISABLED,
        }:
            inc(
                "shared_quota_exhausted_total"
                if classification.kind == FailureKind.SHARED_QUOTA_EXHAUSTED
                else "shared_quota_failover_total",
                kind=classification.kind.value,
                quota_group_id=qg or "unknown",
            )
            if classification.kind == FailureKind.SHARED_QUOTA_EXHAUSTED:
                inc("shared_quota_failover_total", kind="quota_exhausted", quota_group_id=qg or "unknown")

        if ctx.first_byte_sent:
            inc("shared_quota_stream_failure_after_first_byte_total")

    def _apply_classification(
        self,
        c: FailureClassification,
        *,
        quota_group_id: str | None,
        deployment_id: str | None,
        provider_id: str | None,
    ) -> None:
        now = datetime.now(timezone.utc)

        if c.kind in {FailureKind.CONTENT_POLICY, FailureKind.BAD_REQUEST, FailureKind.CONTEXT_LIMIT}:
            # Request-scoped: do not melt account
            return

        if c.kind == FailureKind.SHARED_QUOTA_EXHAUSTED and is_high_confidence_quota_exhaust(c):
            if not quota_group_id:
                return
            base = self.store.get_quota_group(quota_group_id) or QuotaGroup(
                quota_group_id=quota_group_id,
                provider_id=provider_id or "unknown",
                account_id=quota_group_id,
                display_name=quota_group_id,
            )
            self.store.mark_exhausted(
                quota_group_id,
                reason=c.normalized_message or "shared_quota_exhausted",
                reset_at=c.reset_at,
                base=base,
            )
            set_gauge(
                "shared_quota_group_status",
                0.0,
                quota_group_id=quota_group_id,
                status="EXHAUSTED",
            )
            return

        if c.kind in {FailureKind.AUTH_INVALID, FailureKind.ACCOUNT_DISABLED}:
            if not quota_group_id:
                return
            group = self.store.get_quota_group(quota_group_id) or QuotaGroup(
                quota_group_id=quota_group_id,
                provider_id=provider_id or "unknown",
                account_id=quota_group_id,
                display_name=quota_group_id,
            )
            group.status = QuotaGroupStatus.DISABLED
            group.failure_reason = c.kind.value
            group.last_failure_at = now
            group.revision += 1
            self.store.put_quota_group(group)
            # P0-2: clear sticky routes to this account
            try:
                self.store.clear_affinity_for_quota_group(quota_group_id)
            except StateStoreError as exc:
                logger.warning("affinity clear on disable failed: %s", exc)
            self.alert_hook(
                "quota_group_disabled",
                {
                    "quota_group_id": quota_group_id,
                    "kind": c.kind.value,
                    "provider_id": provider_id,
                },
            )
            set_gauge(
                "shared_quota_group_status",
                0.0,
                quota_group_id=quota_group_id,
                status="DISABLED",
            )
            return

        if c.kind == FailureKind.PROVIDER_OUTAGE and provider_id:
            self.store.put_provider_status(
                provider_id,
                ProviderStatus.COOLDOWN,
                ttl_seconds=c.retry_after_seconds or self.short_cooldown_seconds,
            )
            return

        # SHORT_RATE_LIMIT / DEPLOYMENT_ERROR / NETWORK / UNKNOWN → deployment cooldown
        if deployment_id:
            until = now + timedelta(
                seconds=c.retry_after_seconds or self.short_cooldown_seconds
            )
            self.store.put_deployment_state(
                DeploymentRuntimeState(
                    deployment_id=deployment_id,
                    is_in_cooldown=True,
                    cooldown_until=until,
                    last_failure_at=now,
                ),
                ttl_seconds=c.retry_after_seconds or self.short_cooldown_seconds,
            )

    def _classify(self, kwargs: dict[str, Any], response_obj: Any) -> FailureClassification:
        status = None
        body: Any = None
        headers: dict[str, str] | None = None
        message = None

        exc = kwargs.get("exception") or response_obj
        if exc is not None:
            status = getattr(exc, "status_code", None) or getattr(exc, "status", None)
            message = str(exc)
            raw = getattr(exc, "response", None)
            if raw is not None:
                status = status or getattr(raw, "status_code", None)
                try:
                    headers = dict(getattr(raw, "headers", {}) or {})
                except Exception:  # noqa: BLE001
                    headers = None
                try:
                    body = raw.json() if hasattr(raw, "json") else getattr(raw, "text", None)
                except Exception:  # noqa: BLE001
                    body = getattr(raw, "text", None)

        if isinstance(response_obj, dict):
            body = body or response_obj
            err = response_obj.get("error") if isinstance(response_obj.get("error"), dict) else {}
            message = message or err.get("message")

        # litellm sometimes puts status in kwargs
        status = status or kwargs.get("response_status_code")

        error = UpstreamError(
            http_status=int(status) if status is not None else None,
            body=body,
            headers=headers,
            message=message,
            provider_id=(self._extract_deployment_meta(kwargs).get("provider_id")),
        )
        return self.classifier.classify(error)

    def _extract_deployment_meta(self, kwargs: dict[str, Any]) -> dict[str, str]:
        """Best-effort extraction of deployment_id / quota_group_id from litellm kwargs."""
        out: dict[str, str] = {}
        litellm_params = kwargs.get("litellm_params") or {}
        model_info = kwargs.get("model_info") or litellm_params.get("model_info") or {}
        metadata = kwargs.get("metadata") or {}

        for source in (model_info, metadata, litellm_params, kwargs):
            if not isinstance(source, dict):
                continue
            for key in (
                "deployment_id",
                "quota_group_id",
                "provider_id",
                "account_id",
                "model_group",
            ):
                if key not in out and source.get(key) is not None:
                    out[key] = str(source[key])

        # nested model_info
        mi = litellm_params.get("model_info") if isinstance(litellm_params, dict) else None
        if isinstance(mi, dict):
            for key in ("deployment_id", "quota_group_id", "provider_id"):
                if key not in out and mi.get(key) is not None:
                    out[key] = str(mi[key])

        # fallback: model_info id
        if "deployment_id" not in out and isinstance(model_info, dict) and model_info.get("id"):
            out["deployment_id"] = str(model_info["id"])

        return out

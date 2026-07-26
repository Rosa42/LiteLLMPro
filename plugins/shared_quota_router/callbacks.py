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
from shared_quota_router.protocol_context import (
    get_metadata_value,
    inject_protocol_into_data,
)
from shared_quota_router.protocol_errors import ProtocolAwareRoutingError
from shared_quota_router.protocol_gates import enforce_pre_call_gates
from shared_quota_router.registry import DeploymentRegistry
from shared_quota_router.state_store import StateStore, StateStoreError
from shared_quota_router.strategy import (
    context_from_request_kwargs,
    model_list_to_registry,
    save_request_context,
)

logger = logging.getLogger(__name__)

AlertHook = Callable[[str, dict[str, Any]], None]

try:
    from litellm.integrations.custom_logger import CustomLogger as _CustomLoggerBase
except Exception:  # pragma: no cover - unit tests without litellm
    class _CustomLoggerBase:  # type: ignore[no-redef]
        """Minimal stand-in when litellm is not installed."""


def _default_alert(event: str, payload: dict[str, Any]) -> None:
    logger.error("ALERT %s %s", event, payload)


class SharedQuotaCallback(_CustomLoggerBase):
    """Process success/failure events and update Redis quota state.

    Subclasses LiteLLM CustomLogger so proxy post-call hooks exist (no-op or
    thin wrappers). Business logic stays in on_success / on_failure.
    """

    def __init__(
        self,
        store: StateStore,
        lease_manager: LeaseManager | None = None,
        classifier: GenericOpenAIClassifier | None = None,
        alert_hook: AlertHook | None = None,
        short_cooldown_seconds: int = 30,
        reqctx_ttl_seconds: int = 360,
        registry: DeploymentRegistry | None = None,
    ) -> None:
        self.store = store
        self.lease_manager = lease_manager
        self.classifier = classifier or GenericOpenAIClassifier()
        self.alert_hook = alert_hook or _default_alert
        self.short_cooldown_seconds = short_cooldown_seconds
        self.reqctx_ttl_seconds = reqctx_ttl_seconds
        self._registry = registry

    def bind_registry(self, registry: DeploymentRegistry | None) -> None:
        """Attach / refresh deployment capability catalog for M3 endpoint gates."""
        self._registry = registry

    def bind_model_list(self, model_list: list[dict[str, Any]] | None) -> None:
        if not model_list:
            return
        try:
            self._registry = model_list_to_registry(model_list)
        except Exception as exc:  # noqa: BLE001
            logger.warning("bind_model_list failed: %s", exc)

    def _resolve_registry(self) -> DeploymentRegistry | None:
        if self._registry is not None:
            return self._registry
        # Best-effort: pull live router model_list from proxy
        try:
            from litellm.proxy import proxy_server

            router = getattr(proxy_server, "llm_router", None)
            ml = getattr(router, "model_list", None) if router is not None else None
            if ml:
                self._registry = model_list_to_registry(list(ml))
                return self._registry
        except Exception:  # noqa: BLE001
            return self._registry
        return None

    def _ctx(self, kwargs: dict[str, Any]) -> RequestRoutingContext:
        return context_from_request_kwargs(
            kwargs,
            store=self.store,
            ttl_seconds=self.reqctx_ttl_seconds,
        )

    def _save_ctx(self, ctx: RequestRoutingContext) -> None:
        save_request_context(ctx, self.store, ttl_seconds=self.reqctx_ttl_seconds)

    # --- LiteLLM CustomLogger-compatible hooks ---

    async def async_pre_call_hook(
        self,
        user_api_key_dict: Any = None,
        cache: Any = None,
        data: dict | None = None,
        call_type: Any = None,
        **_extra: Any,
    ) -> Any:
        """M2 inject protocol + M3 endpoint/feature gates (before drop_params)."""
        if not isinstance(data, dict):
            return data
        try:
            inject_protocol_into_data(data, call_type=call_type, overwrite=False)
            registry = self._resolve_registry()
            enforce_pre_call_gates(data, call_type=call_type, registry=registry)
        except ProtocolAwareRoutingError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("pre_call protocol gate failed: %s", exc)
        return data

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

    async def async_post_call_success_hook(
        self,
        data: dict,
        user_api_key_dict: Any = None,
        response: Any = None,
        **_extra: Any,
    ) -> Any:
        """Quota accounting + optional C2 response conversion (G0-B mount)."""
        if isinstance(data, dict):
            try:
                self.on_success(data)
            except Exception as exc:  # noqa: BLE001
                logger.warning("post_call_success on_success failed: %s", exc)
            try:
                response = self._maybe_convert_success_response(data, response)
            except ProtocolAwareRoutingError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning("post_call conversion failed: %s", exc)
        return response

    def _maybe_convert_success_response(self, data: dict[str, Any], response: Any) -> Any:
        from shared_quota_router.conversion.dispatch import (
            CONVERSION_DIR_META_KEY,
            ROUTE_MODE_META_KEY,
            convert_upstream_response,
            parse_direction_key,
        )
        from shared_quota_router.feature_flags import (
            is_native_messages_chat_path_active,
        )
        from shared_quota_router.protocol_context import get_metadata_value

        # G0-Native：由 LiteLLM 原生 adapter 返回 Anthropic 形态，跳过项目 G0-B reshape
        if is_native_messages_chat_path_active():
            return response

        mode = get_metadata_value(data, ROUTE_MODE_META_KEY)
        if mode != "convert":
            return response
        direction = parse_direction_key(get_metadata_value(data, CONVERSION_DIR_META_KEY))
        if direction is None:
            return response
        # Prefer dict-shaped responses; ModelResponse-like objects expose model_dump/dict
        payload: dict[str, Any] | None = None
        if isinstance(response, dict):
            payload = response
        elif hasattr(response, "model_dump"):
            try:
                payload = response.model_dump()
            except Exception:  # noqa: BLE001
                payload = None
        elif hasattr(response, "dict"):
            try:
                payload = response.dict()
            except Exception:  # noqa: BLE001
                payload = None
        if not isinstance(payload, dict):
            return response
        converted = convert_upstream_response(payload, direction=direction)
        return converted.payload

    async def async_post_call_failure_hook(
        self,
        request_data: dict,
        original_exception: Exception,
        user_api_key_dict: Any = None,
        traceback_str: str | None = None,
        **_extra: Any,
    ) -> Any:
        """Account failure + reshape client error when possible (P2).

        LiteLLM only honors a returned ``HTTPException`` for error transform.
        """
        if isinstance(request_data, dict):
            try:
                self.on_failure(request_data, original_exception)
            except Exception as exc:  # noqa: BLE001
                logger.warning("post_call_failure on_failure failed: %s", exc)

        http_exc = self._failure_to_http_exception(request_data, original_exception)
        return http_exc

    def _failure_to_http_exception(
        self,
        request_data: Any,
        original_exception: Exception,
    ) -> Any:
        """Return fastapi HTTPException or None (P2-01 / P2-02)."""
        try:
            from fastapi import HTTPException
        except ImportError:
            return None

        data = request_data if isinstance(request_data, dict) else {}

        # P2-01: protocol gate / conversion mapping errors → native shape
        proto_exc: ProtocolAwareRoutingError | None = None
        if isinstance(original_exception, ProtocolAwareRoutingError):
            proto_exc = original_exception
        elif isinstance(data.get("exception"), ProtocolAwareRoutingError):
            proto_exc = data["exception"]
        if proto_exc is not None:
            return HTTPException(status_code=400, detail=proto_exc.to_public_error())

        # P2-02: convert-route upstream failures → public protocol error body
        from shared_quota_router.conversion.dispatch import (
            CONVERSION_DIR_META_KEY,
            ROUTE_MODE_META_KEY,
            convert_upstream_error,
            parse_direction_key,
        )
        from shared_quota_router.protocol_context import get_metadata_value

        mode = get_metadata_value(data, ROUTE_MODE_META_KEY)
        if mode != "convert":
            return None
        direction = parse_direction_key(get_metadata_value(data, CONVERSION_DIR_META_KEY))
        if direction is None:
            return None

        status = (
            getattr(original_exception, "status_code", None)
            or getattr(original_exception, "status", None)
            or 502
        )
        try:
            status_i = int(status)
        except (TypeError, ValueError):
            status_i = 502

        upstream_body: dict[str, Any]
        detail = getattr(original_exception, "detail", None)
        if isinstance(detail, dict):
            upstream_body = detail
        else:
            upstream_body = {
                "error": {
                    "message": str(original_exception),
                    "type": "api_error",
                }
            }
        try:
            shaped = convert_upstream_error(upstream_body, direction=direction)
        except Exception as exc:  # noqa: BLE001
            logger.warning("convert_upstream_error failed: %s", exc)
            return None
        return HTTPException(status_code=status_i, detail=shaped)

    # NOTE: Do NOT override async_post_call_streaming_hook.
    # LiteLLM calls it with response=<accumulated content STRING>. Returning
    # that string replaces the ModelResponseStream chunk and breaks SSE into
    # bare text (e.g. data: 你好), which makes OpenCode / AI SDK fail with
    # "JSON parsing failed: Text: 你好". First-byte marking uses
    # async_log_stream_event only.

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
        # M2-04: deterministic protocol/capability errors never retry across deployments
        exc = kwargs.get("exception")
        if isinstance(exc, ProtocolAwareRoutingError):
            return False
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

        # P1-03: always release lease before any early return (incl. protocol errors)
        if self.lease_manager and qg:
            try:
                self.lease_manager.release(quota_group_id=qg, request_id=request_id)
            except StateStoreError as exc:
                logger.warning("lease release failed: %s", exc)

        # M2-05: pre-call protocol/capability failures are not provider/quota events
        if isinstance(response_obj, ProtocolAwareRoutingError) or isinstance(
            kwargs.get("exception"), ProtocolAwareRoutingError
        ):
            exc = (
                response_obj
                if isinstance(response_obj, ProtocolAwareRoutingError)
                else kwargs.get("exception")
            )
            assert isinstance(exc, ProtocolAwareRoutingError)
            inc(
                "shared_quota_protocol_no_route_total",
                reason=exc.reason.value,
                protocol=exc.protocol.value if exc.protocol else "none",
            )
            logger.info(
                "protocol_no_route reason=%s protocol=%s model_group=%s — no circuit update",
                exc.reason.value,
                exc.protocol.value if exc.protocol else None,
                exc.model_group,
            )
            return

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
                route_mode=str(
                    get_metadata_value(kwargs, "shared_quota_route_mode") or "direct"
                ),
                conversion_dir=get_metadata_value(
                    kwargs, "shared_quota_conversion"
                ),
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
        route_mode: str = "direct",
        conversion_dir: str | None = None,
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

        # C3: convert-path infra failures stay on route-scoped keys so they do not
        # open provider / deployment circuits that would poison sibling direct traffic.
        # Account/quota scopes above still apply (shared fate on the same account).
        if route_mode == "convert":
            if not deployment_id:
                return
            until = now + timedelta(
                seconds=c.retry_after_seconds or self.short_cooldown_seconds
            )
            ttl = c.retry_after_seconds or self.short_cooldown_seconds
            route_key = self.store.route_cooldown_key(
                route_mode="convert",
                conversion_dir=conversion_dir,
            )
            self.store.put_route_cooldown(
                deployment_id,
                route_key,
                cooldown_until=until,
                ttl_seconds=ttl,
            )
            logger.info(
                "convert_route_cooldown deployment_id=%s route_key=%s kind=%s until=%s",
                deployment_id,
                route_key,
                c.kind.value,
                until.isoformat(),
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
            ttl = c.retry_after_seconds or self.short_cooldown_seconds
            self.store.put_deployment_state(
                DeploymentRuntimeState(
                    deployment_id=deployment_id,
                    is_in_cooldown=True,
                    cooldown_until=until,
                    last_failure_at=now,
                ),
                ttl_seconds=ttl,
            )
            # Mirror direct onto route key for unified C3 checks
            self.store.put_route_cooldown(
                deployment_id,
                "direct",
                cooldown_until=until,
                ttl_seconds=ttl,
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
        litellm_metadata = kwargs.get("litellm_metadata") or {}
        nested_meta = (
            litellm_params.get("metadata") if isinstance(litellm_params, dict) else None
        ) or {}

        for source in (
            model_info,
            metadata,
            litellm_metadata,
            nested_meta,
            litellm_params,
            kwargs,
        ):
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

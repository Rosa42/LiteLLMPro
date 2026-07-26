"""Shared-quota routing strategy (Fill First + affinity + tried set).

Implements LiteLLM CustomRoutingStrategyBase contract (v1.90.5).
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone
from typing import Any, Optional, Union

from shared_quota_router.conversion.registry import resolve_route
from shared_quota_router.feature_flags import (
    is_conversion_routing_active,
    is_native_messages_chat_path_active,
    is_protocol_aware_gateway_enabled,
)
from shared_quota_router.lease import LeaseManager
from shared_quota_router.models import (
    ApiProtocol,
    Deployment,
    Feature,
    LogicalModelProtocols,
    ProviderStatus,
    QuotaGroupStatus,
    RequestRoutingContext,
    RouteCandidate,
    RouteMode,
)
from shared_quota_router.protocol_context import (
    RequestProtocolContext,
    get_metadata_value,
    resolve_request_protocol_context,
)
from shared_quota_router.protocol_errors import (
    NoAvailableDeploymentError,
    ProtocolAwareRoutingError,
    ProtocolRoutingReason,
)
from shared_quota_router.protocol_observability import (
    record_protocol_rejection,
    record_route_selection,
)
from shared_quota_router.registry import DeploymentRegistry, deployment_from_model_entry
from shared_quota_router.state_store import StateStore, StateStoreError

logger = logging.getLogger(__name__)

# Re-export for existing imports: `from shared_quota_router.strategy import NoAvailableDeploymentError`
__all__ = [
    "AFFINITY_TTL_SECONDS",
    "DEFAULT_REQUEST_TIMEOUT",
    "NoAvailableDeploymentError",
    "ProtocolAwareRoutingError",
    "SharedQuotaSelector",
    "SharedQuotaRoutingStrategy",
    "context_from_request_kwargs",
    "find_model_entry",
    "model_list_to_registry",
    "resolve_request_id",
    "save_request_context",
    "session_key_from_request",
]

AFFINITY_TTL_SECONDS = 7200  # 2h
DEFAULT_REQUEST_TIMEOUT = 300

# In-process cache only. NEVER put RequestRoutingContext on request kwargs —
# LiteLLM may JSON-serialize kwargs (logs / exception mapping) and would fail.
_CTX_BY_REQUEST_ID: dict[str, RequestRoutingContext] = {}


class SharedQuotaSelector:
    """Pure selection logic over registry + Redis state (unit-test friendly)."""

    def __init__(
        self,
        registry: DeploymentRegistry,
        store: StateStore,
        lease_manager: LeaseManager | None = None,
        logical_models: dict[str, LogicalModelProtocols] | None = None,
    ) -> None:
        self.registry = registry
        self.store = store
        self.lease_manager = lease_manager
        self.logical_models = logical_models or {}

    def model_group_is_protocol_aware(self, model_group: str) -> bool:
        """True when any deployment in the group declares ``upstream_protocol``.

        Groups with zero protocol metadata stay on the legacy selection path so
        older fixtures / gradual rollout keep working. Once any deployment is
        tagged, filtering is fail-closed (missing tag ≠ universal support).
        """
        return any(
            d.upstream_protocol is not None
            for d in self.registry.get_by_model_group(model_group)
        )

    def filter_by_capability(
        self,
        model_group: str,
        protocol_ctx: RequestProtocolContext,
        *,
        logical: LogicalModelProtocols | None = None,
    ) -> list[Deployment]:
        """M2-02 / C1-04: protocol + feature (+ optional convert) before Redis/lease.

        Does not touch Redis. Missing ``upstream_protocol`` ≠ universal support.
        Direct routes are listed before convert routes when both exist.
        """
        routes = self.filter_route_candidates(
            model_group, protocol_ctx, logical=logical
        )
        return [r.deployment for r in routes]

    def filter_route_candidates(
        self,
        model_group: str,
        protocol_ctx: RequestProtocolContext,
        *,
        logical: LogicalModelProtocols | None = None,
    ) -> list[RouteCandidate]:
        if protocol_ctx.protocol is None:
            return []
        lm = logical if logical is not None else self.logical_models.get(model_group)
        stream = Feature.STREAMING in protocol_ctx.required_features or False
        # Also honor raw stream via features already extracted in protocol_ctx
        conversion_on = is_conversion_routing_active()
        out: list[RouteCandidate] = []
        for dep in self.registry.get_by_model_group(model_group):
            route = resolve_route(
                dep,
                public_protocol=protocol_ctx.protocol,
                required_features=protocol_ctx.required_features,
                stream=stream,
                logical=lm,
                conversion_enabled=conversion_on,
            )
            if route is not None:
                out.append(route)
        out.sort(
            key=lambda r: (
                0 if r.route_mode is RouteMode.DIRECT else 1,
                r.deployment.priority,
                r.deployment.deployment_id,
            )
        )
        return out

    def filter_candidates(
        self,
        model_group: str,
        context: RequestRoutingContext,
        *,
        now: datetime | None = None,
        protocol_ctx: RequestProtocolContext | None = None,
    ) -> list[Deployment]:
        if context.first_byte_sent:
            logger.warning(
                "first_byte_sent=True; refusing cross-deployment selection request_id=%s",
                context.request_id,
            )
            return []

        now = now or datetime.now(timezone.utc)

        # M2-02 / C1-04: capability filter first — no Redis, no lease, no tried mutation
        route_by_dep: dict[str, RouteMode] = {}
        conversion_dir_by_dep: dict[str, str | None] = {}
        if protocol_ctx is not None and protocol_ctx.protocol is not None:
            routes = self.filter_route_candidates(model_group, protocol_ctx)
            deployments = [r.deployment for r in routes]
            for r in routes:
                route_by_dep[r.deployment.deployment_id] = r.route_mode
                if r.conversion is not None:
                    conversion_dir_by_dep[r.deployment.deployment_id] = (
                        f"{r.conversion.source.value}>{r.conversion.target.value}"
                    )
                else:
                    conversion_dir_by_dep[r.deployment.deployment_id] = None
        else:
            # Legacy path (unit tests / pre-protocol callers): no protocol gate
            deployments = self.registry.get_by_model_group(model_group)

        candidates: list[Deployment] = []

        for dep in deployments:
            if not dep.enabled:
                continue
            if not context.can_try_quota_group(dep.quota_group_id):
                continue

            try:
                provider_status = self.store.get_provider_status(dep.provider_id)
            except StateStoreError:
                # fail-closed: cannot verify provider
                raise

            # Missing key → treat provider as AVAILABLE (configured, not yet degraded)
            if provider_status is not None and provider_status not in {
                ProviderStatus.AVAILABLE,
                ProviderStatus.DEGRADED,
            }:
                continue

            try:
                quota = self.store.get_quota_group(dep.quota_group_id)
            except StateStoreError:
                raise

            if quota is not None and quota.status != QuotaGroupStatus.AVAILABLE:
                continue

            mode = route_by_dep.get(dep.deployment_id, RouteMode.DIRECT)
            route_key = self.store.route_cooldown_key(
                route_mode=mode.value,
                conversion_dir=conversion_dir_by_dep.get(dep.deployment_id),
            )
            try:
                if self.store.is_route_in_cooldown(
                    dep.deployment_id, route_key, now=now
                ):
                    continue
            except StateStoreError:
                raise

            # Legacy deployment-wide cooldown applies to **direct** only so a
            # convert-path failure (C3) does not block same-deployment direct.
            if mode is RouteMode.DIRECT:
                try:
                    dep_state = self.store.get_deployment_state(dep.deployment_id)
                except StateStoreError:
                    raise

                if dep_state is not None and dep_state.is_in_cooldown:
                    if dep_state.cooldown_until is None or dep_state.cooldown_until > now:
                        continue

            candidates.append(dep)

        return candidates

    def rank_candidates(
        self,
        candidates: list[Deployment],
        *,
        affinity_deployment_id: str | None = None,
        inflight: dict[str, int] | None = None,
        last_success: dict[str, datetime | None] | None = None,
        route_modes: dict[str, RouteMode] | None = None,
    ) -> list[Deployment]:
        inflight = inflight or {}
        last_success = last_success or {}
        route_modes = route_modes or {}

        def sort_key(d: Deployment) -> tuple:
            mode = route_modes.get(d.deployment_id, RouteMode.DIRECT)
            route_mode_rank = 0 if mode is RouteMode.DIRECT else 1
            affinity_rank = 0 if affinity_deployment_id and d.deployment_id == affinity_deployment_id else 1
            # Prefer lower priority number
            prio = d.priority
            infl = inflight.get(d.quota_group_id, 0)
            # Prefer more recent success → invert timestamp
            ls = last_success.get(d.deployment_id)
            ls_key = -(ls.timestamp()) if ls is not None else 0.0
            return (route_mode_rank, affinity_rank, prio, infl, ls_key, d.deployment_id)

        return sorted(candidates, key=sort_key)

    def select(
        self,
        model_group: str,
        context: RequestRoutingContext,
        *,
        affinity_deployment_id: str | None = None,
        request_timeout_seconds: float = DEFAULT_REQUEST_TIMEOUT,
        acquire_lease: bool = True,
        protocol_ctx: RequestProtocolContext | None = None,
    ) -> Deployment:
        route_modes: dict[str, RouteMode] = {}
        # M2-02/M2-05/C1-04: empty capability set → protocol-aware error, no lease / no tried
        if protocol_ctx is not None and protocol_ctx.protocol is not None:
            routes = self.filter_route_candidates(model_group, protocol_ctx)
            route_modes = {r.deployment.deployment_id: r.route_mode for r in routes}
            if not routes:
                # Distinguish feature-only failures on direct deployments (M2 behavior)
                by_protocol = [
                    d
                    for d in self.registry.filter_by_protocol(
                        model_group, protocol_ctx.protocol
                    )
                    if d.enabled
                ]
                if by_protocol:
                    raise ProtocolAwareRoutingError(
                        f"no feature-compatible deployment for model_group={model_group} "
                        f"protocol={protocol_ctx.protocol.value} "
                        f"features={sorted(f.value for f in protocol_ctx.required_features)}",
                        reason=ProtocolRoutingReason.FEATURE_UNSUPPORTED,
                        protocol=protocol_ctx.protocol,
                        model_group=model_group,
                        details={
                            "required_features": sorted(
                                f.value for f in protocol_ctx.required_features
                            ),
                            "source": protocol_ctx.source,
                        },
                    )
                reason = (
                    ProtocolRoutingReason.UNSUPPORTED_PUBLIC_PROTOCOL
                    if protocol_ctx.protocol is ApiProtocol.OPENAI_RESPONSES
                    else ProtocolRoutingReason.NO_COMPATIBLE_DEPLOYMENT
                )
                raise ProtocolAwareRoutingError(
                    f"no protocol-compatible deployment for model_group={model_group} "
                    f"protocol={protocol_ctx.protocol.value}",
                    reason=reason,
                    protocol=protocol_ctx.protocol,
                    model_group=model_group,
                    details={"source": protocol_ctx.source},
                )

        candidates = self.filter_candidates(
            model_group, context, protocol_ctx=protocol_ctx
        )
        if not candidates:
            raise NoAvailableDeploymentError(
                f"no candidates for model_group={model_group} request_id={context.request_id}"
            )

        # M2-03: affinity only among post-capability candidates; ignore if absent
        effective_affinity = affinity_deployment_id
        if effective_affinity and not any(
            d.deployment_id == effective_affinity for d in candidates
        ):
            logger.info(
                "ignoring incompatible affinity deployment_id=%s model_group=%s",
                effective_affinity,
                model_group,
            )
            effective_affinity = None

        inflight: dict[str, int] = {}
        last_success: dict[str, datetime | None] = {}
        if self.lease_manager is not None:
            for d in candidates:
                try:
                    inflight[d.quota_group_id] = self.lease_manager.get_inflight(d.quota_group_id)
                except StateStoreError:
                    raise
        for d in candidates:
            try:
                st = self.store.get_deployment_state(d.deployment_id)
            except StateStoreError:
                raise
            last_success[d.deployment_id] = st.last_success_at if st else None

        ranked = self.rank_candidates(
            candidates,
            affinity_deployment_id=effective_affinity,
            inflight=inflight,
            last_success=last_success,
            route_modes=route_modes,
        )

        for dep in ranked:
            # M2-04: lease only after capability + state filtering
            if acquire_lease and self.lease_manager is not None:
                try:
                    ok = self.lease_manager.acquire(
                        quota_group_id=dep.quota_group_id,
                        request_id=context.request_id,
                        request_timeout_seconds=request_timeout_seconds,
                    )
                except StateStoreError:
                    raise
                if not ok:
                    continue
            context.mark_tried(dep.quota_group_id)
            return dep

        raise NoAvailableDeploymentError(
            f"all candidates failed lease for model_group={model_group}"
        )


def session_key_from_request(
    *,
    model: str,
    messages: list[dict[str, Any]] | None,
    request_kwargs: dict[str, Any] | None,
    client_api_key_id: str | None = None,
) -> str:
    """Build session hash for affinity (design §6.3). Dual-bucket metadata."""
    kwargs = request_kwargs or {}

    for key in ("session_id", "conversation_id"):
        val = get_metadata_value(kwargs, key) or kwargs.get(key)
        if val:
            return hashlib.sha256(str(val).encode()).hexdigest()

    user = kwargs.get("user") or get_metadata_value(kwargs, "user")
    if user:
        return hashlib.sha256(f"user:{user}".encode()).hexdigest()

    headers = kwargs.get("headers") or kwargs.get("secret_fields") or {}
    if isinstance(headers, dict):
        for hk in ("x-llm-session-id", "X-LLM-Session-ID"):
            if headers.get(hk):
                return hashlib.sha256(str(headers[hk]).encode()).hexdigest()

    # Fallback: model + first two message digests + client key id
    parts = [model]
    if messages:
        for m in messages[:2]:
            parts.append(str(m.get("content", ""))[:200])
    if client_api_key_id:
        parts.append(client_api_key_id)
    return hashlib.sha256("|".join(parts).encode()).hexdigest()


def resolve_request_id(
    request_kwargs: dict[str, Any] | None,
    *,
    default_request_id: str = "unknown",
) -> str:
    kwargs = request_kwargs or {}
    rid = (
        kwargs.get("litellm_call_id")
        or kwargs.get("litellm_trace_id")
        or get_metadata_value(kwargs, "request_id")
        or default_request_id
    )
    return str(rid)


def context_from_request_kwargs(
    request_kwargs: dict[str, Any] | None,
    *,
    default_request_id: str = "unknown",
    store: StateStore | None = None,
    persist: bool = True,
    ttl_seconds: int = 360,
) -> RequestRoutingContext:
    """Resolve routing context: process cache → Redis reqctx → new.

    P0: Redis-backed so LiteLLM retries without shared kwargs still see
    tried_quota_groups and first_byte_sent.

    Do not attach the context object onto ``request_kwargs`` — LiteLLM may
    JSON-serialize kwargs and ``RequestRoutingContext`` is not serializable.
    """
    kwargs = request_kwargs if request_kwargs is not None else {}
    request_id = resolve_request_id(kwargs, default_request_id=default_request_id)

    existing = _CTX_BY_REQUEST_ID.get(request_id)
    if isinstance(existing, RequestRoutingContext):
        if store is not None and existing.request_id and existing.request_id != "unknown":
            try:
                remote = store.get_request_context(existing.request_id)
                if remote is not None:
                    existing.tried_quota_groups |= remote.tried_quota_groups
                    existing.first_byte_sent = (
                        existing.first_byte_sent or remote.first_byte_sent
                    )
            except StateStoreError:
                pass
        return existing

    ctx: RequestRoutingContext | None = None
    if store is not None and request_id != "unknown":
        try:
            ctx = store.get_request_context(request_id)
        except StateStoreError:
            ctx = None
    if ctx is None:
        ctx = RequestRoutingContext(request_id=request_id)
        if store is not None and persist and request_id != "unknown":
            try:
                store.put_request_context(ctx, ttl_seconds=ttl_seconds)
            except StateStoreError as exc:
                logger.warning("reqctx create failed: %s", exc)

    if request_id != "unknown":
        _CTX_BY_REQUEST_ID[request_id] = ctx
        # Bound process cache growth (best-effort)
        if len(_CTX_BY_REQUEST_ID) > 2048:
            for old_key in list(_CTX_BY_REQUEST_ID.keys())[:512]:
                _CTX_BY_REQUEST_ID.pop(old_key, None)
    return ctx


def save_request_context(
    ctx: RequestRoutingContext,
    store: StateStore | None,
    *,
    ttl_seconds: int = 360,
) -> None:
    if store is None:
        return
    try:
        store.put_request_context(ctx, ttl_seconds=ttl_seconds)
    except StateStoreError as exc:
        logger.warning("reqctx save failed: %s", exc)


def model_list_to_registry(model_list: list[dict[str, Any]]) -> DeploymentRegistry:
    reg = DeploymentRegistry()
    for entry in model_list:
        try:
            # LiteLLM may use model_info.id instead of deployment_id
            info = dict(entry.get("model_info") or {})
            if "deployment_id" not in info and info.get("id"):
                info["deployment_id"] = info["id"]
            entry = {**entry, "model_info": info}
            reg.add(deployment_from_model_entry(entry))
        except (ValueError, KeyError) as exc:
            logger.debug("skip model entry: %s", exc)
    return reg


def find_model_entry(
    model_list: list[dict[str, Any]], deployment: Deployment
) -> dict[str, Any] | None:
    for entry in model_list:
        info = entry.get("model_info") or {}
        dep_id = info.get("deployment_id") or info.get("id")
        if dep_id == deployment.deployment_id:
            return entry
        # Fallback: match model_name + api_key env fingerprint
        if entry.get("model_name") == deployment.model_group:
            params = entry.get("litellm_params") or {}
            if deployment.api_base and params.get("api_base") == deployment.api_base:
                return entry
    return None


class SharedQuotaRoutingStrategy:
    """LiteLLM CustomRoutingStrategyBase-compatible strategy.

    Does not inherit the base class at import time so unit tests run without litellm.
    C0 tests verify isinstance / duck-typing against CustomRoutingStrategyBase.
    """

    def __init__(
        self,
        *,
        store: StateStore,
        lease_manager: LeaseManager | None = None,
        router: Any = None,
        registry: DeploymentRegistry | None = None,
        logical_models: dict[str, LogicalModelProtocols] | None = None,
    ) -> None:
        self.store = store
        self.lease_manager = lease_manager
        self._router = router
        self._registry = registry
        from shared_quota_router.logical_policy import resolve_runtime_logical_models

        self.logical_models = resolve_runtime_logical_models(explicit=logical_models)
        self._selector: SharedQuotaSelector | None = None

    def bind_router(self, router: Any) -> None:
        self._router = router

    def _get_model_list(self) -> list[dict[str, Any]]:
        if self._router is None:
            return []
        ml = getattr(self._router, "model_list", None)
        if ml is None and hasattr(self._router, "get_model_list"):
            ml = self._router.get_model_list()
        return list(ml or [])

    def _selector_for(self, model_list: list[dict[str, Any]]) -> SharedQuotaSelector:
        registry = self._registry or model_list_to_registry(model_list)
        return SharedQuotaSelector(
            registry,
            self.store,
            self.lease_manager,
            logical_models=self.logical_models,
        )

    async def async_get_available_deployment(
        self,
        model: str,
        messages: Optional[list[dict[str, str]]] = None,
        input: Optional[Union[str, list]] = None,  # noqa: A002
        specific_deployment: Optional[bool] = False,
        request_kwargs: Optional[dict] = None,
    ) -> dict[str, Any]:
        return self.get_available_deployment(
            model=model,
            messages=messages,
            input=input,
            specific_deployment=specific_deployment,
            request_kwargs=request_kwargs,
        )

    def get_available_deployment(
        self,
        model: str,
        messages: Optional[list[dict[str, str]]] = None,
        input: Optional[Union[str, list]] = None,  # noqa: A002
        specific_deployment: Optional[bool] = False,
        request_kwargs: Optional[dict] = None,
    ) -> dict[str, Any]:
        model_list = self._get_model_list()
        if not model_list:
            raise NoAvailableDeploymentError("router model_list is empty")

        selector = self._selector_for(model_list)
        # P0: always load/merge Redis reqctx so retries share tried + first_byte
        ctx = context_from_request_kwargs(request_kwargs, store=self.store)

        # M2-01: authoritative protocol context (dual-bucket + call_type breadcrumb)
        protocol_ctx = resolve_request_protocol_context(request_kwargs)
        if protocol_ctx.source.startswith("invalid:"):
            raise ProtocolAwareRoutingError(
                "invalid protocol metadata on request",
                reason=ProtocolRoutingReason.CONFIGURATION_INVALID,
                protocol=None,
                model_group=model,
                details={"source": protocol_ctx.source},
            )

        # P0-3 hard gate: after first stream byte, refuse any cross-deployment pick
        if ctx.first_byte_sent:
            logger.error(
                "refusing deployment selection: first_byte_sent request_id=%s",
                ctx.request_id,
            )
            raise NoAvailableDeploymentError(
                f"stream first byte already sent; cross-deployment switch forbidden "
                f"request_id={ctx.request_id}"
            )

        try:
            session_hash = session_key_from_request(
                model=model,
                messages=list(messages) if messages else None,
                request_kwargs=request_kwargs,
            )
            affinity_id = None
            try:
                affinity_id = self.store.get_affinity(session_hash)
            except StateStoreError:
                raise

            # When protocol is known AND the model group is capability-tagged, enforce filter.
            # Untagged model groups (legacy / P0 fixtures) keep pre-M2 selection behavior.
            # M4-02: PROTOCOL_AWARE_GATEWAY_ENABLED=false → legacy Chat (skip capability filter).
            select_kwargs: dict[str, Any] = {
                "affinity_deployment_id": affinity_id,
            }
            gateway_on = is_protocol_aware_gateway_enabled()
            if (
                gateway_on
                and protocol_ctx.protocol is not None
                and selector.model_group_is_protocol_aware(model)
            ):
                # M3: public opt-in required (defense in depth with pre-call gates)
                if not selector.registry.model_opts_into_public(
                    model, protocol_ctx.protocol
                ):
                    raise ProtocolAwareRoutingError(
                        f"model {model!r} is not opted into public protocol "
                        f"{protocol_ctx.protocol.value}",
                        reason=ProtocolRoutingReason.UNSUPPORTED_PUBLIC_PROTOCOL,
                        protocol=protocol_ctx.protocol,
                        model_group=model,
                    )
                select_kwargs["protocol_ctx"] = protocol_ctx

            chosen = selector.select(model, ctx, **select_kwargs)
            # Persist tried set for next retry (even if kwargs object differs)
            save_request_context(ctx, self.store)
        except ProtocolAwareRoutingError as exc:
            record_protocol_rejection(
                public_protocol=exc.protocol or (
                    protocol_ctx.protocol if protocol_ctx else None
                ),
                reason=exc.reason.value,
                model_group=model,
            )
            raise
        except StateStoreError as exc:
            logger.error("fail-closed: redis error during routing: %s", exc)
            raise NoAvailableDeploymentError(f"redis unavailable: {exc}") from exc

        entry = find_model_entry(model_list, chosen)
        if entry is None:
            for e in model_list:
                if e.get("model_name") == model:
                    info = e.get("model_info") or {}
                    if info.get("quota_group_id") == chosen.quota_group_id:
                        entry = e
                        break
            if entry is None:
                raise NoAvailableDeploymentError(
                    f"no model_list entry for deployment {chosen.deployment_id}"
                )

        try:
            self.store.set_affinity(
                session_hash,
                chosen.deployment_id,
                quota_group_id=chosen.quota_group_id,
                ttl_seconds=AFFINITY_TTL_SECONDS,
            )
        except StateStoreError as exc:
            logger.warning("affinity write failed: %s", exc)

        # C1-04 / C2-04: resolve route mode for observability + request conversion hook
        route_mode = "direct"
        conversion_dir: str | None = None
        if protocol_ctx.protocol is not None and chosen.upstream_protocol is not None:
            if chosen.upstream_protocol is protocol_ctx.protocol:
                route_mode = "direct"
            elif is_conversion_routing_active():
                # Convert only when selected deployment speaks a different protocol
                route_mode = "convert"
                conversion_dir = (
                    f"{protocol_ctx.protocol.value}>{chosen.upstream_protocol.value}"
                )
                # G0-Native：LiteLLM 原生 Messages→Chat 负责变换；禁止再跑项目 C2 改写
                if not is_native_messages_chat_path_active():
                    try:
                        _apply_convert_to_request_kwargs(
                            request_kwargs,
                            public_protocol=protocol_ctx.protocol,
                            upstream_protocol=chosen.upstream_protocol,
                        )
                    except Exception:
                        # P1-04: convert failure after lease must not leak the lease
                        if self.lease_manager is not None:
                            try:
                                self.lease_manager.release(
                                    quota_group_id=chosen.quota_group_id,
                                    request_id=ctx.request_id,
                                )
                            except StateStoreError as release_exc:
                                logger.warning(
                                    "lease release after convert failure failed: %s",
                                    release_exc,
                                )
                        raise

        _write_route_meta(
            request_kwargs,
            route_mode=route_mode,
            conversion_dir=conversion_dir,
        )

        record_route_selection(
            public_protocol=protocol_ctx.protocol,
            upstream_protocol=chosen.upstream_protocol,
            route_mode=route_mode,
            result="selected",
            model_group=model,
            deployment_id=chosen.deployment_id,
            quota_group_id=chosen.quota_group_id,
        )
        logger.info(
            "selected deployment_id=%s quota_group=%s model=%s request_id=%s "
            "tried=%s route_mode=%s",
            chosen.deployment_id,
            chosen.quota_group_id,
            model,
            ctx.request_id,
            sorted(ctx.tried_quota_groups),
            route_mode,
        )
        return entry


def _metadata_buckets_mutable(
    request_kwargs: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    if not isinstance(request_kwargs, dict):
        return []
    out: list[dict[str, Any]] = []
    for name in ("metadata", "litellm_metadata"):
        bucket = request_kwargs.get(name)
        if not isinstance(bucket, dict):
            bucket = {}
            request_kwargs[name] = bucket
        out.append(bucket)
    return out


def _write_route_meta(
    request_kwargs: dict[str, Any] | None,
    *,
    route_mode: str,
    conversion_dir: str | None,
) -> None:
    from shared_quota_router.conversion.dispatch import (
        CONVERSION_DIR_META_KEY,
        ROUTE_MODE_META_KEY,
    )

    for bucket in _metadata_buckets_mutable(request_kwargs):
        bucket[ROUTE_MODE_META_KEY] = route_mode
        if conversion_dir:
            bucket[CONVERSION_DIR_META_KEY] = conversion_dir
        elif CONVERSION_DIR_META_KEY in bucket:
            bucket.pop(CONVERSION_DIR_META_KEY, None)


def _apply_convert_to_request_kwargs(
    request_kwargs: dict[str, Any] | None,
    *,
    public_protocol: ApiProtocol,
    upstream_protocol: ApiProtocol,
) -> None:
    """Mutate request_kwargs in-place for G0-B convert path (C2-04)."""
    if not isinstance(request_kwargs, dict):
        return
    from shared_quota_router.conversion.dispatch import convert_public_request

    converted = convert_public_request(
        dict(request_kwargs),
        direction=(public_protocol, upstream_protocol),
    )
    # Apply Chat-shaped fields onto kwargs (messages/max_tokens/model)
    for key, value in converted.payload.items():
        request_kwargs[key] = value
    # Drop Anthropic-only top-level keys that must not reach Chat upstream
    request_kwargs.pop("system", None)

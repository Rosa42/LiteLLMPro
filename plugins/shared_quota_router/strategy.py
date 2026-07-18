"""Shared-quota routing strategy (Fill First + affinity + tried set).

Implements LiteLLM CustomRoutingStrategyBase contract (v1.90.5).
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone
from typing import Any, Optional, Union

from shared_quota_router.lease import LeaseManager
from shared_quota_router.models import (
    Deployment,
    ProviderStatus,
    QuotaGroupStatus,
    RequestRoutingContext,
)
from shared_quota_router.registry import DeploymentRegistry, deployment_from_model_entry
from shared_quota_router.state_store import StateStore, StateStoreError

logger = logging.getLogger(__name__)

AFFINITY_TTL_SECONDS = 7200  # 2h
DEFAULT_REQUEST_TIMEOUT = 300

# In-process cache only. NEVER put RequestRoutingContext on request kwargs —
# LiteLLM may JSON-serialize kwargs (logs / exception mapping) and would fail.
_CTX_BY_REQUEST_ID: dict[str, RequestRoutingContext] = {}


class NoAvailableDeploymentError(Exception):
    """Raised when no deployment can be selected (fail-closed or empty candidates)."""


class SharedQuotaSelector:
    """Pure selection logic over registry + Redis state (unit-test friendly)."""

    def __init__(
        self,
        registry: DeploymentRegistry,
        store: StateStore,
        lease_manager: LeaseManager | None = None,
    ) -> None:
        self.registry = registry
        self.store = store
        self.lease_manager = lease_manager

    def filter_candidates(
        self,
        model_group: str,
        context: RequestRoutingContext,
        *,
        now: datetime | None = None,
    ) -> list[Deployment]:
        if context.first_byte_sent:
            logger.warning(
                "first_byte_sent=True; refusing cross-deployment selection request_id=%s",
                context.request_id,
            )
            return []

        now = now or datetime.now(timezone.utc)
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
    ) -> list[Deployment]:
        inflight = inflight or {}
        last_success = last_success or {}

        def sort_key(d: Deployment) -> tuple:
            affinity_rank = 0 if affinity_deployment_id and d.deployment_id == affinity_deployment_id else 1
            # Prefer lower priority number
            prio = d.priority
            infl = inflight.get(d.quota_group_id, 0)
            # Prefer more recent success → invert timestamp
            ls = last_success.get(d.deployment_id)
            ls_key = -(ls.timestamp()) if ls is not None else 0.0
            return (affinity_rank, prio, infl, ls_key, d.deployment_id)

        return sorted(candidates, key=sort_key)

    def select(
        self,
        model_group: str,
        context: RequestRoutingContext,
        *,
        affinity_deployment_id: str | None = None,
        request_timeout_seconds: float = DEFAULT_REQUEST_TIMEOUT,
        acquire_lease: bool = True,
    ) -> Deployment:
        candidates = self.filter_candidates(model_group, context)
        if not candidates:
            raise NoAvailableDeploymentError(
                f"no candidates for model_group={model_group} request_id={context.request_id}"
            )

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
            affinity_deployment_id=affinity_deployment_id,
            inflight=inflight,
            last_success=last_success,
        )

        for dep in ranked:
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
    """Build session hash for affinity (design §6.3)."""
    kwargs = request_kwargs or {}
    metadata = kwargs.get("metadata") or {}
    if not isinstance(metadata, dict):
        metadata = {}

    for key in ("session_id", "conversation_id"):
        val = metadata.get(key) or kwargs.get(key)
        if val:
            return hashlib.sha256(str(val).encode()).hexdigest()

    user = kwargs.get("user") or metadata.get("user")
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
    metadata = kwargs.get("metadata") or {}
    rid = (
        kwargs.get("litellm_call_id")
        or kwargs.get("litellm_trace_id")
        or (metadata.get("request_id") if isinstance(metadata, dict) else None)
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
    ) -> None:
        self.store = store
        self.lease_manager = lease_manager
        self._router = router
        self._registry = registry
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
        return SharedQuotaSelector(registry, self.store, self.lease_manager)

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

            chosen = selector.select(
                model,
                ctx,
                affinity_deployment_id=affinity_id,
            )
            # Persist tried set for next retry (even if kwargs object differs)
            save_request_context(ctx, self.store)
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

        logger.info(
            "selected deployment_id=%s quota_group=%s model=%s request_id=%s tried=%s",
            chosen.deployment_id,
            chosen.quota_group_id,
            model,
            ctx.request_id,
            sorted(ctx.tried_quota_groups),
        )
        return entry

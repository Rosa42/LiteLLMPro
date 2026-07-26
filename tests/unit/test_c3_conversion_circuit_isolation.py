"""C3: conversion-path circuit isolation from direct traffic."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from shared_quota_router.callbacks import SharedQuotaCallback
from shared_quota_router.classifiers.base import FailureKind
from shared_quota_router.models import (
    ApiProtocol,
    ConversionCapability,
    Deployment,
    Feature,
    FidelityClass,
    LogicalModelProtocols,
    RequestRoutingContext,
    RouteMode,
)
from shared_quota_router.protocol_context import RequestProtocolContext
from shared_quota_router.protocol_errors import (
    ProtocolAwareRoutingError,
    ProtocolRoutingReason,
)
from shared_quota_router.registry import DeploymentRegistry
from shared_quota_router.state_store import StateStore
from shared_quota_router.strategy import SharedQuotaSelector


class Mem:
    def __init__(self) -> None:
        self.data: dict[str, str] = {}

    def get(self, name: str):
        return self.data.get(name)

    def set(self, name: str, value, ex=None, nx=False):
        self.data[name] = value if isinstance(value, str) else str(value)
        return True

    def delete(self, *names: str):
        for n in names:
            self.data.pop(n, None)

    def incr(self, name: str):
        v = int(self.data.get(name, "0")) + 1
        self.data[name] = str(v)
        return v

    def decr(self, name: str):
        v = int(self.data.get(name, "0")) - 1
        self.data[name] = str(v)
        return v

    def expire(self, name: str, time: int):
        return True

    def sadd(self, *a, **k):
        return 1

    def smembers(self, name: str):
        return set()

    def eval(self, script, numkeys, *args):
        return [1, "1"]


def test_deterministic_conversion_failure_does_not_mark_quota_exhausted() -> None:
    store = StateStore(Mem())
    cb = SharedQuotaCallback(store=store)
    exc = ProtocolAwareRoutingError(
        "tools unsupported",
        reason=ProtocolRoutingReason.FEATURE_UNSUPPORTED,
        protocol=ApiProtocol.ANTHROPIC_MESSAGES,
        model_group="pilot",
    )
    cb.on_failure(
        {
            "exception": exc,
            "litellm_metadata": {
                "shared_quota_route_mode": "convert",
                "shared_quota_conversion": "anthropic_messages>openai_chat",
            },
            "model": "pilot",
        },
        exc,
    )
    assert store.get_quota_group("q1") is None
    assert (
        store.is_route_in_cooldown(
            "chat-convert", "convert:anthropic_messages>openai_chat"
        )
        is False
    )


def test_convert_path_cooldown_does_not_block_direct_same_deployment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from shared_quota_router.feature_flags import clear_flag_cache

    monkeypatch.setenv("PROTOCOL_CONVERSION_ENABLED", "true")
    monkeypatch.setenv("PROTOCOL_AWARE_GATEWAY_ENABLED", "true")
    from shared_quota_router.feature_flags import set_g0a_messages_mount_ready
    set_g0a_messages_mount_ready(True)
    clear_flag_cache()

    store = StateStore(Mem())
    until = datetime.now(timezone.utc) + timedelta(seconds=60)
    store.put_route_cooldown(
        "shared-dep",
        "convert:anthropic_messages>openai_chat",
        cooldown_until=until,
        ttl_seconds=60,
    )

    cap = ConversionCapability(
        source=ApiProtocol.ANTHROPIC_MESSAGES,
        target=ApiProtocol.OPENAI_CHAT,
        request_features=frozenset({Feature.TEXT}),
        response_features=frozenset({Feature.TEXT}),
        streaming=False,
        fidelity=FidelityClass.EQUIVALENT,
    )
    # Same deployment_id serves Chat direct; also has convert capability
    dep = Deployment(
        deployment_id="shared-dep",
        model_group="pilot",
        upstream_model="openai/pilot",
        provider_id="p",
        quota_group_id="q1",
        priority=10,
        enabled=True,
        upstream_protocol=ApiProtocol.OPENAI_CHAT,
        supported_features=frozenset({Feature.TEXT, Feature.STREAMING, Feature.TOOLS}),
        supports_streaming=True,
        public_protocols=frozenset(
            {ApiProtocol.OPENAI_CHAT, ApiProtocol.ANTHROPIC_MESSAGES}
        ),
        conversions=(cap,),
    )
    # Separate direct Messages deployment for comparison path
    msg = Deployment(
        deployment_id="msg-direct",
        model_group="pilot",
        upstream_model="anthropic/pilot",
        provider_id="p",
        quota_group_id="q2",
        priority=20,
        enabled=True,
        upstream_protocol=ApiProtocol.ANTHROPIC_MESSAGES,
        supported_features=frozenset({Feature.TEXT, Feature.STREAMING}),
        supports_streaming=True,
        public_protocols=frozenset({ApiProtocol.ANTHROPIC_MESSAGES}),
    )
    reg = DeploymentRegistry()
    reg.add(dep)
    reg.add(msg)
    logical = LogicalModelProtocols(
        model_group="pilot",
        public_protocols=frozenset(
            {ApiProtocol.ANTHROPIC_MESSAGES, ApiProtocol.OPENAI_CHAT}
        ),
        allow_conversion=True,
        allowed_conversions=frozenset(
            {(ApiProtocol.ANTHROPIC_MESSAGES, ApiProtocol.OPENAI_CHAT)}
        ),
    )
    sel = SharedQuotaSelector(reg, store, logical_models={"pilot": logical})

    # Chat direct request should still see shared-dep (convert cooldown ignored)
    chat_ctx = RequestProtocolContext(
        protocol=ApiProtocol.OPENAI_CHAT,
        required_features=frozenset({Feature.TEXT}),
        source="test",
    )
    chat_cands = sel.filter_candidates(
        "pilot",
        RequestRoutingContext(request_id="r1"),
        protocol_ctx=chat_ctx,
    )
    assert any(d.deployment_id == "shared-dep" for d in chat_cands)

    # Messages convert candidate for shared-dep should be filtered out
    # (cooldown is applied in filter_candidates, not pure capability resolve)
    msg_ctx = RequestProtocolContext(
        protocol=ApiProtocol.ANTHROPIC_MESSAGES,
        required_features=frozenset({Feature.TEXT}),
        source="test",
    )
    msg_cands = sel.filter_candidates(
        "pilot",
        RequestRoutingContext(request_id="r2"),
        protocol_ctx=msg_ctx,
    )
    msg_ids = [d.deployment_id for d in msg_cands]
    assert "shared-dep" not in msg_ids
    # Direct messages still available
    assert "msg-direct" in msg_ids
    # Capability layer still lists the convert route (state filter is separate)
    msg_routes = sel.filter_route_candidates("pilot", msg_ctx)
    assert any(
        r.deployment.deployment_id == "shared-dep" and r.route_mode is RouteMode.CONVERT
        for r in msg_routes
    )


def test_no_retry_on_conversion_mapping_error() -> None:
    store = StateStore(Mem())
    cb = SharedQuotaCallback(store=store)
    exc = ProtocolAwareRoutingError(
        "dropped fields",
        reason=ProtocolRoutingReason.FEATURE_UNSUPPORTED,
        protocol=ApiProtocol.ANTHROPIC_MESSAGES,
    )
    assert cb.should_allow_retry({"exception": exc}) is False


def test_convert_upstream_failure_writes_convert_cooldown_only() -> None:
    store = StateStore(Mem())
    cb = SharedQuotaCallback(store=store, short_cooldown_seconds=30)

    class Boom(Exception):
        status_code = 503

        def __str__(self) -> str:
            return "upstream down"

    cb.on_failure(
        {
            "exception": Boom(),
            "litellm_call_id": "req-1",
            "model": "pilot",
            # Dual-bucket meta: Messages path uses litellm_metadata for route mode
            "litellm_metadata": {
                "shared_quota_route_mode": "convert",
                "shared_quota_conversion": "anthropic_messages>openai_chat",
            },
            # Deployment identity must be top-level extractable (see _extract_deployment_meta)
            "model_info": {
                "id": "shared-dep",
                "deployment_id": "shared-dep",
                "quota_group_id": "q1",
                "provider_id": "p",
            },
        },
        None,
    )
    assert (
        store.is_route_in_cooldown(
            "shared-dep", "convert:anthropic_messages>openai_chat"
        )
        is True
    )
    # Direct / legacy deployment cooldown must stay clear
    st = store.get_deployment_state("shared-dep")
    assert st is None or st.is_in_cooldown is False
    assert store.is_route_in_cooldown("shared-dep", "direct") is False
    # Provider circuit must not open from convert-path infra failure
    assert store.get_provider_status("p") is None

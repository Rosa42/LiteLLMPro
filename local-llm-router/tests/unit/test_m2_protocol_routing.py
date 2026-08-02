"""M2: protocol-aware pre-lease filtering, affinity, lease invariants, errors."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from shared_quota_router.callbacks import SharedQuotaCallback
from shared_quota_router.lease import LeaseManager
from shared_quota_router.models import (
    ApiProtocol,
    Deployment,
    Feature,
    QuotaGroup,
    QuotaGroupStatus,
    RequestRoutingContext,
)
from shared_quota_router.protocol_context import (
    RequestProtocolContext,
    extract_required_features,
    inject_protocol_into_data,
    resolve_request_protocol_context,
)
from shared_quota_router.protocol_errors import (
    ProtocolAwareRoutingError,
    ProtocolRoutingReason,
)
from shared_quota_router.registry import DeploymentRegistry
from shared_quota_router.state_store import StateStore
from shared_quota_router.strategy import (
    SharedQuotaSelector,
    session_key_from_request,
)


class MemRedis:
    def __init__(self) -> None:
        self.data: dict[str, str] = {}
        self.fail = False
        self.eval_calls = 0

    def get(self, name: str):
        if self.fail:
            raise ConnectionError("down")
        return self.data.get(name)

    def set(self, name: str, value, ex=None, nx=False):
        if self.fail:
            raise ConnectionError("down")
        if nx and name in self.data:
            return False
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

    def sadd(self, name: str, *values):
        return True

    def smembers(self, name: str):
        return set()

    def eval(self, script: str, numkeys: int, *keys_and_args):
        self.eval_calls += 1
        keys = keys_and_args[:numkeys]
        args = keys_and_args[numkeys:]
        if numkeys == 3:
            status_key, inflight_key, lease_key = keys
            ttl, max_inflight, request_id = int(args[0]), int(args[1]), args[2]
            raw = self.data.get(status_key)
            if raw and any(s in raw for s in ('"EXHAUSTED"', '"DISABLED"', '"PROBING"')):
                return [0, "quota_unavailable"]
            inflight = int(self.data.get(inflight_key, "0"))
            if max_inflight > 0 and inflight >= max_inflight:
                return [0, "max_inflight"]
            inflight = self.incr(inflight_key)
            self.set(lease_key, request_id, ex=ttl)
            return [1, str(inflight)]
        if numkeys == 2:
            inflight_key, lease_key = keys
            self.delete(lease_key)
            inflight = int(self.data.get(inflight_key, "0"))
            if inflight > 0:
                inflight = self.decr(inflight_key)
            if inflight < 0:
                self.data[inflight_key] = "0"
                inflight = 0
            return inflight
        raise AssertionError("unexpected eval")


def _chat_dep(dep_id: str, qg: str, *, priority: int = 10, features=None) -> Deployment:
    feats = features or frozenset({Feature.TEXT, Feature.STREAMING, Feature.TOOLS})
    return Deployment(
        deployment_id=dep_id,
        model_group="kimi-k3",
        upstream_model="openai/kimi-k3",
        provider_id="opencode-go",
        quota_group_id=qg,
        priority=priority,
        upstream_protocol=ApiProtocol.OPENAI_CHAT,
        supported_features=feats,
        supports_streaming=Feature.STREAMING in feats,
    )


def _messages_dep(dep_id: str, qg: str, *, priority: int = 10) -> Deployment:
    return Deployment(
        deployment_id=dep_id,
        model_group="kimi-k3",
        upstream_model="anthropic/kimi-k3",
        provider_id="newapi",
        quota_group_id=qg,
        priority=priority,
        upstream_protocol=ApiProtocol.ANTHROPIC_MESSAGES,
        supported_features=frozenset({Feature.TEXT, Feature.STREAMING, Feature.TOOLS}),
        supports_streaming=True,
    )


def _selector(deps: list[Deployment]) -> tuple[SharedQuotaSelector, StateStore, MemRedis]:
    mem = MemRedis()
    store = StateStore(mem)
    lease = LeaseManager(mem)
    return SharedQuotaSelector(DeploymentRegistry(deps), store, lease), store, mem


# ----- M2-01 -----


def test_m2_01_dual_bucket_protocol_chat_metadata() -> None:
    ctx = resolve_request_protocol_context({"metadata": {"protocol": "openai_chat"}})
    assert ctx.protocol is ApiProtocol.OPENAI_CHAT
    assert ctx.source == "metadata"


def test_m2_01_dual_bucket_protocol_messages_litellm_metadata() -> None:
    ctx = resolve_request_protocol_context(
        {"litellm_metadata": {"protocol": "anthropic_messages"}}
    )
    assert ctx.protocol is ApiProtocol.ANTHROPIC_MESSAGES
    assert ctx.source == "litellm_metadata"


def test_m2_01_responses_distinct_from_chat() -> None:
    chat = resolve_request_protocol_context({"metadata": {"protocol": "openai_chat"}})
    resp = resolve_request_protocol_context(
        {"litellm_metadata": {"protocol": "openai_responses"}}
    )
    assert chat.protocol != resp.protocol


def test_m2_01_never_infer_from_messages_vs_input() -> None:
    ctx = resolve_request_protocol_context(
        {"messages": [{"role": "user", "content": "hi"}], "input": "x"}
    )
    assert ctx.protocol is None
    assert ctx.source == "none"


def test_m2_01_call_type_injection_and_features() -> None:
    data: dict = {"model": "kimi-k3", "stream": True, "tools": [{"type": "function"}]}
    inject_protocol_into_data(data, call_type="acompletion")
    assert data["metadata"]["protocol"] == "openai_chat"
    ctx = resolve_request_protocol_context(data)
    assert ctx.protocol is ApiProtocol.OPENAI_CHAT
    assert Feature.STREAMING in ctx.required_features
    assert Feature.TOOLS in ctx.required_features
    assert Feature.TEXT in ctx.required_features


def test_m2_01_pre_call_hook_injects_messages_bucket() -> None:
    cb = SharedQuotaCallback(store=StateStore(MemRedis()))
    data: dict = {"model": "kimi-k3"}

    async def _run() -> dict:
        return await cb.async_pre_call_hook(data=data, call_type="anthropic_messages")

    out = asyncio.run(_run())
    assert out["litellm_metadata"]["protocol"] == "anthropic_messages"


def test_m2_01_serialization_wire_dict() -> None:
    ctx = RequestProtocolContext(
        protocol=ApiProtocol.OPENAI_CHAT,
        required_features=frozenset({Feature.TEXT}),
        source="metadata",
    )
    wire = ctx.as_wire_dict()
    assert wire["protocol"] == "openai_chat"
    assert wire["required_features"] == ["text"]


# ----- M2-02 -----


def test_m2_02_messages_excludes_chat_only_before_lease() -> None:
    sel, _, mem = _selector(
        [
            _chat_dep("chat-a", "qg-a", priority=10),
            _messages_dep("msg-b", "qg-b", priority=20),
        ]
    )
    ctx = RequestRoutingContext(request_id="m2-02-a")
    proto = RequestProtocolContext(
        protocol=ApiProtocol.ANTHROPIC_MESSAGES,
        required_features=frozenset({Feature.TEXT}),
        source="litellm_metadata",
    )
    before = mem.eval_calls
    chosen = sel.select("kimi-k3", ctx, protocol_ctx=proto)
    assert chosen.deployment_id == "msg-b"
    assert mem.eval_calls == before + 1  # only one lease for messages dep
    assert ctx.tried_quota_groups == {"qg-b"}


def test_m2_02_tools_excludes_without_tools() -> None:
    sel, _, mem = _selector(
        [
            _chat_dep(
                "no-tools",
                "qg-a",
                features=frozenset({Feature.TEXT, Feature.STREAMING}),
            ),
            _chat_dep("with-tools", "qg-b", priority=20),
        ]
    )
    ctx = RequestRoutingContext(request_id="m2-02-b")
    proto = RequestProtocolContext(
        protocol=ApiProtocol.OPENAI_CHAT,
        required_features=frozenset({Feature.TEXT, Feature.TOOLS}),
        source="metadata",
    )
    chosen = sel.select("kimi-k3", ctx, protocol_ctx=proto)
    assert chosen.deployment_id == "with-tools"


def test_m2_02_streaming_excludes_without_streaming() -> None:
    sel, _, _ = _selector(
        [
            _chat_dep(
                "no-stream",
                "qg-a",
                features=frozenset({Feature.TEXT, Feature.TOOLS}),
            ),
            _chat_dep("stream-ok", "qg-b", priority=20),
        ]
    )
    ctx = RequestRoutingContext(request_id="m2-02-c")
    proto = RequestProtocolContext(
        protocol=ApiProtocol.OPENAI_CHAT,
        required_features=frozenset({Feature.TEXT, Feature.STREAMING}),
        source="metadata",
    )
    chosen = sel.select("kimi-k3", ctx, protocol_ctx=proto)
    assert chosen.deployment_id == "stream-ok"


def test_m2_02_mismatch_no_lease_no_state_mutation() -> None:
    sel, store, mem = _selector([_chat_dep("chat-a", "qg-a")])
    store.put_quota_group(
        QuotaGroup(
            quota_group_id="qg-a",
            provider_id="opencode-go",
            account_id="a",
            display_name="A",
            status=QuotaGroupStatus.AVAILABLE,
            revision=3,
        )
    )
    ctx = RequestRoutingContext(request_id="m2-02-d")
    proto = RequestProtocolContext(
        protocol=ApiProtocol.ANTHROPIC_MESSAGES,
        required_features=frozenset({Feature.TEXT}),
        source="litellm_metadata",
    )
    before_eval = mem.eval_calls
    with pytest.raises(ProtocolAwareRoutingError) as ei:
        sel.select("kimi-k3", ctx, protocol_ctx=proto)
    assert ei.value.reason is ProtocolRoutingReason.NO_COMPATIBLE_DEPLOYMENT
    assert mem.eval_calls == before_eval
    assert ctx.tried_quota_groups == set()
    assert store.get_quota_group("qg-a").revision == 3


# ----- M2-03 -----


def test_m2_03_incompatible_affinity_ignored() -> None:
    sel, _, mem = _selector(
        [
            _chat_dep("chat-a", "qg-a", priority=10),
            _messages_dep("msg-b", "qg-b", priority=20),
        ]
    )
    ctx = RequestRoutingContext(request_id="m2-03-a")
    proto = RequestProtocolContext(
        protocol=ApiProtocol.OPENAI_CHAT,
        required_features=frozenset({Feature.TEXT}),
        source="metadata",
    )
    before = mem.eval_calls
    chosen = sel.select(
        "kimi-k3",
        ctx,
        affinity_deployment_id="msg-b",  # wrong protocol
        protocol_ctx=proto,
    )
    assert chosen.deployment_id == "chat-a"
    assert ctx.tried_quota_groups == {"qg-a"}
    assert mem.eval_calls == before + 1


def test_m2_03_compatible_affinity_wins() -> None:
    sel, _, _ = _selector(
        [
            _chat_dep("chat-a", "qg-a", priority=10),
            _chat_dep("chat-b", "qg-b", priority=20),
        ]
    )
    ctx = RequestRoutingContext(request_id="m2-03-b")
    proto = RequestProtocolContext(
        protocol=ApiProtocol.OPENAI_CHAT,
        required_features=frozenset({Feature.TEXT}),
        source="metadata",
    )
    chosen = sel.select(
        "kimi-k3",
        ctx,
        affinity_deployment_id="chat-b",
        protocol_ctx=proto,
    )
    assert chosen.deployment_id == "chat-b"


def test_m2_03_session_key_reads_litellm_metadata() -> None:
    h1 = session_key_from_request(
        model="kimi-k3",
        messages=None,
        request_kwargs={"litellm_metadata": {"session_id": "sess-1"}},
    )
    h2 = session_key_from_request(
        model="kimi-k3",
        messages=None,
        request_kwargs={"metadata": {"session_id": "sess-1"}},
    )
    assert h1 == h2


# ----- M2-04 -----


def test_m2_04_first_byte_still_blocks_with_protocol() -> None:
    sel, _, _ = _selector([_chat_dep("chat-a", "qg-a")])
    ctx = RequestRoutingContext(request_id="m2-04-a")
    ctx.mark_first_byte_sent()
    proto = RequestProtocolContext(
        protocol=ApiProtocol.OPENAI_CHAT,
        required_features=frozenset({Feature.TEXT}),
        source="metadata",
    )
    from shared_quota_router.strategy import NoAvailableDeploymentError

    with pytest.raises(NoAvailableDeploymentError):
        sel.select("kimi-k3", ctx, protocol_ctx=proto)
    assert ctx.tried_quota_groups == set()


def test_m2_04_protocol_mismatch_no_retry() -> None:
    cb = SharedQuotaCallback(store=StateStore(MemRedis()))
    err = ProtocolAwareRoutingError(
        "no route",
        reason=ProtocolRoutingReason.NO_COMPATIBLE_DEPLOYMENT,
        protocol=ApiProtocol.ANTHROPIC_MESSAGES,
        model_group="kimi-k3",
    )
    assert cb.should_allow_retry({"exception": err}) is False
    # on_failure must not mutate quota state
    store = cb.store
    store.put_quota_group(
        QuotaGroup(
            quota_group_id="qg-a",
            provider_id="opencode-go",
            account_id="a",
            display_name="A",
            status=QuotaGroupStatus.AVAILABLE,
            revision=1,
        )
    )
    cb.on_failure({"exception": err, "litellm_call_id": "x"}, err)
    assert store.get_quota_group("qg-a").status is QuotaGroupStatus.AVAILABLE
    assert store.get_quota_group("qg-a").revision == 1


# ----- M2-05 -----


def test_m2_05_chat_no_route_openai_shape() -> None:
    err = ProtocolAwareRoutingError(
        "no compatible deployment",
        reason=ProtocolRoutingReason.NO_COMPATIBLE_DEPLOYMENT,
        protocol=ApiProtocol.OPENAI_CHAT,
        model_group="kimi-k3",
    )
    body = err.to_openai_error()
    assert "error" in body
    assert body["error"]["code"] == "no_compatible_deployment"
    assert "Authorization" not in str(body)
    assert "api_key" not in str(body).lower()
    assert "http://" not in str(body)


def test_m2_05_messages_no_route_anthropic_shape() -> None:
    err = ProtocolAwareRoutingError(
        "no compatible deployment",
        reason=ProtocolRoutingReason.NO_COMPATIBLE_DEPLOYMENT,
        protocol=ApiProtocol.ANTHROPIC_MESSAGES,
        model_group="kimi-k3",
    )
    body = err.to_anthropic_error()
    assert body["type"] == "error"
    assert body["error"]["shared_quota"]["protocol"] == "anthropic_messages"


def test_m2_05_responses_disabled_reason() -> None:
    sel, _, mem = _selector([_chat_dep("chat-a", "qg-a")])
    ctx = RequestRoutingContext(request_id="m2-05-r")
    proto = RequestProtocolContext(
        protocol=ApiProtocol.OPENAI_RESPONSES,
        required_features=frozenset({Feature.TEXT}),
        source="litellm_metadata",
    )
    with pytest.raises(ProtocolAwareRoutingError) as ei:
        sel.select("kimi-k3", ctx, protocol_ctx=proto)
    assert ei.value.reason is ProtocolRoutingReason.UNSUPPORTED_PUBLIC_PROTOCOL
    assert mem.eval_calls == 0
    public = ei.value.to_public_error()
    assert public["error"]["code"] == "protocol_not_enabled"


def test_m2_05_public_error_switches_by_protocol() -> None:
    chat_err = ProtocolAwareRoutingError(
        "x",
        reason=ProtocolRoutingReason.NO_COMPATIBLE_DEPLOYMENT,
        protocol=ApiProtocol.OPENAI_CHAT,
    )
    msg_err = ProtocolAwareRoutingError(
        "x",
        reason=ProtocolRoutingReason.NO_COMPATIBLE_DEPLOYMENT,
        protocol=ApiProtocol.ANTHROPIC_MESSAGES,
    )
    assert "error" in chat_err.to_public_error()
    assert msg_err.to_public_error()["type"] == "error"


def test_extract_required_features_defaults_text() -> None:
    feats = extract_required_features({})
    assert feats == frozenset({Feature.TEXT})


def test_legacy_untagged_model_group_skips_protocol_gate_on_router_path() -> None:
    """Deployments without upstream_protocol → legacy path even if kwargs have protocol."""
    from shared_quota_router.strategy import SharedQuotaRoutingStrategy

    untagged = Deployment(
        deployment_id="legacy-a",
        model_group="kimi-k3",
        upstream_model="openai/kimi-k3",
        provider_id="opencode-go",
        quota_group_id="qg-a",
        priority=10,
        # no upstream_protocol
    )
    mem = MemRedis()
    store = StateStore(mem)
    lease = LeaseManager(mem)
    strategy = SharedQuotaRoutingStrategy(
        store=store,
        lease_manager=lease,
        registry=DeploymentRegistry([untagged]),
    )

    class _R:
        model_list = [
            {
                "model_name": "kimi-k3",
                "litellm_params": {"model": "openai/kimi-k3"},
                "model_info": {
                    "deployment_id": "legacy-a",
                    "quota_group_id": "qg-a",
                    "provider_id": "opencode-go",
                },
            }
        ]

    strategy.bind_router(_R())
    entry = strategy.get_available_deployment(
        "kimi-k3",
        messages=[{"role": "user", "content": "hi"}],
        request_kwargs={
            "litellm_call_id": "legacy-1",
            "metadata": {"protocol": "openai_chat"},
        },
    )
    assert entry["model_info"]["deployment_id"] == "legacy-a"

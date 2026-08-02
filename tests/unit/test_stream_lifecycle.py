"""B3/B4: stream lifecycle first-byte + mid-stream wire."""

from __future__ import annotations

import asyncio

import pytest

from shared_quota_router.callbacks import SharedQuotaCallback
from shared_quota_router.lease import LeaseManager
from shared_quota_router.models import Deployment
from shared_quota_router.registry import DeploymentRegistry
from shared_quota_router.state_store import StateStore
from shared_quota_router.strategy import (
    NoAvailableDeploymentError,
    SharedQuotaRoutingStrategy,
    context_from_request_kwargs,
)
from shared_quota_router.stream_lifecycle import (
    ManagedStream,
    StreamLifecycleConfig,
    StreamLifecycleContext,
    bind_upstream_aclose,
    wrap_async_stream,
)
from shared_quota_router.stream_wire import StreamWireProtocol
from tests.unit.test_lease import FakeRedisLua


class MemRedis(FakeRedisLua):
    pass


async def _gen(*chunks):
    for c in chunks:
        yield c


@pytest.mark.asyncio
async def test_managed_stream_marks_first_byte_on_public_chunk() -> None:
    marked = {"ok": False}

    ctx = StreamLifecycleContext(
        quota_group_id="qg",
        request_id="r1",
        on_first_byte=lambda: marked.update(ok=True),
        config=StreamLifecycleConfig(renew_floor_seconds=3600),
    )
    wrapped = wrap_async_stream(_gen({"choices": [{"delta": {"content": "hi"}}]}), ctx)
    chunks = [c async for c in wrapped]
    assert len(chunks) == 1
    assert marked["ok"] is True


@pytest.mark.asyncio
async def test_managed_stream_anthropic_terminal_error_after_first_byte() -> None:
    ctx = StreamLifecycleContext(
        quota_group_id="qg",
        request_id="r1",
        wire_protocol=StreamWireProtocol.ANTHROPIC_MESSAGES,
        config=StreamLifecycleConfig(renew_floor_seconds=3600),
    )

    async def fail_after_first():
        yield {"type": "message_start"}
        raise RuntimeError("upstream broke")

    out: list = []
    wrapped = ManagedStream(fail_after_first(), ctx)
    async for item in wrapped:
        out.append(item)
    text = "".join(str(x) for x in out if isinstance(x, str))
    assert any("event: error" in s for s in out if isinstance(s, str)) or "event: error" in text
    assert "message_stop" not in text


@pytest.mark.asyncio
async def test_callback_wrap_blocks_strategy_after_first_byte() -> None:
    mem = MemRedis()
    store = StateStore(mem)
    lm = LeaseManager(mem)
    cb = SharedQuotaCallback(store=store, lease_manager=lm)
    call_id = "fb-gate-1"
    assert lm.acquire(quota_group_id="opencode-a", request_id=call_id)

    data = {
        "stream": True,
        "litellm_call_id": call_id,
        "metadata": {"protocol": "openai_chat"},
        "model_info": {
            "deployment_id": "dep-1",
            "quota_group_id": "opencode-a",
            "provider_id": "p",
        },
    }

    async def stream():
        yield {"choices": [{"delta": {"content": "x"}}]}

    wrapped = await cb.async_post_call_success_hook(data, response=stream())
    async for _ in wrapped:
        break

    ctx = context_from_request_kwargs({"litellm_call_id": call_id}, store=store)
    assert ctx.first_byte_sent is True

    reg = DeploymentRegistry(
        [
            Deployment(
                deployment_id="dep-1",
                model_group="m",
                upstream_model="openai/m",
                provider_id="p",
                quota_group_id="opencode-a",
                priority=10,
            ),
            Deployment(
                deployment_id="dep-2",
                model_group="m",
                upstream_model="openai/m",
                provider_id="p",
                quota_group_id="other",
                priority=20,
            ),
        ]
    )
    model_list = [
        {
            "model_name": "m",
            "litellm_params": {"model": "openai/m", "api_key": "k"},
            "model_info": {
                "deployment_id": "dep-1",
                "quota_group_id": "opencode-a",
                "provider_id": "p",
                "priority": 10,
            },
        },
        {
            "model_name": "m",
            "litellm_params": {"model": "openai/m", "api_key": "k2"},
            "model_info": {
                "deployment_id": "dep-2",
                "quota_group_id": "other",
                "provider_id": "p",
                "priority": 20,
            },
        },
    ]

    class FakeRouter:
        def __init__(self):
            self.model_list = model_list

    strategy = SharedQuotaRoutingStrategy(
        store=store, lease_manager=lm, router=FakeRouter(), registry=reg
    )
    with pytest.raises(NoAvailableDeploymentError, match="first byte"):
        strategy.get_available_deployment(
            model="m",
            messages=[{"role": "user", "content": "hi"}],
            request_kwargs={"litellm_call_id": call_id},
        )

    async for _ in wrapped:
        pass


@pytest.mark.asyncio
async def test_upstream_aclose_patch_releases_lease() -> None:
    lm = LeaseManager(FakeRedisLua())
    assert lm.acquire(quota_group_id="qg", request_id="r-up")

    class Upstream:
        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

        async def aclose(self) -> None:
            self.closed = True

    upstream = Upstream()
    ctx = StreamLifecycleContext(
        quota_group_id="qg",
        request_id="r-up",
        lease_manager=lm,
        config=StreamLifecycleConfig(renew_floor_seconds=3600),
    )
    managed = ManagedStream(upstream, ctx)
    bind_upstream_aclose(managed, upstream)
    await upstream.aclose()
    assert lm.get_inflight("qg") == 0
    assert upstream.closed is True


@pytest.mark.asyncio
async def test_managed_stream_finalize_is_idempotent() -> None:
    released = {"count": 0}
    lm = LeaseManager(MemRedis())
    assert lm.acquire(quota_group_id="qg", request_id="r-idem")

    ctx = StreamLifecycleContext(
        quota_group_id="qg",
        request_id="r-idem",
        lease_manager=lm,
        config=StreamLifecycleConfig(renew_floor_seconds=3600),
        on_stream_complete=lambda: released.update(count=released["count"] + 1),
    )
    wrapped = ManagedStream(_gen({"choices": [{"delta": {"content": "x"}}]}), ctx)
    async for _ in wrapped:
        pass
    await wrapped.aclose()
    assert lm.get_inflight("qg") == 0
    assert released["count"] == 1

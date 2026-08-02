"""Callbacks: streaming lifecycle defer + dual-hook idempotent release."""

from __future__ import annotations

import asyncio

import pytest

from shared_quota_router.callbacks import SharedQuotaCallback
from shared_quota_router.lease import LeaseManager
from shared_quota_router.state_store import StateStore
from tests.unit.test_lease import FakeRedisLua


async def _gen():
    yield "chunk"


@pytest.mark.asyncio
async def test_streaming_post_call_defers_release_until_stream_ends() -> None:
    redis = FakeRedisLua()
    lm = LeaseManager(redis)
    store = StateStore(redis)
    cb = SharedQuotaCallback(store=store, lease_manager=lm)

    data = {
        "stream": True,
        "litellm_call_id": "stream-req",
        "model_info": {
            "deployment_id": "dep-1",
            "quota_group_id": "qg-1",
            "provider_id": "p",
        },
    }
    assert lm.acquire(quota_group_id="qg-1", request_id="stream-req")

    response = await cb.async_post_call_success_hook(data, response=_gen())
    assert lm.get_inflight("qg-1") == 1

    async for _ in response:
        pass
    assert lm.get_inflight("qg-1") == 0


@pytest.mark.asyncio
async def test_non_streaming_post_call_releases_immediately() -> None:
    redis = FakeRedisLua()
    lm = LeaseManager(redis)
    store = StateStore(redis)
    cb = SharedQuotaCallback(store=store, lease_manager=lm)

    data = {
        "litellm_call_id": "non-stream",
        "model_info": {
            "deployment_id": "dep-1",
            "quota_group_id": "qg-1",
            "provider_id": "p",
        },
    }
    assert lm.acquire(quota_group_id="qg-1", request_id="non-stream")
    await cb.async_post_call_success_hook(data, response={"ok": True})
    assert lm.get_inflight("qg-1") == 0


@pytest.mark.asyncio
async def test_streaming_iterator_hook_releases_on_early_aclose() -> None:
    redis = FakeRedisLua()
    lm = LeaseManager(redis)
    store = StateStore(redis)
    cb = SharedQuotaCallback(store=store, lease_manager=lm)

    data = {
        "stream": True,
        "litellm_call_id": "disc-req",
        "metadata": {"protocol": "openai_chat"},
        "model_info": {
            "deployment_id": "dep-1",
            "quota_group_id": "qg-1",
            "provider_id": "p",
        },
    }
    assert lm.acquire(quota_group_id="qg-1", request_id="disc-req")

    async def source():
        yield {"choices": [{"delta": {"content": "a"}}]}
        await asyncio.sleep(3600)

    agen = cb.async_post_call_streaming_iterator_hook(
        response=source(), request_data=data
    )
    async for _ in agen:
        break
    await agen.aclose()
    assert lm.get_inflight("qg-1") == 0


@pytest.mark.asyncio
async def test_release_stream_lease_if_active_idempotent() -> None:
    redis = FakeRedisLua()
    lm = LeaseManager(redis)
    store = StateStore(redis)
    cb = SharedQuotaCallback(store=store, lease_manager=lm)
    data = {
        "litellm_call_id": "abort-1",
        "model_info": {
            "deployment_id": "dep-1",
            "quota_group_id": "qg-1",
            "provider_id": "p",
        },
    }
    assert lm.acquire(quota_group_id="qg-1", request_id="abort-1")
    cb.release_stream_lease_if_active(data)
    assert lm.get_inflight("qg-1") == 0
    cb.release_stream_lease_if_active(data)
    assert lm.get_inflight("qg-1") == 0


@pytest.mark.asyncio
async def test_dual_success_hooks_idempotent_release() -> None:
    redis = FakeRedisLua()
    lm = LeaseManager(redis)
    store = StateStore(redis)
    cb = SharedQuotaCallback(store=store, lease_manager=lm)

    data = {
        "litellm_call_id": "dual",
        "model_info": {
            "deployment_id": "dep-1",
            "quota_group_id": "qg-1",
            "provider_id": "p",
        },
    }
    assert lm.acquire(quota_group_id="qg-1", request_id="dual")
    await cb.async_post_call_success_hook(data, response={"ok": True})
    await cb.async_log_success_event(data, None, None, None)
    assert lm.get_inflight("qg-1") == 0

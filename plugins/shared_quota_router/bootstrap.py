"""Unique official registration entry for shared-quota routing.

Usage:
  - Tests/SDK: register(router)
  - Proxy: LITELLM_WORKER_STARTUP_HOOKS=shared_quota_router.bootstrap:register_proxy_startup
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from shared_quota_router.lease import LeaseManager
from shared_quota_router.state_store import StateStore
from shared_quota_router.strategy import SharedQuotaRoutingStrategy

logger = logging.getLogger(__name__)

_REGISTERED = False
_STRATEGY: SharedQuotaRoutingStrategy | None = None


def _build_redis_client() -> Any:
    """Create a redis client from env. Returns None if redis package/env missing."""
    try:
        import redis as redis_lib
    except ImportError:
        logger.warning("redis package not installed; routing state will fail-closed if used")
        return None

    url = os.environ.get("REDIS_URL")
    if url:
        return redis_lib.Redis.from_url(url, decode_responses=True)

    host = os.environ.get("REDIS_HOST", "127.0.0.1")
    port = int(os.environ.get("REDIS_PORT", "6379"))
    password = os.environ.get("REDIS_PASSWORD") or None
    db = int(os.environ.get("REDIS_DB", "0"))
    return redis_lib.Redis(host=host, port=port, password=password, db=db, decode_responses=True)


def build_default_strategy(*, router: Any = None) -> SharedQuotaRoutingStrategy:
    redis_client = _build_redis_client()
    if redis_client is None:
        # In-memory stub that always fails get → treated carefully by strategy
        class _FailRedis:
            def get(self, *a, **k):
                raise ConnectionError("redis not configured")

            def set(self, *a, **k):
                raise ConnectionError("redis not configured")

            def delete(self, *a, **k):
                raise ConnectionError("redis not configured")

            def eval(self, *a, **k):
                raise ConnectionError("redis not configured")

            def incr(self, *a, **k):
                raise ConnectionError("redis not configured")

            def decr(self, *a, **k):
                raise ConnectionError("redis not configured")

            def expire(self, *a, **k):
                raise ConnectionError("redis not configured")

        redis_client = _FailRedis()

    store = StateStore(redis_client)
    lease = LeaseManager(redis_client)
    strategy = SharedQuotaRoutingStrategy(
        store=store,
        lease_manager=lease,
        router=router,
    )
    return strategy


def register(router: Any, strategy: SharedQuotaRoutingStrategy | None = None) -> SharedQuotaRoutingStrategy:
    """唯一官方注册入口：挂载自定义策略到 litellm.Router 实例。"""
    global _REGISTERED, _STRATEGY

    strat = strategy or build_default_strategy(router=router)
    strat.bind_router(router)

    if not hasattr(router, "set_custom_routing_strategy"):
        raise TypeError("router does not support set_custom_routing_strategy")

    router.set_custom_routing_strategy(strat)
    _STRATEGY = strat
    _REGISTERED = True
    logger.info("shared_quota_router registered on router id=%s", id(router))
    return strat


def is_registered() -> bool:
    return _REGISTERED


def get_strategy() -> SharedQuotaRoutingStrategy | None:
    return _STRATEGY


async def _wait_and_register(timeout_seconds: float = 60.0, interval: float = 0.1) -> None:
    """Wait until proxy_server.llm_router is ready, then register."""
    global _REGISTERED
    import time

    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            from litellm.proxy import proxy_server

            router = getattr(proxy_server, "llm_router", None)
            if router is not None:
                register(router)
                logger.info("shared_quota_router registered via proxy startup hook")
                return
        except Exception as exc:  # noqa: BLE001
            logger.debug("waiting for llm_router: %s", exc)
        await asyncio.sleep(interval)

    raise RuntimeError(
        "shared_quota_router: llm_router not ready within "
        f"{timeout_seconds}s; custom strategy not registered"
    )


def register_proxy_startup() -> None:
    """LITELLM_WORKER_STARTUP_HOOKS target (sync).

    Schedules deferred registration because hooks run before load_config.
    """
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            loop.create_task(_wait_and_register())
        else:
            loop.run_until_complete(_wait_and_register())
    except RuntimeError:
        # No loop yet — create task when possible
        asyncio.ensure_future(_wait_and_register())  # type: ignore[attr-defined]
        logger.info("scheduled shared_quota_router deferred registration")

"""Unique official registration entry for shared-quota routing.

Usage:
  - Tests/SDK: register(router)
  - Proxy: LITELLM_WORKER_STARTUP_HOOKS=shared_quota_router.bootstrap:register_proxy_startup
  - Callback: shared_quota_router.callback_instance (litellm_settings.callbacks)
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from shared_quota_router.callbacks import SharedQuotaCallback
from shared_quota_router.lease import LeaseManager
from shared_quota_router.state_store import StateStore
from shared_quota_router.strategy import SharedQuotaRoutingStrategy

logger = logging.getLogger(__name__)

_REGISTERED = False
_STRATEGY: SharedQuotaRoutingStrategy | None = None
_STORE: StateStore | None = None
_LEASE: LeaseManager | None = None
_CALLBACK: SharedQuotaCallback | None = None
_REDIS: Any = None


def _build_redis_client() -> Any:
    try:
        import redis as redis_lib
    except ImportError:
        logger.warning("redis package not installed")
        return None

    # protocol=2 (RESP2): works with Redis 5+ Windows builds and Redis 7 Docker.
    # redis-py 5+ defaults to RESP3/HELLO which older redis-server rejects.
    common: dict[str, Any] = {"decode_responses": True, "protocol": 2}

    url = os.environ.get("REDIS_URL")
    if url:
        return redis_lib.Redis.from_url(url, **common)

    host = os.environ.get("REDIS_HOST", "127.0.0.1")
    port = int(os.environ.get("REDIS_PORT", "6379"))
    password = os.environ.get("REDIS_PASSWORD") or None
    db = int(os.environ.get("REDIS_DB", "0"))
    return redis_lib.Redis(
        host=host, port=port, password=password, db=db, **common
    )


def _fail_redis() -> Any:
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

    return _FailRedis()


def get_redis() -> Any:
    global _REDIS
    if _REDIS is None:
        _REDIS = _build_redis_client() or _fail_redis()
    return _REDIS


def get_store() -> StateStore:
    global _STORE
    if _STORE is None:
        _STORE = StateStore(get_redis())
    return _STORE


def get_lease_manager() -> LeaseManager:
    global _LEASE
    if _LEASE is None:
        _LEASE = LeaseManager(get_redis())
    return _LEASE


def get_callback() -> SharedQuotaCallback:
    global _CALLBACK
    if _CALLBACK is None:
        _CALLBACK = SharedQuotaCallback(store=get_store(), lease_manager=get_lease_manager())
    return _CALLBACK


def build_default_strategy(*, router: Any = None) -> SharedQuotaRoutingStrategy:
    return SharedQuotaRoutingStrategy(
        store=get_store(),
        lease_manager=get_lease_manager(),
        router=router,
    )


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


def _try_mount_discovery() -> None:
    """M1-05: project-owned capability discovery routes on proxy app."""
    try:
        from shared_quota_router.discovery_routes import mount_discovery_routes

        if mount_discovery_routes():
            logger.info("shared_quota_router discovery endpoints ready")
    except Exception as exc:  # noqa: BLE001
        logger.warning("discovery route mount skipped: %s", exc)


async def _wait_and_register(timeout_seconds: float = 60.0, interval: float = 0.1) -> None:
    import time

    # Mount discovery as early as the FastAPI app exists (router may still be None).
    _try_mount_discovery()

    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            from litellm.proxy import proxy_server

            router = getattr(proxy_server, "llm_router", None)
            if router is not None:
                register(router)
                # Re-attempt mount in case app was not ready on first try
                _try_mount_discovery()
                logger.info("shared_quota_router registered via proxy startup hook")
                return
        except Exception as exc:  # noqa: BLE001
            logger.debug("waiting for llm_router: %s", exc)
        await asyncio.sleep(interval)

    raise RuntimeError(
        f"shared_quota_router: llm_router not ready within {timeout_seconds}s"
    )


def register_proxy_startup() -> None:
    """LITELLM_WORKER_STARTUP_HOOKS target (sync)."""
    # Best-effort immediate mount (app may already exist at hook time)
    _try_mount_discovery()
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            loop.create_task(_wait_and_register())
        else:
            loop.run_until_complete(_wait_and_register())
    except RuntimeError:
        try:
            asyncio.get_running_loop().create_task(_wait_and_register())
        except RuntimeError:
            logger.warning("no event loop; proxy registration deferred failed")


# Module-level instance for litellm_settings.callbacks
callback_instance = get_callback()

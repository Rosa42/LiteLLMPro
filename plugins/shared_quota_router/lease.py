"""Concurrent request leases per quota group.

lease TTL = request_timeout_seconds + 30 (design §6.4).
"""

from __future__ import annotations

from typing import Any, Protocol

from shared_quota_router.state_store import KEY_LEASE, KEY_QUOTA, StateStoreError


class RedisPipelineLike(Protocol):
    def get(self, name: str) -> Any: ...
    def set(self, name: str, value: Any, ex: int | None = None, nx: bool = False) -> Any: ...
    def delete(self, *names: str) -> Any: ...
    def incr(self, name: str) -> Any: ...
    def decr(self, name: str) -> Any: ...
    def expire(self, name: str, time: int) -> Any: ...
    def eval(self, script: str, numkeys: int, *keys_and_args: Any) -> Any: ...


INFLIGHT_KEY = "sq:inflight:{quota_group_id}"

# Minimal Lua: check optional status key not EXHAUSTED/DISABLED, then incr inflight + set lease
_ACQUIRE_LUA = """
local status_key = KEYS[1]
local inflight_key = KEYS[2]
local lease_key = KEYS[3]
local ttl = tonumber(ARGV[1])
local max_inflight = tonumber(ARGV[2])
local request_id = ARGV[3]

local raw = redis.call('GET', status_key)
if raw then
  if string.find(raw, '"EXHAUSTED"') or string.find(raw, '"DISABLED"') or string.find(raw, '"PROBING"') then
    return {0, 'quota_unavailable'}
  end
end

local inflight = tonumber(redis.call('GET', inflight_key) or '0')
if max_inflight > 0 and inflight >= max_inflight then
  return {0, 'max_inflight'}
end

inflight = redis.call('INCR', inflight_key)
redis.call('EXPIRE', inflight_key, ttl)
redis.call('SET', lease_key, request_id, 'EX', ttl)
return {1, tostring(inflight)}
"""

_RELEASE_LUA = """
local inflight_key = KEYS[1]
local lease_key = KEYS[2]
local request_id = ARGV[1]

local current = redis.call('GET', lease_key)
if not current or current ~= request_id then
  return tonumber(redis.call('GET', inflight_key) or '0')
end

redis.call('DEL', lease_key)
local inflight = tonumber(redis.call('GET', inflight_key) or '0')
if inflight > 0 then
  inflight = redis.call('DECR', inflight_key)
end
if inflight < 0 then
  redis.call('SET', inflight_key, 0)
  inflight = 0
end
return inflight
"""

_RENEW_LUA = """
local inflight_key = KEYS[1]
local lease_key = KEYS[2]
local request_id = ARGV[1]
local ttl = tonumber(ARGV[2])

local current = redis.call('GET', lease_key)
if not current or current ~= request_id then
  return 0
end

redis.call('EXPIRE', lease_key, ttl)
redis.call('EXPIRE', inflight_key, ttl)
return 1
"""


def lease_ttl_seconds(request_timeout_seconds: float | int) -> int:
    return int(request_timeout_seconds) + 30


class LeaseManager:
    def __init__(self, redis: RedisPipelineLike, *, key_prefix: str = "") -> None:
        self._r = redis
        self._prefix = key_prefix

    def _k(self, template: str, **kwargs: str) -> str:
        return self._prefix + template.format(**kwargs)

    def acquire(
        self,
        *,
        quota_group_id: str,
        request_id: str,
        request_timeout_seconds: float = 300,
        max_inflight: int = 0,
    ) -> bool:
        ttl = lease_ttl_seconds(request_timeout_seconds)
        status_key = self._k(KEY_QUOTA, quota_group_id=quota_group_id)
        inflight_key = self._k(INFLIGHT_KEY, quota_group_id=quota_group_id)
        lease_key = self._k(KEY_LEASE, quota_group_id=quota_group_id, request_id=request_id)
        try:
            result = self._r.eval(
                _ACQUIRE_LUA,
                3,
                status_key,
                inflight_key,
                lease_key,
                ttl,
                max_inflight,
                request_id,
            )
        except Exception as exc:  # noqa: BLE001
            raise StateStoreError(f"lease acquire failed: {exc}") from exc

        if isinstance(result, (list, tuple)) and len(result) >= 1:
            ok = int(result[0]) == 1
            return ok
        return False

    def release(self, *, quota_group_id: str, request_id: str) -> int:
        inflight_key = self._k(INFLIGHT_KEY, quota_group_id=quota_group_id)
        lease_key = self._k(KEY_LEASE, quota_group_id=quota_group_id, request_id=request_id)
        try:
            result = self._r.eval(
                _RELEASE_LUA, 2, inflight_key, lease_key, request_id
            )
        except Exception as exc:  # noqa: BLE001
            raise StateStoreError(f"lease release failed: {exc}") from exc
        return int(result or 0)

    def renew(
        self,
        *,
        quota_group_id: str,
        request_id: str,
        ttl_seconds: int,
    ) -> bool:
        """R1: extend lease + inflight TTL while stream is active."""
        inflight_key = self._k(INFLIGHT_KEY, quota_group_id=quota_group_id)
        lease_key = self._k(KEY_LEASE, quota_group_id=quota_group_id, request_id=request_id)
        try:
            result = self._r.eval(
                _RENEW_LUA, 2, inflight_key, lease_key, request_id, ttl_seconds
            )
        except Exception as exc:  # noqa: BLE001
            raise StateStoreError(f"lease renew failed: {exc}") from exc
        return int(result or 0) == 1

    def get_inflight(self, quota_group_id: str) -> int:
        key = self._k(INFLIGHT_KEY, quota_group_id=quota_group_id)
        try:
            raw = self._r.get(key)
        except Exception as exc:  # noqa: BLE001
            raise StateStoreError(f"inflight get failed: {exc}") from exc
        if raw is None:
            return 0
        if isinstance(raw, bytes):
            raw = raw.decode()
        return int(raw)

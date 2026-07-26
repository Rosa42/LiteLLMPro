"""Redis-backed quota / provider / deployment / request context / affinity state.

Key prefix: sq: (see design §12)
fail-closed: Redis errors must not surface as "all available".
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Protocol

from shared_quota_router.models import (
    DeploymentRuntimeState,
    ProviderStatus,
    QuotaGroup,
    QuotaGroupStatus,
    RequestRoutingContext,
)

logger = logging.getLogger(__name__)

KEY_PROVIDER = "sq:provider:{provider_id}"
KEY_QUOTA = "sq:quota:{quota_group_id}"
KEY_WINDOW = "sq:window:{quota_group_id}:{window_type}"
KEY_DEPLOYMENT = "sq:deployment:{deployment_id}"
KEY_ROUTE_COOLDOWN = "sq:cooldown:dep:{deployment_id}:{route_key}"
KEY_AFFINITY = "sq:affinity:{session_hash}"
KEY_AFFINITY_META = "sq:affinity-meta:{session_hash}"
KEY_AFFINITY_IDX = "sq:affinity-idx:{quota_group_id}"
KEY_LEASE = "sq:lease:{quota_group_id}:{request_id}"
KEY_PROBE_LOCK = "sq:probe-lock:{quota_group_id}"
KEY_REQCTX = "sq:reqctx:{request_id}"

DEFAULT_REQCTX_TTL = 360  # seconds
DEFAULT_AFFINITY_TTL = 7200


class RedisLike(Protocol):
    def get(self, name: str) -> Any: ...
    def set(self, name: str, value: Any, ex: int | None = None, nx: bool = False) -> Any: ...
    def delete(self, *names: str) -> Any: ...
    def sadd(self, name: str, *values: Any) -> Any: ...
    def smembers(self, name: str) -> Any: ...
    def expire(self, name: str, time: int) -> Any: ...


class StateStoreError(Exception):
    """Raised on Redis failure — callers must fail-closed."""


class StateStore:
    def __init__(self, redis: RedisLike, *, key_prefix: str = "") -> None:
        self._r = redis
        self._prefix = key_prefix

    def _k(self, template: str, **kwargs: str) -> str:
        return self._prefix + template.format(**kwargs)

    # ----- quota / provider / deployment -----

    def get_quota_group(self, quota_group_id: str) -> QuotaGroup | None:
        try:
            raw = self._r.get(self._k(KEY_QUOTA, quota_group_id=quota_group_id))
        except Exception as exc:  # noqa: BLE001
            raise StateStoreError(f"redis get quota failed: {exc}") from exc
        if raw is None:
            return None
        return _quota_from_dict(_loads(raw))

    def put_quota_group(self, group: QuotaGroup, *, ttl_seconds: int | None = None) -> None:
        payload = _quota_to_dict(group)
        try:
            self._r.set(
                self._k(KEY_QUOTA, quota_group_id=group.quota_group_id),
                json.dumps(payload),
                ex=ttl_seconds,
            )
        except Exception as exc:  # noqa: BLE001
            raise StateStoreError(f"redis set quota failed: {exc}") from exc

    def mark_exhausted(
        self,
        quota_group_id: str,
        *,
        reason: str,
        reset_at: datetime | None = None,
        base: QuotaGroup | None = None,
        expected_revision: int | None = None,
        clear_affinity: bool = True,
    ) -> QuotaGroup:
        """Mark quota group EXHAUSTED; optionally clear session affinity for the group."""
        current = base or self.get_quota_group(quota_group_id)
        if current is None:
            current = QuotaGroup(
                quota_group_id=quota_group_id,
                provider_id="unknown",
                account_id=quota_group_id,
                display_name=quota_group_id,
            )
        if expected_revision is not None and current.revision != expected_revision:
            raise StateStoreError(
                f"revision conflict for {quota_group_id}: "
                f"expected {expected_revision} got {current.revision}"
            )

        now = datetime.now(timezone.utc)
        current.status = QuotaGroupStatus.EXHAUSTED
        current.failure_reason = reason
        current.last_failure_at = now
        current.reset_at = reset_at
        current.next_probe_at = reset_at or current.next_probe_at
        current.revision += 1
        current.consecutive_failures += 1

        ttl: int | None = None
        if reset_at is not None:
            delta = int((reset_at - now).total_seconds())
            if delta > 0:
                ttl = delta + 300

        self.put_quota_group(current, ttl_seconds=ttl)
        if clear_affinity:
            self.clear_affinity_for_quota_group(quota_group_id)
        return current

    def set_quota_status(
        self,
        group: QuotaGroup,
        status: QuotaGroupStatus,
        *,
        reason: str | None = None,
        clear_affinity: bool = False,
    ) -> QuotaGroup:
        group.status = status
        if reason is not None:
            group.failure_reason = reason
        group.revision += 1
        self.put_quota_group(group)
        if clear_affinity and status in {
            QuotaGroupStatus.EXHAUSTED,
            QuotaGroupStatus.DISABLED,
        }:
            self.clear_affinity_for_quota_group(group.quota_group_id)
        return group

    def get_provider_status(self, provider_id: str) -> ProviderStatus | None:
        try:
            raw = self._r.get(self._k(KEY_PROVIDER, provider_id=provider_id))
        except Exception as exc:  # noqa: BLE001
            raise StateStoreError(f"redis get provider failed: {exc}") from exc
        if raw is None:
            return None
        data = _loads(raw)
        return ProviderStatus(data.get("status", ProviderStatus.AVAILABLE.value))

    def put_provider_status(
        self,
        provider_id: str,
        status: ProviderStatus,
        *,
        ttl_seconds: int | None = None,
    ) -> None:
        try:
            self._r.set(
                self._k(KEY_PROVIDER, provider_id=provider_id),
                json.dumps({"provider_id": provider_id, "status": status.value}),
                ex=ttl_seconds,
            )
        except Exception as exc:  # noqa: BLE001
            raise StateStoreError(f"redis set provider failed: {exc}") from exc

    def get_deployment_state(self, deployment_id: str) -> DeploymentRuntimeState | None:
        try:
            raw = self._r.get(self._k(KEY_DEPLOYMENT, deployment_id=deployment_id))
        except Exception as exc:  # noqa: BLE001
            raise StateStoreError(f"redis get deployment failed: {exc}") from exc
        if raw is None:
            return None
        data = _loads(raw)
        cooldown_until = _parse_dt(data.get("cooldown_until"))
        now = datetime.now(timezone.utc)
        in_cd = bool(data.get("is_in_cooldown"))
        if cooldown_until and cooldown_until <= now:
            in_cd = False
        return DeploymentRuntimeState(
            deployment_id=deployment_id,
            is_in_cooldown=in_cd,
            cooldown_until=cooldown_until,
            last_success_at=_parse_dt(data.get("last_success_at")),
            last_failure_at=_parse_dt(data.get("last_failure_at")),
        )

    def put_deployment_state(
        self,
        state: DeploymentRuntimeState,
        *,
        ttl_seconds: int | None = None,
    ) -> None:
        payload = {
            "deployment_id": state.deployment_id,
            "is_in_cooldown": state.is_in_cooldown,
            "cooldown_until": _fmt_dt(state.cooldown_until),
            "last_success_at": _fmt_dt(state.last_success_at),
            "last_failure_at": _fmt_dt(state.last_failure_at),
        }
        try:
            self._r.set(
                self._k(KEY_DEPLOYMENT, deployment_id=state.deployment_id),
                json.dumps(payload),
                ex=ttl_seconds,
            )
        except Exception as exc:  # noqa: BLE001
            raise StateStoreError(f"redis set deployment failed: {exc}") from exc

    @staticmethod
    def route_cooldown_key(
        *,
        route_mode: str = "direct",
        conversion_dir: str | None = None,
    ) -> str:
        """Stable Redis suffix: ``direct`` or ``convert:{source}>{target}``."""
        if route_mode == "convert" and conversion_dir:
            return f"convert:{conversion_dir}"
        if route_mode == "convert":
            return "convert:unknown"
        return "direct"

    def is_route_in_cooldown(
        self,
        deployment_id: str,
        route_key: str,
        *,
        now: datetime | None = None,
    ) -> bool:
        """True when the route-specific cooldown key is active (C3)."""
        now = now or datetime.now(timezone.utc)
        try:
            raw = self._r.get(
                self._k(
                    KEY_ROUTE_COOLDOWN,
                    deployment_id=deployment_id,
                    route_key=route_key,
                )
            )
        except Exception as exc:  # noqa: BLE001
            raise StateStoreError(f"redis get route cooldown failed: {exc}") from exc
        if raw is None:
            return False
        data = _loads(raw)
        until = _parse_dt(data.get("cooldown_until"))
        if until is None:
            return bool(data.get("is_in_cooldown"))
        return until > now

    def put_route_cooldown(
        self,
        deployment_id: str,
        route_key: str,
        *,
        cooldown_until: datetime,
        ttl_seconds: int | None = None,
    ) -> None:
        payload = {
            "deployment_id": deployment_id,
            "route_key": route_key,
            "is_in_cooldown": True,
            "cooldown_until": _fmt_dt(cooldown_until),
        }
        try:
            self._r.set(
                self._k(
                    KEY_ROUTE_COOLDOWN,
                    deployment_id=deployment_id,
                    route_key=route_key,
                ),
                json.dumps(payload),
                ex=ttl_seconds,
            )
        except Exception as exc:  # noqa: BLE001
            raise StateStoreError(f"redis set route cooldown failed: {exc}") from exc

    # ----- request routing context (P0-1 / P0-3) -----

    def get_request_context(self, request_id: str) -> RequestRoutingContext | None:
        if not request_id or request_id == "unknown":
            return None
        try:
            raw = self._r.get(self._k(KEY_REQCTX, request_id=request_id))
        except Exception as exc:  # noqa: BLE001
            raise StateStoreError(f"redis get reqctx failed: {exc}") from exc
        if raw is None:
            return None
        data = _loads(raw)
        tried = data.get("tried_quota_groups") or []
        return RequestRoutingContext(
            request_id=str(data.get("request_id") or request_id),
            tried_quota_groups=set(str(x) for x in tried),
            first_byte_sent=bool(data.get("first_byte_sent")),
            max_quota_groups=int(data.get("max_quota_groups") or 3),
        )

    def put_request_context(
        self,
        ctx: RequestRoutingContext,
        *,
        ttl_seconds: int = DEFAULT_REQCTX_TTL,
    ) -> None:
        if not ctx.request_id or ctx.request_id == "unknown":
            return
        payload = {
            "request_id": ctx.request_id,
            "tried_quota_groups": sorted(ctx.tried_quota_groups),
            "first_byte_sent": ctx.first_byte_sent,
            "max_quota_groups": ctx.max_quota_groups,
        }
        try:
            self._r.set(
                self._k(KEY_REQCTX, request_id=ctx.request_id),
                json.dumps(payload),
                ex=ttl_seconds,
            )
        except Exception as exc:  # noqa: BLE001
            raise StateStoreError(f"redis set reqctx failed: {exc}") from exc

    def load_or_create_request_context(
        self,
        request_id: str,
        *,
        ttl_seconds: int = DEFAULT_REQCTX_TTL,
    ) -> RequestRoutingContext:
        existing = self.get_request_context(request_id)
        if existing is not None:
            return existing
        ctx = RequestRoutingContext(request_id=request_id)
        self.put_request_context(ctx, ttl_seconds=ttl_seconds)
        return ctx

    # ----- affinity + reverse index (P0-2) -----

    def set_affinity(
        self,
        session_hash: str,
        deployment_id: str,
        *,
        quota_group_id: str | None = None,
        ttl_seconds: int = DEFAULT_AFFINITY_TTL,
    ) -> None:
        """Session affinity TTL default 2h; index by quota_group for bulk clear."""
        try:
            self._r.set(
                self._k(KEY_AFFINITY, session_hash=session_hash),
                deployment_id,
                ex=ttl_seconds,
            )
            if quota_group_id:
                self._r.set(
                    self._k(KEY_AFFINITY_META, session_hash=session_hash),
                    quota_group_id,
                    ex=ttl_seconds,
                )
                idx = self._k(KEY_AFFINITY_IDX, quota_group_id=quota_group_id)
                if hasattr(self._r, "sadd"):
                    self._r.sadd(idx, session_hash)
                    if hasattr(self._r, "expire"):
                        self._r.expire(idx, ttl_seconds)
                else:
                    # Fallback list stored as JSON set for minimal fakes
                    self._index_add_fallback(idx, session_hash, ttl_seconds)
        except Exception as exc:  # noqa: BLE001
            raise StateStoreError(f"redis set affinity failed: {exc}") from exc

    def get_affinity(self, session_hash: str) -> str | None:
        try:
            raw = self._r.get(self._k(KEY_AFFINITY, session_hash=session_hash))
        except Exception as exc:  # noqa: BLE001
            raise StateStoreError(f"redis get affinity failed: {exc}") from exc
        if raw is None:
            return None
        return raw.decode() if isinstance(raw, bytes) else str(raw)

    def clear_affinity_for_quota_group(self, quota_group_id: str) -> int:
        """Remove all session affinities bound to this quota group. Returns count cleared."""
        idx = self._k(KEY_AFFINITY_IDX, quota_group_id=quota_group_id)
        sessions: list[str] = []
        try:
            if hasattr(self._r, "smembers"):
                members = self._r.smembers(idx) or set()
                for m in members:
                    sessions.append(m.decode() if isinstance(m, bytes) else str(m))
            else:
                sessions = self._index_list_fallback(idx)
        except Exception as exc:  # noqa: BLE001
            raise StateStoreError(f"redis affinity index read failed: {exc}") from exc

        cleared = 0
        for session_hash in sessions:
            try:
                self._r.delete(
                    self._k(KEY_AFFINITY, session_hash=session_hash),
                    self._k(KEY_AFFINITY_META, session_hash=session_hash),
                )
                cleared += 1
            except Exception as exc:  # noqa: BLE001
                logger.warning("clear affinity session=%s: %s", session_hash, exc)
        try:
            self._r.delete(idx)
        except Exception as exc:  # noqa: BLE001
            logger.debug("delete affinity idx: %s", exc)

        logger.info(
            "cleared affinity for quota_group=%s count=%s",
            quota_group_id,
            cleared,
        )
        return cleared

    def clear_affinity_for_deployment_prefix(self, deployment_id: str) -> None:
        """Deprecated name — prefer clear_affinity_for_quota_group."""
        logger.debug(
            "clear_affinity_for_deployment_prefix(%s) no-op; use quota_group clear",
            deployment_id,
        )

    def _index_add_fallback(self, idx: str, session_hash: str, ttl: int) -> None:
        raw = self._r.get(idx)
        items: list[str] = []
        if raw:
            try:
                items = list(_loads(raw) if not isinstance(raw, list) else raw)
            except Exception:  # noqa: BLE001
                items = []
            if isinstance(raw, (bytes, str)):
                try:
                    items = list(json.loads(raw if isinstance(raw, str) else raw.decode()))
                except Exception:  # noqa: BLE001
                    items = []
        if session_hash not in items:
            items.append(session_hash)
        self._r.set(idx, json.dumps(items), ex=ttl)

    def _index_list_fallback(self, idx: str) -> list[str]:
        raw = self._r.get(idx)
        if raw is None:
            return []
        try:
            data = _loads(raw)
            if isinstance(data, list):
                return [str(x) for x in data]
        except Exception:  # noqa: BLE001
            pass
        return []


def _loads(raw: Any) -> dict[str, Any] | list[Any]:
    if isinstance(raw, bytes):
        raw = raw.decode()
    if isinstance(raw, str):
        return json.loads(raw)
    if isinstance(raw, (dict, list)):
        return raw
    raise StateStoreError(f"unexpected redis payload type: {type(raw)}")


def _fmt_dt(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


def _parse_dt(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value))


def _quota_to_dict(group: QuotaGroup) -> dict[str, Any]:
    return {
        "quota_group_id": group.quota_group_id,
        "provider_id": group.provider_id,
        "account_id": group.account_id,
        "display_name": group.display_name,
        "priority": group.priority,
        "status": group.status.value,
        "reset_at": _fmt_dt(group.reset_at),
        "cooldown_until": _fmt_dt(group.cooldown_until),
        "failure_reason": group.failure_reason,
        "last_failure_at": _fmt_dt(group.last_failure_at),
        "last_success_at": _fmt_dt(group.last_success_at),
        "consecutive_failures": group.consecutive_failures,
        "revision": group.revision,
        "next_probe_at": _fmt_dt(group.next_probe_at),
    }


def _quota_from_dict(data: dict[str, Any]) -> QuotaGroup:
    return QuotaGroup(
        quota_group_id=str(data["quota_group_id"]),
        provider_id=str(data.get("provider_id") or "unknown"),
        account_id=str(data.get("account_id") or data["quota_group_id"]),
        display_name=str(data.get("display_name") or data["quota_group_id"]),
        priority=int(data.get("priority") or 100),
        status=QuotaGroupStatus(data.get("status") or QuotaGroupStatus.AVAILABLE.value),
        reset_at=_parse_dt(data.get("reset_at")),
        cooldown_until=_parse_dt(data.get("cooldown_until")),
        failure_reason=data.get("failure_reason"),
        last_failure_at=_parse_dt(data.get("last_failure_at")),
        last_success_at=_parse_dt(data.get("last_success_at")),
        consecutive_failures=int(data.get("consecutive_failures") or 0),
        revision=int(data.get("revision") or 0),
        next_probe_at=_parse_dt(data.get("next_probe_at")),
    )

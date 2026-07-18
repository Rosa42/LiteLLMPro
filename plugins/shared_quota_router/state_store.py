"""Redis-backed quota / provider / deployment state.

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
)

logger = logging.getLogger(__name__)

KEY_PROVIDER = "sq:provider:{provider_id}"
KEY_QUOTA = "sq:quota:{quota_group_id}"
KEY_WINDOW = "sq:window:{quota_group_id}:{window_type}"
KEY_DEPLOYMENT = "sq:deployment:{deployment_id}"
KEY_AFFINITY = "sq:affinity:{session_hash}"
KEY_LEASE = "sq:lease:{quota_group_id}:{request_id}"
KEY_PROBE_LOCK = "sq:probe-lock:{quota_group_id}"
# sq:audit:{date} deferred to later phase — do not log secrets if implemented


class RedisLike(Protocol):
    def get(self, name: str) -> Any: ...
    def set(self, name: str, value: Any, ex: int | None = None) -> Any: ...
    def delete(self, *names: str) -> Any: ...


class StateStoreError(Exception):
    """Raised on Redis failure — callers must fail-closed."""


class StateStore:
    def __init__(self, redis: RedisLike, *, key_prefix: str = "") -> None:
        self._r = redis
        self._prefix = key_prefix

    def _k(self, template: str, **kwargs: str) -> str:
        return self._prefix + template.format(**kwargs)

    def get_quota_group(self, quota_group_id: str) -> QuotaGroup | None:
        try:
            raw = self._r.get(self._k(KEY_QUOTA, quota_group_id=quota_group_id))
        except Exception as exc:  # noqa: BLE001 — surface as fail-closed
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
    ) -> QuotaGroup:
        """Mark quota group EXHAUSTED with optional reset_at-driven TTL."""
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
            # Keep key at least until probe window; floor at 60s if in the past skip ttl
            if delta > 0:
                ttl = delta + 300

        self.put_quota_group(current, ttl_seconds=ttl)
        return current

    def set_quota_status(
        self,
        group: QuotaGroup,
        status: QuotaGroupStatus,
        *,
        reason: str | None = None,
    ) -> QuotaGroup:
        group.status = status
        if reason is not None:
            group.failure_reason = reason
        group.revision += 1
        self.put_quota_group(group)
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

    def set_affinity(self, session_hash: str, deployment_id: str, *, ttl_seconds: int = 7200) -> None:
        """Session affinity TTL default 2h (design §6.3)."""
        try:
            self._r.set(
                self._k(KEY_AFFINITY, session_hash=session_hash),
                deployment_id,
                ex=ttl_seconds,
            )
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

    def clear_affinity_for_deployment_prefix(self, deployment_id: str) -> None:
        """Best-effort; full index deferred. Callers may clear known session keys."""
        logger.debug("clear_affinity requested for deployment=%s", deployment_id)


def _loads(raw: Any) -> dict[str, Any]:
    if isinstance(raw, bytes):
        raw = raw.decode()
    if isinstance(raw, str):
        return json.loads(raw)
    if isinstance(raw, dict):
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



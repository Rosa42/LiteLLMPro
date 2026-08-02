"""Quota recovery probe worker (phase 9).

Scans EXHAUSTED groups, probes with minimal requests, restores or backs off.
Does not invent a fixed five-hour recovery as fact.
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from shared_quota_router.metrics import inc
from shared_quota_router.models import Deployment, QuotaGroup, QuotaGroupStatus
from shared_quota_router.registry import DeploymentRegistry
from shared_quota_router.state_store import StateStore, StateStoreError

logger = logging.getLogger(__name__)

# Probe schedule (design §10.3): 5m, 15m, 30m, then 60m; max interval reference 2h
PROBE_BACKOFF_SECONDS = [300, 900, 1800, 3600]
MAX_PROBE_INTERVAL_SECONDS = 7200  # reference ceiling, not a hard "must recover at 5h"
PROBE_TIMEOUT_SECONDS = 15
PROBE_PROMPT = "ping"
DEFAULT_SCAN_INTERVAL = 60


def next_probe_delay(consecutive_probe_failures: int) -> int:
    if consecutive_probe_failures <= 0:
        return PROBE_BACKOFF_SECONDS[0]
    idx = min(consecutive_probe_failures, len(PROBE_BACKOFF_SECONDS) - 1)
    delay = PROBE_BACKOFF_SECONDS[idx]
    return min(delay, MAX_PROBE_INTERVAL_SECONDS)


def schedule_next_probe(
    group: QuotaGroup,
    *,
    now: datetime | None = None,
    probe_failed: bool = True,
) -> datetime:
    now = now or datetime.now(timezone.utc)
    if group.reset_at is not None and group.reset_at > now:
        # Prefer explicit reset_at from provider
        return group.reset_at + timedelta(seconds=5)
    if not probe_failed:
        return now
    failures = group.consecutive_failures if probe_failed else 0
    return now + timedelta(seconds=next_probe_delay(failures))


class RecoveryWorker:
    def __init__(
        self,
        store: StateStore,
        registry: DeploymentRegistry,
        *,
        redis: Any = None,
        probe_fn: Callable[[Deployment], bool] | None = None,
        probe_lock_ttl: int = 60,
        probing_timeout_seconds: int = 120,
    ) -> None:
        self.store = store
        self.registry = registry
        self.redis = redis
        self.probe_fn = probe_fn or default_http_probe
        self.probe_lock_ttl = probe_lock_ttl
        self.probing_timeout_seconds = probing_timeout_seconds

    def list_due_groups(self, group_ids: list[str], *, now: datetime | None = None) -> list[QuotaGroup]:
        now = now or datetime.now(timezone.utc)
        due: list[QuotaGroup] = []
        for gid in group_ids:
            try:
                g = self.store.get_quota_group(gid)
            except StateStoreError as exc:
                logger.warning("skip group %s: %s", gid, exc)
                continue
            if g is None:
                continue
            if g.status == QuotaGroupStatus.PROBING:
                # timeout stuck PROBING
                if g.last_failure_at and (
                    now - g.last_failure_at
                ).total_seconds() > self.probing_timeout_seconds:
                    self._revert_probing(g, now=now)
                    g = self.store.get_quota_group(gid)
                    if g is None:
                        continue
                else:
                    continue
            if g.status != QuotaGroupStatus.EXHAUSTED:
                continue
            next_at = g.next_probe_at or g.reset_at
            if next_at is None or next_at <= now:
                due.append(g)
        return due

    def try_acquire_probe_lock(self, quota_group_id: str) -> bool:
        if self.redis is None:
            return True
        key = f"sq:probe-lock:{quota_group_id}"
        try:
            # SET NX EX
            ok = self.redis.set(key, "1", nx=True, ex=self.probe_lock_ttl)
            return bool(ok)
        except Exception as exc:  # noqa: BLE001
            logger.warning("probe lock failed: %s", exc)
            return False

    def release_probe_lock(self, quota_group_id: str) -> None:
        if self.redis is None:
            return
        try:
            self.redis.delete(f"sq:probe-lock:{quota_group_id}")
        except Exception as exc:  # noqa: BLE001
            logger.debug("probe unlock: %s", exc)

    def run_probe_cycle(self, group_ids: list[str] | None = None) -> dict[str, str]:
        ids = group_ids or sorted({d.quota_group_id for d in self.registry.all_deployments()})
        results: dict[str, str] = {}
        now = datetime.now(timezone.utc)
        for group in self.list_due_groups(ids, now=now):
            results[group.quota_group_id] = self.probe_one(group, now=now)
        return results

    def probe_one(self, group: QuotaGroup, *, now: datetime | None = None) -> str:
        now = now or datetime.now(timezone.utc)
        gid = group.quota_group_id
        if not self.try_acquire_probe_lock(gid):
            return "locked"

        try:
            group.status = QuotaGroupStatus.PROBING
            group.last_failure_at = now  # reuse as probe start marker
            group.revision += 1
            self.store.put_quota_group(group)
            inc("shared_quota_probe_total", quota_group_id=gid)

            dep = self.registry.pick_probe_deployment(gid)
            if dep is None:
                self._fail_probe(group, now=now, reason="no_deployment")
                return "no_deployment"

            ok = False
            try:
                ok = bool(self.probe_fn(dep))
            except Exception as exc:  # noqa: BLE001
                logger.info("probe error qg=%s: %s", gid, exc)
                ok = False

            if ok:
                group.status = QuotaGroupStatus.AVAILABLE
                group.failure_reason = None
                group.consecutive_failures = 0
                group.last_success_at = now
                group.next_probe_at = None
                group.revision += 1
                self.store.put_quota_group(group)
                inc("shared_quota_probe_success_total", quota_group_id=gid)
                logger.info("probe success quota_group=%s", gid)
                return "success"

            self._fail_probe(group, now=now, reason="probe_failed")
            return "failed"
        finally:
            self.release_probe_lock(gid)

    def _fail_probe(self, group: QuotaGroup, *, now: datetime, reason: str) -> None:
        group.status = QuotaGroupStatus.EXHAUSTED
        group.failure_reason = reason
        group.consecutive_failures += 1
        group.last_failure_at = now
        # Do NOT invent fixed five-hour recovery
        group.next_probe_at = schedule_next_probe(group, now=now, probe_failed=True)
        group.revision += 1
        self.store.put_quota_group(group)
        logger.info(
            "probe failed qg=%s reason=%s next=%s",
            group.quota_group_id,
            reason,
            group.next_probe_at,
        )

    def _revert_probing(self, group: QuotaGroup, *, now: datetime) -> None:
        group.status = QuotaGroupStatus.EXHAUSTED
        group.failure_reason = "probing_timeout"
        group.next_probe_at = schedule_next_probe(group, now=now, probe_failed=True)
        group.revision += 1
        self.store.put_quota_group(group)
        logger.warning("PROBING timeout → EXHAUSTED qg=%s", group.quota_group_id)


def default_http_probe(deployment: Deployment) -> bool:
    """Minimal chat completion probe — direct HTTP, not via user Router."""
    base = deployment.api_base or os.environ.get("PROBE_API_BASE")
    if not base:
        # Resolve from env mapping if api_base was os.environ/XXX style in config
        logger.warning("probe skip: no api_base for %s", deployment.deployment_id)
        return False

    api_key = None
    if deployment.api_key_env:
        api_key = os.environ.get(deployment.api_key_env)
    if not api_key:
        api_key = os.environ.get("PROBE_API_KEY")
    if not api_key:
        logger.warning("probe skip: no api key for %s", deployment.deployment_id)
        return False

    url = base.rstrip("/") + "/chat/completions"
    # upstream_model may be openai/kimi-k3
    model = deployment.upstream_model.split("/")[-1]
    body = json.dumps(
        {
            "model": model,
            "messages": [{"role": "user", "content": PROBE_PROMPT}],
            "max_tokens": 1,
        }
    ).encode()
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=PROBE_TIMEOUT_SECONDS) as resp:
            return 200 <= getattr(resp, "status", 200) < 300
    except urllib.error.HTTPError as e:
        # 400 with invalid model still means auth/network path worked; treat 401/403/429 carefully
        if e.code in {401, 403}:
            return False
        if e.code == 429:
            return False
        # 404/400 may still mean endpoint up — conservative: only 2xx is success
        return False
    except Exception as exc:  # noqa: BLE001
        logger.debug("probe http error: %s", exc)
        return False


def run_forever(
    store: StateStore,
    registry: DeploymentRegistry,
    redis: Any = None,
    scan_interval: int = DEFAULT_SCAN_INTERVAL,
) -> None:
    worker = RecoveryWorker(store, registry, redis=redis)
    logger.info("recovery worker started scan_interval=%ss", scan_interval)
    while True:
        try:
            results = worker.run_probe_cycle()
            if results:
                logger.info("probe cycle: %s", results)
        except Exception as exc:  # noqa: BLE001
            logger.exception("probe cycle error: %s", exc)
        time.sleep(scan_interval)


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    from shared_quota_router.bootstrap import build_default_strategy, _build_redis_client
    from shared_quota_router.registry import registry_from_model_list
    import yaml

    config_path = os.environ.get("LITELLM_CONFIG", "/app/config.yaml")
    model_list: list[dict] = []
    if os.path.isfile(config_path):
        with open(config_path, encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        model_list = cfg.get("model_list") or []

    # Resolve os.environ/ refs for probe
    for entry in model_list:
        params = entry.get("litellm_params") or {}
        base = params.get("api_base")
        if isinstance(base, str) and base.startswith("os.environ/"):
            params["api_base"] = os.environ.get(base.split("/", 1)[1], base)
            entry["litellm_params"] = params

    redis = _build_redis_client()
    if redis is None:
        raise SystemExit("REDIS required for recovery worker")
    from shared_quota_router.state_store import StateStore

    store = StateStore(redis)
    registry = registry_from_model_list(model_list) if model_list else DeploymentRegistry()
    # Attach env-resolved api_base onto deployments
    for d in registry.all_deployments():
        if d.api_base and str(d.api_base).startswith("os.environ/"):
            env_name = str(d.api_base).split("/", 1)[1]
            d.api_base = os.environ.get(env_name)

    interval = int(os.environ.get("PROBE_SCAN_INTERVAL", str(DEFAULT_SCAN_INTERVAL)))
    run_forever(store, registry, redis=redis, scan_interval=interval)


if __name__ == "__main__":
    main()

"""P0-CLF A9：OpenCode RegionError → DEPLOYMENT_ERROR + deployment cooldown，禁止 qg DISABLED。"""

from __future__ import annotations

import json
from pathlib import Path

from shared_quota_router.callbacks import SharedQuotaCallback
from shared_quota_router.classifiers.base import FailureKind, UpstreamError
from shared_quota_router.classifiers.generic_openai import GenericOpenAIClassifier
from shared_quota_router.classifiers.opencode_go import OpenCodeGoClassifier
from shared_quota_router.models import (
    Deployment,
    QuotaGroupStatus,
    RequestRoutingContext,
)
from shared_quota_router.registry import DeploymentRegistry
from shared_quota_router.state_store import KEY_DEPLOYMENT, KEY_QUOTA, StateStore
from shared_quota_router.strategy import SharedQuotaSelector

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "classifiers" / "opencode_go"

# 与规格 A9 示例对齐的部署 ID
KIMI_DEP = "opencode-a-chat-kimi-k3"
GLM_DEP = "opencode-a-msg-glm-5.2"
QG = "opencode-a"


class MemRedis:
    def __init__(self) -> None:
        self.data: dict[str, str] = {}
        self.ttl: dict[str, int | None] = {}

    def get(self, name: str):
        return self.data.get(name)

    def set(self, name: str, value, ex=None, nx=False):
        self.data[name] = value if isinstance(value, str) else str(value)
        self.ttl[name] = ex
        return True

    def delete(self, *names: str):
        for n in names:
            self.data.pop(n, None)
            self.ttl.pop(n, None)

    def incr(self, name: str):
        v = int(self.data.get(name, "0")) + 1
        self.data[name] = str(v)
        return v

    def decr(self, name: str):
        v = int(self.data.get(name, "0")) - 1
        self.data[name] = str(v)
        return v

    def expire(self, name: str, time: int):
        self.ttl[name] = time
        return True

    def sadd(self, *a, **k):
        return 1

    def smembers(self, name: str):
        return set()

    def eval(self, script, numkeys, *args):
        return [1, "1"]


def _load_fixture(name: str) -> dict:
    return json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))


def _cb(mem: MemRedis | None = None) -> tuple[SharedQuotaCallback, StateStore, MemRedis]:
    redis = mem or MemRedis()
    store = StateStore(redis)
    return SharedQuotaCallback(store=store, lease_manager=None), store, redis


def _region_kwargs(deployment_id: str = KIMI_DEP) -> dict:
    fx = _load_fixture("region_error_403.json")
    return {
        "litellm_call_id": "a9-region",
        "model_info": {
            "deployment_id": deployment_id,
            "quota_group_id": QG,
            "provider_id": "opencode-go",
        },
        "response_status_code": fx["http_status"],
        "exception": Exception(fx["body"]["error"]["message"]),
    }, fx["body"]


def _registry() -> DeploymentRegistry:
    return DeploymentRegistry(
        [
            Deployment(
                deployment_id=KIMI_DEP,
                model_group="kimi-k3",
                upstream_model="openai/kimi-k3",
                provider_id="opencode-go",
                quota_group_id=QG,
                priority=10,
            ),
            Deployment(
                deployment_id=GLM_DEP,
                model_group="glm-5.2",
                upstream_model="openai/glm-5.2",
                provider_id="opencode-go",
                quota_group_id=QG,
                priority=10,
            ),
        ]
    )


# ----- classifier 单元 -----


def test_opencode_region_error_by_type_is_deployment_error() -> None:
    fx = _load_fixture("region_error_403.json")
    result = OpenCodeGoClassifier().classify(
        UpstreamError(
            http_status=fx["http_status"],
            body=fx["body"],
            provider_id="opencode-go",
        )
    )
    assert result.kind == FailureKind.DEPLOYMENT_ERROR
    assert result.scope == "deployment"
    assert result.normalized_message == "region_blocked"
    assert result.confidence >= 0.9


def test_opencode_region_error_by_message_markers() -> None:
    fx = _load_fixture("region_error_403_message_only.json")
    result = OpenCodeGoClassifier().classify(
        UpstreamError(http_status=fx["http_status"], body=fx["body"])
    )
    assert result.kind == FailureKind.DEPLOYMENT_ERROR
    assert result.normalized_message == "region_blocked"
    assert result.confidence >= 0.9


def test_opencode_upstream_failed_stays_bad_request() -> None:
    fx = _load_fixture("upstream_failed_400.json")
    result = OpenCodeGoClassifier().classify(
        UpstreamError(http_status=fx["http_status"], body=fx["body"])
    )
    assert result.kind == FailureKind.BAD_REQUEST
    assert result.scope == "request"


def test_generic_still_treats_403_region_as_auth() -> None:
    """未分发到 OpenCode 时，Generic 仍可能把 403 判成 AUTH（说明分发必要）。"""
    fx = _load_fixture("region_error_403.json")
    result = GenericOpenAIClassifier().classify(
        UpstreamError(http_status=fx["http_status"], body=fx["body"])
    )
    assert result.kind == FailureKind.AUTH_INVALID


# ----- A9 验收 -----


def test_a9a_region_error_does_not_disable_quota_group() -> None:
    cb, store, redis = _cb()
    kwargs, body = _region_kwargs()
    cb.on_failure(kwargs, body)

    quota_key = KEY_QUOTA.format(quota_group_id=QG)
    raw = redis.get(quota_key)
    if raw is not None:
        g = store.get_quota_group(QG)
        assert g is not None
        assert g.status != QuotaGroupStatus.DISABLED
    else:
        # 未写入 qg 也视为未 DISABLED
        assert store.get_quota_group(QG) is None


def test_a9b_deployment_cooldown_key_written_with_ttl_fields() -> None:
    cb, store, redis = _cb()
    kwargs, body = _region_kwargs()
    cb.on_failure(kwargs, body)

    dep_key = KEY_DEPLOYMENT.format(deployment_id=KIMI_DEP)
    assert dep_key in redis.data
    assert redis.ttl.get(dep_key) is not None and redis.ttl[dep_key] > 0

    st = store.get_deployment_state(KIMI_DEP)
    assert st is not None
    assert st.is_in_cooldown is True
    assert st.cooldown_until is not None


def test_a9c_same_qg_glm_still_selectable_after_kimi_region_block() -> None:
    cb, store, _ = _cb()
    kwargs, body = _region_kwargs(KIMI_DEP)
    cb.on_failure(kwargs, body)

    # kimi 已 cooldown；同 qg 的 glm 仍应可选
    selector = SharedQuotaSelector(_registry(), store, lease_manager=None)
    ctx = RequestRoutingContext(request_id="a9c")
    picked = selector.select("glm-5.2", ctx, acquire_lease=False)
    assert picked.deployment_id == GLM_DEP
    assert picked.quota_group_id == QG


def test_upstream_failed_400_does_not_disable_or_cooldown_qg() -> None:
    cb, store, redis = _cb()
    fx = _load_fixture("upstream_failed_400.json")
    cb.on_failure(
        {
            "litellm_call_id": "a9-bad-req",
            "model_info": {
                "deployment_id": KIMI_DEP,
                "quota_group_id": QG,
                "provider_id": "opencode-go",
            },
            "response_status_code": fx["http_status"],
            "exception": Exception(fx["body"]["error"]["message"]),
        },
        fx["body"],
    )
    assert store.get_quota_group(QG) is None
    assert store.get_deployment_state(KIMI_DEP) is None
    assert KEY_DEPLOYMENT.format(deployment_id=KIMI_DEP) not in redis.data

from __future__ import annotations

from datetime import datetime, timezone

from shared_quota_router.callbacks import SharedQuotaCallback
from shared_quota_router.metrics import get_counter, reset_for_tests
from shared_quota_router.models import QuotaGroupStatus, RequestRoutingContext
from shared_quota_router.state_store import StateStore
from shared_quota_router.strategy import context_from_request_kwargs


class MemRedis:
    def __init__(self) -> None:
        self.data: dict[str, str] = {}

    def get(self, name: str):
        return self.data.get(name)

    def set(self, name: str, value, ex=None, nx=False):
        self.data[name] = value if isinstance(value, str) else str(value)
        return True

    def delete(self, *names: str):
        for n in names:
            self.data.pop(n, None)


def _cb() -> tuple[SharedQuotaCallback, StateStore]:
    store = StateStore(MemRedis())
    return SharedQuotaCallback(store=store, lease_manager=None), store


def test_exhaust_marks_quota_group() -> None:
    reset_for_tests()
    cb, store = _cb()
    kwargs = {
        "litellm_call_id": "r1",
        "model_info": {
            "deployment_id": "opencode-a-kimi",
            "quota_group_id": "opencode-a",
            "provider_id": "opencode-go",
        },
        "exception": type("E", (), {"status_code": 429, "response": None})(),
    }
    # craft exception with body via classifier path using response_obj dict
    cb.on_failure(
        kwargs,
        {
            "error": {
                "code": "insufficient_quota",
                "message": "You exceeded your current quota",
            }
        },
    )
    # inject status via kwargs for classifier
    kwargs2 = {
        **kwargs,
        "response_status_code": 429,
        "exception": Exception("insufficient_quota You exceeded your current quota"),
    }
    cb.on_failure(
        kwargs2,
        {
            "error": {
                "code": "insufficient_quota",
                "message": "You exceeded your current quota",
            }
        },
    )
    g = store.get_quota_group("opencode-a")
    assert g is not None
    assert g.status == QuotaGroupStatus.EXHAUSTED


def test_short_429_only_deployment_cooldown() -> None:
    cb, store = _cb()
    kwargs = {
        "litellm_call_id": "r2",
        "model_info": {
            "deployment_id": "opencode-a-kimi",
            "quota_group_id": "opencode-a",
            "provider_id": "opencode-go",
        },
        "response_status_code": 429,
        "exception": Exception("Rate limit reached for TPM"),
    }
    cb.on_failure(
        kwargs,
        {"error": {"code": "rate_limit_exceeded", "message": "Rate limit reached for TPM"}},
    )
    g = store.get_quota_group("opencode-a")
    # short rate limit should NOT exhaust group (may be None/AVAILABLE)
    assert g is None or g.status == QuotaGroupStatus.AVAILABLE
    st = store.get_deployment_state("opencode-a-kimi")
    assert st is not None and st.is_in_cooldown


def test_auth_disables_and_alerts() -> None:
    alerts: list[tuple[str, dict]] = []
    store = StateStore(MemRedis())
    cb = SharedQuotaCallback(store=store, alert_hook=lambda e, p: alerts.append((e, p)))
    kwargs = {
        "litellm_call_id": "r3",
        "model_info": {
            "deployment_id": "opencode-a-kimi",
            "quota_group_id": "opencode-a",
            "provider_id": "opencode-go",
        },
        "response_status_code": 401,
        "exception": Exception("Invalid API key"),
    }
    cb.on_failure(kwargs, {"error": {"message": "Invalid API key", "code": "invalid_api_key"}})
    g = store.get_quota_group("opencode-a")
    assert g is not None and g.status == QuotaGroupStatus.DISABLED
    assert alerts and alerts[0][0] == "quota_group_disabled"


def test_first_byte_blocks_retry() -> None:
    cb, _ = _cb()
    kwargs: dict = {"litellm_call_id": "r4", "metadata": {}}
    assert cb.should_allow_retry(kwargs) is True
    cb.mark_first_byte(kwargs)
    ctx = context_from_request_kwargs(kwargs)
    assert ctx.first_byte_sent is True
    assert cb.should_allow_retry(kwargs) is False


def test_content_policy_no_account_melt() -> None:
    cb, store = _cb()
    kwargs = {
        "litellm_call_id": "r5",
        "model_info": {
            "deployment_id": "opencode-a-kimi",
            "quota_group_id": "opencode-a",
            "provider_id": "opencode-go",
        },
        "response_status_code": 400,
        "exception": Exception("Content policy violation"),
    }
    cb.on_failure(
        kwargs,
        {"error": {"message": "Content policy violation", "code": "content_policy"}},
    )
    assert store.get_quota_group("opencode-a") is None


def test_cross_model_same_group_exhausted_visible() -> None:
    """A/kimi exhaust ⇒ group EXHAUSTED ⇒ A/glm also unusable (checked by strategy store)."""
    cb, store = _cb()
    kwargs = {
        "litellm_call_id": "r6",
        "model_info": {
            "deployment_id": "opencode-a-kimi",
            "quota_group_id": "opencode-a",
            "provider_id": "opencode-go",
        },
        "response_status_code": 429,
        "exception": Exception("insufficient_quota exceeded quota"),
    }
    cb.on_failure(
        kwargs,
        {
            "error": {
                "code": "insufficient_quota",
                "message": "You exceeded your current quota",
            }
        },
    )
    g = store.get_quota_group("opencode-a")
    assert g is not None
    assert g.status == QuotaGroupStatus.EXHAUSTED

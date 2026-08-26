from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from shared_quota_router.models import ApiProtocol, Deployment, QuotaGroup, QuotaGroupStatus
from shared_quota_router.recovery_worker import (
    RecoveryWorker,
    default_http_probe,
    next_probe_delay,
    schedule_next_probe,
)
from shared_quota_router.registry import DeploymentRegistry
from shared_quota_router.state_store import StateStore


class MemRedis:
    def __init__(self) -> None:
        self.data: dict[str, str] = {}

    def get(self, name: str):
        return self.data.get(name)

    def set(self, name: str, value, ex=None, nx=False):
        if nx and name in self.data:
            return False
        self.data[name] = value if isinstance(value, str) else str(value)
        return True

    def delete(self, *names: str):
        for n in names:
            self.data.pop(n, None)


def test_backoff_sequence_and_cap() -> None:
    assert next_probe_delay(0) == 300
    assert next_probe_delay(1) == 900
    assert next_probe_delay(2) == 1800
    assert next_probe_delay(3) == 3600
    assert next_probe_delay(10) == 3600
    assert next_probe_delay(10) <= 7200


def test_no_fixed_five_hour_invention() -> None:
    """Without reset_at, next probe uses backoff minutes — not +5 hours as fact."""
    g = QuotaGroup(
        quota_group_id="a",
        provider_id="p",
        account_id="a",
        display_name="a",
        status=QuotaGroupStatus.EXHAUSTED,
        consecutive_failures=0,
        reset_at=None,
    )
    now = datetime(2026, 7, 18, 12, 0, tzinfo=timezone.utc)
    nxt = schedule_next_probe(g, now=now, probe_failed=True)
    assert nxt == now + timedelta(seconds=300)
    assert (nxt - now).total_seconds() != 5 * 3600


def test_probe_success_restores_group() -> None:
    store = StateStore(MemRedis())
    reg = DeploymentRegistry(
        [
            Deployment(
                deployment_id="a-kimi",
                model_group="kimi-k3",
                upstream_model="kimi-k3",
                provider_id="p",
                quota_group_id="a",
                priority=10,
                api_base="http://127.0.0.1:9",
            ),
            Deployment(
                deployment_id="a-glm",
                model_group="glm-5.2",
                upstream_model="glm-5.2",
                provider_id="p",
                quota_group_id="a",
                priority=10,
            ),
        ]
    )
    store.put_quota_group(
        QuotaGroup(
            quota_group_id="a",
            provider_id="p",
            account_id="a",
            display_name="a",
            status=QuotaGroupStatus.EXHAUSTED,
            next_probe_at=datetime.now(timezone.utc) - timedelta(seconds=1),
        )
    )
    worker = RecoveryWorker(store, reg, redis=MemRedis(), probe_fn=lambda d: True)
    result = worker.run_probe_cycle(["a"])
    assert result["a"] == "success"
    g = store.get_quota_group("a")
    assert g is not None and g.status == QuotaGroupStatus.AVAILABLE


def test_probe_fail_backoff() -> None:
    store = StateStore(MemRedis())
    reg = DeploymentRegistry(
        [
            Deployment(
                deployment_id="a-kimi",
                model_group="kimi-k3",
                upstream_model="kimi-k3",
                provider_id="p",
                quota_group_id="a",
                priority=10,
            )
        ]
    )
    store.put_quota_group(
        QuotaGroup(
            quota_group_id="a",
            provider_id="p",
            account_id="a",
            display_name="a",
            status=QuotaGroupStatus.EXHAUSTED,
            next_probe_at=datetime.now(timezone.utc) - timedelta(seconds=1),
        )
    )
    worker = RecoveryWorker(store, reg, redis=MemRedis(), probe_fn=lambda d: False)
    assert worker.run_probe_cycle(["a"])["a"] == "failed"
    g = store.get_quota_group("a")
    assert g is not None
    assert g.status == QuotaGroupStatus.EXHAUSTED
    assert g.next_probe_at is not None


def test_single_probe_lock() -> None:
    redis = MemRedis()
    store = StateStore(redis)
    reg = DeploymentRegistry([])
    worker = RecoveryWorker(store, reg, redis=redis, probe_fn=lambda d: True)
    assert worker.try_acquire_probe_lock("a") is True
    assert worker.try_acquire_probe_lock("a") is False


class _FakeResp:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *args: object) -> None:
        return None


def test_anthropic_probe_posts_v1_messages(monkeypatch) -> None:
    """Anthropic deployments must probe /v1/messages, not Chat completions."""
    captured: dict[str, object] = {}

    def fake_urlopen(req, timeout=None):  # noqa: ANN001
        captured["url"] = req.full_url
        captured["method"] = req.get_method()
        captured["body"] = json.loads(req.data.decode())
        captured["headers"] = {k.lower(): v for k, v in req.header_items()}
        return _FakeResp()

    monkeypatch.setattr(
        "shared_quota_router.recovery_worker.urllib.request.urlopen", fake_urlopen
    )
    monkeypatch.setenv("VOLC_CODING_KEY_C", "ark-test")
    dep = Deployment(
        deployment_id="volc-c-msg-glm-5.2",
        model_group="glm-5.2",
        upstream_model="anthropic/glm-5.2",
        provider_id="volcengine",
        quota_group_id="volc-c",
        api_base="https://ark.example/api/coding",
        api_key_env="VOLC_CODING_KEY_C",
        upstream_protocol=ApiProtocol.ANTHROPIC_MESSAGES,
    )
    assert default_http_probe(dep) is True
    assert captured["url"] == "https://ark.example/api/coding/v1/messages"
    assert captured["method"] == "POST"
    body = captured["body"]
    assert isinstance(body, dict)
    assert body["model"] == "glm-5.2"
    assert "messages" in body
    headers = captured["headers"]
    assert isinstance(headers, dict)
    assert headers.get("x-api-key") == "ark-test"
    assert headers.get("anthropic-version") == "2023-06-01"


def test_chat_probe_still_posts_chat_completions(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_urlopen(req, timeout=None):  # noqa: ANN001
        captured["url"] = req.full_url
        return _FakeResp()

    monkeypatch.setattr(
        "shared_quota_router.recovery_worker.urllib.request.urlopen", fake_urlopen
    )
    monkeypatch.setenv("OPENCODE_GO_KEY_A", "sk-test")
    dep = Deployment(
        deployment_id="opencode-a-chat-kimi-k3",
        model_group="kimi-k3",
        upstream_model="openai/kimi-k3",
        provider_id="opencode-go",
        quota_group_id="opencode-a",
        api_base="https://opencode.example/zen/go/v1",
        api_key_env="OPENCODE_GO_KEY_A",
        upstream_protocol=ApiProtocol.OPENAI_CHAT,
    )
    assert default_http_probe(dep) is True
    assert captured["url"] == "https://opencode.example/zen/go/v1/chat/completions"

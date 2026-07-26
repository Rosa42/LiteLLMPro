"""M4: protocol observability + feature-flag rollout/rollback."""

from __future__ import annotations

from pathlib import Path

import pytest
from shared_quota_router.config_schema import ConfigValidationError
from shared_quota_router.feature_flags import (
    clear_flag_cache,
    is_protocol_aware_gateway_enabled,
)
from shared_quota_router.generator import write_litellm_yaml_atomic
from shared_quota_router.lease import LeaseManager
from shared_quota_router.metrics import get_counter, reset_for_tests
from shared_quota_router.models import (
    ApiProtocol,
    Deployment,
    Feature,
    QuotaGroup,
    QuotaGroupStatus,
)
from shared_quota_router.protocol_errors import ProtocolAwareRoutingError
from shared_quota_router.protocol_gates import enforce_pre_call_gates
from shared_quota_router.protocol_observability import (
    conversion_metrics_dormant,
    log_text_has_secrets,
    record_protocol_rejection,
    record_route_selection,
    sanitize_operational_label,
)
from shared_quota_router.registry import DeploymentRegistry
from shared_quota_router.state_store import StateStore
from shared_quota_router.strategy import SharedQuotaRoutingStrategy


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

    def incr(self, name: str):
        v = int(self.data.get(name, "0")) + 1
        self.data[name] = str(v)
        return v

    def decr(self, name: str):
        v = int(self.data.get(name, "0")) - 1
        self.data[name] = str(v)
        return v

    def expire(self, name: str, time: int):
        return True

    def sadd(self, name: str, *values):
        return 1

    def smembers(self, name: str):
        return set()

    def eval(self, script: str, numkeys: int, *keys_and_args):
        if numkeys == 3:
            self.incr(keys_and_args[1])
            self.set(keys_and_args[2], keys_and_args[5] if len(keys_and_args) > 5 else "x")
            return [1, "1"]
        if numkeys == 2:
            self.delete(keys_and_args[1])
            return 0
        return 0


@pytest.fixture(autouse=True)
def _clean_metrics_and_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    reset_for_tests()
    clear_flag_cache()
    yield
    reset_for_tests()
    clear_flag_cache()


def _chat_dep(**kwargs) -> Deployment:
    base = dict(
        deployment_id="chat-a",
        model_group="kimi-k3",
        upstream_model="openai/kimi-k3",
        provider_id="opencode-go",
        quota_group_id="qg-a",
        priority=10,
        upstream_protocol=ApiProtocol.OPENAI_CHAT,
        supported_features=frozenset({Feature.TEXT, Feature.STREAMING, Feature.TOOLS}),
        supports_streaming=True,
        public_protocols=frozenset({ApiProtocol.OPENAI_CHAT}),
    )
    base.update(kwargs)
    return Deployment(**base)


# ----- M4-01 observability -----


def test_m4_01_route_and_reject_counters() -> None:
    record_route_selection(
        public_protocol=ApiProtocol.OPENAI_CHAT,
        upstream_protocol=ApiProtocol.OPENAI_CHAT,
        route_mode="direct",
        result="selected",
        model_group="kimi-k3",
        deployment_id="dep-1",
        quota_group_id="qg-a",
    )
    assert get_counter("shared_quota_protocol_route_total") >= 1.0
    record_protocol_rejection(
        public_protocol=ApiProtocol.OPENAI_RESPONSES,
        reason="unsupported_public_protocol",
        model_group="kimi-k3",
    )
    assert get_counter("shared_quota_protocol_reject_total") >= 1.0
    assert conversion_metrics_dormant()


def test_m4_01_label_hash_with_salt(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SHARED_QUOTA_METRICS_LABEL_SALT", "test-salt")
    monkeypatch.delenv("SHARED_QUOTA_METRICS_RAW_LABELS", raising=False)
    clear_flag_cache()
    hashed = sanitize_operational_label("kimi-k3", kind="model")
    assert hashed.startswith("h_")
    assert "kimi" not in hashed


def test_m4_01_secret_scan_helper() -> None:
    assert log_text_has_secrets("Authorization: Bearer sk-abc")
    assert log_text_has_secrets("api_key=secret")
    assert not log_text_has_secrets("protocol=openai_chat result=selected")


# ----- M4-02 feature flag -----


def test_m4_02_flag_default_false(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PROTOCOL_AWARE_GATEWAY_ENABLED", raising=False)
    clear_flag_cache()
    assert is_protocol_aware_gateway_enabled() is False


def test_m4_02_flag_off_legacy_chat_skips_public_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PROTOCOL_AWARE_GATEWAY_ENABLED", "false")
    clear_flag_cache()
    # Chat-only registry but model has no public_protocols → still allowed when flag off
    reg = DeploymentRegistry(
        [
            _chat_dep(public_protocols=frozenset()),
        ]
    )
    data = {"model": "kimi-k3", "messages": [{"role": "user", "content": "hi"}]}
    ctx = enforce_pre_call_gates(data, call_type="acompletion", registry=reg)
    assert ctx.protocol is ApiProtocol.OPENAI_CHAT


def test_m4_02_flag_off_still_blocks_responses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PROTOCOL_AWARE_GATEWAY_ENABLED", "false")
    clear_flag_cache()
    reg = DeploymentRegistry([_chat_dep()])
    data = {"model": "kimi-k3", "input": "hi"}
    with pytest.raises(ProtocolAwareRoutingError):
        enforce_pre_call_gates(data, call_type="aresponses", registry=reg)


def test_m4_02_flag_on_enables_chat_opt_in_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PROTOCOL_AWARE_GATEWAY_ENABLED", "true")
    clear_flag_cache()
    reg = DeploymentRegistry([_chat_dep(public_protocols=frozenset())])
    data = {"model": "kimi-k3"}
    with pytest.raises(ProtocolAwareRoutingError):
        enforce_pre_call_gates(data, call_type="acompletion", registry=reg)


def test_m4_02_rollback_preserves_redis_quota(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Toggling the flag must not clear quota state."""
    mem = MemRedis()
    store = StateStore(mem)
    store.put_quota_group(
        QuotaGroup(
            quota_group_id="qg-a",
            provider_id="opencode-go",
            account_id="a",
            display_name="A",
            status=QuotaGroupStatus.EXHAUSTED,
            revision=9,
        )
    )
    monkeypatch.setenv("PROTOCOL_AWARE_GATEWAY_ENABLED", "true")
    clear_flag_cache()
    assert is_protocol_aware_gateway_enabled() is True
    monkeypatch.setenv("PROTOCOL_AWARE_GATEWAY_ENABLED", "false")
    clear_flag_cache()
    assert is_protocol_aware_gateway_enabled() is False
    g = store.get_quota_group("qg-a")
    assert g is not None
    assert g.status is QuotaGroupStatus.EXHAUSTED
    assert g.revision == 9


def test_m4_02_invalid_generate_leaves_previous(tmp_path: Path) -> None:
    out = tmp_path / "litellm.yaml"
    out.write_text("# AUTO-GENERATED\nmodel_list:\n  - x\nos.environ/FOO\n", encoding="ascii")
    previous = out.read_text(encoding="ascii")
    with pytest.raises(ConfigValidationError):
        write_litellm_yaml_atomic(
            "not-valid-no-header",
            out,
            backup_dir=tmp_path / "backups",
        )
    assert out.read_text(encoding="ascii") == previous


def test_m4_02_strategy_flag_off_selects_without_protocol_filter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PROTOCOL_AWARE_GATEWAY_ENABLED", "false")
    clear_flag_cache()
    mem = MemRedis()
    store = StateStore(mem)
    lease = LeaseManager(mem)
    reg = DeploymentRegistry([_chat_dep()])
    strategy = SharedQuotaRoutingStrategy(
        store=store, lease_manager=lease, registry=reg
    )

    class _R:
        model_list = [
            {
                "model_name": "kimi-k3",
                "litellm_params": {"model": "openai/kimi-k3"},
                "model_info": {
                    "deployment_id": "chat-a",
                    "quota_group_id": "qg-a",
                    "provider_id": "opencode-go",
                    "upstream_protocol": "openai_chat",
                    "public_protocols": ["openai_chat"],
                },
            }
        ]

    strategy.bind_router(_R())
    entry = strategy.get_available_deployment(
        "kimi-k3",
        request_kwargs={
            "litellm_call_id": "m4-flag-off",
            "metadata": {"protocol": "openai_chat"},
        },
    )
    assert entry["model_info"]["deployment_id"] == "chat-a"

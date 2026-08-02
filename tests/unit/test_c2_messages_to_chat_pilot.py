"""C2: conversion flag, metrics, adapter, and dispatch wiring."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from shared_quota_router.feature_flags import (
    clear_flag_cache,
    is_protocol_conversion_enabled,
)
from shared_quota_router.metrics import get_counter, reset_for_tests
from shared_quota_router.protocol_observability import (
    conversion_metrics_dormant,
    record_conversion_result,
)

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "conversion" / "messages_to_chat"


@pytest.fixture(autouse=True)
def _reset_metrics_and_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    reset_for_tests()
    monkeypatch.delenv("PROTOCOL_CONVERSION_ENABLED", raising=False)
    clear_flag_cache()
    yield
    reset_for_tests()
    clear_flag_cache()


def test_conversion_flag_defaults_false() -> None:
    assert is_protocol_conversion_enabled() is False


def test_conversion_routing_requires_gateway_and(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GATEWAY=false × CONVERSION=true must never activate convert routing."""
    from shared_quota_router.feature_flags import (
        is_conversion_routing_active,
        set_g0a_messages_mount_ready,
    )

    set_g0a_messages_mount_ready(False)
    monkeypatch.setenv("PROTOCOL_CONVERSION_ENABLED", "true")
    monkeypatch.setenv("PROTOCOL_AWARE_GATEWAY_ENABLED", "false")
    clear_flag_cache()
    assert is_protocol_conversion_enabled() is True
    assert is_conversion_routing_active() is False

    monkeypatch.setenv("PROTOCOL_AWARE_GATEWAY_ENABLED", "true")
    clear_flag_cache()
    # 仍缺 proven path（P0-G0A：仅 native；g0a 不计入）
    try:
        import litellm

        monkeypatch.setattr(
            litellm, "use_chat_completions_url_for_anthropic_messages", False
        )
    except ImportError:
        monkeypatch.delenv(
            "LITELLM_USE_CHAT_COMPLETIONS_URL_FOR_ANTHROPIC_MESSAGES", raising=False
        )
    clear_flag_cache()
    assert is_conversion_routing_active() is False

    set_g0a_messages_mount_ready(True)
    clear_flag_cache()
    assert is_conversion_routing_active() is False
    set_g0a_messages_mount_ready(False)

    try:
        import litellm

        monkeypatch.setattr(
            litellm, "use_chat_completions_url_for_anthropic_messages", True
        )
    except ImportError:
        monkeypatch.setenv(
            "LITELLM_USE_CHAT_COMPLETIONS_URL_FOR_ANTHROPIC_MESSAGES", "true"
        )
    clear_flag_cache()
    assert is_conversion_routing_active() is True


def test_dispatch_blocked_when_gateway_off_conversion_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from shared_quota_router.conversion.contracts import DIRECTION_MESSAGES_TO_CHAT
    from shared_quota_router.conversion.dispatch import convert_public_request
    from shared_quota_router.protocol_errors import ProtocolAwareRoutingError

    monkeypatch.setenv("PROTOCOL_CONVERSION_ENABLED", "true")
    monkeypatch.setenv("PROTOCOL_AWARE_GATEWAY_ENABLED", "false")
    clear_flag_cache()
    with pytest.raises(ProtocolAwareRoutingError, match="disabled"):
        convert_public_request(
            _load("request_basic.json"), direction=DIRECTION_MESSAGES_TO_CHAT
        )


def test_record_conversion_increments_reserved_counters() -> None:
    assert conversion_metrics_dormant() is True
    record_conversion_result(
        direction="anthropic_messages>openai_chat", result="success"
    )
    assert get_counter("shared_quota_protocol_conversion_total") == 1.0
    record_conversion_result(
        direction="anthropic_messages>openai_chat",
        result="failure",
        reason="dropped_fields",
    )
    assert get_counter("shared_quota_protocol_conversion_failure_total") == 1.0
    assert conversion_metrics_dormant() is False


def _load(name: str) -> dict:
    return json.loads((_FIXTURES / name).read_text(encoding="utf-8"))


def test_convert_request_basic_text(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PROTOCOL_CONVERSION_ENABLED", "true")
    clear_flag_cache()
    from shared_quota_router.conversion.adapters.messages_to_chat import (
        MessagesToChatConverter,
    )

    out = MessagesToChatConverter().convert_request(_load("request_basic.json"))
    assert out.dropped_fields == []
    assert out.payload["messages"] == [{"role": "user", "content": "hello"}]
    assert out.payload["max_tokens"] == 128


def test_convert_request_system_multiturn(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PROTOCOL_CONVERSION_ENABLED", "true")
    clear_flag_cache()
    from shared_quota_router.conversion.adapters.messages_to_chat import (
        MessagesToChatConverter,
    )

    out = MessagesToChatConverter().convert_request(
        _load("request_system_multiturn.json")
    )
    assert out.payload["messages"][0] == {
        "role": "system",
        "content": "You are helpful.",
    }
    assert out.payload["messages"][-1]["content"] == "again"


def test_reject_tools_in_request(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PROTOCOL_CONVERSION_ENABLED", "true")
    clear_flag_cache()
    from shared_quota_router.conversion.adapters.messages_to_chat import (
        MessagesToChatConverter,
    )
    from shared_quota_router.protocol_errors import ProtocolAwareRoutingError

    public = {"model": "x", "messages": [], "tools": [{"name": "t"}]}
    with pytest.raises(ProtocolAwareRoutingError) as ei:
        MessagesToChatConverter().convert_request(public)
    assert ei.value.reason.value == "feature_unsupported"


def test_convert_response_maps_usage_and_finish_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PROTOCOL_CONVERSION_ENABLED", "true")
    clear_flag_cache()
    from shared_quota_router.conversion.adapters.messages_to_chat import (
        MessagesToChatConverter,
    )

    out = MessagesToChatConverter().convert_response(_load("response_usage.json"))
    assert out.payload["usage"] == {"input_tokens": 10, "output_tokens": 20}
    assert out.payload["stop_reason"] == "max_tokens"
    assert out.payload["content"][0]["text"] == "ok"


def test_convert_error_preserves_anthropic_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PROTOCOL_CONVERSION_ENABLED", "true")
    clear_flag_cache()
    from shared_quota_router.conversion.adapters.messages_to_chat import (
        MessagesToChatConverter,
    )

    err = MessagesToChatConverter().convert_error(_load("response_error.json"))
    assert err["type"] == "error"
    assert err["error"]["type"] == "invalid_request_error"
    assert "bad request" in err["error"]["message"]


def test_dispatch_convert_request_requires_flag() -> None:
    from shared_quota_router.conversion.contracts import DIRECTION_MESSAGES_TO_CHAT
    from shared_quota_router.conversion.dispatch import convert_public_request
    from shared_quota_router.protocol_errors import ProtocolAwareRoutingError

    with pytest.raises(ProtocolAwareRoutingError, match="disabled"):
        convert_public_request(
            _load("request_basic.json"), direction=DIRECTION_MESSAGES_TO_CHAT
        )


def test_strategy_convert_uses_native_without_g0b_rewrite(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P0-G0A：convert 走 LITELLM_NATIVE；不做项目 G0-B kwargs 改写。"""
    monkeypatch.setenv("PROTOCOL_CONVERSION_ENABLED", "true")
    monkeypatch.setenv("PROTOCOL_AWARE_GATEWAY_ENABLED", "true")
    from shared_quota_router.feature_flags import set_g0a_messages_mount_ready

    set_g0a_messages_mount_ready(False)
    try:
        import litellm

        monkeypatch.setattr(
            litellm, "use_chat_completions_url_for_anthropic_messages", True
        )
    except ImportError:
        monkeypatch.setenv(
            "LITELLM_USE_CHAT_COMPLETIONS_URL_FOR_ANTHROPIC_MESSAGES", "true"
        )
    clear_flag_cache()

    from shared_quota_router.conversion.dispatch import (
        CONVERSION_DIR_META_KEY,
        ROUTE_MODE_META_KEY,
    )
    from shared_quota_router.lease import LeaseManager
    from shared_quota_router.models import (
        ApiProtocol,
        LogicalModelProtocols,
    )
    from shared_quota_router.state_store import StateStore
    from shared_quota_router.strategy import (
        SharedQuotaRoutingStrategy,
        SharedQuotaSelector,
        model_list_to_registry,
    )

    class Mem:
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

        def sadd(self, *a, **k):
            return 1

        def smembers(self, name: str):
            return set()

        def eval(self, script, numkeys, *args):
            if numkeys == 3:
                self.incr(args[1])
                return [1, "1"]
            return 0

    store = StateStore(Mem())
    lease = LeaseManager(Mem())  # LeaseManager talks to redis client, not StateStore
    model_list = [
        {
            "model_name": "pilot",
            "model_info": {
                "deployment_id": "chat-convert",
                "provider_id": "p",
                "quota_group_id": "q1",
                "priority": 10,
                "enabled": True,
                "upstream_protocol": "openai_chat",
                "supported_features": ["text", "streaming", "tools"],
                "supports_streaming": True,
                "public_protocols": ["anthropic_messages", "openai_chat"],
                "conversions": [
                    {
                        "from": "anthropic_messages",
                        "to": "openai_chat",
                        "fidelity": "equivalent",
                        "streaming": False,
                        "features": {"request": ["text"], "response": ["text"]},
                    }
                ],
            },
            "litellm_params": {"model": "openai/pilot", "api_base": "http://x"},
        }
    ]

    class Router:
        def __init__(self, ml: list) -> None:
            self.model_list = ml

    strat = SharedQuotaRoutingStrategy(store=store, lease_manager=lease)
    strat.bind_router(Router(model_list))
    reg = model_list_to_registry(model_list)
    logical = LogicalModelProtocols(
        model_group="pilot",
        public_protocols=frozenset(
            {ApiProtocol.ANTHROPIC_MESSAGES, ApiProtocol.OPENAI_CHAT}
        ),
        allow_conversion=True,
        allowed_conversions=frozenset(
            {(ApiProtocol.ANTHROPIC_MESSAGES, ApiProtocol.OPENAI_CHAT)}
        ),
    )

    def _sel(_ml: list) -> SharedQuotaSelector:
        return SharedQuotaSelector(reg, store, lease, logical_models={"pilot": logical})

    monkeypatch.setattr(strat, "_selector_for", _sel)

    kwargs: dict = {
        "litellm_metadata": {"protocol": "anthropic_messages"},
        "model": "pilot",
        "system": "sys",
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 8,
    }
    entry = strat.get_available_deployment(
        model="pilot",
        messages=[{"role": "user", "content": "hi"}],
        request_kwargs=kwargs,
    )
    assert entry["model_info"]["deployment_id"] == "chat-convert"
    assert kwargs["litellm_metadata"][ROUTE_MODE_META_KEY] == "convert"
    assert (
        kwargs["litellm_metadata"][CONVERSION_DIR_META_KEY]
        == "anthropic_messages>openai_chat"
    )
    # native：保留 Anthropic system，不做 G0-B Chat 改写
    assert kwargs.get("system") == "sys"
    assert kwargs["messages"] == [{"role": "user", "content": "hi"}]

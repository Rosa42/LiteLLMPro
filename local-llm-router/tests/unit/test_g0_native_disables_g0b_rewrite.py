"""G0-Native: 禁用项目 G0-B 请求改写与成功响应 reshape。"""

from __future__ import annotations

from typing import Any, Optional

import pytest

from shared_quota_router.feature_flags import (
    clear_flag_cache,
    is_conversion_routing_active,
    is_native_messages_chat_path_active,
    set_g0a_messages_mount_ready,
)


@pytest.fixture(autouse=True)
def _reset_path_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    set_g0a_messages_mount_ready(False)
    monkeypatch.delenv(
        "LITELLM_USE_CHAT_COMPLETIONS_URL_FOR_ANTHROPIC_MESSAGES", raising=False
    )
    try:
        import litellm

        monkeypatch.setattr(
            litellm, "use_chat_completions_url_for_anthropic_messages", False
        )
    except ImportError:
        pass
    clear_flag_cache()
    yield
    set_g0a_messages_mount_ready(False)
    clear_flag_cache()


def _enable_native(monkeypatch: pytest.MonkeyPatch) -> None:
    import litellm

    monkeypatch.setattr(litellm, "use_chat_completions_url_for_anthropic_messages", True)
    clear_flag_cache()


def test_native_flag_reads_litellm_attr(monkeypatch: pytest.MonkeyPatch) -> None:
    assert is_native_messages_chat_path_active() is False
    _enable_native(monkeypatch)
    assert is_native_messages_chat_path_active() is True


def test_conversion_routing_requires_path_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PROTOCOL_AWARE_GATEWAY_ENABLED", "true")
    monkeypatch.setenv("PROTOCOL_CONVERSION_ENABLED", "true")
    clear_flag_cache()
    assert is_conversion_routing_active() is False

    _enable_native(monkeypatch)
    assert is_conversion_routing_active() is True


def test_conversion_routing_ignores_g0a_mount_without_native(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P0-G0A：g0a_mount 不计入 Messages→Chat path ready；仅 native 可激活。"""
    monkeypatch.setenv("PROTOCOL_AWARE_GATEWAY_ENABLED", "true")
    monkeypatch.setenv("PROTOCOL_CONVERSION_ENABLED", "true")
    set_g0a_messages_mount_ready(True)
    clear_flag_cache()
    assert is_native_messages_chat_path_active() is False
    assert is_conversion_routing_active() is False


class Mem:
    def __init__(self) -> None:
        self.data: dict[str, str] = {}

    def get(self, name: str) -> Optional[str]:
        return self.data.get(name)

    def set(self, name: str, value: Any, ex: Any = None, nx: bool = False) -> bool:
        self.data[name] = value if isinstance(value, str) else str(value)
        return True

    def delete(self, *names: str) -> None:
        for n in names:
            self.data.pop(n, None)

    def incr(self, name: str) -> int:
        v = int(self.data.get(name, "0")) + 1
        self.data[name] = str(v)
        return v

    def decr(self, name: str) -> int:
        v = int(self.data.get(name, "0")) - 1
        self.data[name] = str(v)
        return v

    def expire(self, name: str, time: int) -> bool:
        return True

    def eval(self, script: str, numkeys: int, *keys_and_args: Any) -> Any:
        if numkeys == 3:
            self.incr(keys_and_args[1])
            self.set(
                keys_and_args[2],
                keys_and_args[5] if len(keys_and_args) > 5 else "1",
            )
            return [1, "1"]
        if numkeys == 2:
            self.delete(keys_and_args[1])
            return 1
        return 0

    def sadd(self, *a: Any, **k: Any) -> int:
        return 1

    def smembers(self, name: str) -> set:
        return set()


def test_strategy_skips_g0b_rewrite_when_native_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PROTOCOL_AWARE_GATEWAY_ENABLED", "true")
    monkeypatch.setenv("PROTOCOL_CONVERSION_ENABLED", "true")
    _enable_native(monkeypatch)

    from shared_quota_router.conversion.dispatch import ROUTE_MODE_META_KEY
    from shared_quota_router.lease import LeaseManager
    from shared_quota_router.models import ApiProtocol, LogicalModelProtocols
    from shared_quota_router.state_store import StateStore
    from shared_quota_router.strategy import (
        SharedQuotaRoutingStrategy,
        SharedQuotaSelector,
        model_list_to_registry,
    )

    store = StateStore(Mem())
    lease = LeaseManager(Mem())
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
    # 原生路径：保留 Anthropic system，不做项目 C2 Chat 改写
    assert kwargs.get("system") == "sys"
    assert kwargs["messages"] == [{"role": "user", "content": "hi"}]


def test_callback_skips_reshape_when_native_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_native(monkeypatch)
    from shared_quota_router.callbacks import SharedQuotaCallback

    cb = SharedQuotaCallback.__new__(SharedQuotaCallback)
    chat_shaped = {
        "id": "chatcmpl-1",
        "choices": [
            {
                "message": {"role": "assistant", "content": "hi"},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
    }
    data = {
        "metadata": {
            "shared_quota_route_mode": "convert",
            "shared_quota_conversion": "anthropic_messages>openai_chat",
        }
    }
    out = cb._maybe_convert_success_response(data, chat_shaped)
    assert out is chat_shaped

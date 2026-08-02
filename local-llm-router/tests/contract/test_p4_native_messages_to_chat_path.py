"""P4-Native: stock Messages path + LiteLLM switch → /chat/completions."""

from __future__ import annotations

import os
import sys
import threading
from http.server import ThreadingHTTPServer
from typing import Any, Optional

import pytest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_PLUGINS = os.path.join(_ROOT, "plugins")
if _PLUGINS not in sys.path:
    sys.path.insert(0, _PLUGINS)

pytest.importorskip("litellm", reason="litellm required for P4-Native probe")
import litellm  # noqa: E402
from litellm import Router  # noqa: E402

from shared_quota_router.bootstrap import register  # noqa: E402
from shared_quota_router.feature_flags import (  # noqa: E402
    clear_flag_cache,
    set_g0a_messages_mount_ready,
)
from shared_quota_router.lease import LeaseManager  # noqa: E402
from shared_quota_router.mock_provider import MockHandler  # noqa: E402
from shared_quota_router.models import (  # noqa: E402
    ApiProtocol,
    LogicalModelProtocols,
)
from shared_quota_router.state_store import StateStore  # noqa: E402
from shared_quota_router.strategy import SharedQuotaRoutingStrategy  # noqa: E402

FAKE_KEY = "fake-key-not-a-secret"
PROBE = "x"


class MemRedis:
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


@pytest.fixture(scope="module")
def mock_base() -> str:
    server = ThreadingHTTPServer(("127.0.0.1", 0), MockHandler)
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{port}"
    server.shutdown()


@pytest.fixture(autouse=True)
def _reset_native(monkeypatch: pytest.MonkeyPatch) -> None:
    set_g0a_messages_mount_ready(False)
    monkeypatch.setattr(
        litellm, "use_chat_completions_url_for_anthropic_messages", False
    )
    clear_flag_cache()
    yield
    set_g0a_messages_mount_ready(False)
    monkeypatch.setattr(
        litellm, "use_chat_completions_url_for_anthropic_messages", False
    )
    clear_flag_cache()


def _last_path() -> str:
    assert MockHandler.last_requests, "mock recorded no requests"
    return str(MockHandler.last_requests[-1]["path"])


def _convert_model_list(mock_base: str) -> list[dict[str, Any]]:
    return [
        {
            "model_name": "pilot",
            "model_info": {
                "id": "chat-convert",
                "deployment_id": "chat-convert",
                "provider_id": "mock",
                "quota_group_id": "q-convert",
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
            "litellm_params": {
                "model": "openai/pilot",
                "api_base": mock_base,
                "api_key": FAKE_KEY,
            },
        }
    ]


def _logical() -> dict[str, LogicalModelProtocols]:
    return {
        "pilot": LogicalModelProtocols(
            model_group="pilot",
            public_protocols=frozenset(
                {ApiProtocol.ANTHROPIC_MESSAGES, ApiProtocol.OPENAI_CHAT}
            ),
            allow_conversion=True,
            allowed_conversions=frozenset(
                {(ApiProtocol.ANTHROPIC_MESSAGES, ApiProtocol.OPENAI_CHAT)}
            ),
        )
    }


@pytest.mark.asyncio
async def test_p4_native_messages_openai_hits_chat_completions(
    mock_base: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Native switch + project strategy: Messages convert → /chat/completions."""
    monkeypatch.setenv("PROTOCOL_AWARE_GATEWAY_ENABLED", "true")
    monkeypatch.setenv("PROTOCOL_CONVERSION_ENABLED", "true")
    monkeypatch.setattr(
        litellm, "use_chat_completions_url_for_anthropic_messages", True
    )
    clear_flag_cache()
    MockHandler.last_requests.clear()

    redis = MemRedis()
    strategy = SharedQuotaRoutingStrategy(
        store=StateStore(redis),
        lease_manager=LeaseManager(redis),
        logical_models=_logical(),
    )
    router = Router(model_list=_convert_model_list(mock_base), set_verbose=False)
    register(router, strategy=strategy)

    resp = await router.aanthropic_messages(
        model="pilot",
        messages=[{"role": "user", "content": PROBE}],
        max_tokens=16,
        litellm_call_id="p4-native-1",
        litellm_metadata={"protocol": "anthropic_messages"},
    )

    path = _last_path()
    assert "/chat/completions" in path, f"expected chat path, got {path!r}"
    assert "/responses" not in path

    payload = resp if isinstance(resp, dict) else getattr(resp, "model_dump", lambda: {})()
    if not isinstance(payload, dict) and hasattr(resp, "json"):
        payload = resp.json()
    # Native adapter should yield Anthropic-shaped message (type=message) when successful
    assert isinstance(payload, dict)
    assert payload.get("type") == "message" or "content" in payload or "choices" in payload


@pytest.mark.asyncio
async def test_p4_native_direct_anthropic_still_messages(
    mock_base: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Native switch on must not break verified anthropic/ upstream."""
    monkeypatch.setenv("PROTOCOL_AWARE_GATEWAY_ENABLED", "true")
    monkeypatch.setenv("PROTOCOL_CONVERSION_ENABLED", "true")
    monkeypatch.setattr(
        litellm, "use_chat_completions_url_for_anthropic_messages", True
    )
    clear_flag_cache()
    MockHandler.last_requests.clear()

    model_list = [
        {
            "model_name": "pilot",
            "model_info": {
                "id": "msg-direct",
                "deployment_id": "msg-direct",
                "provider_id": "mock",
                "quota_group_id": "q-msg",
                "priority": 10,
                "enabled": True,
                "upstream_protocol": "anthropic_messages",
                "supported_features": ["text", "streaming", "tools"],
                "supports_streaming": True,
                "public_protocols": ["anthropic_messages"],
            },
            "litellm_params": {
                "model": "anthropic/pilot",
                "api_base": mock_base,
                "api_key": FAKE_KEY,
            },
        }
    ]
    redis = MemRedis()
    strategy = SharedQuotaRoutingStrategy(
        store=StateStore(redis),
        lease_manager=LeaseManager(redis),
        logical_models={
            "pilot": LogicalModelProtocols(
                model_group="pilot",
                public_protocols=frozenset({ApiProtocol.ANTHROPIC_MESSAGES}),
                allow_conversion=False,
                allowed_conversions=frozenset(),
            )
        },
    )
    router = Router(model_list=model_list, set_verbose=False)
    register(router, strategy=strategy)

    try:
        await router.aanthropic_messages(
            model="pilot",
            messages=[{"role": "user", "content": PROBE}],
            max_tokens=16,
            litellm_call_id="p4-native-direct",
            litellm_metadata={"protocol": "anthropic_messages"},
        )
    except Exception:
        pass

    path = _last_path()
    assert "/messages" in path, f"expected Anthropic /messages, got {path!r}"
    assert "/chat/completions" not in path

"""M1: Responses public → Chat via LiteLLM native bridge."""

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

pytest.importorskip("litellm", reason="litellm required for M1 Responses bridge")
from litellm import Router  # noqa: E402

from shared_quota_router.bootstrap import register  # noqa: E402
from shared_quota_router.feature_flags import clear_flag_cache  # noqa: E402
from shared_quota_router.lease import LeaseManager  # noqa: E402
from shared_quota_router.mock_provider import MockHandler  # noqa: E402
from shared_quota_router.models import (  # noqa: E402
    ApiProtocol,
    LogicalModelProtocols,
)
from shared_quota_router.state_store import StateStore  # noqa: E402
from shared_quota_router.strategy import SharedQuotaRoutingStrategy  # noqa: E402

FAKE_KEY = "fake-key-not-a-secret"


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


def _last_path() -> str:
    assert MockHandler.last_requests, "mock recorded no requests"
    return str(MockHandler.last_requests[-1]["path"])


@pytest.mark.asyncio
async def test_m1_responses_glm_hits_chat_completions_via_native_bridge(
    mock_base: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PROTOCOL_AWARE_GATEWAY_ENABLED", "true")
    monkeypatch.setenv("PROTOCOL_CONVERSION_ENABLED", "true")
    monkeypatch.setenv("SHARED_QUOTA_ENV_PROFILE", "staging")
    clear_flag_cache()
    MockHandler.last_requests.clear()

    model_list = [
        {
            "model_name": "glm-5.2",
            "model_info": {
                "id": "chat-glm",
                "deployment_id": "chat-glm",
                "provider_id": "mock",
                "quota_group_id": "q-glm",
                "priority": 10,
                "enabled": True,
                "upstream_protocol": "openai_chat",
                "supported_features": ["text", "streaming", "tools"],
                "supports_streaming": True,
                "public_protocols": ["openai_chat", "openai_responses"],
            },
            "litellm_params": {
                "model": "openai/glm-5.2",
                "api_base": mock_base,
                "api_key": FAKE_KEY,
                "use_chat_completions_api": True,
            },
        }
    ]
    logical = {
        "glm-5.2": LogicalModelProtocols(
            model_group="glm-5.2",
            public_protocols=frozenset(
                {ApiProtocol.OPENAI_CHAT, ApiProtocol.OPENAI_RESPONSES}
            ),
            allow_conversion=True,
            allowed_conversions=frozenset(
                {(ApiProtocol.OPENAI_RESPONSES, ApiProtocol.OPENAI_CHAT)}
            ),
        )
    }
    redis = MemRedis()
    strategy = SharedQuotaRoutingStrategy(
        store=StateStore(redis),
        lease_manager=LeaseManager(redis),
        logical_models=logical,
    )
    router = Router(model_list=model_list, set_verbose=False, num_retries=0)
    register(router, strategy=strategy)

    resp = await router.aresponses(
        model="glm-5.2",
        input="hello",
        litellm_call_id="m1-responses-1",
        litellm_metadata={"protocol": "openai_responses"},
        metadata={"protocol": "openai_responses"},
    )

    path = _last_path()
    assert "/chat/completions" in path, f"expected chat completions, got {path!r}"
    assert "/responses" not in path or path.rstrip("/").endswith("chat/completions")

    payload = resp if isinstance(resp, dict) else None
    if payload is None and hasattr(resp, "model_dump"):
        payload = resp.model_dump()
    assert isinstance(payload, dict)
    # Native ResponsesAPIResponse shape
    assert payload.get("object") == "response" or "output" in payload or "id" in payload

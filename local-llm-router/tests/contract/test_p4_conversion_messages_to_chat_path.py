"""P4-01：Messages→Chat path probe（P0-G0A native-only）。

g0a mount  alone 不得激活 convert；正向路径见 native contract 测。
"""

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

pytest.importorskip("litellm", reason="litellm required for P4 conversion probe")
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


@pytest.mark.asyncio
async def test_p4_01_g0a_alone_does_not_activate_messages_chat_convert(
    mock_base: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P0-G0A：g0a mount 不计入 path ready；native off 时 convert 不激活（无 ADAPTER 回退）。

    历史曾用 g0a 复现 G0-B 误路由 /responses；本期该路径关闭。
    正向路径见 ``test_p4_native_*``。
    """
    import litellm

    monkeypatch.setenv("PROTOCOL_AWARE_GATEWAY_ENABLED", "true")
    monkeypatch.setenv("PROTOCOL_CONVERSION_ENABLED", "true")
    monkeypatch.setattr(
        litellm, "use_chat_completions_url_for_anthropic_messages", False
    )
    from shared_quota_router.feature_flags import set_g0a_messages_mount_ready

    set_g0a_messages_mount_ready(True)
    clear_flag_cache()
    MockHandler.last_requests.clear()

    model_list = [
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
    logical = {
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
    redis = MemRedis()
    strategy = SharedQuotaRoutingStrategy(
        store=StateStore(redis),
        lease_manager=LeaseManager(redis),
        logical_models=logical,
    )
    router = Router(model_list=model_list, set_verbose=False)
    register(router, strategy=strategy)

    with pytest.raises(Exception):
        await router.aanthropic_messages(
            model="pilot",
            messages=[{"role": "user", "content": PROBE}],
            max_tokens=16,
            litellm_call_id="p4-01-convert",
            litellm_metadata={"protocol": "anthropic_messages"},
        )

    assert MockHandler.last_requests == [], (
        "native off + g0a only 不得打到 upstream（无 convert 候选）"
    )

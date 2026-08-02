"""P1-A5：POST /v1/messages stream convert 拒绝须走 Anthropic wire（非 helper）。

模拟 LiteLLM anthropic_endpoints 将异常剥成 ProxyException + OpenAI
``{"error":...}`` 的路径；断言插件 ``mount_anthropic_wire_guard`` 后仍满足
§8.1：HTTP 400 + 顶层 ``{"type":"error","error":{"type":"invalid_request_error",...}}``。
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any, Optional

import pytest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_PLUGINS = os.path.join(_ROOT, "plugins")
if _PLUGINS not in sys.path:
    sys.path.insert(0, _PLUGINS)

pytest.importorskip("fastapi", reason="ASGI wire 测需要 fastapi")
from fastapi import FastAPI, Request  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from shared_quota_router.anthropic_wire import (  # noqa: E402
    mount_anthropic_wire_guard,
    reset_mount_state_for_tests,
)
from shared_quota_router.callbacks import SharedQuotaCallback  # noqa: E402
from shared_quota_router.feature_flags import (  # noqa: E402
    clear_flag_cache,
    set_g0a_messages_mount_ready,
)
from shared_quota_router.lease import LeaseManager  # noqa: E402
from shared_quota_router.models import (  # noqa: E402
    ApiProtocol,
    Deployment,
    Feature,
    LogicalModelProtocols,
)
from shared_quota_router.registry import DeploymentRegistry  # noqa: E402
from shared_quota_router.state_store import StateStore  # noqa: E402


class MemRedis:
    def __init__(self) -> None:
        self.data: dict[str, str] = {}
        self.incr_calls = 0

    def get(self, name: str) -> Optional[str]:
        return self.data.get(name)

    def set(self, name: str, value: Any, ex: Any = None, nx: bool = False) -> bool:
        self.data[name] = value if isinstance(value, str) else str(value)
        return True

    def delete(self, *names: str) -> None:
        for n in names:
            self.data.pop(n, None)

    def incr(self, name: str) -> int:
        self.incr_calls += 1
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


@pytest.fixture(autouse=True)
def _native_convert_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    """Messages→Chat native 开着；仍禁止 S1a 真流量（仅测本地 ASGI）。"""
    set_g0a_messages_mount_ready(False)
    monkeypatch.setenv("PROTOCOL_CONVERSION_ENABLED", "true")
    monkeypatch.setenv("PROTOCOL_AWARE_GATEWAY_ENABLED", "true")
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
    yield
    set_g0a_messages_mount_ready(False)
    clear_flag_cache()


def _convert_only_registry() -> DeploymentRegistry:
    """kimi 仅 Chat upstream + anthropic public + convert policy（无 direct Messages）。"""
    reg = DeploymentRegistry()
    reg.add(
        Deployment(
            deployment_id="opencode-a-chat-kimi-k3",
            model_group="kimi-k3",
            upstream_model="openai/kimi-k3",
            provider_id="opencode-go",
            quota_group_id="opencode-a",
            priority=10,
            enabled=True,
            upstream_protocol=ApiProtocol.OPENAI_CHAT,
            supported_features=frozenset({Feature.TEXT}),
            supports_streaming=False,
            public_protocols=frozenset({ApiProtocol.ANTHROPIC_MESSAGES}),
            conversions=(),
        )
    )
    return reg


def _logical() -> dict[str, LogicalModelProtocols]:
    return {
        "kimi-k3": LogicalModelProtocols(
            model_group="kimi-k3",
            public_protocols=frozenset({ApiProtocol.ANTHROPIC_MESSAGES}),
            allow_conversion=True,
            allowed_conversions=frozenset(
                {(ApiProtocol.ANTHROPIC_MESSAGES, ApiProtocol.OPENAI_CHAT)}
            ),
        )
    }


def _build_asgi_app(cb: SharedQuotaCallback) -> FastAPI:
    """模拟 LiteLLM：异常 → ProxyException → OpenAI ``{"error":...}`` handler。"""
    app = FastAPI()

    try:
        from litellm.proxy._types import ProxyException
    except ImportError:  # pragma: no cover
        # 无 litellm 时用最小 stub，仍覆盖「剥 detail → OpenAI envelope」形态
        class ProxyException(Exception):  # type: ignore[no-redef]
            def __init__(
                self,
                message: str,
                type: str,
                param: Any,
                code: Any = None,
                headers: Any = None,
                **_k: Any,
            ) -> None:
                self.message = str(message)
                super().__init__(self.message)
                self.type = type
                self.param = param
                self.code = str(code) if code is not None else "500"
                self.headers = headers or {}

            def to_dict(self) -> dict[str, Any]:
                return {
                    "message": self.message,
                    "type": self.type,
                    "param": self.param,
                    "code": self.code,
                }

    @app.exception_handler(ProxyException)
    async def openai_exception_handler(request: Request, exc: ProxyException):
        # 与 litellm.proxy.proxy_server.openai_exception_handler 同形
        status_code = int(exc.code) if getattr(exc, "code", None) else 500
        return JSONResponse(
            status_code=status_code,
            content={"error": exc.to_dict()},
            headers=getattr(exc, "headers", None) or {},
        )

    @app.post("/v1/messages")
    async def anthropic_like_endpoint(request: Request):
        data = await request.json()
        try:
            await cb.async_pre_call_hook(data=data, call_type="anthropic_messages")
            return {"type": "message", "role": "assistant", "content": []}
        except Exception as e:
            # 模拟 anthropic_endpoints：post_call 返回值被忽略，剥成 ProxyException
            raise ProxyException(
                message=getattr(e, "message", str(e)),
                type=getattr(e, "type", "None"),
                param=getattr(e, "param", "None"),
                code=getattr(e, "status_code", 500),
            )

    # 插件侧保证 Anthropic wire（不改 upstream 业务）
    reset_mount_state_for_tests()
    assert mount_anthropic_wire_guard(app) is True
    return app


def test_p1_a5_stream_convert_rejected_via_v1_messages_wire(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mem = MemRedis()
    store = StateStore(mem)
    lease = LeaseManager(mem)
    cb = SharedQuotaCallback(store=store, lease_manager=lease)
    cb.bind_registry(_convert_only_registry())
    monkeypatch.setattr(
        "shared_quota_router.logical_policy.resolve_runtime_logical_models",
        _logical,
    )

    client = TestClient(_build_asgi_app(cb))
    before_keys = set(mem.data.keys())
    before_incr = mem.incr_calls

    resp = client.post(
        "/v1/messages",
        json={
            "model": "kimi-k3",
            "max_tokens": 16,
            "stream": True,
            "messages": [{"role": "user", "content": "hi"}],
        },
    )

    assert resp.status_code == 400, resp.text
    assert "application/json" in (resp.headers.get("content-type") or "")
    body = resp.json()
    # §8.1 Anthropic 顶层 envelope；禁止 OpenAI ``{"error":...}`` 顶层
    assert body.get("type") == "error"
    assert "choices" not in body
    assert isinstance(body.get("error"), dict)
    assert body["error"].get("type") == "invalid_request_error"
    msg = str(body["error"].get("message") or "").lower()
    assert "stream" in msg
    assert ("unsupported" in msg) or ("conversion" in msg)

    # A5：租约 / inflight 无增量
    assert mem.incr_calls == before_incr
    assert not any("lease" in k or "inflight" in k for k in mem.data.keys() - before_keys)


def test_p1_a5_helper_green_alone_is_not_wire_proof() -> None:
    """保留 helper 形态断言，但明确不足以替代 /v1/messages wire。"""
    from shared_quota_router.protocol_errors import (
        ProtocolAwareRoutingError,
        ProtocolRoutingReason,
    )
    from shared_quota_router.models import ApiProtocol

    err = ProtocolAwareRoutingError(
        "streaming conversion is unsupported for model 'kimi-k3'",
        reason=ProtocolRoutingReason.FEATURE_UNSUPPORTED,
        protocol=ApiProtocol.ANTHROPIC_MESSAGES,
        model_group="kimi-k3",
    )
    helper = err.to_anthropic_error()
    assert helper["type"] == "error"
    assert helper["error"]["type"] == "invalid_request_error"
    # 本文件另一测才是 A5 wire 验收

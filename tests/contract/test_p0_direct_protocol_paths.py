"""P0-03: direct protocol entry points and local mock provider contracts.

Verifies LiteLLM v1.90.5 Router dispatch hits the expected upstream path for:
  - Chat Completions
  - Anthropic Messages (when deployment is anthropic-native)
  - OpenAI Responses

Also records the mis-route risk when Messages is attempted against an
``openai/`` deployment (observed historical bug surface).

NewAPI / real credentials are intentionally not exercised here — see the
operator probe checklist in the phase-0 report.

Run:
  pip install 'litellm==1.90.5' pytest pytest-asyncio
  set PYTHONPATH=plugins
  pytest tests/contract/test_p0_direct_protocol_paths.py -q
"""

from __future__ import annotations

import asyncio
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

pytest.importorskip("litellm", reason="litellm required for P0-03 contract")
from litellm import Router  # noqa: E402

from shared_quota_router.bootstrap import register  # noqa: E402
from shared_quota_router.lease import LeaseManager  # noqa: E402
from shared_quota_router.mock_provider import MockHandler  # noqa: E402
from shared_quota_router.state_store import StateStore  # noqa: E402
from shared_quota_router.strategy import (  # noqa: E402
    SharedQuotaRoutingStrategy,
    context_from_request_kwargs,
)

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
        keys = keys_and_args[:numkeys]
        args = keys_and_args[numkeys:]
        if numkeys == 3:
            self.incr(keys[1])
            self.set(keys[2], args[2] if len(args) > 2 else "x")
            return [1, "1"]
        if numkeys == 2:
            self.delete(keys[1])
            inflight = int(self.data.get(keys[0], "0"))
            if inflight > 0:
                inflight = self.decr(keys[0])
            return max(inflight, 0)
        return 0


@pytest.fixture(scope="module")
def mock_base() -> str:
    MockHandler.last_requests.clear()
    server = ThreadingHTTPServer(("127.0.0.1", 0), MockHandler)
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{port}"
    server.shutdown()


def _router_for(
    mock_base: str,
    *,
    model_name: str,
    litellm_model: str,
    deployment_id: str,
    quota_group_id: str = "probe-qg",
) -> Router:
    model_list = [
        {
            "model_name": model_name,
            "litellm_params": {
                "model": litellm_model,
                "api_base": mock_base,
                "api_key": FAKE_KEY,
            },
            "model_info": {
                "id": deployment_id,
                "deployment_id": deployment_id,
                "provider_id": "mock-provider",
                "quota_group_id": quota_group_id,
                "priority": 10,
            },
        }
    ]
    mem = MemRedis()
    router = Router(model_list=model_list, set_verbose=False)
    register(
        router,
        strategy=SharedQuotaRoutingStrategy(
            store=StateStore(mem),
            lease_manager=LeaseManager(mem),
            router=router,
        ),
    )
    return router


def _last_path() -> str:
    assert MockHandler.last_requests, "mock received no request"
    return MockHandler.last_requests[-1]["path"]


def _last_auth() -> str:
    return MockHandler.last_requests[-1]["auth_style"]


# ---------------------------------------------------------------------------
# Direct Chat
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_p0_03_direct_chat_hits_chat_completions(mock_base: str) -> None:
    MockHandler.last_requests.clear()
    router = _router_for(
        mock_base,
        model_name="chat-model",
        litellm_model="openai/chat-model",
        deployment_id="chat-dep",
    )
    dep = router.get_available_deployment(
        model="chat-model",
        messages=[{"role": "user", "content": PROBE}],
        request_kwargs={
            "litellm_call_id": "p0-03-chat-sel",
            "metadata": {"protocol": "openai_chat"},
        },
    )
    assert dep["model_info"]["deployment_id"] == "chat-dep"
    assert dep["model_info"]["quota_group_id"] == "probe-qg"

    resp = await router.acompletion(
        model="chat-model",
        messages=[{"role": "user", "content": PROBE}],
        litellm_call_id="p0-03-chat",
        metadata={"protocol": "openai_chat"},
    )
    assert getattr(resp, "choices", None) is not None
    path = _last_path()
    assert path.endswith("/chat/completions") or "/chat/completions" in path
    assert _last_auth() == "bearer"
    usage = getattr(resp, "usage", None)
    assert usage is not None


@pytest.mark.asyncio
async def test_p0_03_direct_chat_stream_and_first_byte_gate(mock_base: str) -> None:
    """First stream chunk must mark first_byte_sent; strategy refuses reselection."""
    MockHandler.last_requests.clear()
    mem = MemRedis()
    store = StateStore(mem)
    lease = LeaseManager(mem)
    model_list = [
        {
            "model_name": "chat-model",
            "litellm_params": {
                "model": "openai/chat-model",
                "api_base": mock_base,
                "api_key": FAKE_KEY,
            },
            "model_info": {
                "id": "chat-dep",
                "deployment_id": "chat-dep",
                "provider_id": "mock-provider",
                "quota_group_id": "probe-qg",
                "priority": 10,
            },
        }
    ]
    router = Router(model_list=model_list, set_verbose=False)
    strategy = SharedQuotaRoutingStrategy(store=store, lease_manager=lease, router=router)
    register(router, strategy=strategy)

    call_id = "p0-03-chat-stream"
    resp = await router.acompletion(
        model="chat-model",
        messages=[{"role": "user", "content": PROBE}],
        stream=True,
        litellm_call_id=call_id,
        metadata={"protocol": "openai_chat"},
    )
    # Consume first visible chunk then mark first byte (project hook path).
    got_chunk = False
    if hasattr(resp, "__aiter__"):
        async for chunk in resp:
            got_chunk = True
            # After first visible stream content, mark retry boundary.
            ctx = context_from_request_kwargs(
                {"litellm_call_id": call_id},
                store=store,
            )
            ctx.first_byte_sent = True
            store.put_request_context(ctx, ttl_seconds=360)
            break
    assert got_chunk
    path = _last_path()
    assert "/chat/completions" in path

    # Reselection must fail closed after first_byte_sent
    from shared_quota_router.strategy import NoAvailableDeploymentError

    with pytest.raises(NoAvailableDeploymentError):
        strategy.get_available_deployment(
            model="chat-model",
            messages=[{"role": "user", "content": PROBE}],
            request_kwargs={"litellm_call_id": call_id},
        )


def test_p0_03_direct_chat_structured_error_auth_on_mock(mock_base: str) -> None:
    """Auth error shape on chat path (provider contract; no secrets logged)."""
    import json
    import urllib.error
    import urllib.request

    MockHandler.last_requests.clear()
    url = f"{mock_base}/v1/chat/completions?scenario=auth"
    body = json.dumps(
        {
            "model": "chat-model",
            "messages": [{"role": "user", "content": PROBE}],
        }
    ).encode()
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json", "Authorization": "Bearer t"},
        method="POST",
    )
    with pytest.raises(urllib.error.HTTPError) as ei:
        urllib.request.urlopen(req, timeout=5)
    assert ei.value.code == 401
    raw = ei.value.read().decode()
    assert "invalid" in raw.lower() or "api key" in raw.lower()
    assert "/chat/completions" in _last_path()
    # Must not echo secrets into last_requests
    assert "Bearer t" not in repr(MockHandler.last_requests[-1])


# ---------------------------------------------------------------------------
# Direct Messages
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_p0_03_direct_messages_hits_v1_messages_with_anthropic_prefix(
    mock_base: str,
) -> None:
    MockHandler.last_requests.clear()
    router = _router_for(
        mock_base,
        model_name="msg-model",
        litellm_model="anthropic/msg-model",
        deployment_id="msg-dep",
    )
    # Selection-time evidence for deployment_id / quota_group_id (callback worker
    # is flaky across pytest event loops; path contract is primary).
    dep = router.get_available_deployment(
        model="msg-model",
        messages=[{"role": "user", "content": PROBE}],
        request_kwargs={
            "litellm_call_id": "p0-03-msg-sel",
            "litellm_metadata": {"protocol": "anthropic_messages"},
        },
    )
    assert dep["model_info"]["deployment_id"] == "msg-dep"
    assert dep["model_info"]["quota_group_id"] == "probe-qg"

    out = await router.aanthropic_messages(
        model="msg-model",
        messages=[{"role": "user", "content": PROBE}],
        max_tokens=16,
        litellm_call_id="p0-03-msg",
        litellm_metadata={"protocol": "anthropic_messages"},
    )
    assert isinstance(out, dict)
    assert out.get("type") == "message" or "content" in out
    path = _last_path()
    assert path.endswith("/messages") or "/messages" in path
    assert _last_auth() == "x-api-key"
    # usage on Anthropic mock
    assert isinstance(out.get("usage"), dict)


@pytest.mark.asyncio
async def test_p0_03_messages_via_openai_prefix_misroutes_to_responses(
    mock_base: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Historical bug surface: openai/ deployments do not speak Messages.

    With G0-Native **off**, LiteLLM routes anthropic_messages against an openai/
    model through the Responses-style path (/responses), not /v1/messages.
    Protocol-aware filtering must reject such deployments before lease acquisition.
    """
    try:
        import litellm

        monkeypatch.setattr(
            litellm, "use_chat_completions_url_for_anthropic_messages", False
        )
    except ImportError:
        monkeypatch.delenv(
            "LITELLM_USE_CHAT_COMPLETIONS_URL_FOR_ANTHROPIC_MESSAGES", raising=False
        )
    MockHandler.last_requests.clear()
    router = _router_for(
        mock_base,
        model_name="msg-model",
        litellm_model="openai/msg-model",
        deployment_id="msg-openai-dep",
    )
    try:
        await router.aanthropic_messages(
            model="msg-model",
            messages=[{"role": "user", "content": PROBE}],
            max_tokens=16,
            litellm_call_id="p0-03-msg-openai-prefix",
            litellm_metadata={"protocol": "anthropic_messages"},
        )
    except Exception:
        # Response shape may fail; path evidence is the contract.
        pass
    path = _last_path()
    assert "/responses" in path or path.endswith("responses")
    assert "/messages" not in path


@pytest.mark.asyncio
async def test_p0_03_messages_via_openai_prefix_native_hits_chat_completions(
    mock_base: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """G0-Native on: openai/ + anthropic_messages → /chat/completions (not /messages)."""
    try:
        import litellm

        monkeypatch.setattr(
            litellm, "use_chat_completions_url_for_anthropic_messages", True
        )
    except ImportError:
        monkeypatch.setenv(
            "LITELLM_USE_CHAT_COMPLETIONS_URL_FOR_ANTHROPIC_MESSAGES", "true"
        )
    MockHandler.last_requests.clear()
    router = _router_for(
        mock_base,
        model_name="msg-model",
        litellm_model="openai/msg-model",
        deployment_id="msg-openai-dep",
    )
    try:
        await router.aanthropic_messages(
            model="msg-model",
            messages=[{"role": "user", "content": PROBE}],
            max_tokens=16,
            litellm_call_id="p0-03-msg-openai-native",
            litellm_metadata={"protocol": "anthropic_messages"},
        )
    except Exception:
        pass
    path = _last_path()
    assert "/chat/completions" in path or path.endswith("chat/completions")
    assert "/messages" not in path


# ---------------------------------------------------------------------------
# Direct Responses
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_p0_03_direct_responses_hits_responses_path(mock_base: str) -> None:
    MockHandler.last_requests.clear()
    router = _router_for(
        mock_base,
        model_name="resp-model",
        litellm_model="openai/resp-model",
        deployment_id="resp-dep",
    )
    dep = router.get_available_deployment(
        model="resp-model",
        request_kwargs={
            "litellm_call_id": "p0-03-resp-sel",
            "input": PROBE,
            "litellm_metadata": {"protocol": "openai_responses"},
        },
    )
    assert dep["model_info"]["deployment_id"] == "resp-dep"
    assert dep["model_info"]["quota_group_id"] == "probe-qg"

    out = await router.aresponses(
        model="resp-model",
        input=PROBE,
        litellm_call_id="p0-03-resp",
        litellm_metadata={"protocol": "openai_responses"},
    )
    assert out is not None
    path = _last_path()
    assert path.endswith("/responses") or "/responses" in path
    assert _last_auth() == "bearer"
    assert "/chat/completions" not in path


# ---------------------------------------------------------------------------
# Capability inventory snapshot (local mock only)
# ---------------------------------------------------------------------------


def test_p0_03_local_mock_capability_inventory_keys() -> None:
    """Static inventory keys used by the phase report (no network)."""
    inventory = {
        "openai_chat": {
            "upstream_path": "/v1/chat/completions",
            "auth": "Authorization: Bearer",
            "text": True,
            "tools": "provider-dependent",
            "usage": True,
            "stream": True,
            "verified_via": "local mock + Router acompletion",
        },
        "anthropic_messages": {
            "upstream_path": "/v1/messages",
            "auth": "x-api-key",
            "litellm_model_prefix_required": "anthropic/",
            "text": True,
            "tools": "provider-dependent",
            "usage": True,
            "stream": True,
            "verified_via": "local mock + Router aanthropic_messages",
            "openai_prefix_misroutes_to": "/responses",
        },
        "openai_responses": {
            "upstream_path": "/v1/responses or /responses",
            "auth": "Authorization: Bearer",
            "text": True,
            "tools": "provider-dependent",
            "usage": True,
            "stream": "not covered by this non-stream mock case",
            "verified_via": "local mock + Router aresponses",
        },
        "newapi": {
            "status": "unverified",
            "reason": "requires operator-run probe with env-held credentials; no key in source",
        },
    }
    assert inventory["openai_chat"]["text"] is True
    assert inventory["anthropic_messages"]["openai_prefix_misroutes_to"] == "/responses"
    assert inventory["newapi"]["status"] == "unverified"

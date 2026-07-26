"""P0-02: protocol metadata propagation contract harness (LiteLLM v1.90.5).

Proves whether an injected protocol string reaches:
  - custom strategy ``request_kwargs`` at selection time
  - success / failure / stream CustomLogger callbacks

Boundary under test: Router call paths that mirror public endpoints
  - Chat      → ``router.acompletion``            (proxy ``route_type=acompletion``)
  - Responses → ``router.aresponses``             (proxy ``route_type=aresponses``)
  - Messages  → ``router.aanthropic_messages``    (proxy ``route_type=anthropic_messages``)

Protocol is injected at the earliest kwargs boundary available to project code
(the same keys the proxy pre-call layer uses: ``metadata`` vs ``litellm_metadata``).

Important LiteLLM quirk (source + this harness):
  - Chat carries protocol under ``request_kwargs["metadata"]``
  - Responses / Messages carry protocol under ``request_kwargs["litellm_metadata"]``
  - Success callbacks nest the same fields under ``kwargs["litellm_params"]``

Run:
  pip install 'litellm==1.90.5' pytest pytest-asyncio
  set PYTHONPATH=plugins
  pytest tests/contract/test_p0_protocol_metadata_propagation.py -q

Security: fake keys only; prompt content is a single character; no Authorization
headers or full response bodies are asserted or logged.
"""

from __future__ import annotations

import asyncio
import copy
import os
import sys
from typing import Any, Optional

import pytest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_PLUGINS = os.path.join(_ROOT, "plugins")
if _PLUGINS not in sys.path:
    sys.path.insert(0, _PLUGINS)

litellm = pytest.importorskip("litellm", reason="litellm required for P0-02 contract")
from litellm import Router  # noqa: E402
from litellm.integrations.custom_logger import CustomLogger  # noqa: E402
from litellm.litellm_core_utils.logging_worker import (  # noqa: E402
    GLOBAL_LOGGING_WORKER,
)

from shared_quota_router.bootstrap import register  # noqa: E402
from shared_quota_router.lease import LeaseManager  # noqa: E402
from shared_quota_router.state_store import StateStore  # noqa: E402
from shared_quota_router.strategy import SharedQuotaRoutingStrategy  # noqa: E402

PROTOCOL_CHAT = "openai_chat"
PROTOCOL_RESPONSES = "openai_responses"
PROTOCOL_MESSAGES = "anthropic_messages"
PROBE_CONTENT = "x"
FAKE_KEY = "fake-key-not-a-secret"
FAKE_BASE = "https://example.invalid/mock"


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


def _model_list() -> list[dict[str, Any]]:
    return [
        {
            "model_name": "probe-model",
            "litellm_params": {
                "model": "openai/probe",
                "api_base": FAKE_BASE,
                "api_key": FAKE_KEY,
                "mock_response": "ok",
            },
            "model_info": {
                "id": "probe-dep-1",
                "deployment_id": "probe-dep-1",
                "provider_id": "mock-provider",
                "quota_group_id": "probe-qg-1",
                "priority": 10,
            },
        }
    ]


def _extract_protocol_from_strategy_kwargs(request_kwargs: dict[str, Any] | None) -> str | None:
    if not isinstance(request_kwargs, dict):
        return None
    for key in ("metadata", "litellm_metadata"):
        bucket = request_kwargs.get(key)
        if isinstance(bucket, dict) and bucket.get("protocol"):
            return str(bucket["protocol"])
    return None


def _extract_protocol_from_callback_kwargs(kwargs: dict[str, Any]) -> str | None:
    if not isinstance(kwargs, dict):
        return None
    for key in ("metadata", "litellm_metadata"):
        bucket = kwargs.get(key)
        if isinstance(bucket, dict) and bucket.get("protocol"):
            return str(bucket["protocol"])
    lp = kwargs.get("litellm_params")
    if isinstance(lp, dict):
        for key in ("metadata", "litellm_metadata"):
            bucket = lp.get(key)
            if isinstance(bucket, dict) and bucket.get("protocol"):
                return str(bucket["protocol"])
    return None


def _assert_no_sensitive_payload(blob: Any) -> None:
    text = repr(blob)
    assert "Authorization" not in text
    assert "Bearer " not in text


class CapturingStrategy(SharedQuotaRoutingStrategy):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.captures: list[dict[str, Any]] = []

    def get_available_deployment(
        self,
        model: str,
        messages: Optional[list] = None,
        input: Optional[Any] = None,  # noqa: A002
        specific_deployment: Optional[bool] = False,
        request_kwargs: Optional[dict] = None,
    ) -> dict[str, Any]:
        rk = request_kwargs if isinstance(request_kwargs, dict) else {}
        snap = {
            "model": model,
            "messages_present": messages is not None,
            "input_named_arg_present": input is not None,
            "input_in_kwargs": "input" in rk,
            "metadata": copy.deepcopy(rk.get("metadata")),
            "litellm_metadata": copy.deepcopy(rk.get("litellm_metadata")),
            "protocol": _extract_protocol_from_strategy_kwargs(rk),
            "litellm_call_id": rk.get("litellm_call_id"),
            "kw_keys": sorted(rk.keys()),
        }
        _assert_no_sensitive_payload(snap)
        self.captures.append(snap)
        return super().get_available_deployment(
            model=model,
            messages=messages,
            input=input,
            specific_deployment=specific_deployment,
            request_kwargs=request_kwargs,
        )


class CapturingCallback(CustomLogger):
    def __init__(self) -> None:
        super().__init__()
        self.success: list[dict[str, Any]] = []
        self.failure: list[dict[str, Any]] = []
        self.stream: list[dict[str, Any]] = []

    def _snap(self, kwargs: dict[str, Any]) -> dict[str, Any]:
        lp = kwargs.get("litellm_params") if isinstance(kwargs.get("litellm_params"), dict) else {}
        model_info = lp.get("model_info") if isinstance(lp, dict) else None
        snap = {
            "call_type": kwargs.get("call_type"),
            "protocol": _extract_protocol_from_callback_kwargs(kwargs),
            "deployment_id": (model_info or {}).get("deployment_id")
            if isinstance(model_info, dict)
            else None,
            "quota_group_id": (model_info or {}).get("quota_group_id")
            if isinstance(model_info, dict)
            else None,
        }
        _assert_no_sensitive_payload(snap)
        return snap

    async def async_log_success_event(
        self, kwargs: dict, response_obj: Any, start_time: Any, end_time: Any
    ) -> None:
        self.success.append(self._snap(kwargs))

    async def async_log_failure_event(
        self, kwargs: dict, response_obj: Any, start_time: Any, end_time: Any
    ) -> None:
        self.failure.append(self._snap(kwargs))

    async def async_log_stream_event(
        self, kwargs: dict, response_obj: Any, start_time: Any, end_time: Any
    ) -> None:
        self.stream.append(self._snap(kwargs))


def _build_harness() -> dict[str, Any]:
    mem = MemRedis()
    store = StateStore(mem)
    lease = LeaseManager(mem)
    router = Router(model_list=_model_list(), set_verbose=False)
    strategy = CapturingStrategy(store=store, lease_manager=lease, router=router)
    register(router, strategy=strategy)
    callback = CapturingCallback()
    return {
        "router": router,
        "strategy": strategy,
        "callback": callback,
        "store": store,
        "prev_callbacks": list(getattr(litellm, "callbacks", []) or []),
    }


async def _wait_for_success(callback: CapturingCallback, n: int = 1, timeout: float = 5.0) -> bool:
    """Drain the global logging worker until ``n`` success events arrive."""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if len(callback.success) >= n:
            return True
        try:
            GLOBAL_LOGGING_WORKER.start()
            await GLOBAL_LOGGING_WORKER.flush()
        except Exception:  # noqa: BLE001
            pass
        await asyncio.sleep(0.05)
    return len(callback.success) >= n


# ---------------------------------------------------------------------------
# Strategy-only tests (sync selection; no async logging worker dependency)
# ---------------------------------------------------------------------------


def test_p0_02_chat_strategy_sees_metadata_protocol() -> None:
    h = _build_harness()
    router: Router = h["router"]
    strategy: CapturingStrategy = h["strategy"]
    dep = router.get_available_deployment(
        model="probe-model",
        messages=[{"role": "user", "content": PROBE_CONTENT}],
        request_kwargs={
            "litellm_call_id": "p0-02-chat-sync",
            "metadata": {"protocol": PROTOCOL_CHAT},
        },
    )
    assert dep["model_info"]["deployment_id"] == "probe-dep-1"
    assert strategy.captures[-1]["protocol"] == PROTOCOL_CHAT
    assert (strategy.captures[-1]["metadata"] or {}).get("protocol") == PROTOCOL_CHAT


def test_p0_02_responses_strategy_sees_litellm_metadata_protocol() -> None:
    h = _build_harness()
    strategy: CapturingStrategy = h["strategy"]
    # Mirror generic helper: messages=None, input lives in kwargs only.
    dep = strategy.get_available_deployment(
        model="probe-model",
        messages=None,
        input=None,
        request_kwargs={
            "litellm_call_id": "p0-02-resp-sync",
            "input": PROBE_CONTENT,
            "litellm_metadata": {"protocol": PROTOCOL_RESPONSES},
        },
    )
    assert dep["model_info"]["quota_group_id"] == "probe-qg-1"
    last = strategy.captures[-1]
    assert last["protocol"] == PROTOCOL_RESPONSES
    assert (last["litellm_metadata"] or {}).get("protocol") == PROTOCOL_RESPONSES
    assert last["input_named_arg_present"] is False
    assert last["input_in_kwargs"] is True
    assert last["messages_present"] is False


def test_p0_02_messages_strategy_sees_litellm_metadata_protocol() -> None:
    h = _build_harness()
    strategy: CapturingStrategy = h["strategy"]
    dep = strategy.get_available_deployment(
        model="probe-model",
        messages=[{"role": "user", "content": PROBE_CONTENT}],
        request_kwargs={
            "litellm_call_id": "p0-02-msg-sync",
            "messages": [{"role": "user", "content": PROBE_CONTENT}],
            "litellm_metadata": {"protocol": PROTOCOL_MESSAGES},
        },
    )
    assert dep["model_info"]["deployment_id"] == "probe-dep-1"
    last = strategy.captures[-1]
    assert last["protocol"] == PROTOCOL_MESSAGES
    assert (last["litellm_metadata"] or {}).get("protocol") == PROTOCOL_MESSAGES
    assert last["messages_present"] is True


def test_p0_02_responses_metadata_only_not_copied_into_litellm_bucket() -> None:
    """Wrong bucket still visible on ``metadata``; not auto-copied to litellm_metadata."""
    h = _build_harness()
    strategy: CapturingStrategy = h["strategy"]
    strategy.get_available_deployment(
        model="probe-model",
        request_kwargs={
            "litellm_call_id": "p0-02-resp-wrong-bucket",
            "input": PROBE_CONTENT,
            "metadata": {"protocol": PROTOCOL_RESPONSES},
            "litellm_metadata": {"model_group": "probe-model"},
        },
    )
    last = strategy.captures[-1]
    assert (last["metadata"] or {}).get("protocol") == PROTOCOL_RESPONSES
    assert (last["litellm_metadata"] or {}).get("protocol") is None
    assert last["protocol"] == PROTOCOL_RESPONSES  # combined reader


def test_p0_02_failure_callback_extractor() -> None:
    """Failure hook contract shape (no network); mirrors success nesting."""
    cb = CapturingCallback()

    async def _run() -> None:
        await cb.async_log_failure_event(
            {
                "call_type": "acompletion",
                "litellm_params": {
                    "metadata": {
                        "protocol": PROTOCOL_CHAT,
                        "model_info": {
                            "deployment_id": "probe-dep-1",
                            "quota_group_id": "probe-qg-1",
                        },
                    },
                    "model_info": {
                        "deployment_id": "probe-dep-1",
                        "quota_group_id": "probe-qg-1",
                    },
                },
            },
            response_obj=None,
            start_time=None,
            end_time=None,
        )

    asyncio.run(_run())
    assert cb.failure[-1]["protocol"] == PROTOCOL_CHAT
    assert cb.failure[-1]["deployment_id"] == "probe-dep-1"


# ---------------------------------------------------------------------------
# Full Router + callback matrix (single event loop — avoids LoggingWorker death)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_p0_02_router_paths_strategy_and_success_callback_matrix() -> None:
    """One loop, three endpoint paths: strategy + success callback must see protocol.

    Runs Chat / Responses / Messages sequentially on a single event loop so
    LiteLLM's GLOBAL_LOGGING_WORKER is not destroyed between pytest tests.
    """
    h = _build_harness()
    router: Router = h["router"]
    strategy: CapturingStrategy = h["strategy"]
    callback: CapturingCallback = h["callback"]
    prev = h["prev_callbacks"]
    litellm.callbacks = [callback]
    GLOBAL_LOGGING_WORKER.start()

    cases = [
        (
            "chat",
            PROTOCOL_CHAT,
            "metadata",
            lambda: router.acompletion(
                model="probe-model",
                messages=[{"role": "user", "content": PROBE_CONTENT}],
                metadata={"protocol": PROTOCOL_CHAT},
                litellm_call_id="p0-02-matrix-chat",
            ),
            "acompletion",
        ),
        (
            "responses",
            PROTOCOL_RESPONSES,
            "litellm_metadata",
            lambda: router.aresponses(
                model="probe-model",
                input=PROBE_CONTENT,
                litellm_metadata={"protocol": PROTOCOL_RESPONSES},
                litellm_call_id="p0-02-matrix-responses",
            ),
            "aresponses",
        ),
        (
            "messages",
            PROTOCOL_MESSAGES,
            "litellm_metadata",
            lambda: router.aanthropic_messages(
                model="probe-model",
                messages=[{"role": "user", "content": PROBE_CONTENT}],
                max_tokens=8,
                litellm_metadata={"protocol": PROTOCOL_MESSAGES},
                litellm_call_id="p0-02-matrix-messages",
            ),
            "anthropic_messages",
        ),
    ]

    results: dict[str, dict[str, Any]] = {}
    try:
        for name, protocol, bucket, invoker, call_type in cases:
            strategy.captures.clear()
            before = len(callback.success)
            await invoker()
            assert strategy.captures, f"{name}: strategy not called"
            strat = strategy.captures[-1]
            assert strat["protocol"] == protocol, f"{name}: strategy protocol"
            assert (strat.get(bucket) or {}).get("protocol") == protocol, (
                f"{name}: expected bucket {bucket}"
            )

            ok = await _wait_for_success(callback, n=before + 1, timeout=5.0)
            assert ok, f"{name}: success callback did not fire"
            succ = callback.success[-1]
            assert succ["protocol"] == protocol, f"{name}: callback protocol"
            assert succ["call_type"] == call_type, f"{name}: call_type"
            assert succ["deployment_id"] == "probe-dep-1"
            assert succ["quota_group_id"] == "probe-qg-1"

            results[name] = {
                "strategy_protocol": strat["protocol"],
                "callback_protocol": succ["protocol"],
                "bucket": bucket,
                "messages_present": strat["messages_present"],
                "input_in_kwargs": strat["input_in_kwargs"],
                "call_type": succ["call_type"],
            }

        # Chat streaming: strategy always; success and/or stream hooks when available
        strategy.captures.clear()
        before_s = len(callback.success)
        before_st = len(callback.stream)
        resp = await router.acompletion(
            model="probe-model",
            messages=[{"role": "user", "content": PROBE_CONTENT}],
            metadata={"protocol": PROTOCOL_CHAT},
            stream=True,
            litellm_call_id="p0-02-matrix-stream",
        )
        if hasattr(resp, "__aiter__"):
            async for _ in resp:
                pass
        assert strategy.captures[-1]["protocol"] == PROTOCOL_CHAT
        # Drain logging; mock stream may skip per-chunk stream hooks.
        await _wait_for_success(callback, n=before_s + 1, timeout=5.0)
        stream_fired = len(callback.stream) > before_st
        if stream_fired:
            assert callback.stream[-1]["protocol"] == PROTOCOL_CHAT
        if len(callback.success) > before_s:
            assert callback.success[-1]["protocol"] == PROTOCOL_CHAT
        results["chat_stream"] = {
            "strategy_protocol": PROTOCOL_CHAT,
            "stream_hook_fired": stream_fired,
            "success_after_stream": len(callback.success) > before_s,
        }

        assert set(results) >= {"chat", "responses", "messages", "chat_stream"}
        for name in ("chat", "responses", "messages"):
            assert results[name]["strategy_protocol"] == results[name]["callback_protocol"]
    finally:
        litellm.callbacks = prev

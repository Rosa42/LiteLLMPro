"""P0 Probe B: env-gated pre-call / post-select message mutation (default off)."""

from __future__ import annotations

from shared_quota_router.callbacks import _inject_p0_probe_b_marker
from shared_quota_router.feature_flags import flag_snapshot, p0_probe_b_marker
from shared_quota_router.lease import LeaseManager
from shared_quota_router.mock_provider import MockHandler
from shared_quota_router.state_store import StateStore
from shared_quota_router.strategy import SharedQuotaRoutingStrategy

FAKE_AUTH = "sk-fake-auth-never-store"
UNIT_MARKER = "P0B_UNITTEST01"


def _handler_with_auth() -> MockHandler:
    handler = MockHandler.__new__(MockHandler)
    handler.command = "POST"
    handler.headers = {"Authorization": f"Bearer {FAKE_AUTH}"}
    return handler


def test_empty_env_inject_noop(monkeypatch: object) -> None:
    monkeypatch.delenv("P0_PROBE_B_MARKER", raising=False)
    original = "Reply with exactly: pong"
    data = {"messages": [{"role": "user", "content": original}]}
    _inject_p0_probe_b_marker(data)
    assert data["messages"][0]["content"] == original
    assert p0_probe_b_marker() == ""
    snap = flag_snapshot()
    assert "P0_PROBE_B_MARKER" not in snap
    assert "p0_probe_b_marker" not in snap


def test_string_content_suffix_preserves_original(monkeypatch: object) -> None:
    monkeypatch.setenv("P0_PROBE_B_MARKER", UNIT_MARKER)
    original = "Reply with exactly: pong"
    data = {"messages": [{"role": "user", "content": original}]}
    _inject_p0_probe_b_marker(data)
    content = data["messages"][0]["content"]
    assert isinstance(content, str)
    assert content.startswith(original)
    assert UNIT_MARKER in content
    assert content != original


def test_list_content_appends_last_text_block(monkeypatch: object) -> None:
    monkeypatch.setenv("P0_PROBE_B_MARKER", UNIT_MARKER)
    data = {
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "first"},
                    {"type": "text", "text": "last"},
                ],
            }
        ]
    }
    _inject_p0_probe_b_marker(data)
    blocks = data["messages"][0]["content"]
    assert blocks[0]["text"] == "first"
    assert blocks[1]["text"].startswith("last")
    assert UNIT_MARKER in blocks[1]["text"]
    assert UNIT_MARKER not in blocks[0]["text"]


def test_mock_record_probe_marker_hit_true(monkeypatch: object) -> None:
    monkeypatch.setenv("P0_PROBE_B_MARKER", UNIT_MARKER)
    MockHandler.last_requests.clear()
    handler = _handler_with_auth()
    handler._record(
        "/v1/messages",
        {"messages": [{"role": "user", "content": f"hello {UNIT_MARKER}"}]},
    )
    rec = MockHandler.last_requests[-1]
    assert rec["probe_marker_hit"] is True
    dumped = repr(MockHandler.last_requests)
    assert FAKE_AUTH not in dumped
    assert "Bearer " not in dumped


def test_mock_record_probe_marker_hit_false(monkeypatch: object) -> None:
    monkeypatch.setenv("P0_PROBE_B_MARKER", UNIT_MARKER)
    MockHandler.last_requests.clear()
    handler = _handler_with_auth()
    handler._record(
        "/v1/messages",
        {"messages": [{"role": "user", "content": "Reply with exactly: pong"}]},
    )
    rec = MockHandler.last_requests[-1]
    assert rec["probe_marker_hit"] is False
    dumped = repr(MockHandler.last_requests)
    assert FAKE_AUTH not in dumped
    assert UNIT_MARKER not in dumped


class _MemRedis:
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

    def eval(self, script: str, numkeys: int, *keys_and_args):
        keys = keys_and_args[:numkeys]
        args = keys_and_args[numkeys:]
        if numkeys == 3:
            inflight_key, lease_key = keys[1], keys[2]
            ttl, request_id = int(args[0]), args[2]
            inflight = self.incr(inflight_key)
            self.set(lease_key, request_id, ex=ttl)
            return [1, str(inflight)]
        if numkeys == 2:
            inflight_key, lease_key = keys
            self.delete(lease_key)
            inflight = int(self.data.get(inflight_key, "0"))
            if inflight > 0:
                inflight = self.decr(inflight_key)
            return inflight
        raise AssertionError("unexpected eval")


def _direct_messages_strategy() -> SharedQuotaRoutingStrategy:
    store = StateStore(_MemRedis())
    lease = LeaseManager(_MemRedis())
    model_list = [
        {
            "model_name": "MiniMax-M3",
            "model_info": {
                "deployment_id": "minimax-official-msg-MiniMax-M3",
                "provider_id": "minimax",
                "quota_group_id": "minimax-official",
                "priority": 25,
                "enabled": True,
                "upstream_protocol": "anthropic_messages",
                "supported_features": ["text", "streaming", "tools", "reasoning"],
                "supports_streaming": True,
                "public_protocols": ["anthropic_messages"],
            },
            "litellm_params": {"model": "anthropic/MiniMax-M3"},
        }
    ]

    class Router:
        def __init__(self, ml: list) -> None:
            self.model_list = ml

    strat = SharedQuotaRoutingStrategy(store=store, lease_manager=lease)
    strat.bind_router(Router(model_list))
    return strat


def test_strategy_select_injects_marker_into_request_kwargs(monkeypatch: object) -> None:
    """S1: after select, request_kwargs.messages must carry the env marker."""
    monkeypatch.setenv("P0_PROBE_B_MARKER", UNIT_MARKER)
    original = "Reply with exactly: pong"
    kwargs = {
        "litellm_metadata": {"protocol": "anthropic_messages"},
        "messages": [{"role": "user", "content": original}],
    }
    named = [{"role": "user", "content": original}]
    strat = _direct_messages_strategy()
    strat.get_available_deployment(
        model="MiniMax-M3",
        messages=named,
        request_kwargs=kwargs,
    )
    kw_content = kwargs["messages"][0]["content"]
    assert isinstance(kw_content, str)
    assert kw_content.startswith(original)
    assert UNIT_MARKER in kw_content
    named_content = named[0]["content"]
    assert UNIT_MARKER in named_content


def test_strategy_select_skips_inject_when_marker_empty(monkeypatch: object) -> None:
    monkeypatch.delenv("P0_PROBE_B_MARKER", raising=False)
    original = "Reply with exactly: pong"
    kwargs = {
        "litellm_metadata": {"protocol": "anthropic_messages"},
        "messages": [{"role": "user", "content": original}],
    }
    strat = _direct_messages_strategy()
    strat.get_available_deployment(
        model="MiniMax-M3",
        messages=[{"role": "user", "content": original}],
        request_kwargs=kwargs,
    )
    assert kwargs["messages"][0]["content"] == original

"""Vision agent presets: detect, fallback, cache isolation."""

from __future__ import annotations

import pytest

from shared_quota_router.feature_flags import clear_flag_cache
from shared_quota_router.models import ApiProtocol
from shared_quota_router.pipeline import EnhanceEnvelope, run_pipeline
from shared_quota_router.vision_agents.detect import resolve_preset
from shared_quota_router.vision_agents.generic import GenericPreset
from shared_quota_router.vision_agents.opencode import OpenCodePreset

PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQ"
    "AAAABJRU5ErkJggg=="
)
IR = "<visual-evidence><pre>x</pre></visual-evidence>"


def _env(messages: list, **overrides) -> EnhanceEnvelope:
    base = dict(
        model_group="glm-5.2-vision",
        protocol=ApiProtocol.ANTHROPIC_MESSAGES,
        streaming=False,
        messages=messages,
        workspace=None,
        visual_evidence=[],
        memory_hits=[],
        internal_call=False,
        parent_request_id="r1",
        parent_quota_group_id="volc-c",
        stage_ms={},
    )
    base.update(overrides)
    return EnhanceEnvelope(**base)


def _image_messages(text: str = "describe") -> list[dict]:
    return [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": text},
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": "image/png", "data": PNG_B64},
                },
            ],
        }
    ]


def test_resolve_fallback_without_headers() -> None:
    preset, match = resolve_preset({}, _image_messages())
    assert preset.id == "generic"
    assert match == "fallback"


def test_resolve_explicit_opencode_header() -> None:
    preset, match = resolve_preset(
        {"x-agent-client": "opencode"},
        _image_messages("I use a browser"),
    )
    assert preset.id == "opencode"
    assert match == "header"


def test_resolve_header_force_generic_beats_ua() -> None:
    preset, match = resolve_preset(
        {"x-agent-client": "generic", "user-agent": "opencode/1.2.3"},
        _image_messages(),
    )
    assert preset.id == "generic"
    assert match == "header_force"


def test_resolve_unknown_header_falls_through_to_ua() -> None:
    preset, match = resolve_preset(
        {"x-agent-client": "cursor", "user-agent": "opencode/1.0"},
        _image_messages(),
    )
    assert preset.id == "opencode"
    assert match == "ua"


def test_resolve_unknown_header_without_ua_is_generic() -> None:
    preset, match = resolve_preset(
        {"x-agent-client": "cursor"},
        _image_messages(),
    )
    assert preset.id == "generic"
    assert match == "fallback"


def test_resolve_ua_opencode_slash() -> None:
    preset, match = resolve_preset(
        {"User-Agent": "opencode/1.2.3 ai-sdk/anthropic"},
        _image_messages(),
    )
    assert preset.id == "opencode"
    assert match == "ua"


def test_user_chat_mentioning_opencode_is_not_a_match() -> None:
    preset, match = resolve_preset(
        {},
        _image_messages("I use OpenCode every day"),
    )
    assert preset.id == "generic"
    assert match == "fallback"


def test_opencode_match_messages_ignores_chat_mentions() -> None:
    assert OpenCodePreset().match_messages(_image_messages("opencode tool bash")) is False
    assert OpenCodePreset().match_messages(_image_messages("I use OpenCode")) is False


def test_generic_never_wins_match() -> None:
    g = GenericPreset()
    assert g.match_header({"user-agent": "opencode/1"}) is False
    assert g.match_messages(_image_messages()) is False


def test_opencode_addendum_under_cap() -> None:
    text = OpenCodePreset().system_addendum().strip()
    assert text
    assert len(text) <= 1000
    assert "chrome" in text.lower() or "traceback" in text.lower()


@pytest.mark.asyncio
async def test_pipeline_cache_isolated_across_presets(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GATEWAY_ENHANCE_ENABLED", "true")
    monkeypatch.setenv("VISION_COMPOSE_ENABLED", "true")
    monkeypatch.setenv("GATEWAY_VISION_CACHE_DIR", str(tmp_path))
    monkeypatch.setenv("S5_COMPOSED_MODELS", "glm-5.2-vision")
    clear_flag_cache()
    calls = {"n": 0}

    async def fake(_png: bytes, _guide: str = "") -> str:
        calls["n"] += 1
        return IR

    msgs = _image_messages("same caption")
    await run_pipeline(_env(msgs, translator=fake, headers={"user-agent": "curl/8"}))
    await run_pipeline(
        _env(
            _image_messages("same caption"),
            translator=fake,
            headers={"user-agent": "opencode/1.0"},
        )
    )
    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_opencode_header_appends_system_addendum(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GATEWAY_ENHANCE_ENABLED", "true")
    monkeypatch.setenv("VISION_COMPOSE_ENABLED", "true")
    monkeypatch.setenv("GATEWAY_VISION_CACHE_DIR", str(tmp_path))
    monkeypatch.setenv("S5_COMPOSED_MODELS", "glm-5.2-vision")
    clear_flag_cache()
    captured: dict[str, str] = {}

    async def fake_post(url: str, *, headers: dict, json: dict, timeout: float = 60.0):
        captured["system"] = str(json.get("system") or "")
        captured["ua"] = str(headers.get("User-Agent") or headers.get("user-agent") or "")

        class _Resp:
            status_code = 200

            def json(self):
                return {"content": [{"type": "text", "text": IR}]}

            def raise_for_status(self) -> None:
                return None

        return _Resp()

    def select(model, **kwargs):
        return {
            "model_name": "MiniMax-M3",
            "model_info": {"quota_group_id": "minimax-official"},
            "litellm_params": {
                "model": "anthropic/MiniMax-M3",
                "api_base": "https://api.minimaxi.com/anthropic",
                "api_key": "secret-key-do-not-log",
            },
        }

    await run_pipeline(
        _env(
            _image_messages("read the terminal"),
            headers={"x-agent-client": "opencode"},
            select_deployment=select,
            http_post=fake_post,
        )
    )
    assert "Do not role-play OpenCode" in captured["system"]
    assert "visual-evidence" in captured["system"]
    assert "opencode/" not in captured["ua"].lower()


@pytest.mark.asyncio
async def test_oversized_addendum_falls_back_to_generic(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GATEWAY_ENHANCE_ENABLED", "true")
    monkeypatch.setenv("VISION_COMPOSE_ENABLED", "true")
    monkeypatch.setenv("GATEWAY_VISION_CACHE_DIR", str(tmp_path))
    monkeypatch.setenv("S5_COMPOSED_MODELS", "glm-5.2-vision")
    clear_flag_cache()
    monkeypatch.setattr(
        OpenCodePreset,
        "system_addendum",
        lambda self: "x" * 1001,
    )
    captured: dict[str, str] = {}

    async def fake_post(url: str, *, headers: dict, json: dict, timeout: float = 60.0):
        captured["system"] = str(json.get("system") or "")

        class _Resp:
            status_code = 200

            def json(self):
                return {"content": [{"type": "text", "text": IR}]}

            def raise_for_status(self) -> None:
                return None

        return _Resp()

    def select(model, **kwargs):
        return {
            "model_name": "MiniMax-M3",
            "model_info": {"quota_group_id": "minimax-official"},
            "litellm_params": {
                "model": "anthropic/MiniMax-M3",
                "api_base": "https://api.minimaxi.com/anthropic",
                "api_key": "secret-key-do-not-log",
            },
        }

    await run_pipeline(
        _env(
            _image_messages("read the terminal"),
            headers={"x-agent-client": "opencode"},
            select_deployment=select,
            http_post=fake_post,
        )
    )
    assert "x" * 20 not in captured["system"]
    assert "Do not role-play OpenCode" not in captured["system"]


@pytest.mark.asyncio
async def test_extract_error_falls_back_to_generic(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GATEWAY_ENHANCE_ENABLED", "true")
    monkeypatch.setenv("VISION_COMPOSE_ENABLED", "true")
    monkeypatch.setenv("GATEWAY_VISION_CACHE_DIR", str(tmp_path))
    monkeypatch.setenv("S5_COMPOSED_MODELS", "glm-5.2-vision")
    clear_flag_cache()

    def boom(self, messages, image_ref):
        raise RuntimeError("bad extractor")

    monkeypatch.setattr(OpenCodePreset, "extract_guide", boom)
    seen: list[str] = []

    async def fake(_png: bytes, guide: str = "") -> str:
        seen.append(guide)
        return IR

    env = _env(
        _image_messages("still translate me"),
        translator=fake,
        headers={"x-agent-client": "opencode"},
    )
    await run_pipeline(env)
    assert seen
    assert "still translate me" in seen[0]
    assert "image" not in [
        b.get("type") for b in env.messages[0]["content"] if isinstance(b, dict)
    ]


@pytest.mark.asyncio
async def test_strategy_passes_proxy_headers_to_preset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from shared_quota_router.lease import LeaseManager
    from shared_quota_router.state_store import StateStore
    from shared_quota_router.strategy import SharedQuotaRoutingStrategy

    monkeypatch.setenv("PROTOCOL_AWARE_GATEWAY_ENABLED", "true")
    monkeypatch.setenv("GATEWAY_ENHANCE_ENABLED", "true")
    monkeypatch.setenv("VISION_COMPOSE_ENABLED", "true")
    monkeypatch.setenv("S5_COMPOSED_MODELS", "glm-5.2-vision")
    clear_flag_cache()
    seen: dict[str, object] = {}

    async def spy(env: EnhanceEnvelope) -> None:
        preset, match = resolve_preset(env.headers or {}, env.messages)
        seen["agent"] = preset.id
        seen["match"] = match
        seen["headers"] = dict(env.headers or {})

    monkeypatch.setattr("shared_quota_router.pipeline.run_pipeline", spy)

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

    store = StateStore(_MemRedis())
    lease = LeaseManager(_MemRedis())
    model_list = [
        {
            "model_name": "glm-5.2-vision",
            "model_info": {
                "deployment_id": "volc-c-msg-glm-5.2-vision",
                "provider_id": "volcengine",
                "quota_group_id": "volc-c",
                "priority": 20,
                "enabled": True,
                "upstream_protocol": "anthropic_messages",
                "supported_features": ["text", "streaming", "tools", "reasoning"],
                "supports_streaming": True,
                "public_protocols": ["anthropic_messages"],
            },
            "litellm_params": {"model": "anthropic/glm-5.2"},
        }
    ]

    class Router:
        def __init__(self, ml: list) -> None:
            self.model_list = ml

    strat = SharedQuotaRoutingStrategy(store=store, lease_manager=lease)
    strat.bind_router(Router(model_list))
    kwargs = {
        "litellm_metadata": {"protocol": "anthropic_messages"},
        "messages": [{"role": "user", "content": "hi"}],
        "litellm_call_id": "hdr-1",
        "proxy_server_request": {
            "headers": {
                "User-Agent": "opencode/9.9.9",
                "X-Agent-Client": "opencode",
            }
        },
    }
    await strat.async_get_available_deployment(
        model="glm-5.2-vision",
        messages=[{"role": "user", "content": "hi"}],
        request_kwargs=kwargs,
    )
    assert seen["agent"] == "opencode"
    assert seen["match"] == "header"
    headers = seen["headers"]
    assert isinstance(headers, dict)
    assert "user-agent" in {k.lower() for k in headers}
    assert "x-agent-client" in {k.lower() for k in headers}


def _opencode_fixture(name: str) -> dict:
    import json
    from pathlib import Path

    path = (
        Path(__file__).resolve().parents[1]
        / "fixtures"
        / "vision_agents"
        / "opencode"
        / name
    )
    return json.loads(path.read_text(encoding="utf-8"))


def test_tool_result_fixture_extract_uses_human_task_not_tool_dump() -> None:
    from shared_quota_router.vision_agents.generic import GenericPreset
    from shared_quota_router.vision_compose import collect_image_refs

    payload = _opencode_fixture("tool_result_image.json")
    messages = payload["messages"]
    refs = collect_image_refs(messages)
    assert len(refs) == 1
    ref, _block = refs[0]
    assert ref.path == (0, 1)
    guide = GenericPreset().extract_guide(messages, ref)
    assert "Use the read_screenshot tool" in guide
    assert guide.startswith("task:")
    assert "Image read successfully" in guide


def test_user_media_fixture_extracts_caption() -> None:
    from shared_quota_router.vision_agents.generic import GenericPreset
    from shared_quota_router.vision_compose import collect_image_refs

    payload = _opencode_fixture("user_media_image.json")
    messages = payload["messages"]
    refs = collect_image_refs(messages)
    assert len(refs) == 1
    assert refs[0][0].path == (1,)
    guide = GenericPreset().extract_guide(messages, refs[0][0])
    assert "terminal screenshot" in guide


def test_opencode_fixtures_do_not_fingerprint() -> None:
    for name in ("tool_result_image.json", "user_media_image.json"):
        messages = _opencode_fixture(name)["messages"]
        assert OpenCodePreset().match_messages(messages) is False
        preset, match = resolve_preset({}, messages)
        assert preset.id == "generic"
        assert match == "fallback"


def test_live_read_tool_fixture_fingerprints() -> None:
    for name in ("live.json", "live-2.json"):
        messages = _opencode_fixture(name)["messages"]
        assert OpenCodePreset().match_messages(messages) is True
        preset, match = resolve_preset({}, messages)
        assert preset.id == "opencode"
        assert match == "fingerprint"


def test_live_extract_skips_read_wrapper() -> None:
    from shared_quota_router.vision_compose import collect_image_refs

    messages = _opencode_fixture("live-2.json")["messages"]
    refs = collect_image_refs(messages)
    assert refs
    guide = OpenCodePreset().extract_guide(messages, refs[0][0])
    assert "pong" in guide.lower()
    assert "Called the Read tool" not in guide
    assert "C:\\Users" not in guide
    assert "SUPERR" not in guide


def test_fingerprint_flag_off_ignores_match_messages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VISION_AGENT_FINGERPRINTS", "false")
    monkeypatch.setattr(OpenCodePreset, "match_messages", lambda self, _m: True)
    preset, match = resolve_preset({}, _image_messages("I use OpenCode every day"))
    assert preset.id == "generic"
    assert match == "fallback"


def test_fingerprint_flag_on_uses_match_messages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VISION_AGENT_FINGERPRINTS", "true")
    monkeypatch.setattr(OpenCodePreset, "match_messages", lambda self, _m: True)
    preset, match = resolve_preset({}, _image_messages())
    assert preset.id == "opencode"
    assert match == "fingerprint"


def test_source_ua_token_matches_opencode() -> None:
    headers = _opencode_fixture("source_headers.json")["headers"]
    preset, match = resolve_preset(headers, _image_messages())
    assert preset.id == "opencode"
    assert match == "ua"


@pytest.mark.asyncio
async def test_internal_call_does_not_run_vision_preset(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GATEWAY_ENHANCE_ENABLED", "true")
    monkeypatch.setenv("VISION_COMPOSE_ENABLED", "true")
    monkeypatch.setenv("GATEWAY_VISION_CACHE_DIR", str(tmp_path))
    monkeypatch.setenv("S5_COMPOSED_MODELS", "glm-5.2-vision")
    clear_flag_cache()
    called = {"n": 0}

    async def fake(_png: bytes, _guide: str = "") -> str:
        called["n"] += 1
        return IR

    env = _env(
        _image_messages("keep the pixels"),
        translator=fake,
        internal_call=True,
        headers={"x-agent-client": "opencode"},
    )
    await run_pipeline(env)
    assert called["n"] == 0
    types = [b.get("type") for b in env.messages[0]["content"] if isinstance(b, dict)]
    assert "image" in types

"""F3a: internal_call ids, vision file cache, fake translator peel."""

from __future__ import annotations

import pytest

from shared_quota_router.feature_flags import clear_flag_cache
from shared_quota_router.internal_call import assert_quota_exclusive, child_request_id
from shared_quota_router.models import ApiProtocol
from shared_quota_router.pipeline import EnhanceEnvelope, run_pipeline
from shared_quota_router.protocol_errors import (
    ProtocolAwareRoutingError,
    ProtocolRoutingReason,
)
from shared_quota_router.vision_cache import SCHEMA_VER, cache_key, get_cached, put_cached
from shared_quota_router.vision_compose import reset_circuit_for_tests

PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQ"
    "AAAABJRU5ErkJggg=="
)
IR = "<visual-evidence><pre>x</pre></visual-evidence>"


@pytest.fixture(autouse=True)
def _reset_vision_circuit() -> None:
    reset_circuit_for_tests()
    yield
    reset_circuit_for_tests()


def test_child_request_id_distinct() -> None:
    pid = "abc-parent"
    a = child_request_id(pid, "vision", "deadbeefcafebabe")
    assert a.startswith("abc-parent#vision:")
    assert a != pid
    assert a == "abc-parent#vision:deadbeef"


def test_quota_exclusive_rejects_same_group() -> None:
    with pytest.raises(ProtocolAwareRoutingError) as ei:
        assert_quota_exclusive("volc-c", "volc-c")
    assert ei.value.reason is ProtocolRoutingReason.CONFIGURATION_INVALID


def test_quota_exclusive_allows_different_groups() -> None:
    assert_quota_exclusive("volc-c", "minimax-official")


def test_vision_cache_roundtrip(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GATEWAY_VISION_CACHE_DIR", str(tmp_path))
    digest = "a" * 64
    assert get_cached(digest) is None
    put_cached(digest, IR)
    assert get_cached(digest) == IR
    assert cache_key(digest) == f"vision:{SCHEMA_VER}:{digest}"
    assert get_cached(digest, schema_ver=SCHEMA_VER + 1) is None


def _image_messages(*, nested: bool = False) -> list[dict]:
    image = {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/png", "data": PNG_B64},
    }
    if nested:
        return [
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "t1",
                        "content": [image, {"type": "text", "text": "see"}],
                    }
                ],
            }
        ]
    return [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "describe"},
                dict(image),
            ],
        }
    ]


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


@pytest.mark.asyncio
async def test_fake_translator_replaces_image_and_tool_result(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GATEWAY_ENHANCE_ENABLED", "true")
    monkeypatch.setenv("VISION_COMPOSE_ENABLED", "true")
    monkeypatch.setenv("GATEWAY_VISION_CACHE_DIR", str(tmp_path))
    clear_flag_cache()

    calls = {"n": 0}

    async def fake(_png: bytes, _guide: str = "") -> str:
        calls["n"] += 1
        return IR

    env = _env(_image_messages(nested=True), translator=fake)
    await run_pipeline(env)
    types = []

    def _collect(content):
        if not isinstance(content, list):
            return
        for b in content:
            if not isinstance(b, dict):
                continue
            types.append(b.get("type"))
            if b.get("type") == "tool_result":
                _collect(b.get("content"))

    _collect(env.messages[0]["content"])
    assert "image" not in types
    assert IR in env.messages[0]["content"][0]["content"][0]["text"]
    assert env.visual_evidence == [IR]
    assert calls["n"] == 1

    env2 = _env(_image_messages(nested=True), translator=fake)
    await run_pipeline(env2)
    assert calls["n"] == 1  # cache hit


@pytest.mark.asyncio
async def test_composed_image_fail_closed_without_translator(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GATEWAY_ENHANCE_ENABLED", "true")
    monkeypatch.setenv("VISION_COMPOSE_ENABLED", "true")
    monkeypatch.setenv("GATEWAY_VISION_CACHE_DIR", str(tmp_path))
    monkeypatch.setenv("S5_COMPOSED_MODELS", "glm-5.2-vision")
    monkeypatch.delenv("S5_STUB_PEEL", raising=False)
    clear_flag_cache()
    env = _env(_image_messages())
    with pytest.raises(ProtocolAwareRoutingError) as ei:
        await run_pipeline(env)
    assert ei.value.reason is ProtocolRoutingReason.FEATURE_UNSUPPORTED
    assert ei.value.details.get("vision") == "no_translator"


@pytest.mark.asyncio
async def test_too_many_images_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GATEWAY_ENHANCE_ENABLED", "true")
    monkeypatch.setenv("VISION_COMPOSE_ENABLED", "true")
    monkeypatch.setenv("S5_COMPOSED_MODELS", "glm-5.2-vision")
    clear_flag_cache()

    async def fake(_png: bytes, _guide: str = "") -> str:
        return IR

    image = {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/png", "data": PNG_B64},
    }
    messages = [
        {"role": "user", "content": [dict(image) for _ in range(7)]},
    ]
    env = _env(messages, translator=fake)
    with pytest.raises(ProtocolAwareRoutingError) as ei:
        await run_pipeline(env)
    assert ei.value.details.get("vision_limit") == "images"


@pytest.mark.asyncio
async def test_circuit_opens_after_three_quality_failures(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from shared_quota_router.metrics import get_counter, reset_for_tests
    from shared_quota_router.vision_compose import reset_circuit_for_tests

    monkeypatch.setenv("GATEWAY_ENHANCE_ENABLED", "true")
    monkeypatch.setenv("VISION_COMPOSE_ENABLED", "true")
    monkeypatch.setenv("GATEWAY_VISION_CACHE_DIR", str(tmp_path))
    monkeypatch.setenv("S5_COMPOSED_MODELS", "glm-5.2-vision")
    clear_flag_cache()
    reset_circuit_for_tests()
    reset_for_tests()
    calls = {"n": 0}

    async def bad(_png: bytes, _guide: str = "") -> str:
        calls["n"] += 1
        return "<visual-evidence></visual-evidence>"

    for _ in range(3):
        env = _env(_image_messages(), translator=bad)
        with pytest.raises(ProtocolAwareRoutingError) as ei:
            await run_pipeline(env)
        assert ei.value.details.get("vision") == "empty_or_reject"
    assert calls["n"] == 3

    env = _env(_image_messages(), translator=bad)
    with pytest.raises(ProtocolAwareRoutingError) as ei:
        await run_pipeline(env)
    assert ei.value.details.get("vision") == "circuit_open"
    assert calls["n"] == 3
    assert get_counter("enhance_vision_circuit_open") >= 1


@pytest.mark.asyncio
async def test_out_of_scope_reject_does_not_open_circuit(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from shared_quota_router.vision_compose import reset_circuit_for_tests

    monkeypatch.setenv("GATEWAY_ENHANCE_ENABLED", "true")
    monkeypatch.setenv("VISION_COMPOSE_ENABLED", "true")
    monkeypatch.setenv("GATEWAY_VISION_CACHE_DIR", str(tmp_path))
    monkeypatch.setenv("S5_COMPOSED_MODELS", "glm-5.2-vision")
    clear_flag_cache()
    reset_circuit_for_tests()
    calls = {"n": 0}

    async def reject(_png: bytes, _guide: str = "") -> str:
        calls["n"] += 1
        return '<visual-evidence data-reject="out-of-scope"></visual-evidence>'

    for _ in range(4):
        env = _env(_image_messages(), translator=reject)
        with pytest.raises(ProtocolAwareRoutingError) as ei:
            await run_pipeline(env)
        assert ei.value.details.get("vision") == "rejected_scope"
    assert calls["n"] == 4


@pytest.mark.asyncio
async def test_minimax_translator_posts_and_strips_images(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from shared_quota_router.metrics import get_counter, reset_for_tests

    monkeypatch.setenv("GATEWAY_ENHANCE_ENABLED", "true")
    monkeypatch.setenv("VISION_COMPOSE_ENABLED", "true")
    monkeypatch.setenv("GATEWAY_VISION_CACHE_DIR", str(tmp_path))
    monkeypatch.setenv("S5_COMPOSED_MODELS", "glm-5.2-vision")
    clear_flag_cache()
    reset_for_tests()
    posts: list[dict] = []
    selects: list[dict] = []
    released: list[tuple[str, str]] = []

    async def fake_post(url: str, *, headers: dict, json: dict, timeout: float = 60.0):
        posts.append(
            {
                "url": url,
                "has_key": "x-api-key" in {k.lower() for k in headers},
                "model": json.get("model"),
                "has_image": any(
                    isinstance(b, dict) and b.get("type") == "image"
                    for m in json.get("messages") or []
                    for b in (m.get("content") if isinstance(m.get("content"), list) else [])
                ),
            }
        )
        assert "secret-key" not in url

        class _Resp:
            status_code = 200

            def json(self):
                return {"content": [{"type": "text", "text": IR}]}

            def raise_for_status(self) -> None:
                return None

        return _Resp()

    def select(model, **kwargs):
        kw = kwargs.get("request_kwargs") or {}
        selects.append(
            {
                "model": model,
                "rid": kw.get("litellm_call_id"),
                "internal": (kw.get("litellm_metadata") or {}).get("internal_call"),
            }
        )
        return {
            "model_name": "MiniMax-M3",
            "model_info": {"quota_group_id": "minimax-official"},
            "litellm_params": {
                "model": "anthropic/MiniMax-M3",
                "api_base": "https://api.minimaxi.com/anthropic",
                "api_key": "secret-key-do-not-log",
            },
        }

    env = _env(
        _image_messages(),
        select_deployment=select,
        release_lease=lambda qg, rid: released.append((qg, rid)),
        http_post=fake_post,
    )
    await run_pipeline(env)
    assert env.messages[0]["content"][1]["type"] == "text"
    assert "image" not in [b.get("type") for b in env.messages[0]["content"]]
    assert IR in env.messages[0]["content"][1]["text"]
    assert selects and selects[0]["model"] == "MiniMax-M3"
    assert selects[0]["internal"] is True
    assert "#vision:" in str(selects[0]["rid"])
    assert posts and posts[0]["url"].endswith("/v1/messages")
    assert posts[0]["model"] == "MiniMax-M3"
    assert posts[0]["has_image"] is True
    assert released == [("minimax-official", selects[0]["rid"])]
    assert get_counter("enhance_vision_ok") >= 1

    env2 = _env(
        _image_messages(),
        select_deployment=select,
        http_post=fake_post,
    )
    await run_pipeline(env2)
    assert len(posts) == 1
    assert get_counter("enhance_vision_cache_hit") >= 1


@pytest.mark.asyncio
async def test_no_image_skips_minimax(monkeypatch: pytest.MonkeyPatch) -> None:
    from shared_quota_router.metrics import get_counter, reset_for_tests

    monkeypatch.setenv("GATEWAY_ENHANCE_ENABLED", "true")
    monkeypatch.setenv("VISION_COMPOSE_ENABLED", "true")
    monkeypatch.setenv("S5_COMPOSED_MODELS", "glm-5.2-vision")
    clear_flag_cache()
    reset_for_tests()
    selects: list[str] = []

    def select(model, **_kwargs):
        selects.append(model)
        raise AssertionError("select must not run")

    env = _env(
        [{"role": "user", "content": "no picture here"}],
        select_deployment=select,
    )
    await run_pipeline(env)
    assert selects == []
    assert get_counter("vision_translate_skipped") >= 1


def test_s5_peel_defers_only_inside_async_select(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from shared_quota_router.composed_vision import peel_composed_images_on_select
    from shared_quota_router.vision_async_flag import (
        mark_async_select,
        reset_async_select,
    )

    monkeypatch.setenv("GATEWAY_ENHANCE_ENABLED", "true")
    monkeypatch.setenv("VISION_COMPOSE_ENABLED", "true")
    monkeypatch.setenv("S5_COMPOSED_MODELS", "glm-5.2-vision")
    monkeypatch.delenv("S5_STUB_PEEL", raising=False)
    clear_flag_cache()
    msgs = _image_messages()
    with pytest.raises(ProtocolAwareRoutingError) as ei:
        peel_composed_images_on_select(
            "glm-5.2-vision",
            {"messages": msgs},
            msgs,
            protocol=ApiProtocol.ANTHROPIC_MESSAGES,
        )
    assert ei.value.details.get("vision") == "sync_path"
    assert msgs[0]["content"][1]["type"] == "image"

    token = mark_async_select()
    try:
        peel_composed_images_on_select(
            "glm-5.2-vision",
            {"messages": msgs},
            msgs,
            protocol=ApiProtocol.ANTHROPIC_MESSAGES,
        )
    finally:
        reset_async_select(token)
    assert msgs[0]["content"][1]["type"] == "image"


def test_guide_text_includes_user_words_and_prior_turns() -> None:
    from shared_quota_router.vision_compose import guide_text_from_messages

    messages = [
        {"role": "user", "content": "open the failing test"},
        {"role": "assistant", "content": "I ran pytest."},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "What does this traceback mean?"},
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": "image/png", "data": PNG_B64},
                },
            ],
        },
    ]
    guide = guide_text_from_messages(messages)
    assert "What does this traceback mean?" in guide
    assert "open the failing test" in guide
    assert "I ran pytest." in guide
    assert PNG_B64 not in guide
    assert "sk-abcdefghijklmnopqrstuvwxyz" not in guide_text_from_messages(
        [{"role": "user", "content": "key sk-abcdefghijklmnopqrstuvwxyz look"}]
    )


@pytest.mark.asyncio
async def test_minimax_post_includes_user_task_not_only_generic_line(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GATEWAY_ENHANCE_ENABLED", "true")
    monkeypatch.setenv("VISION_COMPOSE_ENABLED", "true")
    monkeypatch.setenv("GATEWAY_VISION_CACHE_DIR", str(tmp_path))
    monkeypatch.setenv("S5_COMPOSED_MODELS", "glm-5.2-vision")
    clear_flag_cache()
    captured: dict[str, str] = {}

    async def fake_post(url: str, *, headers: dict, json: dict, timeout: float = 60.0):
        texts: list[str] = []
        for m in json.get("messages") or []:
            content = m.get("content")
            if isinstance(content, list):
                for b in content:
                    if isinstance(b, dict) and b.get("type") == "text":
                        texts.append(str(b.get("text") or ""))
        captured["user"] = "\n".join(texts)
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

    env = _env(
        [
            {"role": "user", "content": "the login form is broken"},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Read the stack from this terminal"},
                    {
                        "type": "image",
                        "source": {"type": "base64", "media_type": "image/png", "data": PNG_B64},
                    },
                ],
            },
        ],
        select_deployment=select,
        http_post=fake_post,
    )
    await run_pipeline(env)
    assert "Read the stack from this terminal" in captured["user"]
    assert "the login form is broken" in captured["user"]
    assert "Translate this screenshot." not in captured["user"] or "Read the stack" in captured["user"]
    assert "do not answer" in captured["system"].lower() or "do not solve" in captured["system"].lower()


@pytest.mark.asyncio
async def test_same_image_different_user_text_skips_cache(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GATEWAY_ENHANCE_ENABLED", "true")
    monkeypatch.setenv("VISION_COMPOSE_ENABLED", "true")
    monkeypatch.setenv("GATEWAY_VISION_CACHE_DIR", str(tmp_path))
    monkeypatch.setenv("S5_COMPOSED_MODELS", "glm-5.2-vision")
    clear_flag_cache()
    posts = {"n": 0}

    async def fake_post(url: str, *, headers: dict, json: dict, timeout: float = 60.0):
        posts["n"] += 1

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

    def msgs(text: str) -> list[dict]:
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

    await run_pipeline(
        _env(msgs("what is the error"), select_deployment=select, http_post=fake_post)
    )
    await run_pipeline(
        _env(msgs("what git branch is this"), select_deployment=select, http_post=fake_post)
    )
    assert posts["n"] == 2


def test_schema_ver_is_3() -> None:
    from shared_quota_router.vision_cache import SCHEMA_VER

    assert SCHEMA_VER == 3


def test_digest_includes_agent_id_and_prompt_rev() -> None:
    from shared_quota_router.vision_compose import vision_cache_digest

    png = b"\x89PNG\r\n"
    guide = "task:\nfix the test"
    generic = vision_cache_digest(png, guide, agent_id="generic", prompt_rev=1)
    other_agent = vision_cache_digest(png, guide, agent_id="opencode", prompt_rev=1)
    other_rev = vision_cache_digest(png, guide, agent_id="generic", prompt_rev=2)
    assert generic != other_agent
    assert generic != other_rev
    assert generic == vision_cache_digest(png, guide, agent_id="generic", prompt_rev=1)


@pytest.mark.asyncio
async def test_fake_translator_receives_guide(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GATEWAY_ENHANCE_ENABLED", "true")
    monkeypatch.setenv("VISION_COMPOSE_ENABLED", "true")
    monkeypatch.setenv("GATEWAY_VISION_CACHE_DIR", str(tmp_path))
    monkeypatch.setenv("S5_COMPOSED_MODELS", "glm-5.2-vision")
    clear_flag_cache()
    seen: list[str] = []

    async def fake(_png: bytes, guide: str = "") -> str:
        seen.append(guide)
        return IR

    env = _env(
        [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Read this traceback"},
                    {
                        "type": "image",
                        "source": {"type": "base64", "media_type": "image/png", "data": PNG_B64},
                    },
                ],
            }
        ],
        translator=fake,
    )
    await run_pipeline(env)
    assert seen
    assert "Read this traceback" in seen[0]


@pytest.mark.asyncio
async def test_second_image_guide_excludes_first_ir(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GATEWAY_ENHANCE_ENABLED", "true")
    monkeypatch.setenv("VISION_COMPOSE_ENABLED", "true")
    monkeypatch.setenv("GATEWAY_VISION_CACHE_DIR", str(tmp_path))
    monkeypatch.setenv("S5_COMPOSED_MODELS", "glm-5.2-vision")
    clear_flag_cache()
    user_texts: list[str] = []

    async def fake_post(url: str, *, headers: dict, json: dict, timeout: float = 60.0):
        texts: list[str] = []
        for m in json.get("messages") or []:
            content = m.get("content")
            if isinstance(content, list):
                for b in content:
                    if isinstance(b, dict) and b.get("type") == "text":
                        texts.append(str(b.get("text") or ""))
        user_texts.append("\n".join(texts))
        n = len(user_texts)
        ir = f"<visual-evidence><pre>SHOT{n}</pre></visual-evidence>"

        class _Resp:
            status_code = 200

            def json(self):
                return {"content": [{"type": "text", "text": ir}]}

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

    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "first screenshot caption"},
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": "image/png", "data": PNG_B64},
                },
            ],
        },
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "second screenshot caption"},
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": "image/png", "data": PNG_B64},
                },
            ],
        },
    ]
    await run_pipeline(
        _env(messages, select_deployment=select, http_post=fake_post)
    )
    assert len(user_texts) == 2
    assert "first screenshot caption" in user_texts[0]
    assert "second screenshot caption" in user_texts[1]
    assert "SHOT1" not in user_texts[1]
    assert "gateway visual translation" not in user_texts[1].lower()
    assert "<visual-evidence><pre>SHOT1" not in user_texts[1]

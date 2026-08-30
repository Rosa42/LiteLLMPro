"""Memory extract queue: enqueue-only + redact."""

from __future__ import annotations

from shared_quota_router.feature_flags import clear_flag_cache
from shared_quota_router.memory_extract import (
    enqueue,
    enqueue_from_kwargs,
    pending_jobs,
    redact,
    reset_queue_for_tests,
)

import pytest


def setup_function() -> None:
    reset_queue_for_tests()


def test_redact_strips_keys() -> None:
    text = "token sk-abcdefghijklmnopqrstuvwxyz and Bearer abc.def"
    out = redact(text)
    assert "sk-abcdefghijklmnopqrstuvwxyz" not in out
    assert "Bearer abc.def" not in out
    assert "[redacted]" in out


def test_enqueue_from_kwargs_only_when_flags_on(monkeypatch) -> None:
    monkeypatch.setenv("GATEWAY_ENHANCE_ENABLED", "true")
    monkeypatch.setenv("GATEWAY_MEMORY_ENABLED", "true")
    monkeypatch.setenv("GATEWAY_MEMORY_EXTRACT_ENABLED", "true")
    clear_flag_cache()
    ok = enqueue_from_kwargs(
        {
            "litellm_metadata": {"workspace_root": r"E:\LiteLLMPro"},
            "messages": [{"role": "user", "content": "hi"}],
        }
    )
    assert ok is True
    assert pending_jobs()


def test_enqueue_skipped_when_extract_off(monkeypatch) -> None:
    monkeypatch.setenv("GATEWAY_ENHANCE_ENABLED", "true")
    monkeypatch.setenv("GATEWAY_MEMORY_ENABLED", "true")
    monkeypatch.delenv("GATEWAY_MEMORY_EXTRACT_ENABLED", raising=False)
    clear_flag_cache()
    assert enqueue_from_kwargs(
        {"litellm_metadata": {"workspace_root": r"E:\LiteLLMPro"}}
    ) is False
    assert pending_jobs() == []


def test_queue_drops_when_full(monkeypatch) -> None:
    monkeypatch.setenv("GATEWAY_ENHANCE_ENABLED", "true")
    monkeypatch.setenv("GATEWAY_MEMORY_ENABLED", "true")
    monkeypatch.setenv("GATEWAY_MEMORY_EXTRACT_ENABLED", "true")
    clear_flag_cache()
    for i in range(32):
        assert enqueue({"i": i}) is True
    assert enqueue({"i": 99}) is False


@pytest.mark.asyncio
async def test_remember_rule_writes_after_process(tmp_path, monkeypatch) -> None:
    from shared_quota_router.memory_extract import process_next_job
    from shared_quota_router.memory_store import load_entries

    monkeypatch.setenv("GATEWAY_ENHANCE_ENABLED", "true")
    monkeypatch.setenv("GATEWAY_MEMORY_ENABLED", "true")
    monkeypatch.setenv("GATEWAY_MEMORY_EXTRACT_ENABLED", "true")
    monkeypatch.setenv("GATEWAY_MEMORY_DIR", str(tmp_path))
    clear_flag_cache()
    ws = str(tmp_path.resolve())
    ok = enqueue_from_kwargs(
        {
            "litellm_call_id": "parent-1",
            "litellm_metadata": {
                "workspace_root": ws,
                "quota_group_id": "volc-c",
            },
            "model_info": {"quota_group_id": "volc-c"},
            "messages": [
                {
                    "role": "user",
                    "content": "please remember that this repo pins LiteLLM v1.90.5",
                }
            ],
        }
    )
    assert ok is True
    assert load_entries(ws) == []
    wrote = await process_next_job()
    assert wrote is True
    texts = [e["text"] for e in load_entries(ws)]
    assert any("LiteLLM v1.90.5" in t for t in texts)


@pytest.mark.asyncio
async def test_extract_http_failure_does_not_write(tmp_path, monkeypatch) -> None:
    from shared_quota_router.memory_extract import process_next_job
    from shared_quota_router.memory_store import load_entries

    monkeypatch.setenv("GATEWAY_ENHANCE_ENABLED", "true")
    monkeypatch.setenv("GATEWAY_MEMORY_ENABLED", "true")
    monkeypatch.setenv("GATEWAY_MEMORY_EXTRACT_ENABLED", "true")
    monkeypatch.setenv("GATEWAY_MEMORY_DIR", str(tmp_path))
    monkeypatch.setenv("GATEWAY_MEMORY_EXTRACT_MODEL", "MiniMax-M2.5")
    clear_flag_cache()
    ws = str(tmp_path.resolve())

    async def boom(*_a, **_k):
        raise RuntimeError("upstream down")

    def select(model, **_kwargs):
        return {
            "model_name": model,
            "model_info": {"quota_group_id": "minimax-official"},
            "litellm_params": {
                "model": "anthropic/MiniMax-M2.5",
                "api_base": "https://api.minimaxi.com/anthropic",
                "api_key": "secret",
            },
        }

    assert enqueue_from_kwargs(
        {
            "litellm_call_id": "parent-2",
            "litellm_metadata": {"workspace_root": ws, "quota_group_id": "volc-c"},
            "model_info": {"quota_group_id": "volc-c"},
            "messages": [{"role": "user", "content": "we decided to pin LiteLLM"}],
            "_sq_select_deployment": select,
            "_sq_http_post": boom,
        }
    )
    assert await process_next_job() is False
    assert load_entries(ws) == []


@pytest.mark.asyncio
async def test_extract_select_uses_trusted_internal(tmp_path, monkeypatch) -> None:
    from shared_quota_router.internal_call import is_trusted_internal
    from shared_quota_router.memory_extract import process_next_job

    monkeypatch.setenv("GATEWAY_ENHANCE_ENABLED", "true")
    monkeypatch.setenv("GATEWAY_MEMORY_ENABLED", "true")
    monkeypatch.setenv("GATEWAY_MEMORY_EXTRACT_ENABLED", "true")
    monkeypatch.setenv("GATEWAY_MEMORY_DIR", str(tmp_path))
    monkeypatch.setenv("GATEWAY_MEMORY_EXTRACT_MODEL", "MiniMax-M2.5")
    clear_flag_cache()
    ws = str(tmp_path.resolve())
    seen: list[bool] = []

    async def boom(*_a, **_k):
        raise RuntimeError("upstream down")

    def select(model, **_kwargs):
        seen.append(is_trusted_internal())
        return {
            "model_name": model,
            "model_info": {"quota_group_id": "minimax-official"},
            "litellm_params": {
                "model": "anthropic/MiniMax-M2.5",
                "api_base": "https://api.minimaxi.com/anthropic",
                "api_key": "secret",
            },
        }

    assert enqueue_from_kwargs(
        {
            "litellm_call_id": "parent-3",
            "litellm_metadata": {"workspace_root": ws, "quota_group_id": "volc-c"},
            "model_info": {"quota_group_id": "volc-c"},
            "messages": [{"role": "user", "content": "we decided to pin LiteLLM"}],
            "_sq_select_deployment": select,
            "_sq_http_post": boom,
        }
    )
    await process_next_job()
    assert seen == [True]
    assert is_trusted_internal() is False

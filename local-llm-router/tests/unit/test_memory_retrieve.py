"""Workspace normalization and JSONL memory retrieve."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from shared_quota_router.feature_flags import clear_flag_cache
from shared_quota_router.memory_store import append_entry, load_entries, memory_file_for
from shared_quota_router.memory_workspace import (
    collect_request_headers,
    normalize_workspace,
    resolve_workspace,
)
from shared_quota_router.models import ApiProtocol
from shared_quota_router.pipeline import EnhanceEnvelope, run_pipeline


def test_normalize_rejects_relative_and_empty() -> None:
    assert normalize_workspace("") is None
    assert normalize_workspace("foo/bar") is None
    assert normalize_workspace("http://example.com/x") is None


def test_normalize_windows_abs_stable_on_any_os() -> None:
    assert normalize_workspace(r"E:\LiteLLMPro") == r"E:\LiteLLMPro"
    assert normalize_workspace("e:/LiteLLMPro/foo/../bar") == r"E:\LiteLLMPro\bar"
    assert normalize_workspace(r"E:\foo\..\..\Windows") is None
    assert normalize_workspace("E:/") is None


def test_normalize_absolute(tmp_path: Path) -> None:
    target = tmp_path / "proj"
    target.mkdir()
    got = normalize_workspace(str(target))
    assert got is not None
    assert Path(got) == target.resolve()


def test_infer_requires_two_absolute_paths(tmp_path: Path) -> None:
    a = tmp_path / "repo" / "a.py"
    b = tmp_path / "repo" / "pkg" / "b.py"
    a.parent.mkdir(parents=True)
    b.parent.mkdir(parents=True)
    a.write_text("x", encoding="utf-8")
    b.write_text("y", encoding="utf-8")
    messages = [
        {"role": "user", "content": f"edit {a} and {b} please remember pin"},
    ]
    ws = resolve_workspace(messages=messages)
    assert ws is not None
    assert Path(ws) == (tmp_path / "repo").resolve()


def test_trusted_header_wins(tmp_path: Path) -> None:
    ws = tmp_path / "trusted"
    ws.mkdir()
    other = tmp_path / "other" / "x.py"
    other.parent.mkdir()
    other.write_text("z", encoding="utf-8")
    got = resolve_workspace(
        headers={"X-Workspace-Root": str(ws)},
        messages=[{"role": "user", "content": str(other)}],
    )
    assert Path(got) == ws.resolve()


def test_collect_headers_from_proxy_server_request() -> None:
    kwargs = {
        "proxy_server_request": {
            "headers": {"x-workspace-root": r"E:\LiteLLMPro"},
        }
    }
    headers = collect_request_headers(kwargs)
    assert resolve_workspace(headers=headers) == r"E:\LiteLLMPro"


def test_top_level_headers_override_proxy_server_request() -> None:
    kwargs = {
        "proxy_server_request": {
            "headers": {"x-workspace-root": r"E:\from-proxy"},
        },
        "headers": {"X-Workspace-Root": r"E:\from-top"},
    }
    headers = collect_request_headers(kwargs)
    assert resolve_workspace(headers=headers) == r"E:\from-top"


def test_jsonl_roundtrip_same_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GATEWAY_MEMORY_DIR", str(tmp_path))
    ws = str((tmp_path / "proj").resolve())
    append_entry(
        ws,
        {
            "id": "n1",
            "ts": "2026-08-25T00:00:00Z",
            "kind": "note",
            "text": "This repo pins LiteLLM v1.90.5",
            "source": "hand",
        },
    )
    assert load_entries(ws)[0]["text"].startswith("This repo pins")
    assert memory_file_for(ws).is_file()


@pytest.mark.asyncio
async def test_retrieve_injects_and_unknown_skips(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GATEWAY_ENHANCE_ENABLED", "true")
    monkeypatch.setenv("GATEWAY_MEMORY_ENABLED", "true")
    monkeypatch.setenv("GATEWAY_MEMORY_DIR", str(tmp_path))
    clear_flag_cache()
    ws = str((tmp_path / "proj").resolve())
    (tmp_path / "proj").mkdir()
    note = {
        "id": "n1",
        "ts": "2026-08-25T00:00:00Z",
        "kind": "note",
        "text": "This repo pins LiteLLM v1.90.5",
        "source": "hand",
    }
    (tmp_path / "proj").mkdir(exist_ok=True)
    memory_file_for(ws).parent.mkdir(parents=True, exist_ok=True)
    memory_file_for(ws).write_text(json.dumps(note) + "\n", encoding="utf-8")

    env = EnhanceEnvelope(
        model_group="glm-5.2",
        protocol=ApiProtocol.ANTHROPIC_MESSAGES,
        streaming=False,
        messages=[{"role": "user", "content": "What LiteLLM version is pinned here?"}],
        workspace=ws,
        visual_evidence=[],
        memory_hits=[],
        internal_call=False,
        parent_request_id="r1",
        parent_quota_group_id="volc-c",
        stage_ms={},
    )
    await run_pipeline(env)
    assert env.memory_hits
    content = env.messages[0]["content"]
    assert isinstance(content, list)
    assert content[0]["type"] == "text"
    assert "<gateway_memory>" in content[0]["text"]
    assert env.messages[0]["role"] == "user"

    env2 = EnhanceEnvelope(
        model_group="glm-5.2",
        protocol=ApiProtocol.ANTHROPIC_MESSAGES,
        streaming=False,
        messages=[{"role": "user", "content": "What LiteLLM version is pinned here?"}],
        workspace=None,
        visual_evidence=[],
        memory_hits=[],
        internal_call=False,
        parent_request_id="r2",
        parent_quota_group_id="volc-c",
        stage_ms={},
    )
    await run_pipeline(env2)
    assert env2.memory_hits == []
    assert env2.messages[0]["content"] == "What LiteLLM version is pinned here?"


@pytest.mark.asyncio
async def test_memory_off_does_not_inject(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GATEWAY_ENHANCE_ENABLED", "true")
    monkeypatch.delenv("GATEWAY_MEMORY_ENABLED", raising=False)
    monkeypatch.setenv("GATEWAY_MEMORY_DIR", str(tmp_path))
    clear_flag_cache()
    env = EnhanceEnvelope(
        model_group="glm-5.2",
        protocol=None,
        streaming=False,
        messages=[{"role": "user", "content": "What LiteLLM version is pinned here?"}],
        workspace=str(tmp_path),
        visual_evidence=[],
        memory_hits=[],
        internal_call=False,
        parent_request_id="r1",
        parent_quota_group_id="volc-c",
        stage_ms={},
    )
    await run_pipeline(env)
    assert env.messages[0]["content"] == "What LiteLLM version is pinned here?"

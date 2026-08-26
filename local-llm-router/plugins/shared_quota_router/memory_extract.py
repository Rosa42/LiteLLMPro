"""Fail-open memory extract queue. Callbacks may only enqueue."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import uuid
from datetime import datetime, timezone
from typing import Any

from shared_quota_router.anthropic_direct import (
    anthropic_headers,
    extract_text_from_messages_response,
    httpx_post_json,
    messages_url,
    resolve_env_ref,
    upstream_model_name,
)
from shared_quota_router.feature_flags import is_gateway_memory_extract_enabled
from shared_quota_router.internal_call import assert_quota_exclusive, child_request_id
from shared_quota_router.memory_store import append_entry
from shared_quota_router.models import ApiProtocol
from shared_quota_router.pipeline import is_internal_call
from shared_quota_router.protocol_errors import ProtocolAwareRoutingError

logger = logging.getLogger(__name__)

_QUEUE_MAX = 32
_queue: list[dict[str, Any]] = []
_AUTO_DRAIN = True
_worker: asyncio.Task[Any] | None = None

_SK = re.compile(r"sk-[A-Za-z0-9]{10,}")
_BEARER = re.compile(r"Bearer\s+\S+", re.I)
_ARK = re.compile(r"ark-[A-Za-z0-9]{8,}")
_APIKEY = re.compile(r"(?i)(api[_-]?key)\s*[:=]\s*['\"]?[^'\"\s]{8,}")
_REMEMBER = re.compile(
    r"(?:记住|please remember|remember that)\s+(.+)",
    re.IGNORECASE | re.DOTALL,
)

EXTRACT_SYSTEM = (
    "Extract durable project facts the user would want remembered across apps. "
    "Reply with plain notes, one per line. No secrets. If nothing durable, reply EMPTY."
)


def redact(text: str) -> str:
    out = _SK.sub("[redacted]", text)
    out = _BEARER.sub("Bearer [redacted]", out)
    out = _ARK.sub("[redacted]", out)
    out = _APIKEY.sub(r"\1=[redacted]", out)
    return out


def reset_queue_for_tests() -> None:
    global _AUTO_DRAIN, _worker
    _queue.clear()
    _AUTO_DRAIN = False
    _worker = None


def pending_jobs() -> list[dict[str, Any]]:
    return list(_queue)


def enqueue(job: dict[str, Any]) -> bool:
    if not is_gateway_memory_extract_enabled():
        return False
    if len(_queue) >= _QUEUE_MAX:
        logger.warning("memory extract queue full; dropping job")
        return False
    _queue.append(job)
    _try_start_worker()
    return True


def _try_start_worker() -> None:
    if not _AUTO_DRAIN:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    global _worker
    if _worker is None or _worker.done():
        _worker = loop.create_task(_drain_worker())


async def _drain_worker() -> None:
    while _queue:
        try:
            await process_next_job()
        except Exception as exc:  # noqa: BLE001 — fail-open
            logger.warning("memory extract worker failed: %s", exc)


def _user_text(messages: Any) -> str:
    chunks: list[str] = []
    if not isinstance(messages, list):
        return ""
    for msg in messages:
        if not isinstance(msg, dict) or msg.get("role") != "user":
            continue
        content = msg.get("content")
        if isinstance(content, str):
            chunks.append(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    chunks.append(str(block.get("text") or ""))
    return "\n".join(chunks)


def _quota_group(kwargs: dict[str, Any]) -> str:
    for source in (
        kwargs.get("model_info"),
        kwargs.get("litellm_metadata"),
        kwargs.get("metadata"),
        kwargs,
    ):
        if isinstance(source, dict) and source.get("quota_group_id"):
            return str(source["quota_group_id"])
    return ""


def enqueue_from_kwargs(kwargs: dict[str, Any] | None) -> bool:
    """Synchronous enqueue only. Never call an upstream model here."""
    if not isinstance(kwargs, dict):
        return False
    if is_internal_call(kwargs):
        return False
    from shared_quota_router.protocol_context import get_metadata_value

    workspace = get_metadata_value(kwargs, "workspace_root")
    if not workspace:
        return False
    user_text = redact(_user_text(kwargs.get("messages")))
    job: dict[str, Any] = {
        "workspace": str(workspace),
        "user_text": user_text,
        "parent_request_id": str(kwargs.get("litellm_call_id") or "unknown"),
        "parent_quota_group_id": _quota_group(kwargs),
        "select_deployment": kwargs.get("_sq_select_deployment"),
        "http_post": kwargs.get("_sq_http_post"),
        "kind": "extract-pending",
    }
    match = _REMEMBER.search(user_text)
    if match:
        job["remember"] = redact(match.group(1)).strip()
    return enqueue(job)


def _write_note(workspace: str, text: str, *, source: str) -> bool:
    cleaned = redact(text).strip()
    if not cleaned:
        return False
    append_entry(
        workspace,
        {
            "id": str(uuid.uuid4()),
            "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "kind": "note",
            "text": cleaned,
            "source": source,
        },
    )
    return True


def _extract_model() -> str:
    return (os.environ.get("GATEWAY_MEMORY_EXTRACT_MODEL") or "").strip()


async def process_next_job() -> bool:
    if not _queue:
        return False
    job = _queue.pop(0)
    return await process_job(job)


async def process_job(job: dict[str, Any]) -> bool:
    try:
        workspace = str(job.get("workspace") or "").strip()
        if not workspace:
            return False
        remember = str(job.get("remember") or "").strip()
        if remember:
            return _write_note(workspace, remember, source="extract")
        return await _extract_with_model(job)
    except Exception as exc:  # noqa: BLE001 — fail-open, never write on error
        logger.warning("memory extract job failed: %s", exc)
        return False


async def _extract_with_model(job: dict[str, Any]) -> bool:
    model = _extract_model()
    if not model:
        return False
    user_text = str(job.get("user_text") or "").strip()
    if not user_text:
        return False
    parent_qg = str(job.get("parent_quota_group_id") or "")
    parent_id = str(job.get("parent_request_id") or "unknown")
    select = job.get("select_deployment")
    if select is None:
        try:
            from shared_quota_router.bootstrap import get_strategy

            strategy = get_strategy()
            select = getattr(strategy, "get_available_deployment", None) if strategy else None
        except Exception:  # noqa: BLE001
            select = None
    if select is None:
        return False
    child_id = child_request_id(parent_id, "memory-extract", uuid.uuid4().hex)
    child_kwargs = {
        "litellm_call_id": child_id,
        "messages": [{"role": "user", "content": user_text[:4000]}],
        "litellm_metadata": {
            "protocol": ApiProtocol.ANTHROPIC_MESSAGES.value,
            "internal_call": True,
            "internal_kind": "memory-extract",
        },
        "metadata": {
            "protocol": ApiProtocol.ANTHROPIC_MESSAGES.value,
            "internal_call": True,
            "internal_kind": "memory-extract",
        },
    }
    child_qg = ""
    try:
        entry = select(
            model,
            messages=child_kwargs["messages"],
            request_kwargs=child_kwargs,
        )
        if not isinstance(entry, dict):
            return False
        info = entry.get("model_info") if isinstance(entry.get("model_info"), dict) else {}
        child_qg = str(info.get("quota_group_id") or "")
        assert_quota_exclusive(
            parent_qg,
            child_qg,
            protocol=ApiProtocol.ANTHROPIC_MESSAGES,
            model_group=model,
        )
        params = entry.get("litellm_params") if isinstance(entry.get("litellm_params"), dict) else {}
        api_base = resolve_env_ref(params.get("api_base"))
        api_key = resolve_env_ref(params.get("api_key"))
        if not api_base or not api_key:
            return False
        post = job.get("http_post") or httpx_post_json
        resp = await post(
            messages_url(api_base),
            headers=anthropic_headers(api_key),
            json={
                "model": upstream_model_name(params.get("model") or model),
                "max_tokens": 512,
                "temperature": 0,
                "system": EXTRACT_SYSTEM,
                "messages": [{"role": "user", "content": user_text[:4000]}],
            },
            timeout=30.0,
        )
        status = int(getattr(resp, "status_code", 200) or 200)
        if status >= 400:
            return False
        payload = resp.json() if hasattr(resp, "json") else {}
        if callable(payload):
            payload = payload()
        text = extract_text_from_messages_response(payload).strip()
        if not text or text.upper() == "EMPTY":
            return False
        return _write_note(str(job["workspace"]), text, source="extract")
    except ProtocolAwareRoutingError:
        return False
    except Exception as exc:  # noqa: BLE001
        logger.warning("memory extract http failed: %s", exc)
        return False
    finally:
        if child_qg:
            try:
                from shared_quota_router.bootstrap import get_strategy

                strategy = get_strategy()
                lease = getattr(strategy, "lease_manager", None) if strategy else None
                if lease is not None:
                    lease.release(quota_group_id=child_qg, request_id=child_id)
            except Exception as exc:  # noqa: BLE001
                logger.warning("memory extract lease release failed: %s", exc)

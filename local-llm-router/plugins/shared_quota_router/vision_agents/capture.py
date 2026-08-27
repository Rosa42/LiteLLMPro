"""Redact a dumped Anthropic Messages request for vision-agent fixtures.

Strips image bytes to a 1x1 PNG, redacts secrets, and drops hop-by-hop
Authorization headers. Never writes original pixels.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Mapping

from shared_quota_router.memory_extract import redact
from shared_quota_router.vision_agents.text import IMAGE_TYPES

logger = logging.getLogger(__name__)

CAPTURE_ENV = "GATEWAY_VISION_AGENT_CAPTURE"

TINY_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQ"
    "AAAABJRU5ErkJggg=="
)

_DROP_HEADERS = frozenset(
    {
        "authorization",
        "x-api-key",
        "api-key",
        "cookie",
        "set-cookie",
        "proxy-authorization",
        "connection",
        "content-length",
        "accept-encoding",
        "transfer-encoding",
    }
)
# OpenCode embeds Windows paths inside JSON strings, so backslashes are often doubled.
_HOME_WIN = re.compile(r"(?i)[a-z]:(?:\\+|/)users(?:\\+|/)[^\\\"/\s]+")
_HOME_UNIX = re.compile(r"(?i)/(?:Users|home)/[^/\s]+")
_SESSION_ID = re.compile(r"ses_[A-Za-z0-9]{8,}")


def redact_text(text: str) -> str:
    out = redact(text)
    out = _HOME_WIN.sub("[path]", out)
    out = _HOME_UNIX.sub("[path]", out)
    return _SESSION_ID.sub("ses_redacted", out)


def redact_headers(headers: Mapping[str, Any] | None) -> dict[str, str]:
    out: dict[str, str] = {}
    if not isinstance(headers, Mapping):
        return out
    for key, value in headers.items():
        name = str(key).lower()
        if name in _DROP_HEADERS:
            continue
        out[name] = redact_text(str(value or ""))
    return out


def _redact_block(block: Any) -> Any:
    if not isinstance(block, dict):
        return block
    btype = str(block.get("type") or "")
    if btype in IMAGE_TYPES:
        source = block.get("source")
        cleaned = dict(block)
        if isinstance(source, dict):
            src = dict(source)
            if src.get("data"):
                src["data"] = TINY_PNG_B64
            cleaned["source"] = src
        elif cleaned.get("data"):
            cleaned["data"] = TINY_PNG_B64
        return cleaned
    if btype in {"text", ""}:
        text = block.get("text")
        if isinstance(text, str):
            return {**block, "text": redact_text(text)}
        return block
    if btype == "tool_result":
        inner = block.get("content")
        cleaned = dict(block)
        cleaned["content"] = redact_content(inner)
        return cleaned
    return block


def redact_content(content: Any) -> Any:
    if isinstance(content, str):
        return redact_text(content)
    if isinstance(content, list):
        return [_redact_block(item) for item in content]
    return content


def redact_messages(messages: Any) -> list[Any]:
    if not isinstance(messages, list):
        return []
    out: list[Any] = []
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        copy_msg = dict(msg)
        copy_msg["content"] = redact_content(msg.get("content"))
        out.append(copy_msg)
    return out


def redact_capture(
    payload: Mapping[str, Any] | None,
) -> dict[str, Any]:
    data = copy.deepcopy(dict(payload or {}))
    headers = data.get("headers")
    if headers is None and isinstance(data.get("proxy_server_request"), dict):
        headers = data["proxy_server_request"].get("headers")
    return {
        "headers": redact_headers(headers if isinstance(headers, Mapping) else {}),
        "messages": redact_messages(data.get("messages")),
    }


def maybe_write_capture(
    headers: Mapping[str, Any] | None,
    messages: Any,
) -> None:
    """If ``GATEWAY_VISION_AGENT_CAPTURE`` is a path, write one redacted dump."""
    dest = (os.environ.get(CAPTURE_ENV) or "").strip()
    if not dest:
        return
    try:
        payload = redact_capture({"headers": headers or {}, "messages": messages})
        payload["provenance"] = {
            "kind": "live-gateway",
            "live_gateway": True,
            "source": "GATEWAY_VISION_AGENT_CAPTURE",
        }
        path = Path(dest)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    except Exception as exc:  # noqa: BLE001 — capture must not fail-close vision
        logger.warning(
            "enhance_vision capture_write_failed type=%s",
            type(exc).__name__,
        )

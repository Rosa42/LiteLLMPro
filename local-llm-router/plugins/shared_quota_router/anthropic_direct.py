"""Direct Anthropic Messages HTTP helpers for nested vision / extract calls.

Does not log api keys. Used by vision compose and memory extract — not a
front gateway.
"""

from __future__ import annotations

import os
import ssl
from typing import Any

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)
ANTHROPIC_VERSION = "2023-06-01"


def messages_url(base: str) -> str:
    b = (base or "").strip().rstrip("/")
    if b.endswith("/v1/messages"):
        return b
    if b.endswith("/v1"):
        return f"{b}/messages"
    return f"{b}/v1/messages"


def resolve_env_ref(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if text.startswith("os.environ/"):
        return (os.environ.get(text.split("/", 1)[1]) or "").strip()
    return text


def upstream_model_name(litellm_model: Any) -> str:
    raw = str(litellm_model or "").strip()
    if "/" in raw:
        return raw.split("/", 1)[1]
    return raw


def extract_text_from_messages_response(body: Any) -> str:
    if not isinstance(body, dict):
        return ""
    content = body.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return str(body.get("text") or "")
    chunks: list[str] = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            chunks.append(str(block.get("text") or ""))
    return "".join(chunks)


def anthropic_headers(api_key: str) -> dict[str, str]:
    return {
        "x-api-key": api_key,
        "anthropic-version": ANTHROPIC_VERSION,
        "content-type": "application/json",
        "User-Agent": DEFAULT_UA,
    }


def _tls_context() -> ssl.SSLContext:
    # MiniMax (and some Windows/Python stacks) EOF unless ALPN is HTTP/1.1.
    ctx = ssl.create_default_context()
    ctx.set_alpn_protocols(["http/1.1"])
    return ctx


async def httpx_post_json(
    url: str,
    *,
    headers: dict[str, str],
    json: dict[str, Any],
    timeout: float = 60.0,
) -> Any:
    import httpx

    async with httpx.AsyncClient(
        timeout=timeout, verify=_tls_context(), http2=False
    ) as client:
        return await client.post(url, headers=headers, json=json)

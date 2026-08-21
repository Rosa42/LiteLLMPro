#!/usr/bin/env python3
"""P0 Probe A: MiniMax-M3 vision via official Anthropic Messages (bypass gateway).

Reads secrets from local-llm-router/.env only. Never prints API keys, full
prompts, or full base64. Direct MiniMax URL — do not send this through the
local gateway (plans.yaml currently has no image feature for MiniMax-M3).

Usage (from local-llm-router):
  F:\\anaconda\\envs\\py312\\python.exe scripts\\p0_probe_a_minimax_vision.py

Transport: http.client HTTPSConnection (not urllib.request.urlopen). On this
Windows/Python combo, urllib hits SSLEOFError against api.minimaxi.com while
http.client with HTTP/1.1 ALPN succeeds. Headers/body still match the existing
Anthropic probe style (x-api-key, anthropic-version, browser User-Agent).
"""

from __future__ import annotations

import http.client
import json
import re
import ssl
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
ENV_PATH = ROOT / ".env"

# Public 1x1 PNG test pixel (not a secret). Still redacted in printed snippets.
PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQ"
    "AAAABJRU5ErkJggg=="
)

PROMPT_TEXT = (
    "You are given one image. If you received image input, "
    "reply with exactly: VISION_OK. Otherwise: VISION_MISSING."
)

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

# MiniMax Anthropic docs: image via URL or base64 (JPEG/PNG/GIF/WEBP).
# Primary = Anthropic native base64 source. Retry = documented URL source
# with a data URI so MiniMax does not need to fetch an external host.
SHAPE_BASE64 = "anthropic_base64_source"
SHAPE_URL = "anthropic_url_source_data_uri"


def load_env(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.is_file():
        raise SystemExit(f"missing .env: {path}")
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def redact(text: str, secrets: list[str]) -> str:
    s = text
    for secret in secrets:
        if secret and secret in s:
            s = s.replace(secret, "***")
    return s


def snippet(raw: str, secrets: list[str], limit: int = 500) -> str:
    return redact(raw.replace("\n", " ").replace("\r", " "), secrets)[:limit]


def messages_url(base: str) -> str:
    b = base.rstrip("/")
    if b.endswith("/v1/messages"):
        return b
    if b.endswith("/v1"):
        return f"{b}/messages"
    return f"{b}/v1/messages"


def image_content(shape: str) -> list[dict[str, Any]]:
    if shape == SHAPE_URL:
        image_block: dict[str, Any] = {
            "type": "image",
            "source": {
                "type": "url",
                "url": f"data:image/png;base64,{PNG_B64}",
            },
        }
    else:
        image_block = {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/png",
                "data": PNG_B64,
            },
        }
    return [
        image_block,
        {"type": "text", "text": PROMPT_TEXT},
    ]


def request_body(shape: str) -> dict[str, Any]:
    return {
        "model": "MiniMax-M3",
        "max_tokens": 64,
        "temperature": 0,
        "messages": [{"role": "user", "content": image_content(shape)}],
    }


def _ssl_context() -> ssl.SSLContext:
    # urllib.request.urlopen hits SSLEOFError on api.minimaxi.com with this
    # Python/Windows combo; http.client + HTTP/1.1 ALPN completes the handshake.
    ctx = ssl.create_default_context()
    ctx.set_alpn_protocols(["http/1.1"])
    return ctx


def http_json(
    url: str,
    *,
    headers: dict[str, str],
    body: dict[str, Any],
    timeout: float = 60.0,
) -> tuple[int, str, dict | list | None]:
    parsed = urlparse(url)
    host = parsed.hostname
    if not host:
        return -1, "ValueError: missing host", None
    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"
    port = parsed.port or 443
    hdrs = dict(headers)
    if "User-Agent" not in hdrs and "user-agent" not in hdrs:
        hdrs["User-Agent"] = DEFAULT_UA
    data = json.dumps(body).encode("utf-8")
    try:
        conn = http.client.HTTPSConnection(
            host, port=port, timeout=timeout, context=_ssl_context()
        )
        try:
            conn.request("POST", path, body=data, headers=hdrs)
            resp = conn.getresponse()
            raw = resp.read().decode("utf-8", errors="replace")
            status = resp.status
        finally:
            conn.close()
    except Exception as e:  # noqa: BLE001 — probe must report transport errors
        reason = getattr(e, "reason", None)
        extra = type(reason).__name__ if reason is not None else ""
        label = type(e).__name__
        if extra and extra != label:
            label = f"{label}/{extra}"
        return -1, f"{label}: {type(e).__name__}", None
    try:
        parsed_json = json.loads(raw) if raw else None
    except json.JSONDecodeError:
        parsed_json = None
    return status, raw, parsed_json


def looks_vision_unsupported(text: str) -> bool:
    t = text.lower()
    patterns = (
        r"does not support (image|vision|multimodal)",
        r"(image|images|vision|multimodal) (is |are )?(not |un)supported",
        r"unsupported (content )?type ['\"]?image",
        r"image input is not (supported|allowed|enabled)",
        r"not support(ed)? image",
        r"images? are not (supported|allowed)",
        r"vision is not (supported|available)",
        r"content type image is not supported",
    )
    return any(re.search(p, t) for p in patterns)


def looks_wrong_image_shape(status: int, text: str) -> bool:
    """4xx that looks like schema/field shape, not 'vision unsupported'."""
    if status not in {400, 422}:
        return False
    if looks_vision_unsupported(text):
        return False
    t = text.lower()
    markers = (
        "invalid",
        "unknown",
        "unexpected",
        "missing",
        "expected",
        "schema",
        "parse",
        "source",
        "media_type",
        "media type",
        "content block",
        "content_block",
        "field",
        "type error",
        "validation",
    )
    return any(m in t for m in markers)


def assistant_text(parsed: dict | list | None) -> str | None:
    """Join Anthropic `content` text blocks. None if the body is not that shape."""
    if not isinstance(parsed, dict):
        return None
    content = parsed.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return None
    parts: list[str] = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            piece = block.get("text")
            if isinstance(piece, str):
                parts.append(piece)
    if not parts:
        return None
    return "".join(parts)


def classify(
    status: int, raw: str, parsed: dict | list | None
) -> tuple[str, int]:
    if status == -1:
        return "INCONCLUSIVE", 3
    if status == 200:
        text = assistant_text(parsed)
        # Parse failure: do not substring-match raw (prompt contains VISION_OK).
        if text is None:
            return "INCONCLUSIVE", 3
        token = text.strip().upper()
        if token == "VISION_OK":
            return "PASS", 0
        if token == "VISION_MISSING":
            return "FAIL", 1
        if looks_vision_unsupported(text) or looks_vision_unsupported(raw):
            return "FAIL", 1
        return "INCONCLUSIVE", 3
    if looks_vision_unsupported(raw):
        return "FAIL", 1
    if status in {400, 404, 415, 422}:
        return "FAIL", 1
    return "INCONCLUSIVE", 3


def main() -> int:
    env = load_env(ENV_PATH)
    key = env.get("MINIMAX_API_KEY") or ""
    base = (env.get("MINIMAX_ANTHROPIC_BASE_URL") or "").rstrip("/")
    if not key or not base:
        print("FAIL missing MINIMAX_API_KEY or MINIMAX_ANTHROPIC_BASE_URL")
        return 2

    secrets = [key, PNG_B64, PROMPT_TEXT]
    url = messages_url(base)
    headers = {
        "Content-Type": "application/json",
        "x-api-key": key,
        "anthropic-version": "2023-06-01",
        "User-Agent": DEFAULT_UA,
    }

    # Host/path only — never print the key. Base URL is an endpoint, not a secret.
    print(f"PROBE_A url={url}")
    print(f"PROBE_A model=MiniMax-M3 shape={SHAPE_BASE64}")

    status, raw, parsed = http_json(
        url, headers=headers, body=request_body(SHAPE_BASE64)
    )
    used_shape = SHAPE_BASE64
    retried = False

    if status == -1:
        err_type = raw.split(":", 1)[0].strip()
        print(f"status={status}")
        print(f"INCONCLUSIVE transport {err_type}")
        print("SUMMARY PROBE_A=INCONCLUSIVE")
        return 3

    if looks_wrong_image_shape(status, raw):
        print(
            f"shape_retry reason=wrong_image_field_shape status={status} "
            f"from={SHAPE_BASE64} to={SHAPE_URL}"
        )
        status, raw, parsed = http_json(
            url, headers=headers, body=request_body(SHAPE_URL)
        )
        used_shape = SHAPE_URL
        retried = True
        if status == -1:
            err_type = raw.split(":", 1)[0].strip()
            print(f"status={status}")
            print(f"INCONCLUSIVE transport {err_type}")
            print("SUMMARY PROBE_A=INCONCLUSIVE")
            return 3

    verdict, code = classify(status, raw, parsed)
    print(f"status={status}")
    print(f"shape_used={used_shape} retried={str(retried).lower()}")
    print(f"body_snippet={snippet(raw, secrets)}")
    print(f"SUMMARY PROBE_A={verdict}")
    return code


if __name__ == "__main__":
    raise SystemExit(main())

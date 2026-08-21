#!/usr/bin/env python3
"""P0 Probe B: does async_pre_call_hook mutation of data['messages'] reach live Messages?

Reads LITELLM_MASTER_KEY and P0_PROBE_B_MARKER from local-llm-router/.env.
Does not write or delete .env (controller sequence owns marker lifecycle).
Never prints master key, full marker, or full prompts.

Usage (from local-llm-router, after container has MARKER SET):
  F:\\anaconda\\envs\\py312\\python.exe scripts\\p0_probe_b_precall_mutate.py
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
ENV_PATH = ROOT / ".env"
PROXY_URL = "http://127.0.0.1:4000/v1/messages"
CLIENT_PROMPT = "Reply with exactly: pong"
DEFAULT_MODEL = "glm-5.2"
DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)


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


def request_body(model: str) -> dict[str, Any]:
    return {
        "model": model,
        "max_tokens": 32,
        "stream": False,
        "messages": [{"role": "user", "content": CLIENT_PROMPT}],
    }


def http_json(
    url: str,
    *,
    headers: dict[str, str],
    body: dict[str, Any],
    timeout: float = 90.0,
) -> tuple[int, str, dict | list | None]:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            status = int(resp.status)
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        status = int(e.code)
    except Exception as e:  # noqa: BLE001 — probe must report transport errors
        return -1, f"{type(e).__name__}: {type(e).__name__}", None
    try:
        parsed_json = json.loads(raw) if raw else None
    except json.JSONDecodeError:
        parsed_json = None
    return status, raw, parsed_json


def classify(status: int, parsed: dict | list | None, marker: str) -> tuple[str, int]:
    if status == -1:
        return "INCONCLUSIVE", 3
    if status != 200:
        return "INCONCLUSIVE", 3
    text = assistant_text(parsed)
    if text is None:
        return "INCONCLUSIVE", 3
    has_marker = marker in text
    has_pong = "pong" in text.lower()
    if has_marker:
        return "PASS", 0
    if has_pong:
        return "FAIL", 1
    return "INCONCLUSIVE", 3


def main() -> int:
    env = load_env(ENV_PATH)
    marker = (env.get("P0_PROBE_B_MARKER") or "").strip()
    if not marker:
        print("FAIL missing marker")
        print("SUMMARY PROBE_B=FAIL")
        return 2
    master = (env.get("LITELLM_MASTER_KEY") or "").strip()
    if not master:
        print("INCONCLUSIVE missing LITELLM_MASTER_KEY")
        print("SUMMARY PROBE_B=INCONCLUSIVE")
        return 3

    model = (env.get("P0_PROBE_B_MODEL") or DEFAULT_MODEL).strip() or DEFAULT_MODEL
    body = request_body(model)
    client_json = json.dumps(body, ensure_ascii=False)
    client_has_marker = marker in client_json
    print(f"PROBE_B url={PROXY_URL}")
    print(f"PROBE_B model={model}")
    print(f"PROBE_B marker_prefix={marker[:4]}")
    print(f"PROBE_B client_body_has_marker={str(client_has_marker).lower()}")
    if client_has_marker:
        print("FAIL client JSON must not contain marker")
        print("SUMMARY PROBE_B=FAIL")
        return 1

    secrets = [master, marker]
    headers = {
        "Content-Type": "application/json",
        "x-api-key": master,
        "anthropic-version": "2023-06-01",
        "User-Agent": DEFAULT_UA,
    }
    status, raw, parsed = http_json(PROXY_URL, headers=headers, body=body)
    if status == -1:
        err_type = raw.split(":", 1)[0].strip()
        print(f"status={status}")
        print(f"INCONCLUSIVE transport {err_type}")
        print("SUMMARY PROBE_B=INCONCLUSIVE")
        return 3

    verdict, code = classify(status, parsed, marker)
    print(f"status={status}")
    print(f"body_snippet={snippet(raw, secrets)}")
    print(f"SUMMARY PROBE_B={verdict}")
    return code


if __name__ == "__main__":
    raise SystemExit(main())

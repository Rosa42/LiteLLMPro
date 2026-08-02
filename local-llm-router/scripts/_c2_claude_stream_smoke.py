#!/usr/bin/env python3
"""C2 canary smoke: claude-opus-4-8 Anthropic Messages streaming."""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path


def load_env(path: str = ".env") -> dict[str, str]:
    out: dict[str, str] = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k] = v.strip().strip('"').strip("'")
    return out


def main() -> int:
    env = load_env()
    mk = env["LITELLM_MASTER_KEY"]
    base = "http://127.0.0.1:4000"
    ua = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    )
    ok = True
    headers = {
        "x-api-key": mk,
        "anthropic-version": "2023-06-01",
        "Content-Type": "application/json",
        "User-Agent": ua,
    }

    def post_stream() -> tuple[int, int, bool]:
        body = json.dumps(
            {
                "model": "claude-opus-4-8",
                "stream": True,
                "max_tokens": 32,
                "messages": [{"role": "user", "content": "Say hi"}],
            }
        ).encode()
        req = urllib.request.Request(
            f"{base}/v1/messages", data=body, method="POST", headers=headers
        )
        try:
            with urllib.request.urlopen(req, timeout=90) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
                events = sum(
                    1
                    for ln in raw.splitlines()
                    if ln.startswith("event:") or ln.startswith("data:")
                )
                stop = "message_stop" in raw
                return resp.getcode(), events, stop
        except urllib.error.HTTPError as exc:
            err = exc.read().decode("utf-8", errors="replace")[:300]
            print(f"  claude stream: HTTP={exc.code} {err}")
            return exc.code, 0, False

    def post_non_stream() -> int:
        body = json.dumps(
            {
                "model": "claude-opus-4-8",
                "max_tokens": 16,
                "messages": [{"role": "user", "content": "pong"}],
            }
        ).encode()
        req = urllib.request.Request(
            f"{base}/v1/messages", data=body, method="POST", headers=headers
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return resp.status
        except urllib.error.HTTPError as exc:
            return exc.code

    print("=== C2: claude anthropic stream (Software B) ===")
    code, events, stop = post_stream()
    print(f"  claude stream: HTTP={code} events={events} message_stop={stop}")
    if code != 200 or events < 1 or not stop:
        ok = False

    print("=== C2: claude non-stream still OK ===")
    ns = post_non_stream()
    print(f"  claude non-stream: HTTP={ns}")
    if ns != 200:
        ok = False

    print("=== C2 negative: claude OpenAI chat should reject ===")
    body = json.dumps(
        {
            "model": "claude-opus-4-8",
            "max_tokens": 16,
            "messages": [{"role": "user", "content": "hi"}],
        }
    ).encode()
    req = urllib.request.Request(
        f"{base}/v1/chat/completions",
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {mk}",
            "Content-Type": "application/json",
            "User-Agent": ua,
        },
    )
    try:
        urllib.request.urlopen(req, timeout=30)
        print("  claude openai chat: unexpected 200")
        ok = False
    except urllib.error.HTTPError as exc:
        print(f"  claude openai chat: HTTP={exc.code} (expect 400)")
        if exc.code != 400:
            ok = False

    if ok:
        print("C2 CLAUDE STREAM SMOKE PASS")
        return 0
    print("C2 CLAUDE STREAM SMOKE FAIL")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

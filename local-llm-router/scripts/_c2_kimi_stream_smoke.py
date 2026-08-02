#!/usr/bin/env python3
"""C2 canary smoke: kimi-k3 OpenAI Chat streaming after plans enable.

Prerequisites: Layer1+Layer2 probes PASS; plans kimi has streaming; proxy restarted.
Baseline: scripts/_dual_protocol_smoke.py (non-stream dual protocol).
"""

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

    def post_stream(model: str) -> tuple[int, int, bool]:
        body = json.dumps(
            {
                "model": model,
                "stream": True,
                "max_tokens": 32,
                "messages": [{"role": "user", "content": "Say hi"}],
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
            with urllib.request.urlopen(req, timeout=90) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
                events = sum(1 for ln in raw.splitlines() if ln.startswith("data:"))
                done = "data: [DONE]" in raw or "[DONE]" in raw
                return resp.getcode(), events, done
        except urllib.error.HTTPError as exc:
            err = exc.read().decode("utf-8", errors="replace")[:300]
            print(f"  {model} stream: HTTP={exc.code} {err}")
            return exc.code, 0, False

    def post_non_stream(model: str) -> int:
        body = json.dumps(
            {
                "model": model,
                "max_tokens": 32,
                "messages": [{"role": "user", "content": "Reply pong"}],
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
            with urllib.request.urlopen(req, timeout=60) as resp:
                return resp.getcode()
        except urllib.error.HTTPError as exc:
            return exc.code

    print("=== C2: kimi stream (Software A) ===")
    code, events, done = post_stream("kimi-k3")
    print(f"  kimi stream: HTTP={code} events={events} done={done}")
    if code != 200 or events < 1 or not done:
        ok = False

    print("=== C2: kimi non-stream still OK ===")
    ns = post_non_stream("kimi-k3")
    print(f"  kimi non-stream: HTTP={ns}")
    if ns != 200:
        ok = False

    print("=== C2: kimi convert anthropic non-stream ===")
    body = json.dumps(
        {
            "model": "kimi-k3",
            "max_tokens": 16,
            "messages": [{"role": "user", "content": "pong"}],
        }
    ).encode()
    req = urllib.request.Request(
        f"{base}/v1/messages",
        data=body,
        method="POST",
        headers={
            "x-api-key": mk,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
            "User-Agent": ua,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            print(f"  kimi convert: HTTP={resp.status}")
            if resp.status != 200:
                ok = False
    except urllib.error.HTTPError as exc:
        print(f"  kimi convert: HTTP={exc.code}")
        ok = False

    print("=== C2 negative: kimi convert anthropic stream ===")
    body = json.dumps(
        {
            "model": "kimi-k3",
            "stream": True,
            "max_tokens": 16,
            "messages": [{"role": "user", "content": "hi"}],
        }
    ).encode()
    req = urllib.request.Request(
        f"{base}/v1/messages",
        data=body,
        method="POST",
        headers={
            "x-api-key": mk,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
            "User-Agent": ua,
        },
    )
    try:
        urllib.request.urlopen(req, timeout=30)
        print("  kimi convert stream: unexpected 200")
        ok = False
    except urllib.error.HTTPError as exc:
        print(f"  kimi convert stream: HTTP={exc.code} (expect 400)")
        if exc.code != 400:
            ok = False

    print("=== C2 regression: deepseek stream still OK ===")
    dc, dev, dd = post_stream("deepseek-v4-flash")
    print(f"  deepseek stream: HTTP={dc} events={dev} done={dd}")
    if dc != 200 or dev < 1 or not dd:
        ok = False

    if ok:
        print("C2 KIMI STREAM SMOKE PASS")
        return 0
    print("C2 KIMI STREAM SMOKE FAIL")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

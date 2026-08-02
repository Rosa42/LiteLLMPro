#!/usr/bin/env python3
"""Dual public protocol smoke: Software A (OpenAI) + Software B (Anthropic).

Non-stream baseline only. For streaming probes use scripts/probe_stream_*.py (B6).
"""
from __future__ import annotations

import json
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
        out[k] = v
    return out


def main() -> None:
    env = load_env()
    mk = env["LITELLM_MASTER_KEY"]
    ua = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    )

    def chat(model: str) -> tuple[int, str]:
        body = json.dumps(
            {
                "model": model,
                "max_tokens": 32,
                "messages": [{"role": "user", "content": "Reply with exactly: pong"}],
            }
        ).encode()
        req = urllib.request.Request(
            "http://127.0.0.1:4000/v1/chat/completions",
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {mk}",
                "Content-Type": "application/json",
                "User-Agent": ua,
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=90) as r:
                return r.status, r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace")

    def messages(model: str) -> tuple[int, str]:
        body = json.dumps(
            {
                "model": model,
                "max_tokens": 32,
                "messages": [{"role": "user", "content": "Reply with exactly: pong"}],
            }
        ).encode()
        req = urllib.request.Request(
            "http://127.0.0.1:4000/v1/messages",
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
            with urllib.request.urlopen(req, timeout=90) as r:
                return r.status, r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace")

    print("=== Software A: OpenAI /v1/chat/completions ===")
    for m in ("deepseek-v4-flash", "kimi-k3"):
        code, raw = chat(m)
        ok = code == 200 and '"choices"' in raw
        print(f"  {m}: HTTP={code} openai_shape={ok} body={raw[:180].replace(chr(10), ' ')}")
        if not ok:
            raise SystemExit(f"FAIL Software A: {m}")

    print("=== Software A negative: glm/claude should NOT be OpenAI-public ===")
    for m in ("glm-5.2", "claude-opus-4-8"):
        code, raw = chat(m)
        print(f"  {m}: HTTP={code} body={raw[:160].replace(chr(10), ' ')}")
        if code == 200:
            raise SystemExit(f"FAIL: {m} must not succeed on Chat without openai_chat opt-in")

    print("=== Software B: Anthropic /v1/messages ===")
    for m in ("glm-5.2", "claude-opus-4-8"):
        code, raw = messages(m)
        ok = code == 200 and "message" in raw and '"choices"' not in raw
        print(f"  {m}: HTTP={code} anthropic_shape={ok} body={raw[:180].replace(chr(10), ' ')}")
        if not ok:
            raise SystemExit(f"FAIL Software B: {m}")

    print("=== Dual: deepseek/kimi still on Anthropic (convert) ===")
    for m in ("deepseek-v4-flash", "kimi-k3"):
        code, raw = messages(m)
        ok = code == 200 and "message" in raw
        print(f"  {m}: HTTP={code} anthropic_ok={ok} body={raw[:160].replace(chr(10), ' ')}")
        if not ok:
            raise SystemExit(f"FAIL dual anthropic: {m}")

    print("=== discovery ===")
    req = urllib.request.Request(
        "http://127.0.0.1:4000/v1/router/model-capabilities",
        headers={"Authorization": f"Bearer {mk}"},
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        cap = json.load(r)
    for x in cap.get("data", []):
        md = x.get("metadata") or {}
        print(f"  {x.get('id')} public={md.get('public_protocols')}")

    print("DUAL PROTOCOL PASS")


if __name__ == "__main__":
    main()

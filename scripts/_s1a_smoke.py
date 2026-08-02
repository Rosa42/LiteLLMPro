#!/usr/bin/env python3
"""S1a canary smoke: A1–A3 / A5 / A6. No secrets printed."""
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

    def post_messages(model: str, *, stream: bool = False) -> tuple[int, str]:
        body: dict = {
            "model": model,
            "max_tokens": 32,
            "messages": [{"role": "user", "content": "Reply with exactly: pong"}],
        }
        if stream:
            body["stream"] = True
        data = json.dumps(body).encode()
        req = urllib.request.Request(
            "http://127.0.0.1:4000/v1/messages",
            data=data,
            method="POST",
            headers={
                "x-api-key": mk,
                "anthropic-version": "2023-06-01",
                "User-Agent": ua,
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=90) as r:
                raw = r.read().decode("utf-8", "replace")
                code = r.status
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", "replace")
            code = e.code
        snip = raw[:280].replace("\n", " ")
        compact = raw.replace(" ", "")
        shape = '"type":"message"' in compact
        choices = '"choices"' in raw
        err = '"type":"error"' in compact
        print(
            f"[{model} stream={stream}] HTTP={code} anthropic_msg={shape} "
            f"choices={choices} err_envelope={err} body={snip}"
        )
        return code, raw

    print("=== A1-A3 ===")
    post_messages("glm-5.2")
    post_messages("claude-opus-4-8")
    post_messages("kimi-k3")
    print("=== A5 ===")
    _c5, raw5 = post_messages("kimi-k3", stream=True)
    print("A5 full:", raw5[:600])
    print("=== A6 ===")
    req = urllib.request.Request(
        "http://127.0.0.1:4000/v1/router/model-capabilities",
        headers={"Authorization": f"Bearer {mk}"},
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        cap = json.load(r)
    for x in cap.get("data", []):
        md = x.get("metadata") or {}
        print(
            f"{x.get('id')} public={md.get('public_protocols')} "
            f"allow_conversion={md.get('allow_conversion')}"
        )


if __name__ == "__main__":
    main()

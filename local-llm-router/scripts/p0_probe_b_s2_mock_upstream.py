#!/usr/bin/env python3
"""P0 Probe B remount S2: live proxy outbound body vs mock probe_marker_hit.

Client POST /v1/messages must not contain the marker. Hard evidence is
GET http://127.0.0.1:18080/probe/last (boolean only; no prompt/key).

Usage (from local-llm-router, mock-s2 published on 18080):
  F:\\anaconda\\envs\\py312\\python.exe scripts\\p0_probe_b_s2_mock_upstream.py --expect miss
  F:\\anaconda\\envs\\py312\\python.exe scripts\\p0_probe_b_s2_mock_upstream.py --expect hit
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
ENV_PATH = ROOT / ".env"
PROXY_URL = "http://127.0.0.1:4000/v1/messages"
MOCK_LAST = "http://127.0.0.1:18080/probe/last"
MOCK_RESET = "http://127.0.0.1:18080/probe/reset"
CLIENT_PROMPT = "Reply with exactly: pong"
DEFAULT_MODEL = "MiniMax-M3"
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


def snippet(raw: str, secrets: list[str], limit: int = 400) -> str:
    return redact(raw.replace("\n", " ").replace("\r", " "), secrets)[:limit]


def http_json(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    body: dict[str, Any] | None = None,
    timeout: float = 90.0,
) -> tuple[int, str, dict | list | None]:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        url, data=data, method=method, headers=headers or {}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            status = int(resp.status)
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        status = int(e.code)
    except Exception as e:  # noqa: BLE001 — probe must report transport errors
        return -1, f"{type(e).__name__}", None
    try:
        parsed_json = json.loads(raw) if raw else None
    except json.JSONDecodeError:
        parsed_json = None
    return status, raw, parsed_json


def classify(
    *,
    expect: str,
    client_has_marker: bool,
    proxy_status: int,
    hit: bool | None,
) -> tuple[str, int]:
    if client_has_marker:
        return "FAIL", 1
    if proxy_status == -1 or hit is None:
        return "INCONCLUSIVE", 3
    if expect == "hit":
        if proxy_status == 200 and hit is True:
            return "PASS", 0
        if proxy_status == 200 and hit is False:
            return "FAIL", 1
        return "INCONCLUSIVE", 3
    # expect miss
    if proxy_status == 200 and hit is False:
        return "PASS", 0
    if proxy_status == 200 and hit is True:
        return "FAIL", 1
    return "INCONCLUSIVE", 3


def main() -> int:
    parser = argparse.ArgumentParser(description="S2 mock-upstream marker probe")
    parser.add_argument("--expect", choices=("hit", "miss"), required=True)
    args = parser.parse_args()

    env = load_env(ENV_PATH)
    marker = (env.get("P0_PROBE_B_MARKER") or "").strip()
    if not marker:
        print("FAIL missing marker in .env (mock needs it to score the body)")
        print("SUMMARY PROBE_B_S2=FAIL")
        return 2
    master = (env.get("LITELLM_MASTER_KEY") or "").strip()
    if not master:
        print("INCONCLUSIVE missing LITELLM_MASTER_KEY")
        print("SUMMARY PROBE_B_S2=INCONCLUSIVE")
        return 3

    model = (env.get("P0_PROBE_B_MODEL") or DEFAULT_MODEL).strip() or DEFAULT_MODEL
    body = {
        "model": model,
        "max_tokens": 32,
        "stream": False,
        "messages": [{"role": "user", "content": CLIENT_PROMPT}],
    }
    client_json = json.dumps(body, ensure_ascii=False)
    client_has_marker = marker in client_json
    print(f"PROBE_B_S2 expect={args.expect}")
    print(f"PROBE_B_S2 url={PROXY_URL}")
    print(f"PROBE_B_S2 model={model}")
    print(f"PROBE_B_S2 marker_prefix={marker[:4]}")
    print(f"PROBE_B_S2 client_body_has_marker={str(client_has_marker).lower()}")
    if client_has_marker:
        print("FAIL client JSON must not contain marker")
        print("SUMMARY PROBE_B_S2=FAIL")
        return 1

    secrets = [master, marker]
    reset_status, _, _ = http_json(MOCK_RESET, timeout=5.0)
    print(f"PROBE_B_S2 mock_reset_status={reset_status}")
    if reset_status != 200:
        print("INCONCLUSIVE mock /probe/reset unreachable")
        print("SUMMARY PROBE_B_S2=INCONCLUSIVE")
        return 3

    headers = {
        "Content-Type": "application/json",
        "x-api-key": master,
        "anthropic-version": "2023-06-01",
        "User-Agent": DEFAULT_UA,
    }
    proxy_status, raw, _parsed = http_json(
        PROXY_URL, method="POST", headers=headers, body=body
    )
    print(f"PROBE_B_S2 proxy_status={proxy_status}")
    print(f"PROBE_B_S2 proxy_snippet={snippet(raw, secrets)}")

    last_status, last_raw, last_parsed = http_json(MOCK_LAST, timeout=5.0)
    hit: bool | None = None
    last_path = ""
    if isinstance(last_parsed, dict) and "probe_marker_hit" in last_parsed:
        hit = bool(last_parsed.get("probe_marker_hit"))
        last_path = str(last_parsed.get("path") or "")
    print(f"PROBE_B_S2 mock_last_status={last_status}")
    print(f"PROBE_B_S2 mock_last_path={last_path or 'empty'}")
    print(f"PROBE_B_S2 probe_marker_hit={hit if hit is not None else 'none'}")
    dumped = snippet(last_raw, secrets)
    print(f"PROBE_B_S2 mock_last_snippet={dumped}")
    if marker in last_raw:
        print("FAIL mock last payload leaked marker")
        print("SUMMARY PROBE_B_S2=FAIL")
        return 1

    verdict, code = classify(
        expect=args.expect,
        client_has_marker=client_has_marker,
        proxy_status=proxy_status,
        hit=hit,
    )
    print(f"SUMMARY PROBE_B_S2={verdict}")
    return code


if __name__ == "__main__":
    raise SystemExit(main())

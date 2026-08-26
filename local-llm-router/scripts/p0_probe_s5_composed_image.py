#!/usr/bin/env python3
"""S5 live probe: IMAGE gate vs composed stub peel.

--expect reject  POST glm-5.2 + image → FEATURE_UNSUPPORTED (mock unused)
--expect peeled  POST glm-5.2-vision + image → mock has_image false,
                 probe_marker_hit true, client JSON has image but no marker

Never prints master key, full marker, or image bytes.
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
PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQ"
    "AAAABJRU5ErkJggg=="
)
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
    return s.replace(PNG_B64, "<png>")


def snippet(raw: str, secrets: list[str], limit: int = 400) -> str:
    return redact(raw.replace("\n", " ").replace("\r", " "), secrets)[:limit]


def image_body(model: str) -> dict[str, Any]:
    return {
        "model": model,
        "max_tokens": 32,
        "stream": False,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/png",
                            "data": PNG_B64,
                        },
                    },
                    {"type": "text", "text": "Reply with exactly: pong"},
                ],
            }
        ],
    }


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
    except Exception as e:  # noqa: BLE001
        return -1, type(e).__name__, None
    try:
        parsed = json.loads(raw) if raw else None
    except json.JSONDecodeError:
        parsed = None
    return status, raw, parsed


def main() -> int:
    parser = argparse.ArgumentParser(description="S5 composed IMAGE probe")
    parser.add_argument("--expect", choices=("reject", "peeled"), required=True)
    parser.add_argument("--model", default="")
    args = parser.parse_args()

    env = load_env(ENV_PATH)
    marker = (env.get("P0_PROBE_B_MARKER") or "").strip()
    master = (env.get("LITELLM_MASTER_KEY") or "").strip()
    if not master:
        print("INCONCLUSIVE missing LITELLM_MASTER_KEY")
        print("SUMMARY PROBE_S5=INCONCLUSIVE")
        return 3
    if args.expect == "peeled" and not marker:
        print("FAIL missing marker in .env")
        print("SUMMARY PROBE_S5=FAIL")
        return 2

    model = (args.model or ("glm-5.2" if args.expect == "reject" else "glm-5.2-vision")).strip()
    body = image_body(model)
    client_json = json.dumps(body, ensure_ascii=False)
    client_has_image = "image" in client_json and PNG_B64 in client_json
    client_has_marker = bool(marker) and marker in client_json
    print(f"PROBE_S5 expect={args.expect}")
    print(f"PROBE_S5 model={model}")
    print(f"PROBE_S5 client_has_image={str(client_has_image).lower()}")
    print(f"PROBE_S5 client_has_marker={str(client_has_marker).lower()}")
    if not client_has_image:
        print("FAIL client must send an image block")
        print("SUMMARY PROBE_S5=FAIL")
        return 1
    if client_has_marker:
        print("FAIL client JSON must not contain marker")
        print("SUMMARY PROBE_S5=FAIL")
        return 1

    secrets = [master, marker]
    headers = {
        "Content-Type": "application/json",
        "x-api-key": master,
        "anthropic-version": "2023-06-01",
        "User-Agent": DEFAULT_UA,
    }

    if args.expect == "reject":
        status, raw, parsed = http_json(
            PROXY_URL, method="POST", headers=headers, body=body
        )
        print(f"PROBE_S5 proxy_status={status}")
        print(f"PROBE_S5 proxy_snippet={snippet(raw, secrets)}")
        reason = ""
        if isinstance(parsed, dict):
            err = parsed.get("error")
            if isinstance(err, dict):
                reason = str(err.get("code") or err.get("type") or "")
                sq = err.get("shared_quota")
                if isinstance(sq, dict):
                    reason = str(sq.get("reason") or reason)
        blob = raw.lower()
        rejected = status in {400, 422} and (
            "feature_unsupported" in blob
            or reason == "feature_unsupported"
            or ("required features" in blob and "image" in blob)
        )
        if rejected:
            print("SUMMARY PROBE_S5=PASS")
            return 0
        if status == -1:
            print("SUMMARY PROBE_S5=INCONCLUSIVE")
            return 3
        print("SUMMARY PROBE_S5=FAIL")
        return 1

    reset_status, _, _ = http_json(MOCK_RESET, timeout=5.0)
    print(f"PROBE_S5 mock_reset_status={reset_status}")
    if reset_status != 200:
        print("INCONCLUSIVE mock /probe/reset unreachable")
        print("SUMMARY PROBE_S5=INCONCLUSIVE")
        return 3

    status, raw, _parsed = http_json(
        PROXY_URL, method="POST", headers=headers, body=body
    )
    print(f"PROBE_S5 proxy_status={status}")
    print(f"PROBE_S5 proxy_snippet={snippet(raw, secrets)}")

    last_status, last_raw, last_parsed = http_json(MOCK_LAST, timeout=5.0)
    hit = None
    has_image = None
    last_path = ""
    if isinstance(last_parsed, dict):
        if "probe_marker_hit" in last_parsed:
            hit = bool(last_parsed.get("probe_marker_hit"))
        if "has_image" in last_parsed:
            has_image = bool(last_parsed.get("has_image"))
        last_path = str(last_parsed.get("path") or "")
    print(f"PROBE_S5 mock_last_status={last_status}")
    print(f"PROBE_S5 mock_last_path={last_path or 'empty'}")
    print(f"PROBE_S5 has_image={has_image if has_image is not None else 'none'}")
    print(f"PROBE_S5 probe_marker_hit={hit if hit is not None else 'none'}")
    print(f"PROBE_S5 mock_last_snippet={snippet(last_raw, secrets)}")
    if marker and marker in last_raw:
        print("FAIL mock last payload leaked marker")
        print("SUMMARY PROBE_S5=FAIL")
        return 1
    if (
        status == 200
        and last_path.endswith("/messages")
        and has_image is False
        and hit is True
    ):
        print("SUMMARY PROBE_S5=PASS")
        return 0
    if status == 200 and has_image is True:
        print("SUMMARY PROBE_S5=FAIL")
        return 1
    print("SUMMARY PROBE_S5=INCONCLUSIVE")
    return 3


if __name__ == "__main__":
    raise SystemExit(main())

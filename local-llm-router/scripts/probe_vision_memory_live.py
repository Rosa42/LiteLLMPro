#!/usr/bin/env python3
"""Live probe: glm-5.2 IMAGE gate, glm-5.2-vision peel, memory inject.

Never prints master key, image bytes, or workspace path contents beyond a hash prefix.
"""

from __future__ import annotations

import argparse
import hashlib
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


def http_json(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    body: dict[str, Any] | None = None,
    timeout: float = 120.0,
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


def image_body(model: str, text: str, png_b64: str | None = None) -> dict[str, Any]:
    data = png_b64 or PNG_B64
    return {
        "model": model,
        "max_tokens": 64,
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
                            "data": data,
                        },
                    },
                    {"type": "text", "text": text},
                ],
            }
        ],
    }


def text_body(model: str, text: str) -> dict[str, Any]:
    return {
        "model": model,
        "max_tokens": 64,
        "stream": False,
        "messages": [{"role": "user", "content": text}],
    }


def vision_fail_closed(reason: str, raw: str) -> bool:
    """True only for a vision-stage reject — not because the model name contains 'vision'."""
    blob = f"{reason} {raw}".lower()
    if "peel=disabled" in blob or "s5 peeled" in blob or "图片已省略" in blob:
        return False
    if "vision translate" in blob or "visual-evidence" in reason.lower():
        return True
    if reason.lower().startswith("vision:") or ":vision" in reason.lower():
        return True
    return "details" in blob and '"vision"' in blob and "glm-5.2-vision" not in reason.lower()


def sq_reason(parsed: Any) -> str:
    if not isinstance(parsed, dict):
        return ""
    err = parsed.get("error")
    if not isinstance(err, dict):
        return ""
    reason = str(err.get("code") or err.get("type") or "")
    sq = err.get("shared_quota")
    if isinstance(sq, dict):
        reason = str(sq.get("reason") or reason)
        details = sq.get("details")
        if isinstance(details, dict) and details.get("vision"):
            return f"{reason}:{details.get('vision')}"
        if isinstance(details, dict) and details.get("composed_peel"):
            return f"{reason}:peel={details.get('composed_peel')}"
    return reason


def mock_last() -> dict[str, Any]:
    status, _raw, parsed = http_json(MOCK_LAST, timeout=5.0)
    if status != 200 or not isinstance(parsed, dict):
        return {"reachable": status == 200, "status": status}
    parsed["reachable"] = True
    parsed["status"] = status
    return parsed


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--leg",
        choices=("reject", "vision", "memory-known", "memory-unknown", "no-image", "extract-remember"),
        required=True,
    )
    parser.add_argument("--workspace", default="")
    parser.add_argument("--png-file", default="")
    parser.add_argument("--token", default="")
    args = parser.parse_args()
    env = load_env(ENV_PATH)
    master = (env.get("LITELLM_MASTER_KEY") or "").strip()
    if not master:
        print("INCONCLUSIVE missing LITELLM_MASTER_KEY")
        return 3
    secrets = [master]
    headers = {
        "Content-Type": "application/json",
        "x-api-key": master,
        "anthropic-version": "2023-06-01",
        "User-Agent": DEFAULT_UA,
    }
    ws = args.workspace.strip()
    if ws:
        headers["X-Workspace-Root"] = ws
        secrets.append(ws)

    png_b64 = PNG_B64
    png_path = args.png_file.strip()
    if png_path:
        blob = Path(png_path).read_bytes()
        import base64

        png_b64 = base64.b64encode(blob).decode("ascii")
        secrets.append(png_b64)

    if args.leg == "reject":
        status, raw, parsed = http_json(
            PROXY_URL, method="POST", headers=headers, body=image_body("glm-5.2", "pong", png_b64)
        )
        reason = sq_reason(parsed)
        blob = raw.lower()
        rejected = status in {400, 422} and (
            "feature_unsupported" in blob
            or "feature_unsupported" in reason
            or ("required features" in blob and "image" in blob)
        )
        print(f"LEG reject status={status} reason={reason or 'n/a'}")
        print(f"snippet={snippet(raw, secrets)}")
        print("SUMMARY " + ("PASS" if rejected else ("INCONCLUSIVE" if status == -1 else "FAIL")))
        return 0 if rejected else (3 if status == -1 else 1)

    if args.leg == "no-image":
        http_json(MOCK_RESET, timeout=5.0)
        status, raw, _parsed = http_json(
            PROXY_URL,
            method="POST",
            headers=headers,
            body=text_body("glm-5.2-vision", "Reply with exactly: pong"),
        )
        last = mock_last()
        print(f"LEG no-image status={status} mock_has_image={last.get('has_image')}")
        print(f"snippet={snippet(raw, secrets)}")
        stub = "s5 peeled" in raw.lower() or "图片已省略" in raw
        ok = status == 200 and not stub
        if last.get("reachable") is True:
            ok = ok and last.get("has_image") is False
        print("SUMMARY " + ("PASS" if ok else ("INCONCLUSIVE" if status == -1 else "FAIL")))
        return 0 if ok else (3 if status == -1 else 1)

    if args.leg == "vision":
        http_json(MOCK_RESET, timeout=5.0)
        status, raw, parsed = http_json(
            PROXY_URL,
            method="POST",
            headers=headers,
            body=image_body("glm-5.2-vision", "Reply with exactly: pong", png_b64),
        )
        reason = sq_reason(parsed)
        last = mock_last()
        stub = "s5 peeled" in raw.lower() or "图片已省略" in raw
        print(
            f"LEG vision status={status} reason={reason or 'n/a'} "
            f"mock_has_image={last.get('has_image')} mock_path={last.get('path')}"
        )
        print(f"snippet={snippet(raw, secrets)}")
        if stub or last.get("has_image") is True:
            print("SUMMARY FAIL")
            return 1
        if status == 200 and last.get("has_image") is False:
            print("SUMMARY PASS")
            return 0
        if status == 200 and last.get("reachable") is not True:
            print("SUMMARY PASS live-execute-no-mock")
            return 0
        if status in {400, 422} and vision_fail_closed(reason, raw):
            print("SUMMARY PASS fail-closed-after-translate")
            return 0
        print("SUMMARY " + ("INCONCLUSIVE" if status == -1 else "FAIL"))
        return 3 if status == -1 else 1

    if args.leg == "memory-known":
        http_json(MOCK_RESET, timeout=5.0)
        status, raw, _parsed = http_json(
            PROXY_URL,
            method="POST",
            headers=headers,
            body=text_body(
                "glm-5.2",
                "What LiteLLM version is pinned here? Reply with the version only.",
            ),
        )
        last = mock_last()
        print(
            f"LEG memory-known status={status} "
            f"has_gateway_memory={last.get('has_gateway_memory')} "
            f"ws_hash={hashlib.sha256(ws.encode()).hexdigest()[:8] if ws else 'none'}"
        )
        print(f"snippet={snippet(raw, secrets)}")
        if last.get("reachable") is True:
            ok = status == 200 and last.get("has_gateway_memory") is True
        else:
            ok = status == 200 and "gateway_memory" in raw.lower()
        print("SUMMARY " + ("PASS" if ok else "FAIL"))
        return 0 if ok else 1

    if args.leg == "memory-unknown":
        headers.pop("X-Workspace-Root", None)
        http_json(MOCK_RESET, timeout=5.0)
        status, raw, _parsed = http_json(
            PROXY_URL,
            method="POST",
            headers=headers,
            body=text_body("glm-5.2", "What LiteLLM version is pinned here?"),
        )
        last = mock_last()
        print(
            f"LEG memory-unknown status={status} "
            f"has_gateway_memory={last.get('has_gateway_memory')}"
        )
        print(f"snippet={snippet(raw, secrets)}")
        if last.get("reachable") is True:
            ok = status == 200 and last.get("has_gateway_memory") is not True
        else:
            ok = status == 200 and "gateway_memory" not in raw.lower()
        print("SUMMARY " + ("PASS" if ok else "FAIL"))
        return 0 if ok else 1

    if args.leg == "extract-remember":
        import time as _time

        if not ws:
            print("INCONCLUSIVE missing --workspace")
            return 3
        token = (args.token or hashlib.sha256(str(_time.time()).encode()).hexdigest()[:12]).strip()
        secrets.append(token)
        before = ""
        mem_path = ROOT / "data" / "gateway-memory" / (
            hashlib.sha256(ws.encode("utf-8")).hexdigest()[:32] + ".jsonl"
        )
        if mem_path.is_file():
            before = mem_path.read_text(encoding="utf-8")
        status, raw, _parsed = http_json(
            PROXY_URL,
            method="POST",
            headers=headers,
            body=text_body(
                "glm-5.2",
                f"please remember that extract-probe-{token} is the live extract marker",
            ),
        )
        wrote = False
        if status == 200:
            for _ in range(20):
                _time.sleep(0.25)
                now = mem_path.read_text(encoding="utf-8") if mem_path.is_file() else ""
                if token in now and now != before:
                    wrote = True
                    break
        print(f"LEG extract-remember status={status} wrote={wrote}")
        print(f"snippet={snippet(raw, secrets)}")
        ok = status == 200 and wrote
        print("SUMMARY " + ("PASS" if ok else "FAIL"))
        return 0 if ok else 1

    return 2


if __name__ == "__main__":
    raise SystemExit(main())

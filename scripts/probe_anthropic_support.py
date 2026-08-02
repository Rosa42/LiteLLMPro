#!/usr/bin/env python3
"""Probe upstream providers for Anthropic Messages API support.

Reads secrets from local .env only. Never prints API keys.
Usage (from local-llm-router):
  .\\.venv\\Scripts\\python.exe scripts\\probe_anthropic_support.py
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENV_PATH = ROOT / ".env"

ANTHROPIC_BODY = {
    "max_tokens": 32,
    "messages": [{"role": "user", "content": "Reply with exactly: pong"}],
    "temperature": 0,
}

CHAT_BODY = {
    "max_tokens": 32,
    "messages": [{"role": "user", "content": "Reply with exactly: pong"}],
    "temperature": 0,
}


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


DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)


def http_json(
    method: str,
    url: str,
    *,
    headers: dict[str, str],
    body: dict | None = None,
    timeout: float = 45.0,
) -> tuple[int, str, dict | list | None]:
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method)
    # Cloudflare (e.g. OpenCode) returns 1010 without a browser-like UA.
    if "User-Agent" not in headers and "user-agent" not in headers:
        req.add_header("User-Agent", DEFAULT_UA)
    for k, v in headers.items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            status = resp.getcode()
            try:
                parsed = json.loads(raw) if raw else None
            except json.JSONDecodeError:
                parsed = None
            return status, raw[:1200], parsed
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(raw) if raw else None
        except json.JSONDecodeError:
            parsed = None
        return e.code, raw[:1200], parsed
    except Exception as e:  # noqa: BLE001 — probe must report transport errors
        return -1, f"{type(e).__name__}: {e}", None


def looks_anthropic_ok(status: int, parsed: dict | list | None) -> bool:
    if status != 200 or not isinstance(parsed, dict):
        return False
    # Anthropic Messages success shape
    if parsed.get("type") == "message" and "content" in parsed:
        return True
    # Some gateways wrap or partially emulate
    if "content" in parsed and isinstance(parsed.get("content"), list):
        return True
    return False


def looks_chat_ok(status: int, parsed: dict | list | None) -> bool:
    if status != 200 or not isinstance(parsed, dict):
        return False
    return "choices" in parsed


def classify_anthropic(status: int, body: str, parsed: dict | list | None) -> str:
    if status == 200 and looks_anthropic_ok(status, parsed):
        return "SUPPORTED (Anthropic Messages 200)"
    if status == 200 and looks_chat_ok(status, parsed):
        return "PARTIAL/WRONG_SHAPE (200 but OpenAI Chat body)"
    if status == 200:
        return "UNKNOWN_200 (non-Anthropic JSON shape)"
    if status in (401, 403):
        return "AUTH_FAIL (endpoint may exist)"
    if status == 404:
        return "NOT_FOUND (no Anthropic path)"
    if status == 405:
        return "METHOD_NOT_ALLOWED"
    if status in (400, 422):
        # Often means path exists but model/payload rejected
        return "PATH_LIKELY_EXISTS (4xx validation/model)"
    if status == -1:
        return f"TRANSPORT_ERROR ({body.split(':', 1)[0]})"
    return f"UNSUPPORTED_OR_ERROR (HTTP {status})"


def collect_urls(base: str, extra_bases: list[str] | None = None) -> list[tuple[str, str]]:
    """Build candidate Anthropic Messages URLs from one or more bases."""
    bases = [base.rstrip("/")]
    for b in extra_bases or []:
        if b and b.rstrip("/") not in bases:
            bases.append(b.rstrip("/"))

    candidates: list[tuple[str, str]] = []
    for b in bases:
        candidates.append((f"msg@{b}", f"{b}/messages"))
        candidates.append((f"v1msg@{b}", f"{b}/v1/messages"))
        if b.endswith("/v1"):
            root = b[: -len("/v1")]
            candidates.append((f"root_v1msg@{root}", f"{root}/v1/messages"))
            candidates.append((f"root_msg@{root}", f"{root}/messages"))
        if b.endswith("/v3"):
            parent = b[: -len("/v3")]
            candidates.append((f"coding_msg@{parent}", f"{parent}/messages"))
            candidates.append((f"coding_v1msg@{parent}", f"{parent}/v1/messages"))

    seen: set[str] = set()
    uniq: list[tuple[str, str]] = []
    for label, url in candidates:
        if url not in seen:
            seen.add(url)
            uniq.append((label, url))
    return uniq


def probe_provider(
    name: str,
    *,
    base_url: str,
    api_key: str,
    models: list[str],
    secrets: list[str],
    anthropic_bases: list[str] | None = None,
    chat_models: list[str] | None = None,
) -> None:
    print("=" * 72)
    print(f"PROVIDER: {name}")
    print(f"CHAT_BASE: {base_url}")
    if anthropic_bases:
        print(f"ANTHROPIC_BASES: {', '.join(anthropic_bases)}")
    print("-" * 72)

    base = base_url.rstrip("/")
    uniq = collect_urls(base, anthropic_bases)
    model = models[0]
    chat_model = (chat_models or models)[0]

    # Baseline: OpenAI Chat (sanity that key/base work)
    chat_url = f"{base}/chat/completions"
    chat_headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    chat_body = {**CHAT_BODY, "model": chat_model}
    st, raw, parsed = http_json("POST", chat_url, headers=chat_headers, body=chat_body)
    chat_verdict = (
        "OK" if looks_chat_ok(st, parsed) else classify_anthropic(st, raw, parsed)
    )
    print(f"[CHAT baseline] POST {chat_url}")
    print(f"  model={chat_model} status={st} verdict={chat_verdict}")
    print(f"  body={redact(raw.replace(chr(10), ' ')[:400], secrets)}")

    # If chat failed with 404, try /v1/chat/completions when base lacks /v1
    if st in (404, -1) and not base.endswith("/v1"):
        alt = f"{base}/v1/chat/completions"
        st2, raw2, parsed2 = http_json(
            "POST", alt, headers=chat_headers, body=chat_body
        )
        print(f"[CHAT alt] POST {alt}")
        print(
            f"  status={st2} verdict="
            f"{'OK' if looks_chat_ok(st2, parsed2) else classify_anthropic(st2, raw2, parsed2)}"
        )
        print(f"  body={redact(raw2.replace(chr(10), ' ')[:400], secrets)}")

    print()
    print("Anthropic Messages probes:")
    any_supported = False
    path_exists = False
    for label, url in uniq:
        for auth_mode in ("x-api-key", "bearer"):
            headers = {
                "Content-Type": "application/json",
                "anthropic-version": "2023-06-01",
            }
            if auth_mode == "x-api-key":
                headers["x-api-key"] = api_key
            else:
                headers["Authorization"] = f"Bearer {api_key}"

            body = {**ANTHROPIC_BODY, "model": model}
            st, raw, parsed = http_json("POST", url, headers=headers, body=body)
            verdict = classify_anthropic(st, raw, parsed)
            if verdict.startswith("SUPPORTED"):
                any_supported = True
            if verdict.startswith(("SUPPORTED", "PATH_LIKELY_EXISTS", "AUTH_FAIL")):
                path_exists = True
            print(f"  [{label}|{auth_mode}] POST {url}")
            print(f"    model={model} status={st} verdict={verdict}")
            print(f"    body={redact(raw.replace(chr(10), ' ')[:350], secrets)}")

            # If path exists but model rejected, try remaining models
            if (
                verdict.startswith(("PATH_LIKELY_EXISTS", "AUTH_FAIL"))
                or (st == 400 and not verdict.startswith("SUPPORTED"))
            ) and len(models) > 1:
                for m2 in models[1:4]:
                    body2 = {**ANTHROPIC_BODY, "model": m2}
                    st3, raw3, parsed3 = http_json(
                        "POST", url, headers=headers, body=body2
                    )
                    v3 = classify_anthropic(st3, raw3, parsed3)
                    if v3.startswith("SUPPORTED"):
                        any_supported = True
                    if v3.startswith(("SUPPORTED", "PATH_LIKELY_EXISTS", "AUTH_FAIL")):
                        path_exists = True
                    print(f"    retry model={m2} status={st3} verdict={v3}")
                    print(
                        f"    body={redact(raw3.replace(chr(10), ' ')[:300], secrets)}"
                    )
                    if v3.startswith("SUPPORTED"):
                        break
            if any_supported:
                break
        if any_supported:
            break

    print()
    if any_supported:
        summary = "ANTHROPIC_SUPPORTED"
    elif path_exists:
        summary = "ANTHROPIC_PATH_EXISTS_BUT_NO_SUCCESS"
    else:
        summary = "ANTHROPIC_NOT_CONFIRMED"
    print(f"SUMMARY {name}: {summary}")
    print()


def main() -> int:
    env = load_env(ENV_PATH)
    secrets = [
        env.get("OPENCODE_GO_KEY_A", ""),
        env.get("OPENCODE_GO_KEY_B", ""),
        env.get("VOLC_CODING_KEY_C", ""),
        env.get("PLAN_NEWAPI_A_API_KEY", ""),
    ]

    opencode_base = env.get("OPENCODE_GO_BASE_URL", "")
    volc_chat = env.get("VOLC_CODING_BASE_URL", "")
    # Official Anthropic base for Volc Coding Plan (without /v3)
    volc_anthropic = "https://ark.cn-beijing.volces.com/api/coding"
    newapi_base = env.get("PLAN_NEWAPI_A_BASE_URL", "")

    providers = [
        {
            "name": "OpenCode Go",
            "base_url": opencode_base,
            "api_key": env.get("OPENCODE_GO_KEY_A") or env.get("OPENCODE_GO_KEY_B", ""),
            # qwen3.7-max documented as Anthropic-only on Go; also try glm + claude ids
            "models": ["qwen3.7-max", "glm-5.2", "kimi-k3", "claude-sonnet-4-5"],
            "chat_models": ["glm-5.2", "kimi-k3"],
            "anthropic_bases": [
                opencode_base,
                "https://opencode.ai/zen/go",
                "https://opencode.ai/zen/go/v1",
            ],
        },
        {
            "name": "Volc Coding Plan",
            "base_url": volc_chat,
            "api_key": env.get("VOLC_CODING_KEY_C", ""),
            "models": ["ark-code-latest", "glm-5.2", "doubao-seed-2.0-code"],
            "chat_models": ["glm-5.2", "ark-code-latest"],
            "anthropic_bases": [volc_anthropic, volc_chat],
        },
        {
            "name": "NewAPI",
            "base_url": newapi_base,
            "api_key": env.get("PLAN_NEWAPI_A_API_KEY", ""),
            "models": ["claude-opus-4-8", "claude-fable-5", "claude-3-5-sonnet-20241022"],
            "chat_models": ["claude-opus-4-8", "gpt-4o-mini"],
            "anthropic_bases": [
                newapi_base,
                f"{newapi_base.rstrip('/')}/v1" if newapi_base else "",
            ],
        },
    ]

    missing = [p["name"] for p in providers if not p["base_url"] or not p["api_key"]]
    if missing:
        print("Missing base_url or api_key for:", ", ".join(missing), file=sys.stderr)

    for p in providers:
        if not p["base_url"] or not p["api_key"]:
            print(f"SKIP {p['name']}: incomplete credentials")
            continue
        probe_provider(
            p["name"],
            base_url=p["base_url"],
            api_key=p["api_key"],
            models=p["models"],
            secrets=secrets,
            anthropic_bases=p.get("anthropic_bases"),
            chat_models=p.get("chat_models"),
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

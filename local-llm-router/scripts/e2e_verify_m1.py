"""End-to-end verification for M1 deliverables against a running proxy.

Usage (proxy must be up on 127.0.0.1:4000):
  set PYTHONPATH=plugins
  python scripts/e2e_verify_m1.py
"""

from __future__ import annotations

import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load_key() -> str:
    for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
        if line.strip().startswith("LITELLM_MASTER_KEY="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise SystemExit("LITELLM_MASTER_KEY missing in .env")


def main() -> int:
    key = _load_key()
    base = "http://127.0.0.1:4000"
    auth = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    rows: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        rows.append((name, bool(ok), detail))
        print(f"[{'PASS' if ok else 'FAIL'}] {name}: {detail}")

    def get(path: str) -> tuple[int, dict | list | str]:
        req = urllib.request.Request(
            base + path, headers={"Authorization": f"Bearer {key}"}
        )
        with urllib.request.urlopen(req, timeout=20) as r:
            raw = r.read().decode()
            try:
                return r.status, json.loads(raw)
            except json.JSONDecodeError:
                return r.status, raw

    def post(path: str, body: dict) -> tuple[int, dict]:
        data = json.dumps(body).encode()
        req = urllib.request.Request(
            base + path, data=data, headers=auth, method="POST"
        )
        with urllib.request.urlopen(req, timeout=90) as r:
            return r.status, json.loads(r.read().decode())

    def protos(m: dict) -> list[str]:
        p = (m.get("metadata") or {}).get("public_protocols")
        if p is None:
            return []
        if isinstance(p, str):
            return [p]
        return list(p)

    # --- offline config ---
    text = (ROOT / "config" / "litellm.yaml").read_text(encoding="ascii")
    check("config upstream_protocol", "upstream_protocol: openai_chat" in text)
    check("config public_protocols", "public_protocols: [openai_chat]" in text)
    check(
        "config no secret-like material",
        not re.search(r"sk-[a-zA-Z0-9]{10,}", text) and "Bearer " not in text,
    )
    check(
        "config api_key env refs",
        all("os.environ/" in ln for ln in text.splitlines() if "api_key:" in ln),
    )

    # --- live ---
    try:
        req = urllib.request.Request(base + "/health/liveliness")
        with urllib.request.urlopen(req, timeout=5) as r:
            check("health/liveliness", r.status == 200, r.read().decode()[:48])
    except Exception as exc:  # noqa: BLE001
        check("health/liveliness", False, str(exc))
        print("Proxy down; remaining live checks skipped.")
        return 1

    st, models = get("/v1/models")
    assert isinstance(models, dict)
    ids = [m["id"] for m in models.get("data", [])]
    has_pp = any(protos(m) for m in models.get("data", []))
    check(
        "GET /v1/models",
        st == 200 and len(ids) > 0,
        f"count={len(ids)} stock_has_public_protocols={has_pp}",
    )
    check("v1/models includes kimi-k3", "kimi-k3" in ids)
    # Document: stock /v1/models does not carry public_protocols in v1.90.5
    check(
        "v1/models lacks protocol metadata (expected v1.90.5)",
        has_pp is False,
        "custom public_protocols not on stock listing",
    )

    st, caps = get("/v1/router/model-capabilities")
    assert isinstance(caps, dict)
    cids = [m["id"] for m in caps.get("data", [])]
    kimi = next((m for m in caps.get("data", []) if m["id"] == "kimi-k3"), None)
    claude_n = sum(1 for i in cids if str(i).startswith("claude"))
    all_chat = all(protos(m) == ["openai_chat"] for m in caps.get("data", []))
    check(
        "GET /v1/router/model-capabilities",
        st == 200 and caps.get("source") == "shared_quota_router.discovery",
        f"count={len(cids)} source={caps.get('source')}",
    )
    check(
        "capabilities: chat-only, no claude, kimi ok",
        all_chat and claude_n == 0 and kimi is not None and protos(kimi) == ["openai_chat"],
        f"all_chat={all_chat} claude={claude_n} kimi={protos(kimi) if kimi else None}",
    )
    check("capabilities disclaimer", bool(caps.get("disclaimer")))

    st2, _ = get("/shared-quota/v1/model-capabilities")
    check("GET capabilities alias", st2 == 200)

    for model in ("kimi-k3", "glm-5.2"):
        try:
            st, chat = post(
                "/v1/chat/completions",
                {
                    "model": model,
                    "messages": [{"role": "user", "content": "Reply with only OK"}],
                    "max_tokens": 32,
                },
            )
            usage = (chat.get("usage") or {}).get("total_tokens") or 0
            msg = (chat.get("choices") or [{}])[0].get("message") or {}
            has_out = bool(msg.get("content") or msg.get("reasoning_content"))
            check(
                f"POST chat/{model}",
                st == 200 and usage > 0 and has_out,
                f"status={st} tokens={usage} has_output={has_out}",
            )
        except urllib.error.HTTPError as e:
            body = e.read()[:180]
            check(f"POST chat/{model}", False, f"HTTP {e.code}: {body!r}")

    try:
        st, _resp = post("/v1/responses", {"model": "kimi-k3", "input": "ping"})
        check(
            "POST /v1/responses (observed)",
            True,
            f"status={st} note=M3 gate not implemented; LiteLLM path still open",
        )
    except urllib.error.HTTPError as e:
        check(
            "POST /v1/responses (observed)",
            True,
            f"status={e.code} note=upstream/path error recorded",
        )

    failed = sum(1 for _, ok, _ in rows if not ok)
    print("==== SUMMARY ====")
    print(f"PASS={len(rows) - failed} FAIL={failed} TOTAL={len(rows)}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

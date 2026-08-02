#!/usr/bin/env python3
"""Layer2: stream probe via LiteLLM Proxy public entry (P0-PROBE B6).

Non-stream baseline: scripts/_dual_protocol_smoke.py
Exit 0 = PASS, 1 = FAIL.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENV_PATH = ROOT / ".env"
DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


@dataclass
class ProxyStreamProbeReport:
    layer: str = "layer2_proxy"
    deployment_id: str = ""
    protocol: str = ""
    model: str = ""
    pass_: bool = False
    http_status: int | None = None
    ttfe_ms: float | None = None
    duration_ms: float | None = None
    event_count: int = 0
    notes: list[str] = field(default_factory=list)

    def to_json(self) -> str:
        d = asdict(self)
        d["pass"] = d.pop("pass_")
        return json.dumps(d, indent=2)


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


def main() -> int:
    parser = argparse.ArgumentParser(description="Layer2 proxy stream probe")
    parser.add_argument("--protocol", required=True, choices=["openai_chat", "anthropic_messages"])
    parser.add_argument("--model", required=True)
    parser.add_argument("--deployment-id", required=True)
    parser.add_argument("--proxy-url", default="http://127.0.0.1:4000")
    parser.add_argument("--timeout", type=float, default=90.0)
    parser.add_argument("--report", type=Path, default=None)
    args = parser.parse_args()

    env = load_env(ENV_PATH)
    mk = env.get("LITELLM_MASTER_KEY", "")
    if not mk:
        print("LITELLM_MASTER_KEY missing", file=sys.stderr)
        return 1

    if args.protocol == "openai_chat":
        url = args.proxy_url.rstrip("/") + "/v1/chat/completions"
        body = {
            "model": args.model,
            "stream": True,
            "max_tokens": 32,
            "messages": [{"role": "user", "content": "Say hi"}],
        }
        headers = {"Authorization": f"Bearer {mk}", "Content-Type": "application/json"}
    else:
        url = args.proxy_url.rstrip("/") + "/v1/messages"
        body = {
            "model": args.model,
            "stream": True,
            "max_tokens": 32,
            "messages": [{"role": "user", "content": "Say hi"}],
        }
        headers = {
            "x-api-key": mk,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }

    report = ProxyStreamProbeReport(
        protocol=args.protocol,
        model=args.model,
        deployment_id=args.deployment_id,
    )
    t0 = time.monotonic()
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        method="POST",
        headers={**headers, "User-Agent": DEFAULT_UA},
    )
    try:
        with urllib.request.urlopen(req, timeout=args.timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            report.http_status = resp.getcode()
    except urllib.error.HTTPError as exc:
        report.http_status = exc.code
        report.notes.append(exc.read().decode("utf-8", errors="replace")[:200])
        text = report.to_json()
        print(text)
        if args.report:
            args.report.write_text(text + "\n", encoding="utf-8")
        return 1

    report.duration_ms = (time.monotonic() - t0) * 1000
    for line in raw.splitlines():
        if line.startswith("data:") or line.startswith("event:"):
            if report.ttfe_ms is None:
                report.ttfe_ms = (time.monotonic() - t0) * 1000
            report.event_count += 1
    report.pass_ = report.http_status == 200 and report.event_count > 0
    text = report.to_json()
    print(text)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(text + "\n", encoding="utf-8")
    return 0 if report.pass_ else 1


if __name__ == "__main__":
    raise SystemExit(main())

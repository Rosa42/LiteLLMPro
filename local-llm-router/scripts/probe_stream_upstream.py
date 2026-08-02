#!/usr/bin/env python3
"""Layer1: stream probe against real upstream deployment (P0-PROBE B6).

Reads secrets from .env only. Never prints API keys.
Exit 0 = PASS, 1 = FAIL.

Usage:
  python scripts/probe_stream_upstream.py \\
    --protocol openai_chat \\
    --base-url-env OPENCODE_GO_BASE_URL \\
    --api-key-env OPENCODE_GO_KEY_A \\
    --model deepseek-v4-flash \\
    --deployment-id opencode-a-chat-deepseek-v4-flash
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


def openai_chat_completions_url(base: str) -> str:
    b = base.rstrip("/")
    if b.endswith("/v1"):
        return f"{b}/chat/completions"
    return f"{b}/v1/chat/completions"


@dataclass
class StreamProbeReport:
    layer: str = "layer1_upstream"
    deployment_id: str = ""
    protocol: str = ""
    model: str = ""
    pass_: bool = False
    ttfe_ms: float | None = None
    duration_ms: float | None = None
    event_count: int = 0
    saw_done: bool = False
    saw_message_stop: bool = False
    error_in_body: bool = False
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


def parse_sse_lines(raw: str) -> list[tuple[str | None, str]]:
    events: list[tuple[str | None, str]] = []
    event_type: str | None = None
    for line in raw.splitlines():
        if line.startswith("event:"):
            event_type = line.split(":", 1)[1].strip()
        elif line.startswith("data:"):
            events.append((event_type, line.split(":", 1)[1].strip()))
            event_type = None
    return events


def probe_openai_chat(base: str, key: str, model: str, timeout: float) -> StreamProbeReport:
    report = StreamProbeReport(protocol="openai_chat", model=model)
    url = openai_chat_completions_url(base)
    body = json.dumps(
        {
            "model": model,
            "stream": True,
            "max_tokens": 32,
            "messages": [{"role": "user", "content": "Say hi in one word"}],
        }
    ).encode()
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Authorization", f"Bearer {key}")
    req.add_header("Content-Type", "application/json")
    req.add_header("User-Agent", DEFAULT_UA)
    t0 = time.monotonic()
    ttfe: float | None = None
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        report.notes.append(f"HTTP {exc.code}")
        return report
    duration = (time.monotonic() - t0) * 1000
    report.duration_ms = duration
    for _evt, data in parse_sse_lines(raw):
        if ttfe is None:
            ttfe = (time.monotonic() - t0) * 1000
            report.ttfe_ms = ttfe
        report.event_count += 1
        if data == "[DONE]":
            report.saw_done = True
        if '"error"' in data:
            report.error_in_body = True
    report.pass_ = report.event_count > 0 and report.saw_done and not report.error_in_body
    return report


def probe_anthropic_messages(base: str, key: str, model: str, timeout: float) -> StreamProbeReport:
    report = StreamProbeReport(protocol="anthropic_messages", model=model)
    url = base.rstrip("/") + "/v1/messages"
    body = json.dumps(
        {
            "model": model,
            "stream": True,
            "max_tokens": 32,
            "messages": [{"role": "user", "content": "Say hi in one word"}],
        }
    ).encode()
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("x-api-key", key)
    req.add_header("anthropic-version", "2023-06-01")
    req.add_header("Content-Type", "application/json")
    req.add_header("User-Agent", DEFAULT_UA)
    t0 = time.monotonic()
    ttfe: float | None = None
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        report.notes.append(f"HTTP {exc.code}")
        return report
    report.duration_ms = (time.monotonic() - t0) * 1000
    for evt, data in parse_sse_lines(raw):
        if ttfe is None:
            ttfe = (time.monotonic() - t0) * 1000
            report.ttfe_ms = ttfe
        report.event_count += 1
        if evt == "message_stop" or "message_stop" in data:
            report.saw_message_stop = True
        if evt == "error" or '"type":"error"' in data or '"type": "error"' in data:
            report.error_in_body = True
    report.pass_ = (
        report.event_count > 0 and report.saw_message_stop and not report.error_in_body
    )
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Layer1 upstream stream probe")
    parser.add_argument("--protocol", required=True, choices=["openai_chat", "anthropic_messages"])
    parser.add_argument("--base-url-env", required=True)
    parser.add_argument("--api-key-env", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--deployment-id", required=True)
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("--report", type=Path, default=None)
    args = parser.parse_args()

    env = load_env(ENV_PATH)
    base = env.get(args.base_url_env, "")
    key = env.get(args.api_key_env, "")
    if not base or not key:
        print("missing base URL or API key env", file=sys.stderr)
        return 1

    if args.protocol == "openai_chat":
        report = probe_openai_chat(base, key, args.model, args.timeout)
    else:
        report = probe_anthropic_messages(base, key, args.model, args.timeout)
    report.deployment_id = args.deployment_id

    text = report.to_json()
    print(text)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(text + "\n", encoding="utf-8")
    return 0 if report.pass_ else 1


if __name__ == "__main__":
    raise SystemExit(main())

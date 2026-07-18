"""Local mock OpenAI-compatible provider for integration / e2e (phase 10).

Run:
  python -m shared_quota_router.mock_provider --port 18080

Scenario control via header X-Mock-Scenario or query ?scenario=
  ok | exhaust | short_429 | auth | timeout | stream_ok | stream_fail_after
"""

from __future__ import annotations

import argparse
import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse


def _sse(data: dict[str, Any]) -> bytes:
    return f"data: {json.dumps(data)}\n\n".encode()


class MockHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"[mock] {self.address_string()} {fmt % args}")

    def _scenario(self) -> str:
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        if "scenario" in qs:
            return qs["scenario"][0]
        return self.headers.get("X-Mock-Scenario", "ok")

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            return json.loads(raw.decode() or "{}")
        except json.JSONDecodeError:
            return {}

    def do_GET(self) -> None:  # noqa: N802
        if self.path.startswith("/health"):
            self._json(200, {"status": "ok"})
            return
        if self.path.startswith("/v1/models") or self.path.startswith("/models"):
            self._json(
                200,
                {
                    "object": "list",
                    "data": [
                        {"id": "kimi-k3", "object": "model"},
                        {"id": "glm-5.2", "object": "model"},
                    ],
                },
            )
            return
        self._json(404, {"error": {"message": "not found"}})

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if not path.endswith("/chat/completions") and "/chat/completions" not in path:
            self._json(404, {"error": {"message": "not found"}})
            return

        body = self._read_json()
        scenario = self._scenario()
        model = body.get("model") or "kimi-k3"
        stream = bool(body.get("stream"))

        if scenario == "timeout":
            time.sleep(30)
            self._json(200, self._ok_completion(model, "slow"))
            return

        if scenario == "auth":
            self._json(
                401,
                {"error": {"message": "Invalid API key", "type": "invalid_request_error", "code": "invalid_api_key"}},
            )
            return

        if scenario == "exhaust":
            self._json(
                429,
                {
                    "error": {
                        "message": "You exceeded your current quota for this coding plan",
                        "type": "insufficient_quota",
                        "code": "insufficient_quota",
                    }
                },
                headers={"Retry-After": "60"},
            )
            return

        if scenario == "short_429":
            self._json(
                429,
                {
                    "error": {
                        "message": "Rate limit reached for TPM",
                        "type": "rate_limit_exceeded",
                        "code": "rate_limit_exceeded",
                    }
                },
                headers={"Retry-After": "5"},
            )
            return

        if stream or scenario in {"stream_ok", "stream_fail_after"}:
            self._stream(model, fail_after=(scenario == "stream_fail_after"))
            return

        self._json(200, self._ok_completion(model, "hello from mock"))

    def _ok_completion(self, model: str, text: str) -> dict[str, Any]:
        return {
            "id": "chatcmpl-mock",
            "object": "chat.completion",
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": text},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }

    def _stream(self, model: str, *, fail_after: bool) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        chunks = ["Hel", "lo"]
        for i, part in enumerate(chunks):
            payload = {
                "id": "chatcmpl-mock",
                "object": "chat.completion.chunk",
                "model": model,
                "choices": [{"index": 0, "delta": {"content": part}, "finish_reason": None}],
            }
            self.wfile.write(_sse(payload))
            self.wfile.flush()
            if fail_after and i == 0:
                # Mid-stream error: close without clean finish (client sees truncated stream)
                return
        done = {
            "id": "chatcmpl-mock",
            "object": "chat.completion.chunk",
            "model": model,
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        }
        self.wfile.write(_sse(done))
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()

    def _json(
        self,
        code: int,
        payload: dict[str, Any],
        headers: dict[str, str] | None = None,
    ) -> None:
        data = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        if headers:
            for k, v in headers.items():
                self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description="Mock OpenAI-compatible provider")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=18080)
    args = p.parse_args(argv)
    server = ThreadingHTTPServer((args.host, args.port), MockHandler)
    print(f"mock provider on http://{args.host}:{args.port}")
    server.serve_forever()


if __name__ == "__main__":
    main()

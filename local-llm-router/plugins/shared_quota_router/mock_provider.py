"""Local multi-protocol mock provider for contract / integration / e2e tests.

Supports:
  - OpenAI Chat Completions:  POST /v1/chat/completions, /chat/completions
  - OpenAI Responses:         POST /v1/responses, /responses
  - Anthropic Messages:       POST /v1/messages, /messages

Run:
  python -m shared_quota_router.mock_provider --port 18080

Scenario control via header X-Mock-Scenario or query ?scenario=
  ok | exhaust | short_429 | auth | timeout | stream_ok | stream_fail_after

Request path recording (test helpers):
  MockHandler.last_requests — list of {path, method, auth_style, has_tools, stream,
  probe_marker_hit}
  Does not store Authorization values or full prompt bodies.
"""

from __future__ import annotations

import argparse
import json
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, ClassVar
from urllib.parse import parse_qs, urlparse


def _sse(data: dict[str, Any]) -> bytes:
    return f"data: {json.dumps(data)}\n\n".encode()


class MockHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    # Process-wide capture for contract tests (path evidence only).
    last_requests: ClassVar[list[dict[str, Any]]] = []

    def log_message(self, fmt: str, *args: Any) -> None:
        # Avoid logging Authorization or body content.
        print(f"[mock] {self.address_string()} {self.command} {urlparse(self.path).path}")

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

    def _auth_style(self) -> str:
        if self.headers.get("Authorization"):
            return "bearer"
        if self.headers.get("x-api-key") or self.headers.get("X-Api-Key"):
            return "x-api-key"
        return "none"

    def _record(self, path: str, body: dict[str, Any]) -> None:
        marker = (os.environ.get("P0_PROBE_B_MARKER") or "").strip()
        probe_marker_hit = False
        if marker:
            try:
                blob = json.dumps(body, ensure_ascii=False)
            except (TypeError, ValueError):
                blob = ""
            probe_marker_hit = marker in blob
        MockHandler.last_requests.append(
            {
                "path": path,
                "method": self.command,
                "auth_style": self._auth_style(),
                "stream": bool(body.get("stream")),
                "has_tools": bool(body.get("tools")),
                "has_messages": "messages" in body,
                "has_input": "input" in body,
                # Length only — never store body text.
                "body_keys": sorted(body.keys()),
                "probe_marker_hit": probe_marker_hit,
            }
        )

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path.startswith("/health"):
            self._json(200, {"status": "ok"})
            return
        if path.startswith("/v1/models") or path.startswith("/models"):
            self._json(
                200,
                {
                    "object": "list",
                    "data": [
                        {"id": "kimi-k3", "object": "model"},
                        {"id": "glm-5.2", "object": "model"},
                        {"id": "probe-model", "object": "model"},
                    ],
                },
            )
            return
        self._json(404, {"error": {"message": "not found"}})

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        body = self._read_json()
        self._record(path, body)
        scenario = self._scenario()

        if self._is_chat_path(path):
            self._handle_chat(body, scenario)
            return
        if self._is_responses_path(path):
            self._handle_responses(body, scenario)
            return
        if self._is_messages_path(path):
            self._handle_messages(body, scenario)
            return

        self._json(404, {"error": {"message": f"not found: {path}"}})

    @staticmethod
    def _is_chat_path(path: str) -> bool:
        return path.endswith("/chat/completions") or "/chat/completions" in path

    @staticmethod
    def _is_responses_path(path: str) -> bool:
        # Match /responses and /v1/responses but not unrelated paths.
        return path.rstrip("/").endswith("/responses") or path.rstrip("/").endswith(
            "responses"
        )

    @staticmethod
    def _is_messages_path(path: str) -> bool:
        return path.rstrip("/").endswith("/messages") or path.rstrip("/").endswith(
            "messages"
        )

    def _handle_common_errors(self, scenario: str) -> bool:
        if scenario == "timeout":
            time.sleep(30)
            return False
        if scenario == "auth":
            self._json(
                401,
                {
                    "error": {
                        "message": "Invalid API key",
                        "type": "invalid_request_error",
                        "code": "invalid_api_key",
                    }
                },
            )
            return True
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
            return True
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
            return True
        return False

    def _handle_chat(self, body: dict[str, Any], scenario: str) -> None:
        model = body.get("model") or "kimi-k3"
        stream = bool(body.get("stream"))
        if self._handle_common_errors(scenario):
            return
        if scenario == "timeout":
            self._json(200, self._ok_completion(model, "slow"))
            return
        if stream or scenario in {"stream_ok", "stream_fail_after"}:
            self._stream_chat(model, fail_after=(scenario == "stream_fail_after"))
            return
        self._json(200, self._ok_completion(model, "hello from mock"))

    def _handle_responses(self, body: dict[str, Any], scenario: str) -> None:
        model = body.get("model") or "probe-model"
        if self._handle_common_errors(scenario):
            return
        if scenario == "timeout":
            self._json(200, self._ok_responses(model, "slow"))
            return
        self._json(200, self._ok_responses(model, "hello from mock responses"))

    def _handle_messages(self, body: dict[str, Any], scenario: str) -> None:
        model = body.get("model") or "probe-model"
        if self._handle_common_errors(scenario):
            return
        if scenario == "timeout":
            self._json(200, self._ok_messages(model, "slow"))
            return
        stream = bool(body.get("stream"))
        if stream or scenario in {"stream_ok", "stream_fail_after"}:
            self._stream_messages(model, fail_after=(scenario == "stream_fail_after"))
            return
        self._json(200, self._ok_messages(model, "hello from mock messages"))

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

    def _ok_responses(self, model: str, text: str) -> dict[str, Any]:
        return {
            "id": "resp_mock",
            "object": "response",
            "created_at": 1,
            "status": "completed",
            "model": model,
            "output": [
                {
                    "id": "msg_mock",
                    "type": "message",
                    "role": "assistant",
                    "status": "completed",
                    "content": [
                        {"type": "output_text", "text": text, "annotations": []}
                    ],
                }
            ],
            "usage": {
                "input_tokens": 1,
                "output_tokens": 1,
                "total_tokens": 2,
            },
            "parallel_tool_calls": True,
            "tool_choice": "auto",
            "tools": [],
        }

    def _ok_messages(self, model: str, text: str) -> dict[str, Any]:
        return {
            "id": "msg_mock",
            "type": "message",
            "role": "assistant",
            "content": [{"type": "text", "text": text}],
            "model": model,
            "stop_reason": "end_turn",
            "stop_sequence": None,
            "usage": {"input_tokens": 1, "output_tokens": 1},
        }

    def _stream_chat(self, model: str, *, fail_after: bool) -> None:
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
                "choices": [
                    {
                        "index": 0,
                        "delta": {"content": part},
                        "finish_reason": None,
                    }
                ],
            }
            self.wfile.write(_sse(payload))
            self.wfile.flush()
            if fail_after and i == 0:
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

    def _stream_messages(self, model: str, *, fail_after: bool) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        # Minimal Anthropic SSE sequence (message_start → content_block_delta → message_stop)
        events = [
            (
                "message_start",
                {
                    "type": "message_start",
                    "message": {
                        "id": "msg_mock",
                        "type": "message",
                        "role": "assistant",
                        "content": [],
                        "model": model,
                        "stop_reason": None,
                        "usage": {"input_tokens": 1, "output_tokens": 0},
                    },
                },
            ),
            (
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {"type": "text", "text": ""},
                },
            ),
            (
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": "Hel"},
                },
            ),
        ]
        for i, (event, payload) in enumerate(events):
            line = f"event: {event}\ndata: {json.dumps(payload)}\n\n".encode()
            self.wfile.write(line)
            self.wfile.flush()
            if fail_after and i == 2:
                return
        for event, payload in (
            (
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": "lo"},
                },
            ),
            (
                "content_block_stop",
                {"type": "content_block_stop", "index": 0},
            ),
            (
                "message_delta",
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": "end_turn"},
                    "usage": {"output_tokens": 1},
                },
            ),
            ("message_stop", {"type": "message_stop"}),
        ):
            line = f"event: {event}\ndata: {json.dumps(payload)}\n\n".encode()
            self.wfile.write(line)
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
    p = argparse.ArgumentParser(description="Mock multi-protocol LLM provider")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=18080)
    args = p.parse_args(argv)
    server = ThreadingHTTPServer((args.host, args.port), MockHandler)
    print(f"mock provider on http://{args.host}:{args.port}")
    server.serve_forever()


if __name__ == "__main__":
    main()

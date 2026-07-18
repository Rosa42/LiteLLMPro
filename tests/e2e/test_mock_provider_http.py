"""E2E against in-process mock HTTP provider (no Docker required)."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from shared_quota_router.mock_provider import MockHandler


@pytest.fixture(scope="module")
def mock_base() -> str:
    server = ThreadingHTTPServer(("127.0.0.1", 0), MockHandler)
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{port}"
    server.shutdown()


def _post(base: str, scenario: str, stream: bool = False) -> tuple[int, bytes, dict]:
    url = f"{base}/v1/chat/completions?scenario={scenario}"
    body = json.dumps(
        {
            "model": "kimi-k3",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": stream,
            "max_tokens": 8,
        }
    ).encode()
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json", "Authorization": "Bearer t"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, resp.read(), dict(resp.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers)


def test_mock_ok(mock_base: str) -> None:
    code, raw, _ = _post(mock_base, "ok")
    assert code == 200
    data = json.loads(raw)
    assert data["choices"][0]["message"]["content"]


def test_mock_exhaust(mock_base: str) -> None:
    code, raw, headers = _post(mock_base, "exhaust")
    assert code == 429
    assert b"quota" in raw.lower() or b"insufficient" in raw.lower()


def test_mock_auth(mock_base: str) -> None:
    code, _, _ = _post(mock_base, "auth")
    assert code == 401


def test_mock_stream_ok(mock_base: str) -> None:
    code, raw, _ = _post(mock_base, "stream_ok", stream=True)
    assert code == 200
    assert b"data:" in raw

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


def _get(base: str, path: str) -> tuple[int, dict]:
    req = urllib.request.Request(f"{base}{path}", method="GET")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            parsed = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            parsed = {"raw": raw}
        return e.code, parsed


def _post_json(base: str, path: str, body: dict, headers: dict | None = None) -> int:
    data = json.dumps(body).encode()
    hdrs = {"Content-Type": "application/json"}
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(f"{base}{path}", data=data, headers=hdrs, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return int(resp.status)
    except urllib.error.HTTPError as e:
        return int(e.code)


def test_probe_last_empty_after_reset(mock_base: str) -> None:
    code, payload = _get(mock_base, "/probe/reset")
    assert code == 200
    assert payload.get("cleared") is True
    code, payload = _get(mock_base, "/probe/last")
    assert code == 404
    assert payload.get("empty") is True
    assert payload.get("probe_marker_hit") is False
    dumped = json.dumps(payload)
    assert "Authorization" not in dumped
    assert "Bearer" not in dumped


def test_probe_last_hit_true_without_storing_body(
    mock_base: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = "P0B_UNITTEST01"
    monkeypatch.setenv("P0_PROBE_B_MARKER", marker)
    _get(mock_base, "/probe/reset")
    status = _post_json(
        mock_base,
        "/v1/messages",
        {
            "model": "MiniMax-M3",
            "messages": [{"role": "user", "content": f"hello {marker}"}],
        },
        headers={"x-api-key": "sk-fake-never-store"},
    )
    assert status == 200
    code, payload = _get(mock_base, "/probe/last")
    assert code == 200
    assert payload["probe_marker_hit"] is True
    assert payload["has_messages"] is True
    assert str(payload["path"]).endswith("/messages")
    dumped = json.dumps(payload)
    assert marker not in dumped
    assert "sk-fake-never-store" not in dumped
    assert "hello" not in dumped


def test_messages_image_returns_visual_evidence_without_storing_bytes(
    mock_base: str,
) -> None:
    _get(mock_base, "/probe/reset")
    png = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAAB"
    payload = {
        "model": "MiniMax-M3",
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {"type": "base64", "data": png},
                    },
                    {"type": "text", "text": "translate"},
                ],
            }
        ],
    }
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        f"{mock_base}/v1/messages",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        body = json.loads(resp.read().decode())
    text = body["content"][0]["text"]
    assert "<visual-evidence>" in text
    assert "mock visual translation" in text
    code, last = _get(mock_base, "/probe/last")
    assert code == 200
    assert last["has_image"] is True
    dumped = json.dumps(last)
    assert png not in dumped
    assert "translate" not in dumped


def test_probe_last_hit_false_when_body_lacks_marker(
    mock_base: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("P0_PROBE_B_MARKER", "P0B_UNITTEST01")
    _get(mock_base, "/probe/reset")
    status = _post_json(
        mock_base,
        "/v1/messages",
        {
            "model": "MiniMax-M3",
            "messages": [{"role": "user", "content": "Reply with exactly: pong"}],
        },
    )
    assert status == 200
    code, payload = _get(mock_base, "/probe/last")
    assert code == 200
    assert payload["probe_marker_hit"] is False
    dumped = json.dumps(payload)
    assert "pong" not in dumped
    assert "P0B_UNITTEST01" not in dumped

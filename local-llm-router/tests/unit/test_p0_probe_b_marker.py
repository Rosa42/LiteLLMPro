"""P0 Probe B: env-gated pre-call message mutation (default off)."""

from __future__ import annotations

from shared_quota_router.callbacks import _inject_p0_probe_b_marker
from shared_quota_router.feature_flags import flag_snapshot, p0_probe_b_marker
from shared_quota_router.mock_provider import MockHandler

FAKE_AUTH = "sk-fake-auth-never-store"
UNIT_MARKER = "P0B_UNITTEST01"


def _handler_with_auth() -> MockHandler:
    handler = MockHandler.__new__(MockHandler)
    handler.command = "POST"
    handler.headers = {"Authorization": f"Bearer {FAKE_AUTH}"}
    return handler


def test_empty_env_inject_noop(monkeypatch: object) -> None:
    monkeypatch.delenv("P0_PROBE_B_MARKER", raising=False)
    original = "Reply with exactly: pong"
    data = {"messages": [{"role": "user", "content": original}]}
    _inject_p0_probe_b_marker(data)
    assert data["messages"][0]["content"] == original
    assert p0_probe_b_marker() == ""
    snap = flag_snapshot()
    assert "P0_PROBE_B_MARKER" not in snap
    assert "p0_probe_b_marker" not in snap


def test_string_content_suffix_preserves_original(monkeypatch: object) -> None:
    monkeypatch.setenv("P0_PROBE_B_MARKER", UNIT_MARKER)
    original = "Reply with exactly: pong"
    data = {"messages": [{"role": "user", "content": original}]}
    _inject_p0_probe_b_marker(data)
    content = data["messages"][0]["content"]
    assert isinstance(content, str)
    assert content.startswith(original)
    assert UNIT_MARKER in content
    assert content != original


def test_list_content_appends_last_text_block(monkeypatch: object) -> None:
    monkeypatch.setenv("P0_PROBE_B_MARKER", UNIT_MARKER)
    data = {
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "first"},
                    {"type": "text", "text": "last"},
                ],
            }
        ]
    }
    _inject_p0_probe_b_marker(data)
    blocks = data["messages"][0]["content"]
    assert blocks[0]["text"] == "first"
    assert blocks[1]["text"].startswith("last")
    assert UNIT_MARKER in blocks[1]["text"]
    assert UNIT_MARKER not in blocks[0]["text"]


def test_mock_record_probe_marker_hit_true(monkeypatch: object) -> None:
    monkeypatch.setenv("P0_PROBE_B_MARKER", UNIT_MARKER)
    MockHandler.last_requests.clear()
    handler = _handler_with_auth()
    handler._record(
        "/v1/messages",
        {"messages": [{"role": "user", "content": f"hello {UNIT_MARKER}"}]},
    )
    rec = MockHandler.last_requests[-1]
    assert rec["probe_marker_hit"] is True
    dumped = repr(MockHandler.last_requests)
    assert FAKE_AUTH not in dumped
    assert "Bearer " not in dumped


def test_mock_record_probe_marker_hit_false(monkeypatch: object) -> None:
    monkeypatch.setenv("P0_PROBE_B_MARKER", UNIT_MARKER)
    MockHandler.last_requests.clear()
    handler = _handler_with_auth()
    handler._record(
        "/v1/messages",
        {"messages": [{"role": "user", "content": "Reply with exactly: pong"}]},
    )
    rec = MockHandler.last_requests[-1]
    assert rec["probe_marker_hit"] is False
    dumped = repr(MockHandler.last_requests)
    assert FAKE_AUTH not in dumped
    assert UNIT_MARKER not in dumped

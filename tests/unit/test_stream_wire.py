"""P0-WIRE: mid-stream SSE error formatting."""

from __future__ import annotations

from shared_quota_router.stream_wire import (
    StreamWireProtocol,
    format_anthropic_stream_error_chunks,
    format_openai_stream_error_chunks,
    terminal_stream_chunks,
)


def test_anthropic_mid_stream_error_shape() -> None:
    chunks = format_anthropic_stream_error_chunks("upstream failed", error_type="api_error")
    text = "".join(chunks)
    assert "event: error" in text
    assert '"type": "error"' in text
    assert "message_stop" not in text
    assert "data: {\"error\"" not in text


def test_openai_mid_stream_error_shape() -> None:
    chunks = format_openai_stream_error_chunks("upstream failed", error_type="server_error")
    text = "".join(chunks)
    assert 'data: {"error":' in text
    assert "data: [DONE]" in text


def test_anthropic_not_openai_error_shape() -> None:
    chunks = format_anthropic_stream_error_chunks("x")
    joined = "".join(chunks)
    assert not joined.startswith('data: {"error"')


def test_terminal_stream_chunks_by_protocol() -> None:
    a = terminal_stream_chunks(StreamWireProtocol.ANTHROPIC_MESSAGES, "fail")
    o = terminal_stream_chunks(StreamWireProtocol.OPENAI_CHAT, "fail")
    assert "event: error" in "".join(a)
    assert "[DONE]" in "".join(o)

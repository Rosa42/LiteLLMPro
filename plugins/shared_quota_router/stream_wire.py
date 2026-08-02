"""P0-WIRE: mid-stream SSE error shapes (Anthropic / OpenAI Chat).

Pre-stream errors remain in ``anthropic_wire.py`` (JSON 400).
Post-``t_first_public`` errors use this module.
"""

from __future__ import annotations

import json
from enum import Enum
from typing import Any

from shared_quota_router.models import ApiProtocol


class StreamWireProtocol(str, Enum):
    OPENAI_CHAT = "openai_chat"
    ANTHROPIC_MESSAGES = "anthropic_messages"

    @classmethod
    def from_wire(cls, value: str | None) -> StreamWireProtocol | None:
        if not value:
            return None
        raw = value.strip().lower()
        if raw in ("openai_chat", "chat", "acompletion"):
            return cls.OPENAI_CHAT
        if raw in ("anthropic_messages", "messages", "anthropic"):
            return cls.ANTHROPIC_MESSAGES
        try:
            return cls(ApiProtocol(raw).value)
        except ValueError:
            return None


def format_openai_stream_error_chunks(
    message: str,
    *,
    error_type: str = "server_error",
    code: str | None = None,
) -> list[str]:
    """OpenAI Chat stream terminal: error SSE + ``[DONE]`` (B4-03)."""
    err: dict[str, Any] = {"message": message, "type": error_type}
    if code:
        err["code"] = code
    return [
        f"data: {json.dumps({'error': err}, ensure_ascii=False)}\n\n",
        "data: [DONE]\n\n",
    ]


def format_anthropic_stream_error_chunks(
    message: str,
    *,
    error_type: str = "api_error",
) -> list[str]:
    """Anthropic mid-stream: ``event: error`` + body; no ``message_stop`` (B4-01)."""
    payload = {
        "type": "error",
        "error": {"type": error_type, "message": message},
    }
    return [
        "event: error\n",
        f"data: {json.dumps(payload, ensure_ascii=False)}\n\n",
    ]


def terminal_stream_chunks(
    protocol: StreamWireProtocol | None,
    message: str,
    *,
    error_type: str = "api_error",
) -> list[str]:
    if protocol is StreamWireProtocol.ANTHROPIC_MESSAGES:
        return format_anthropic_stream_error_chunks(message, error_type=error_type)
    if protocol is StreamWireProtocol.OPENAI_CHAT:
        return format_openai_stream_error_chunks(message, error_type=error_type)
    return format_openai_stream_error_chunks(message, error_type=error_type)


def chunk_is_public_stream_event(item: Any) -> bool:
    """True when chunk represents a non-empty public stream event (B3 boundary)."""
    if item is None:
        return False
    if isinstance(item, str):
        s = item.strip()
        if not s:
            return False
        if s.startswith("data:") and s.split(":", 1)[1].strip() in ("", "[DONE]"):
            return False
        return True
    if isinstance(item, (bytes, bytearray)):
        return bool(item.strip())
    if isinstance(item, dict):
        if item.get("error"):
            return True
        choices = item.get("choices")
        if isinstance(choices, list) and choices:
            return True
        if item.get("type") in ("message_start", "content_block_delta", "message_delta"):
            return True
    # ModelResponseStream-like
    choices = getattr(item, "choices", None)
    if choices:
        return True
    typ = getattr(item, "type", None)
    if typ in ("message_start", "content_block_delta", "message_delta", "message_stop"):
        return True
    return bool(item)

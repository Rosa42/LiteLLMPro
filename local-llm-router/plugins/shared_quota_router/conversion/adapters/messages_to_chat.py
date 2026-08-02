"""anthropic_messages (public) → openai_chat (upstream) text-only converter (C2)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from shared_quota_router.conversion.contracts import (
    DIRECTION_MESSAGES_TO_CHAT,
    ConvertedRequest,
    ConvertedResponse,
)
from shared_quota_router.models import ApiProtocol
from shared_quota_router.protocol_errors import (
    ProtocolAwareRoutingError,
    ProtocolRoutingReason,
)

_FINISH_MAP_PATH = (
    Path(__file__).resolve().parents[4]
    / "tests"
    / "fixtures"
    / "conversion"
    / "messages_to_chat"
    / "finish_reason_map.json"
)

# Fallback if fixtures not packaged beside runtime
_DEFAULT_FINISH_MAP = {
    "stop": "end_turn",
    "length": "max_tokens",
    "content_filter": "refusal",
    "tool_calls": "tool_use",
    "function_call": "tool_use",
}


def _finish_map() -> dict[str, str]:
    try:
        return json.loads(_FINISH_MAP_PATH.read_text(encoding="utf-8"))
    except OSError:
        return dict(_DEFAULT_FINISH_MAP)


def _content_to_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    parts.append(str(block.get("text") or ""))
                elif "text" in block:
                    parts.append(str(block["text"]))
            elif isinstance(block, str):
                parts.append(block)
        return "".join(parts)
    return str(content)


class MessagesToChatConverter:
    """C2 pilot: non-streaming text only. tools/images/reasoning/stream rejected."""

    direction = DIRECTION_MESSAGES_TO_CHAT

    # Optional params not mapped in C2 pilot — must be declared (no silent drop, §6.6)
    _OPTIONAL_UNMAPPED = (
        "temperature",
        "top_p",
        "top_k",
        "stop",
        "stop_sequences",
        "metadata",
        "user",
        "presence_penalty",
        "frequency_penalty",
        "logit_bias",
        "n",
        "seed",
        "response_format",
    )

    def convert_request(self, public_payload: dict[str, Any]) -> ConvertedRequest:
        self._reject_unsupported_request(public_payload)
        dropped: list[str] = []
        for key in self._OPTIONAL_UNMAPPED:
            if public_payload.get(key) not in (None, "", [], {}):
                dropped.append(key)

        messages_out: list[dict[str, Any]] = []
        system = public_payload.get("system")
        if system not in (None, ""):
            messages_out.append(
                {"role": "system", "content": _content_to_text(system)}
            )
        for msg in public_payload.get("messages") or []:
            if not isinstance(msg, dict):
                continue
            role = str(msg.get("role") or "user")
            messages_out.append(
                {"role": role, "content": _content_to_text(msg.get("content"))}
            )

        payload: dict[str, Any] = {
            "model": public_payload.get("model"),
            "messages": messages_out,
        }
        if "max_tokens" in public_payload and public_payload["max_tokens"] is not None:
            payload["max_tokens"] = public_payload["max_tokens"]
        if public_payload.get("stream") is True:
            # Should already be rejected; belt-and-suspenders
            raise ProtocolAwareRoutingError(
                "streaming conversion not supported in C2 pilot",
                reason=ProtocolRoutingReason.FEATURE_UNSUPPORTED,
                protocol=ApiProtocol.ANTHROPIC_MESSAGES,
            )
        return ConvertedRequest(
            payload=payload, warnings=[], dropped_fields=dropped
        )

    def convert_response(self, upstream_payload: dict[str, Any]) -> ConvertedResponse:
        choices = upstream_payload.get("choices") or []
        choice = choices[0] if choices else {}
        message = choice.get("message") if isinstance(choice, dict) else {}
        text = ""
        if isinstance(message, dict):
            text = _content_to_text(message.get("content"))
        finish = None
        if isinstance(choice, dict):
            finish = choice.get("finish_reason")
        stop_reason = _finish_map().get(str(finish), "end_turn") if finish else "end_turn"

        usage_in = upstream_payload.get("usage") or {}
        usage = {
            "input_tokens": int(usage_in.get("prompt_tokens") or 0),
            "output_tokens": int(usage_in.get("completion_tokens") or 0),
        }

        payload = {
            "id": upstream_payload.get("id") or "msg_converted",
            "type": "message",
            "role": "assistant",
            "model": upstream_payload.get("model"),
            "content": [{"type": "text", "text": text}],
            "stop_reason": stop_reason,
            "stop_sequence": None,
            "usage": usage,
        }
        return ConvertedResponse(payload=payload, warnings=[], dropped_fields=[])

    def convert_error(self, upstream_error: dict[str, Any]) -> dict[str, Any]:
        err = upstream_error.get("error") if isinstance(upstream_error, dict) else None
        if not isinstance(err, dict):
            err = {"message": str(upstream_error), "type": "api_error"}
        err_type = str(err.get("type") or "api_error")
        # Map OpenAI invalid_request_error → Anthropic invalid_request_error
        if err_type in {"invalid_request_error", "api_error"}:
            anth_type = err_type
        else:
            anth_type = "api_error"
        return {
            "type": "error",
            "error": {
                "type": anth_type,
                "message": str(err.get("message") or "upstream error"),
            },
        }

    def _reject_unsupported_request(self, public_payload: dict[str, Any]) -> None:
        if public_payload.get("stream") is True:
            raise ProtocolAwareRoutingError(
                "streaming not supported on messages→chat conversion pilot",
                reason=ProtocolRoutingReason.FEATURE_UNSUPPORTED,
                protocol=ApiProtocol.ANTHROPIC_MESSAGES,
                details={"feature": "streaming"},
            )
        for key in ("tools", "tool_choice", "functions", "function_call"):
            if public_payload.get(key) not in (None, "", [], {}):
                raise ProtocolAwareRoutingError(
                    f"field {key!r} not supported on messages→chat conversion pilot",
                    reason=ProtocolRoutingReason.FEATURE_UNSUPPORTED,
                    protocol=ApiProtocol.ANTHROPIC_MESSAGES,
                    details={"feature": "tools", "field": key},
                )
        # Multimodal / thinking blocks in content
        for msg in public_payload.get("messages") or []:
            if not isinstance(msg, dict):
                continue
            content = msg.get("content")
            if isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    btype = block.get("type")
                    if btype in {"image", "tool_use", "tool_result", "thinking"}:
                        raise ProtocolAwareRoutingError(
                            f"content block type {btype!r} unsupported in C2 pilot",
                            reason=ProtocolRoutingReason.FEATURE_UNSUPPORTED,
                            protocol=ApiProtocol.ANTHROPIC_MESSAGES,
                            details={"feature": str(btype)},
                        )

"""Plain-text walks over Anthropic Messages content. No image bytes."""

from __future__ import annotations

from typing import Any

from shared_quota_router.vision_agents.types import ImageRef

IMAGE_TYPES = frozenset({"image", "image_url"})


def content_plain_texts(content: Any) -> list[str]:
    if isinstance(content, str) and content.strip():
        return [content.strip()]
    if not isinstance(content, list):
        return []
    out: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = str(block.get("type") or "")
        if btype in IMAGE_TYPES:
            continue
        if btype in {"text", ""}:
            text = str(block.get("text") or "").strip()
            if text:
                out.append(text)
            continue
        if btype == "tool_result":
            out.extend(content_plain_texts(block.get("content")))
    return out


def human_plain_texts(content: Any) -> list[str]:
    """User-visible text that is not a tool_result dump."""
    if isinstance(content, str) and content.strip():
        return [content.strip()]
    if not isinstance(content, list):
        return []
    out: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = str(block.get("type") or "")
        if btype in IMAGE_TYPES:
            continue
        if btype in {"text", ""}:
            text = str(block.get("text") or "").strip()
            if text:
                out.append(text)
    return out


def list_at(messages: list[Any], ref: ImageRef) -> list[Any]:
    if ref.message_index < 0 or ref.message_index >= len(messages):
        return []
    msg = messages[ref.message_index]
    if not isinstance(msg, dict):
        return []
    node = msg.get("content")
    if not isinstance(node, list):
        return []
    for index in ref.path[:-1]:
        if index < 0 or index >= len(node) or not isinstance(node[index], dict):
            return []
        inner = node[index].get("content")
        if not isinstance(inner, list):
            return []
        node = inner
    return node

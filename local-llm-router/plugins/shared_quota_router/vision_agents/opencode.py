"""OpenCode vision preset. Header/UA plus live Read-tool image fingerprint."""

from __future__ import annotations

import re
from typing import Any, Mapping

from shared_quota_router.vision_agents.capture import redact_text
from shared_quota_router.vision_agents.generic import (
    MAX_CONTEXT_CHARS,
    MAX_TASK_CHARS,
)
from shared_quota_router.vision_agents.text import IMAGE_TYPES, human_plain_texts, list_at
from shared_quota_router.vision_agents.types import ImageRef

_UA_OPENCODE = re.compile(r"(?i)(?:^|[^a-z0-9])opencode/")

_CALLED_READ = "Called the Read tool with the following input:"
_IMAGE_OK = "Image read successfully"
_TITLE_PREFIX = "Generate a title for this conversation:"

_ADDENDUM = """Screenshots may be a user image after a Read-tool wrapper, or inside tool_result.
Ignore TUI/terminal chrome, spinners, and model-name bars.
Prefer exact tracebacks, commands, paths, and test-failure summaries.
Do not role-play OpenCode, emit patches, or call tools."""


class OpenCodePreset:
    id = "opencode"
    prompt_rev = 2

    def match_header(self, headers: Mapping[str, Any]) -> bool:
        ua = _header(headers, "user-agent")
        return bool(ua and _UA_OPENCODE.search(ua))

    def match_messages(self, messages: list[Any]) -> bool:
        """Live 2026-08-27: Read-tool images are inlined on a user list.

        Requires all three: an image, the Read wrapper, and the success line.
        User chat mentioning OpenCode is not enough. Nested tool_result images
        alone are not enough (Anthropic-standard).
        """
        if not isinstance(messages, list):
            return False
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            if str(msg.get("role") or "") != "user":
                continue
            content = msg.get("content")
            if not isinstance(content, list):
                continue
            has_image = False
            has_ok = False
            has_called = False
            for block in content:
                if not isinstance(block, dict):
                    continue
                btype = str(block.get("type") or "")
                if btype in IMAGE_TYPES:
                    has_image = True
                    continue
                if btype not in {"text", ""}:
                    continue
                text = str(block.get("text") or "")
                stripped = text.strip()
                if stripped == _IMAGE_OK:
                    has_ok = True
                if stripped.startswith(_CALLED_READ):
                    has_called = True
            if has_image and has_ok and has_called:
                return True
        return False

    def system_addendum(self) -> str:
        return _ADDENDUM

    def extract_guide(self, messages: list[Any], image_ref: ImageRef) -> str:
        if not isinstance(messages, list):
            return ""
        same = [
            t
            for t in _plain_texts(list_at(messages, image_ref))
            if not _is_wrapper_dump(t)
        ]
        task = ""
        for i in range(image_ref.message_index, -1, -1):
            msg = messages[i]
            if not isinstance(msg, dict):
                continue
            if str(msg.get("role") or "") != "user":
                continue
            human = " ".join(
                t for t in human_plain_texts(msg.get("content")) if not _is_chrome(t)
            ).strip()
            if human:
                task = human
                break
        prior: list[str] = []
        for i in range(image_ref.message_index - 1, -1, -1):
            msg = messages[i]
            if not isinstance(msg, dict):
                continue
            if str(msg.get("role") or "") != "user":
                continue
            human = " ".join(
                t for t in human_plain_texts(msg.get("content")) if not _is_chrome(t)
            ).strip()
            if not human:
                continue
            prior.append(human)
            if len(prior) >= 2:
                break
        prior.reverse()
        task = redact_text(task)[:MAX_TASK_CHARS]
        ctx_chunks: list[str] = []
        same_blob = redact_text(" ".join(same)).strip()
        if same_blob and same_blob != task:
            ctx_chunks.append(same_blob)
        if prior:
            ctx_chunks.append(redact_text("\n".join(prior)))
        ctx = "\n".join(ctx_chunks)
        if len(ctx) > MAX_CONTEXT_CHARS:
            ctx = ctx[-MAX_CONTEXT_CHARS:]
        parts: list[str] = []
        if task:
            parts.append(f"task:\n{task}")
        if ctx:
            parts.append(f"context:\n{ctx}")
        return "\n\n".join(parts)


def _is_chrome(text: str) -> bool:
    stripped = text.strip().strip('"')
    if stripped == _IMAGE_OK:
        return True
    if stripped.startswith(_CALLED_READ):
        return True
    if stripped.startswith(_TITLE_PREFIX):
        return True
    return False


def _is_wrapper_dump(text: str) -> bool:
    stripped = text.strip()
    return stripped.startswith(_CALLED_READ) or stripped.startswith(_TITLE_PREFIX)


def _plain_texts(content: Any) -> list[str]:
    from shared_quota_router.vision_agents.text import content_plain_texts

    return content_plain_texts(content)


def _header(headers: Mapping[str, Any], name: str) -> str:
    want = name.lower()
    for key, value in headers.items():
        if str(key).lower() == want:
            return str(value or "")
    return ""

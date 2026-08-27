"""Generic vision prompt preset. Complete fallback, never wins matching."""

from __future__ import annotations

from typing import Any, Mapping

from shared_quota_router.vision_agents.text import (
    content_plain_texts,
    human_plain_texts,
    list_at,
)
from shared_quota_router.vision_agents.types import ImageRef

MAX_TASK_CHARS = 1500
MAX_CONTEXT_CHARS = 2000


class GenericPreset:
    id = "generic"
    prompt_rev = 1

    def match_header(self, headers: Mapping[str, Any]) -> bool:
        return False

    def match_messages(self, messages: list[Any]) -> bool:
        return False

    def system_addendum(self) -> str:
        return ""

    def extract_guide(self, messages: list[Any], image_ref: ImageRef) -> str:
        if not isinstance(messages, list):
            return ""
        from shared_quota_router.memory_extract import redact

        same_texts = content_plain_texts(list_at(messages, image_ref))
        task = ""
        for i in range(image_ref.message_index, -1, -1):
            msg = messages[i]
            if not isinstance(msg, dict):
                continue
            if str(msg.get("role") or "") != "user":
                continue
            human = " ".join(human_plain_texts(msg.get("content"))).strip()
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
            human = " ".join(human_plain_texts(msg.get("content"))).strip()
            if not human:
                continue
            prior.append(human)
            if len(prior) >= 2:
                break
        prior.reverse()
        task = redact(task)[:MAX_TASK_CHARS]
        ctx_chunks: list[str] = []
        same = redact(" ".join(same_texts)).strip()
        if same and same != task:
            ctx_chunks.append(same)
        if prior:
            ctx_chunks.append(redact("\n".join(prior)))
        ctx = "\n".join(ctx_chunks)
        if len(ctx) > MAX_CONTEXT_CHARS:
            ctx = ctx[-MAX_CONTEXT_CHARS:]
        parts: list[str] = []
        if task:
            parts.append(f"task:\n{task}")
        if ctx:
            parts.append(f"context:\n{ctx}")
        return "\n\n".join(parts)

"""Keyword retrieve + ``<gateway_memory>`` inject. Fail-open."""

from __future__ import annotations

import logging
import re
import time
from typing import Any

from shared_quota_router.feature_flags import is_gateway_memory_enabled
from shared_quota_router.memory_store import load_entries
from shared_quota_router.pipeline import EnhanceEnvelope

logger = logging.getLogger(__name__)

_TOKEN_RE = re.compile(r"[A-Za-z0-9_\u4e00-\u9fff]+")
_MAX_TOKENS = 2000
_TIMEOUT_S = 0.3
_MIN_QUERY_CHARS = 8


def _tokens(text: str) -> set[str]:
    return {t.lower() for t in _TOKEN_RE.findall(text) if len(t) >= 2}


def _user_text(messages: Any) -> str:
    chunks: list[str] = []
    if not isinstance(messages, list):
        return ""
    for msg in messages:
        if not isinstance(msg, dict) or msg.get("role") != "user":
            continue
        content = msg.get("content")
        if isinstance(content, str):
            chunks.append(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    chunks.append(str(block.get("text") or ""))
    return "\n".join(chunks)


def score_entries(query: str, entries: list[dict[str, Any]]) -> list[str]:
    qtok = _tokens(query)
    ranked: list[tuple[int, str]] = []
    for entry in entries:
        text = str(entry.get("text") or "")
        overlap = len(qtok & _tokens(text))
        if overlap <= 0:
            continue
        ranked.append((overlap, text))
    ranked.sort(key=lambda item: item[0], reverse=True)
    picked: list[str] = []
    used = 0
    for _score, text in ranked:
        approx = max(1, len(text) // 4)
        if used + approx > _MAX_TOKENS:
            continue
        picked.append(text)
        used += approx
    return picked


def _inject(messages: list[Any], blob: str) -> None:
    block = {"type": "text", "text": f"<gateway_memory>\n{blob}\n</gateway_memory>"}
    for msg in messages:
        if not isinstance(msg, dict) or msg.get("role") != "user":
            continue
        content = msg.get("content")
        if isinstance(content, str):
            msg["content"] = [block, {"type": "text", "text": content}]
            return
        if isinstance(content, list):
            content.insert(0, block)
            return
        msg["content"] = [block]
        return
    messages.insert(0, {"role": "user", "content": [block]})


class MemoryRetrieveStage:
    name = "memory_retrieve"

    def enabled(self) -> bool:
        return is_gateway_memory_enabled()

    async def run(self, env: EnhanceEnvelope) -> None:
        if not env.workspace:
            logger.info(
                "enhance_memory model=%s workspace_known=false injected=0",
                env.model_group,
            )
            return
        user = _user_text(env.messages)
        if len(user.strip()) < _MIN_QUERY_CHARS and not env.visual_evidence:
            return
        query = "\n".join([user, *env.visual_evidence, env.workspace])
        started = time.perf_counter()
        try:
            entries = load_entries(env.workspace)
            hits = score_entries(query, entries)
        except Exception:
            return
        if (time.perf_counter() - started) > _TIMEOUT_S:
            return
        if not hits:
            logger.info(
                "enhance_memory model=%s workspace_known=true injected=0",
                env.model_group,
            )
            return
        env.memory_hits.extend(hits)
        _inject(env.messages, "\n---\n".join(hits))
        logger.info(
            "enhance_memory model=%s workspace_known=true injected=%s",
            env.model_group,
            len(hits),
        )

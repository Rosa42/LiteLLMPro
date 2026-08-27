"""Shared types for vision agent presets."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Protocol


@dataclass(frozen=True, slots=True)
class ImageRef:
    ordinal: int
    message_index: int
    path: tuple[int, ...]


class AgentPreset(Protocol):
    id: str
    prompt_rev: int

    def match_header(self, headers: Mapping[str, Any]) -> bool: ...

    def match_messages(self, messages: list[Any]) -> bool: ...

    def extract_guide(self, messages: list[Any], image_ref: ImageRef) -> str: ...

    def system_addendum(self) -> str: ...

"""Protocol converter interface (C2)."""

from __future__ import annotations

from typing import Any, Protocol

from shared_quota_router.conversion.contracts import (
    ConvertedRequest,
    ConvertedResponse,
    Direction,
)


class ProtocolConverter(Protocol):
    direction: Direction

    def convert_request(self, public_payload: dict[str, Any]) -> ConvertedRequest: ...

    def convert_response(self, upstream_payload: dict[str, Any]) -> ConvertedResponse: ...

    def convert_error(self, upstream_error: dict[str, Any]) -> dict[str, Any]: ...

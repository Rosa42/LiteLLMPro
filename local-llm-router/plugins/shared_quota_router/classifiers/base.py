"""Failure classification types and classifier protocol.

Single source of truth for FailureKind / FailureClassification (not models.py).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any


class FailureKind(str, Enum):
    SHARED_QUOTA_EXHAUSTED = "shared_quota_exhausted"
    SHORT_RATE_LIMIT = "short_rate_limit"
    AUTH_INVALID = "auth_invalid"
    ACCOUNT_DISABLED = "account_disabled"
    PROVIDER_OUTAGE = "provider_outage"
    DEPLOYMENT_ERROR = "deployment_error"
    CONTEXT_LIMIT = "context_limit"
    CONTENT_POLICY = "content_policy"
    BAD_REQUEST = "bad_request"
    NETWORK_ERROR = "network_error"
    UNKNOWN = "unknown"


class FailureScope(str, Enum):
    QUOTA_GROUP = "quota_group"
    DEPLOYMENT = "deployment"
    PROVIDER = "provider"
    REQUEST = "request"


@dataclass(slots=True)
class FailureClassification:
    kind: FailureKind
    retryable: bool
    scope: str
    retry_after_seconds: int | None = None
    reset_at: datetime | None = None
    confidence: float = 0.0
    normalized_message: str = ""


@dataclass(slots=True)
class UpstreamError:
    """Normalized error input for classifiers."""

    http_status: int | None = None
    body: dict[str, Any] | str | None = None
    headers: dict[str, str] | None = None
    message: str | None = None
    provider_id: str | None = None


class BaseClassifier(ABC):
    @abstractmethod
    def classify(self, error: UpstreamError) -> FailureClassification:
        raise NotImplementedError


def parse_retry_after_seconds(headers: dict[str, str] | None) -> int | None:
    if not headers:
        return None
    # Case-insensitive lookup
    for k, v in headers.items():
        if k.lower() == "retry-after":
            try:
                return int(str(v).strip())
            except ValueError:
                return None
    return None


def extract_error_code_and_message(
    body: dict[str, Any] | str | None,
) -> tuple[str | None, str | None]:
    if body is None:
        return None, None
    if isinstance(body, str):
        return None, body
    err = body.get("error") if isinstance(body, dict) else None
    if isinstance(err, dict):
        code = err.get("code") or err.get("type")
        msg = err.get("message")
        return (str(code) if code is not None else None, str(msg) if msg is not None else None)
    if isinstance(err, str):
        return None, err
    msg = body.get("message") if isinstance(body, dict) else None
    return None, str(msg) if msg is not None else None

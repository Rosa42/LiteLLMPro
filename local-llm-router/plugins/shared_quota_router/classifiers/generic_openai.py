"""Generic OpenAI-compatible failure classifier."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from shared_quota_router.classifiers.base import (
    BaseClassifier,
    FailureClassification,
    FailureKind,
    FailureScope,
    UpstreamError,
    extract_error_code_and_message,
    parse_retry_after_seconds,
)

# High confidence threshold required before SHARED_QUOTA_EXHAUSTED
HIGH_CONFIDENCE = 0.85

_QUOTA_EXHAUST_MARKERS = (
    "quota",
    "insufficient_quota",
    "exceeded your current quota",
    "billing",
    "balance",
    "额度",
    "套餐",
    "用量已用尽",
    "quota exceeded",
    "rate limit reached for",
    "tokens per day",
    "tokens per 5",
    "5-hour",
    "five hour",
)

_SHORT_RATE_MARKERS = (
    "rate_limit",
    "rate limit",
    "too many requests",
    "tpm",
    "rpm",
    "concurrency",
)

_AUTH_MARKERS = (
    "invalid_api_key",
    "incorrect api key",
    "authentication",
    "unauthorized",
    "invalid token",
    "api key not found",
)

_POLICY_MARKERS = (
    "content_policy",
    "content filter",
    "safety",
    "moderation",
    "responsibleai",
)

_CONTEXT_MARKERS = (
    "context_length",
    "maximum context",
    "too many tokens",
    "context window",
)


class GenericOpenAIClassifier(BaseClassifier):
    def classify(self, error: UpstreamError) -> FailureClassification:
        status = error.http_status
        code, msg = extract_error_code_and_message(error.body)
        text = " ".join(
            x for x in (error.message, msg, code if isinstance(code, str) else None) if x
        ).lower()
        headers = error.headers or {}
        retry_after = parse_retry_after_seconds(headers)
        reset_at = None
        if retry_after is not None:
            reset_at = datetime.now(timezone.utc) + timedelta(seconds=retry_after)

        # Auth before generic invalid_request_error (providers often reuse that type).
        if status in {401, 403} or any(m in text for m in _AUTH_MARKERS):
            return FailureClassification(
                kind=FailureKind.AUTH_INVALID,
                retryable=True,  # retryable across accounts, not same key
                scope=FailureScope.QUOTA_GROUP.value,
                confidence=0.95,
                normalized_message="auth_invalid",
            )

        # Client / non-switch errors
        if status == 400 or (code and str(code).lower() in {"invalid_request_error", "bad_request"}):
            if any(m in text for m in _CONTEXT_MARKERS):
                return FailureClassification(
                    kind=FailureKind.CONTEXT_LIMIT,
                    retryable=False,
                    scope=FailureScope.REQUEST.value,
                    confidence=0.9,
                    normalized_message="context_limit",
                    reset_at=reset_at,
                    retry_after_seconds=retry_after,
                )
            if any(m in text for m in _POLICY_MARKERS):
                return FailureClassification(
                    kind=FailureKind.CONTENT_POLICY,
                    retryable=False,
                    scope=FailureScope.REQUEST.value,
                    confidence=0.9,
                    normalized_message="content_policy",
                )
            return FailureClassification(
                kind=FailureKind.BAD_REQUEST,
                retryable=False,
                scope=FailureScope.REQUEST.value,
                confidence=0.85,
                normalized_message="bad_request",
            )

        if status == 429 or "rate" in text or "quota" in text:
            # Prefer explicit quota/exhaust signals at high confidence
            if code and str(code).lower() in {
                "insufficient_quota",
                "quota_exceeded",
                "billing_not_active",
            }:
                return FailureClassification(
                    kind=FailureKind.SHARED_QUOTA_EXHAUSTED,
                    retryable=True,
                    scope=FailureScope.QUOTA_GROUP.value,
                    confidence=0.95,
                    normalized_message="shared_quota_exhausted",
                    reset_at=reset_at,
                    retry_after_seconds=retry_after,
                )
            if any(m in text for m in _QUOTA_EXHAUST_MARKERS) and not _looks_like_short_only(text):
                return FailureClassification(
                    kind=FailureKind.SHARED_QUOTA_EXHAUSTED,
                    retryable=True,
                    scope=FailureScope.QUOTA_GROUP.value,
                    confidence=0.9,
                    normalized_message="shared_quota_exhausted",
                    reset_at=reset_at,
                    retry_after_seconds=retry_after,
                )
            # Generic 429 without strong quota signal → short rate limit (not full account burn)
            conf = 0.8 if any(m in text for m in _SHORT_RATE_MARKERS) else 0.7
            return FailureClassification(
                kind=FailureKind.SHORT_RATE_LIMIT,
                retryable=True,
                scope=FailureScope.DEPLOYMENT.value,
                confidence=conf,
                normalized_message="short_rate_limit",
                reset_at=reset_at,
                retry_after_seconds=retry_after,
            )

        if status in {500, 502, 503, 504}:
            return FailureClassification(
                kind=FailureKind.PROVIDER_OUTAGE,
                retryable=True,
                scope=FailureScope.PROVIDER.value,
                confidence=0.75,
                normalized_message="provider_outage",
                retry_after_seconds=retry_after,
                reset_at=reset_at,
            )

        if status is None and error.message:
            return FailureClassification(
                kind=FailureKind.NETWORK_ERROR,
                retryable=True,
                scope=FailureScope.DEPLOYMENT.value,
                confidence=0.7,
                normalized_message="network_error",
            )

        return FailureClassification(
            kind=FailureKind.UNKNOWN,
            retryable=True,
            scope=FailureScope.DEPLOYMENT.value,
            confidence=0.4,
            normalized_message="unknown",
            retry_after_seconds=retry_after,
            reset_at=reset_at,
        )


def _looks_like_short_only(text: str) -> bool:
    """True when text is clearly short rate limit without account quota wording."""
    if any(m in text for m in ("tpm", "rpm", "requests per minute", "tokens per minute")):
        if not any(m in text for m in ("quota", "billing", "额度", "套餐", "5-hour", "daily")):
            return True
    return False


def is_high_confidence_quota_exhaust(c: FailureClassification) -> bool:
    return (
        c.kind == FailureKind.SHARED_QUOTA_EXHAUSTED and c.confidence >= HIGH_CONFIDENCE
    )

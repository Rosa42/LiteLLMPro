from shared_quota_router.classifiers.base import FailureKind, UpstreamError
from shared_quota_router.classifiers.generic_openai import (
    GenericOpenAIClassifier,
    is_high_confidence_quota_exhaust,
)


clf = GenericOpenAIClassifier()


def test_insufficient_quota_is_shared_exhaust() -> None:
    err = UpstreamError(
        http_status=429,
        body={"error": {"code": "insufficient_quota", "message": "You exceeded your current quota"}},
    )
    result = clf.classify(err)
    assert result.kind == FailureKind.SHARED_QUOTA_EXHAUSTED
    assert result.scope == "quota_group"
    assert is_high_confidence_quota_exhaust(result)


def test_short_rate_limit_not_full_account() -> None:
    err = UpstreamError(
        http_status=429,
        body={"error": {"code": "rate_limit_exceeded", "message": "Rate limit reached for TPM"}},
        headers={"Retry-After": "10"},
    )
    result = clf.classify(err)
    assert result.kind == FailureKind.SHORT_RATE_LIMIT
    assert result.scope == "deployment"
    assert result.retry_after_seconds == 10
    assert result.reset_at is not None


def test_auth_invalid() -> None:
    err = UpstreamError(
        http_status=401,
        body={"error": {"message": "Invalid API key", "type": "invalid_request_error"}},
    )
    result = clf.classify(err)
    assert result.kind == FailureKind.AUTH_INVALID
    assert result.scope == "quota_group"


def test_content_policy_not_cross_account() -> None:
    err = UpstreamError(
        http_status=400,
        body={"error": {"message": "Content policy violation", "code": "content_policy"}},
    )
    result = clf.classify(err)
    assert result.kind == FailureKind.CONTENT_POLICY
    assert result.retryable is False
    assert result.scope == "request"


def test_bad_request_not_cross_account() -> None:
    err = UpstreamError(
        http_status=400,
        body={"error": {"message": "Invalid parameter", "type": "invalid_request_error"}},
    )
    result = clf.classify(err)
    assert result.kind == FailureKind.BAD_REQUEST
    assert result.retryable is False

"""OpenCode Go 失败分类：RegionError 优先，其余委托 Generic。"""

from __future__ import annotations

from shared_quota_router.classifiers.base import (
    FailureClassification,
    FailureKind,
    FailureScope,
    UpstreamError,
    extract_error_code_and_message,
)
from shared_quota_router.classifiers.generic_openai import GenericOpenAIClassifier

# RegionError 文案信号（大小写不敏感）
_REGION_MSG_MARKERS = (
    "requires explicit opt in",
    "hosted in china",
)


def _is_opencode_region_error(error: UpstreamError) -> bool:
    """403 + type=RegionError，或 message 含 region 关键词。"""
    if error.http_status != 403:
        return False
    code, msg = extract_error_code_and_message(error.body)
    type_or_code = (code or "").strip()
    if type_or_code.lower() == "regionerror":
        return True
    text = " ".join(
        x for x in (error.message, msg, type_or_code) if x
    ).lower()
    return any(m in text for m in _REGION_MSG_MARKERS)


class OpenCodeGoClassifier(GenericOpenAIClassifier):
    """先匹配 RegionError → DEPLOYMENT_ERROR；再委托 generic。"""

    def classify(self, error: UpstreamError) -> FailureClassification:
        if _is_opencode_region_error(error):
            return FailureClassification(
                kind=FailureKind.DEPLOYMENT_ERROR,
                retryable=True,  # 可换同账号其他 deployment
                scope=FailureScope.DEPLOYMENT.value,
                confidence=0.9,
                normalized_message="region_blocked",
            )
        return super().classify(error)

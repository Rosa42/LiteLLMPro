"""Volcengine Coding Plan classifier skeleton."""

from __future__ import annotations

from shared_quota_router.classifiers.base import FailureClassification, UpstreamError
from shared_quota_router.classifiers.generic_openai import GenericOpenAIClassifier


class VolcengineClassifier(GenericOpenAIClassifier):
    def classify(self, error: UpstreamError) -> FailureClassification:
        return super().classify(error)

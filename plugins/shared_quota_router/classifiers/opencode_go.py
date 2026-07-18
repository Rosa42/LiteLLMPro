"""OpenCode Go classifier skeleton — extend with real fixtures; no fake high confidence."""

from __future__ import annotations

from shared_quota_router.classifiers.base import FailureClassification, UpstreamError
from shared_quota_router.classifiers.generic_openai import GenericOpenAIClassifier


class OpenCodeGoClassifier(GenericOpenAIClassifier):
    """Provider-specific overrides can be added when real error samples are collected."""

    def classify(self, error: UpstreamError) -> FailureClassification:
        # Delegate to generic path; do not invent high-confidence exhaust without samples.
        result = super().classify(error)
        return result

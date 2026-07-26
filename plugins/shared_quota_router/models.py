"""Core data models for shared-quota routing.

FailureKind / FailureClassification live in classifiers.base (single source of truth).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Iterable


class ProviderStatus(str, Enum):
    AVAILABLE = "AVAILABLE"
    DEGRADED = "DEGRADED"
    COOLDOWN = "COOLDOWN"
    DISABLED = "DISABLED"


class QuotaGroupStatus(str, Enum):
    AVAILABLE = "AVAILABLE"
    DEGRADED = "DEGRADED"
    EXHAUSTED = "EXHAUSTED"
    PROBING = "PROBING"
    DISABLED = "DISABLED"


class WindowType(str, Enum):
    FIVE_HOUR = "FIVE_HOUR"
    DAILY = "DAILY"
    WEEKLY = "WEEKLY"
    MONTHLY = "MONTHLY"
    UNKNOWN = "UNKNOWN"


class WindowStatus(str, Enum):
    OK = "OK"
    EXHAUSTED = "EXHAUSTED"
    UNKNOWN = "UNKNOWN"


class ApiProtocol(str, Enum):
    """Public / upstream API protocol identifiers (MVP).

    Values are stable wire strings. Never infer these from model names.
    """

    OPENAI_CHAT = "openai_chat"
    OPENAI_RESPONSES = "openai_responses"
    ANTHROPIC_MESSAGES = "anthropic_messages"


class Feature(str, Enum):
    """Minimal MVP feature set for capability filtering.

    Post-MVP features (reasoning, prompt cache, citations, …) stay out of
    runtime code until explicitly required.
    """

    TEXT = "text"
    STREAMING = "streaming"
    TOOLS = "tools"


def parse_api_protocol(value: Any) -> ApiProtocol:
    """Parse a protocol string; unknown values raise ValueError."""
    if isinstance(value, ApiProtocol):
        return value
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"invalid protocol: {value!r}")
    key = value.strip().lower()
    try:
        return ApiProtocol(key)
    except ValueError as exc:
        known = ", ".join(p.value for p in ApiProtocol)
        raise ValueError(f"unknown protocol {value!r}; expected one of: {known}") from exc


def parse_feature(value: Any) -> Feature:
    if isinstance(value, Feature):
        return value
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"invalid feature: {value!r}")
    key = value.strip().lower()
    try:
        return Feature(key)
    except ValueError as exc:
        known = ", ".join(f.value for f in Feature)
        raise ValueError(f"unknown feature {value!r}; expected one of: {known}") from exc


def parse_feature_set(values: Any) -> frozenset[Feature]:
    """Parse an iterable of features. Missing/None → empty (not universal support)."""
    if values is None:
        return frozenset()
    if isinstance(values, (str, bytes)) or not isinstance(values, Iterable):
        raise ValueError(f"supported_features must be a list, got {type(values).__name__}")
    return frozenset(parse_feature(v) for v in values)


@dataclass(frozen=True, slots=True)
class LogicalModelProtocols:
    """Project-owned declaration of which public protocols a logical model opts into.

    Empty ``public_protocols`` means the model is unavailable on every public
    endpoint (missing protocol does **not** imply universal support).
    """

    model_group: str
    public_protocols: frozenset[ApiProtocol] = frozenset()

    def supports(self, protocol: ApiProtocol) -> bool:
        return protocol in self.public_protocols

    @classmethod
    def from_config(
        cls,
        model_group: str,
        public_protocols: Any,
    ) -> LogicalModelProtocols:
        if public_protocols is None:
            return cls(model_group=model_group, public_protocols=frozenset())
        if isinstance(public_protocols, (str, bytes)) or not isinstance(
            public_protocols, Iterable
        ):
            raise ValueError(
                f"public_protocols for {model_group!r} must be a list of protocol strings"
            )
        protocols = frozenset(parse_api_protocol(p) for p in public_protocols)
        return cls(model_group=model_group, public_protocols=protocols)


@dataclass(slots=True)
class Provider:
    provider_id: str
    name: str
    base_url: str
    classifier_type: str
    status: ProviderStatus = ProviderStatus.AVAILABLE


@dataclass(slots=True)
class QuotaGroup:
    """One Coding Plan account / shared quota boundary."""

    quota_group_id: str
    provider_id: str
    account_id: str
    display_name: str
    priority: int = 100
    status: QuotaGroupStatus = QuotaGroupStatus.AVAILABLE
    reset_at: datetime | None = None
    cooldown_until: datetime | None = None
    failure_reason: str | None = None
    last_failure_at: datetime | None = None
    last_success_at: datetime | None = None
    consecutive_failures: int = 0
    revision: int = 0
    next_probe_at: datetime | None = None


@dataclass(slots=True)
class QuotaWindow:
    """Optional multi-window observation. Phase 1–9: fields only, no active ops UI."""

    quota_group_id: str
    window_type: WindowType
    status: WindowStatus = WindowStatus.UNKNOWN
    remaining: float | None = None
    reset_at: datetime | None = None
    observed_at: datetime | None = None


@dataclass(slots=True)
class Deployment:
    """One (account × model) upstream target."""

    deployment_id: str
    model_group: str
    upstream_model: str
    provider_id: str
    quota_group_id: str
    priority: int = 100
    weight: int = 1
    enabled: bool = True
    api_base: str | None = None
    api_key_env: str | None = None
    # Protocol capability metadata (M1). Missing protocol is not universal support.
    upstream_protocol: ApiProtocol | None = None
    supported_features: frozenset[Feature] = field(default_factory=frozenset)
    supports_streaming: bool = False
    extra: dict[str, Any] = field(default_factory=dict)

    def supports_protocol(self, protocol: ApiProtocol) -> bool:
        """True only when protocol is explicitly declared."""
        return self.upstream_protocol is not None and self.upstream_protocol == protocol

    def supports_feature(self, feature: Feature) -> bool:
        if feature is Feature.STREAMING:
            return self.supports_streaming or Feature.STREAMING in self.supported_features
        return feature in self.supported_features


@dataclass(slots=True)
class DeploymentRuntimeState:
    deployment_id: str
    is_in_cooldown: bool = False
    cooldown_until: datetime | None = None
    last_success_at: datetime | None = None
    last_failure_at: datetime | None = None


@dataclass(slots=True)
class RequestRoutingContext:
    """Per-request routing collaboration between strategy (read) and callbacks (write).

    Semantics (hard constraints):
    - Same request: at most one attempt per quota_group
    - Same request: at most max_quota_groups distinct groups
    - first_byte_sent=True: forbid cross-deployment retry/switch
    """

    request_id: str
    tried_quota_groups: set[str] = field(default_factory=set)
    first_byte_sent: bool = False
    max_quota_groups: int = 3

    def can_try_quota_group(self, quota_group_id: str) -> bool:
        if self.first_byte_sent:
            return False
        if quota_group_id in self.tried_quota_groups:
            return False
        if (
            quota_group_id not in self.tried_quota_groups
            and len(self.tried_quota_groups) >= self.max_quota_groups
        ):
            return False
        return True

    def mark_tried(self, quota_group_id: str) -> None:
        self.tried_quota_groups.add(quota_group_id)

    def mark_first_byte_sent(self) -> None:
        self.first_byte_sent = True

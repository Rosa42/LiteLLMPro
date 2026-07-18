"""Core data models for shared-quota routing.

FailureKind / FailureClassification live in classifiers.base (single source of truth).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


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
    extra: dict[str, Any] = field(default_factory=dict)


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

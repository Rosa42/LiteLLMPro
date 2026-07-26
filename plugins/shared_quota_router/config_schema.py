"""Plans configuration schema and fail-closed validation (M1-03).

Source of truth for operator config: ``config/plans.yaml``.
Secrets must appear only as environment variable *names* (values live in ``.env``).

Rules (summary):
- ``upstream_protocol`` is plan/deployment capability — never auto-exposes public APIs.
- ``logical_models[].public_protocols`` is explicit public opt-in per logical model.
- Missing / empty public opt-in ⇒ model unavailable on every public endpoint.
- Unknown protocols, duplicate plan IDs, and Responses opt-in without a Responses
  deployment are configuration errors (fail before LiteLLM starts).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

import yaml

from shared_quota_router.models import (
    ApiProtocol,
    Feature,
    LogicalModelProtocols,
    parse_api_protocol,
    parse_feature_set,
)

# MVP defaults when a plan declares openai_chat without listing features.
DEFAULT_CHAT_FEATURES: frozenset[Feature] = frozenset(
    {Feature.TEXT, Feature.STREAMING, Feature.TOOLS}
)

_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
# Reject accidental secret material in config files (values, not env names).
_SECRETISH_RE = re.compile(
    r"(?i)\b(sk-[a-zA-Z0-9]{10,}|ark-[a-zA-Z0-9]{8,}|Bearer\s+\S+|api[_-]?key\s*[:=]\s*['\"]?[^'\"\s]{8,})"
)


class ConfigValidationError(ValueError):
    """Invalid plans configuration (fail-closed)."""


@dataclass(slots=True)
class PlanModelEntry:
    """One model under a plan. String form in YAML expands to model-only entry."""

    model: str
    upstream_protocol: ApiProtocol | None = None
    supported_features: frozenset[Feature] | None = None
    supports_streaming: bool | None = None
    enabled: bool | None = None


@dataclass(slots=True)
class PlanConfig:
    id: str
    display_name: str
    provider_id: str
    priority: int
    base_url_env: str
    api_key_env: str
    models: list[PlanModelEntry]
    upstream_protocol: ApiProtocol | None = None
    supported_features: frozenset[Feature] = field(default_factory=frozenset)
    supports_streaming: bool = False
    enabled: bool = True

    def resolved_protocol(self, model: PlanModelEntry) -> ApiProtocol | None:
        if model.upstream_protocol is not None:
            return model.upstream_protocol
        return self.upstream_protocol

    def resolved_features(self, model: PlanModelEntry) -> frozenset[Feature]:
        if model.supported_features is not None:
            return model.supported_features
        if self.supported_features:
            return self.supported_features
        proto = self.resolved_protocol(model)
        if proto is ApiProtocol.OPENAI_CHAT:
            return DEFAULT_CHAT_FEATURES
        return frozenset()

    def resolved_streaming(self, model: PlanModelEntry) -> bool:
        if model.supports_streaming is not None:
            return model.supports_streaming
        features = self.resolved_features(model)
        if Feature.STREAMING in features:
            return True
        return self.supports_streaming

    def resolved_enabled(self, model: PlanModelEntry) -> bool:
        if not self.enabled:
            return False
        if model.enabled is not None:
            return model.enabled
        # No protocol ⇒ not protocol-capable; keep generation disabled.
        return self.resolved_protocol(model) is not None


@dataclass(slots=True)
class PlansDocument:
    plans: list[PlanConfig]
    logical_models: dict[str, LogicalModelProtocols] = field(default_factory=dict)

    def enabled_deployments_protocols(self) -> set[ApiProtocol]:
        out: set[ApiProtocol] = set()
        for plan in self.plans:
            for m in plan.models:
                if not plan.resolved_enabled(m):
                    continue
                proto = plan.resolved_protocol(m)
                if proto is not None:
                    out.add(proto)
        return out


def _reject_secrets(blob: str, *, context: str) -> None:
    if _SECRETISH_RE.search(blob):
        raise ConfigValidationError(
            f"{context}: possible secret material detected; keep credentials in .env only"
        )


def _require_env_name(name: str, *, field_name: str, plan_id: str) -> str:
    value = (name or "").strip()
    if not value:
        raise ConfigValidationError(f"plan {plan_id!r}: {field_name} is required")
    if not _ENV_NAME_RE.match(value):
        raise ConfigValidationError(
            f"plan {plan_id!r}: {field_name}={value!r} must be an env var name "
            f"(not a secret value)"
        )
    if value.startswith("os.environ/"):
        raise ConfigValidationError(
            f"plan {plan_id!r}: {field_name} should be bare env name, not os.environ/..."
        )
    return value


def _parse_model_entry(raw: Any, *, plan_id: str) -> PlanModelEntry:
    if isinstance(raw, str):
        model = raw.strip()
        if not model or model.startswith("#"):
            raise ConfigValidationError(f"plan {plan_id!r}: empty model name")
        return PlanModelEntry(model=model)
    if not isinstance(raw, Mapping):
        raise ConfigValidationError(
            f"plan {plan_id!r}: model entry must be a string or mapping, got {type(raw).__name__}"
        )
    model = str(raw.get("model") or raw.get("name") or "").strip()
    if not model:
        raise ConfigValidationError(f"plan {plan_id!r}: model mapping missing 'model'")
    proto_raw = raw.get("upstream_protocol")
    proto = parse_api_protocol(proto_raw) if proto_raw not in (None, "") else None
    features = None
    if "supported_features" in raw:
        features = parse_feature_set(raw.get("supported_features"))
    streaming = raw.get("supports_streaming")
    enabled = raw.get("enabled")
    return PlanModelEntry(
        model=model,
        upstream_protocol=proto,
        supported_features=features,
        supports_streaming=bool(streaming) if streaming is not None else None,
        enabled=bool(enabled) if enabled is not None else None,
    )


def _parse_plan(raw: Mapping[str, Any]) -> PlanConfig:
    plan_id = str(raw.get("id") or "").strip()
    if not plan_id:
        raise ConfigValidationError("plan missing id")
    _reject_secrets(plan_id, context=f"plan id {plan_id!r}")

    base_url_env = _require_env_name(
        str(raw.get("base_url_env") or ""),
        field_name="base_url_env",
        plan_id=plan_id,
    )
    api_key_env = _require_env_name(
        str(raw.get("api_key_env") or ""),
        field_name="api_key_env",
        plan_id=plan_id,
    )

    models_raw = raw.get("models")
    if not isinstance(models_raw, list) or not models_raw:
        raise ConfigValidationError(f"plan {plan_id!r}: models must be a non-empty list")

    models = [_parse_model_entry(m, plan_id=plan_id) for m in models_raw]
    names = [m.model for m in models]
    if len(names) != len(set(names)):
        raise ConfigValidationError(f"plan {plan_id!r}: duplicate model entries")

    proto_raw = raw.get("upstream_protocol")
    try:
        upstream_protocol = (
            parse_api_protocol(proto_raw) if proto_raw not in (None, "") else None
        )
    except ValueError as exc:
        raise ConfigValidationError(f"plan {plan_id!r}: {exc}") from exc

    try:
        features = parse_feature_set(raw.get("supported_features"))
    except ValueError as exc:
        raise ConfigValidationError(f"plan {plan_id!r}: {exc}") from exc

    if not features and upstream_protocol is ApiProtocol.OPENAI_CHAT:
        features = DEFAULT_CHAT_FEATURES

    supports_streaming = bool(raw.get("supports_streaming", Feature.STREAMING in features))
    enabled = bool(raw.get("enabled", True))
    display_name = str(raw.get("display_name") or plan_id).strip()
    provider_id = str(raw.get("provider_id") or "unknown").strip()
    try:
        priority = int(raw.get("priority", 100))
    except (TypeError, ValueError) as exc:
        raise ConfigValidationError(f"plan {plan_id!r}: priority must be int") from exc

    return PlanConfig(
        id=plan_id,
        display_name=display_name,
        provider_id=provider_id,
        priority=priority,
        base_url_env=base_url_env,
        api_key_env=api_key_env,
        models=models,
        upstream_protocol=upstream_protocol,
        supported_features=features,
        supports_streaming=supports_streaming,
        enabled=enabled,
    )


def _parse_logical_models(raw: Any) -> dict[str, LogicalModelProtocols]:
    """Parse logical_models map.

    Forms:
      logical_models:
        kimi-k3:
          public_protocols: [openai_chat]
      # or list form:
      logical_models:
        - model_group: kimi-k3
          public_protocols: [openai_chat]
    """
    if raw is None:
        return {}
    result: dict[str, LogicalModelProtocols] = {}

    def _add(model_group: str, public_raw: Any) -> None:
        mg = model_group.strip()
        if not mg:
            raise ConfigValidationError("logical_models entry missing model_group")
        if public_raw is None:
            # Explicit key missing inside mapping → unavailable (empty set)
            result[mg] = LogicalModelProtocols(model_group=mg, public_protocols=frozenset())
            return
        if isinstance(public_raw, list) and len(public_raw) == 0:
            raise ConfigValidationError(
                f"logical model {mg!r}: public_protocols must not be an empty list "
                f"(omit the model or declare at least one protocol)"
            )
        try:
            lm = LogicalModelProtocols.from_config(mg, public_raw)
        except ValueError as exc:
            raise ConfigValidationError(f"logical model {mg!r}: {exc}") from exc
        if not lm.public_protocols:
            raise ConfigValidationError(
                f"logical model {mg!r}: public_protocols resolved empty; "
                f"omit the entry instead of declaring empty opt-in"
            )
        result[mg] = lm

    if isinstance(raw, Mapping):
        for key, val in raw.items():
            if isinstance(val, Mapping):
                _add(str(key), val.get("public_protocols"))
            elif isinstance(val, list):
                _add(str(key), val)
            else:
                raise ConfigValidationError(
                    f"logical_models.{key}: expected mapping or list of protocols"
                )
        return result

    if isinstance(raw, list):
        for item in raw:
            if not isinstance(item, Mapping):
                raise ConfigValidationError("logical_models list items must be mappings")
            mg = str(item.get("model_group") or item.get("model") or "")
            _add(mg, item.get("public_protocols"))
        return result

    raise ConfigValidationError("logical_models must be a mapping or list")


def validate_plans_document(doc: PlansDocument) -> None:
    """Cross-field validation after parse."""
    if not doc.plans:
        raise ConfigValidationError("plans must be a non-empty list")

    ids = [p.id for p in doc.plans]
    if len(ids) != len(set(ids)):
        raise ConfigValidationError("duplicate plan id")

    # Plan-level protocol never grants public exposure automatically:
    # only logical_models.public_protocols does.
    available = doc.enabled_deployments_protocols()

    for mg, lm in doc.logical_models.items():
        for proto in lm.public_protocols:
            if proto not in available:
                raise ConfigValidationError(
                    f"logical model {mg!r} opts into public protocol {proto.value!r} "
                    f"but no enabled deployment declares matching upstream_protocol"
                )
            if proto is ApiProtocol.OPENAI_RESPONSES:
                # Extra explicit gate for Responses.
                if ApiProtocol.OPENAI_RESPONSES not in available:
                    raise ConfigValidationError(
                        f"logical model {mg!r}: Responses public opt-in requires at least "
                        f"one enabled deployment with upstream_protocol=openai_responses"
                    )


def load_plans_dict(data: Mapping[str, Any]) -> PlansDocument:
    """Validate a already-loaded YAML mapping."""
    text_blob = yaml.safe_dump(dict(data), default_flow_style=False, allow_unicode=True)
    _reject_secrets(text_blob, context="plans config")

    plans_raw = data.get("plans")
    if not isinstance(plans_raw, list):
        raise ConfigValidationError("root key 'plans' must be a list")

    plans: list[PlanConfig] = []
    for item in plans_raw:
        if not isinstance(item, Mapping):
            raise ConfigValidationError("each plan must be a mapping")
        try:
            plans.append(_parse_plan(item))
        except ConfigValidationError:
            raise
        except ValueError as exc:
            raise ConfigValidationError(str(exc)) from exc

    logical = _parse_logical_models(data.get("logical_models"))
    doc = PlansDocument(plans=plans, logical_models=logical)
    validate_plans_document(doc)
    return doc


def load_plans_file(path: str | Path) -> PlansDocument:
    p = Path(path)
    if not p.is_file():
        raise ConfigValidationError(f"plans file not found: {p}")
    raw_text = p.read_text(encoding="utf-8")
    _reject_secrets(raw_text, context=str(p))
    try:
        data = yaml.safe_load(raw_text)
    except yaml.YAMLError as exc:
        raise ConfigValidationError(f"invalid YAML in {p}: {exc}") from exc
    if not isinstance(data, Mapping):
        raise ConfigValidationError(f"{p}: root must be a mapping")
    return load_plans_dict(data)


def iter_all_model_groups(doc: PlansDocument) -> list[str]:
    """Stable unique model groups appearing under any plan."""
    seen: list[str] = []
    for plan in doc.plans:
        for m in plan.models:
            if m.model not in seen:
                seen.append(m.model)
    return seen


def public_protocols_for(doc: PlansDocument, model_group: str) -> frozenset[ApiProtocol]:
    lm = doc.logical_models.get(model_group)
    if lm is None:
        return frozenset()
    return lm.public_protocols

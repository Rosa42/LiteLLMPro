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
    ConversionCapability,
    Feature,
    FidelityClass,
    LogicalModelProtocols,
    parse_api_protocol,
    parse_feature_set,
    parse_fidelity_class,
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
    conversions: tuple[ConversionCapability, ...] | None = None


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
    conversions: tuple[ConversionCapability, ...] = ()

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

    def resolved_conversions(self, model: PlanModelEntry) -> tuple[ConversionCapability, ...]:
        if model.conversions is not None:
            return model.conversions
        return self.conversions

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


def _parse_conversion_list(raw: Any, *, context: str) -> tuple[ConversionCapability, ...]:
    """Parse deployment/plan conversions list (design §8.6; C1 forces streaming=false)."""
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ConfigValidationError(f"{context}: conversions must be a list")
    out: list[ConversionCapability] = []
    seen: set[tuple[ApiProtocol, ApiProtocol]] = set()
    for i, item in enumerate(raw):
        ctx = f"{context}[{i}]"
        if not isinstance(item, Mapping):
            raise ConfigValidationError(f"{ctx}: conversion entry must be a mapping")
        try:
            source = parse_api_protocol(item.get("from") or item.get("source"))
            target = parse_api_protocol(item.get("to") or item.get("target"))
            fidelity = parse_fidelity_class(item.get("fidelity", FidelityClass.EQUIVALENT.value))
        except ValueError as exc:
            raise ConfigValidationError(f"{ctx}: {exc}") from exc
        if source == target:
            raise ConfigValidationError(f"{ctx}: conversion source and target must differ")
        direction = (source, target)
        if direction in seen:
            raise ConfigValidationError(
                f"{ctx}: duplicate conversion direction {source.value} -> {target.value}"
            )
        seen.add(direction)
        streaming = bool(item.get("streaming", False))
        if streaming:
            raise ConfigValidationError(
                f"{ctx}: streaming: true is not allowed until C4 streaming conversion "
                f"is proven (set streaming: false)"
            )
        feat_block = item.get("features") if isinstance(item.get("features"), Mapping) else {}
        try:
            req = parse_feature_set(feat_block.get("request") if feat_block else None)
            resp = parse_feature_set(feat_block.get("response") if feat_block else None)
        except ValueError as exc:
            raise ConfigValidationError(f"{ctx}: {exc}") from exc
        if not req:
            req = frozenset({Feature.TEXT})
        if not resp:
            resp = frozenset({Feature.TEXT})
        out.append(
            ConversionCapability(
                source=source,
                target=target,
                request_features=req,
                response_features=resp,
                streaming=False,
                fidelity=fidelity,
            )
        )
    return tuple(out)


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
    conversions = None
    if "conversions" in raw:
        conversions = _parse_conversion_list(
            raw.get("conversions"), context=f"plan {plan_id!r} model {model!r}"
        )
    return PlanModelEntry(
        model=model,
        upstream_protocol=proto,
        supported_features=features,
        supports_streaming=bool(streaming) if streaming is not None else None,
        enabled=bool(enabled) if enabled is not None else None,
        conversions=conversions,
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

    plan_conversions = _parse_conversion_list(
        raw.get("conversions"), context=f"plan {plan_id!r}"
    )

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
        conversions=plan_conversions,
    )


def _parse_allowed_conversion_policy(
    raw: Any, *, model_group: str
) -> frozenset[tuple[ApiProtocol, ApiProtocol]]:
    if raw is None:
        return frozenset()
    if not isinstance(raw, Mapping):
        raise ConfigValidationError(
            f"logical model {model_group!r}: conversion_policy must be a mapping"
        )
    allowed = raw.get("allowed")
    if allowed is None:
        return frozenset()
    if not isinstance(allowed, list):
        raise ConfigValidationError(
            f"logical model {model_group!r}: conversion_policy.allowed must be a list"
        )
    pairs: set[tuple[ApiProtocol, ApiProtocol]] = set()
    for i, item in enumerate(allowed):
        ctx = f"logical model {model_group!r} conversion_policy.allowed[{i}]"
        if not isinstance(item, Mapping):
            raise ConfigValidationError(f"{ctx}: must be a mapping")
        try:
            source = parse_api_protocol(item.get("from") or item.get("source"))
            target = parse_api_protocol(item.get("to") or item.get("target"))
            if "fidelity" in item and item.get("fidelity") not in (None, ""):
                parse_fidelity_class(item.get("fidelity"))
        except ValueError as exc:
            raise ConfigValidationError(f"{ctx}: {exc}") from exc
        if source == target:
            raise ConfigValidationError(f"{ctx}: from and to must differ")
        if (source, target) in pairs:
            raise ConfigValidationError(
                f"{ctx}: duplicate direction {source.value} -> {target.value}"
            )
        pairs.add((source, target))
    return frozenset(pairs)


def _parse_logical_models(raw: Any) -> dict[str, LogicalModelProtocols]:
    """Parse logical_models map with optional allow_conversion / conversion_policy."""
    if raw is None:
        return {}
    result: dict[str, LogicalModelProtocols] = {}

    def _add(model_group: str, entry: Mapping[str, Any] | None, public_raw: Any) -> None:
        mg = model_group.strip()
        if not mg:
            raise ConfigValidationError("logical_models entry missing model_group")
        allow_conversion = bool((entry or {}).get("allow_conversion", False))
        allowed = _parse_allowed_conversion_policy(
            (entry or {}).get("conversion_policy"), model_group=mg
        )
        if not allow_conversion and allowed:
            raise ConfigValidationError(
                f"logical model {mg!r}: conversion_policy.allowed is set but "
                f"allow_conversion is false"
            )
        if public_raw is None:
            result[mg] = LogicalModelProtocols(
                model_group=mg,
                public_protocols=frozenset(),
                allow_conversion=False,
                allowed_conversions=frozenset(),
            )
            return
        if isinstance(public_raw, list) and len(public_raw) == 0:
            raise ConfigValidationError(
                f"logical model {mg!r}: public_protocols must not be an empty list "
                f"(omit the model or declare at least one protocol)"
            )
        try:
            lm = LogicalModelProtocols.from_config(
                mg,
                public_raw,
                allow_conversion=allow_conversion,
                allowed_conversions=allowed,
            )
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
                _add(str(key), val, val.get("public_protocols"))
            elif isinstance(val, list):
                _add(str(key), None, val)
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
            _add(mg, item, item.get("public_protocols"))
        return result

    raise ConfigValidationError("logical_models must be a mapping or list")


def _has_direct_upstream(
    doc: PlansDocument, model_group: str, protocol: ApiProtocol
) -> bool:
    for plan in doc.plans:
        for m in plan.models:
            if m.model != model_group:
                continue
            if not plan.resolved_enabled(m):
                continue
            if plan.resolved_protocol(m) is protocol:
                return True
    return False


def _has_conversion_route(
    doc: PlansDocument,
    lm: LogicalModelProtocols,
    public_protocol: ApiProtocol,
) -> bool:
    if not lm.allow_conversion:
        return False
    for source, target in lm.allowed_conversions:
        if source is not public_protocol:
            continue
        for plan in doc.plans:
            for m in plan.models:
                if m.model != lm.model_group:
                    continue
                if not plan.resolved_enabled(m):
                    continue
                if plan.resolved_protocol(m) is not target:
                    continue
                for cap in plan.resolved_conversions(m):
                    if cap.source is source and cap.target is target:
                        return True
    return False


def _iter_declared_conversions(
    doc: PlansDocument, model_group: str
) -> list[ConversionCapability]:
    out: list[ConversionCapability] = []
    for plan in doc.plans:
        for m in plan.models:
            if m.model != model_group:
                continue
            out.extend(plan.resolved_conversions(m))
    return out


def validate_plans_document(doc: PlansDocument) -> None:
    """Cross-field validation after parse."""
    if not doc.plans:
        raise ConfigValidationError("plans must be a non-empty list")

    ids = [p.id for p in doc.plans]
    if len(ids) != len(set(ids)):
        raise ConfigValidationError("duplicate plan id")

    available = doc.enabled_deployments_protocols()

    for mg, lm in doc.logical_models.items():
        declared = _iter_declared_conversions(doc, mg)
        if declared and not lm.allow_conversion:
            raise ConfigValidationError(
                f"logical model {mg!r}: plan/model conversions declared but "
                f"allow_conversion is false"
            )
        for cap in declared:
            if (cap.source, cap.target) not in lm.allowed_conversions:
                raise ConfigValidationError(
                    f"logical model {mg!r}: conversion {cap.source.value}->{cap.target.value} "
                    f"is not in conversion_policy.allowed"
                )
            if cap.target not in available:
                raise ConfigValidationError(
                    f"logical model {mg!r}: conversion target {cap.target.value} has no "
                    f"enabled deployment with matching upstream_protocol"
                )

        for proto in lm.public_protocols:
            if proto is ApiProtocol.OPENAI_RESPONSES:
                if not _has_direct_upstream(doc, mg, ApiProtocol.OPENAI_RESPONSES):
                    raise ConfigValidationError(
                        f"logical model {mg!r}: Responses public opt-in requires at least "
                        f"one enabled deployment with upstream_protocol=openai_responses"
                    )
                continue

            if _has_direct_upstream(doc, mg, proto):
                continue
            if _has_conversion_route(doc, lm, proto):
                continue
            raise ConfigValidationError(
                f"logical model {mg!r} opts into public protocol {proto.value!r} "
                f"but no enabled direct deployment or explicit conversion route exists"
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

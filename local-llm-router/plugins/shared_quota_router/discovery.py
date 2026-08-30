"""Model capability discovery (M1-05).

LiteLLM v1.90.5 ``GET /v1/models`` uses ``create_model_info_response`` and does
**not** surface custom ``model_info.public_protocols`` (only id/object/owned_by,
optional fallbacks when include_metadata=true).

Therefore discovery is project-owned:

- Pure aggregation from generated ``model_list`` / plans document
- Optional HTTP routes under ``/v1/router/*`` (registered at proxy startup)

Rules:
- One entry per logical model (``model_name`` / model_group)
- ``metadata.public_protocols`` lists only explicit opt-in protocols
- Missing public_protocols ⇒ model omitted (unavailable everywhere)
- Unknown / disabled protocols never appear
- Presence in discovery does **not** prove endpoint routability
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

from shared_quota_router.feature_flags import (
    is_gateway_enhance_enabled,
    is_vision_compose_enabled,
)
from shared_quota_router.models import (
    ApiProtocol,
    Feature,
    LogicalModelProtocols,
    parse_api_protocol,
    parse_feature_set,
)

_DISCLAIMER = (
    "Presence in this listing indicates public protocol opt-in only. "
    "It does not prove that a matching deployment is currently available, "
    "healthy, or that a given endpoint will accept the request."
)

_FEATURE_ORDER = (
    Feature.TEXT,
    Feature.STREAMING,
    Feature.TOOLS,
    Feature.REASONING,
    Feature.IMAGE,
)


def _ordered_feature_values(features: Iterable[Feature]) -> list[str]:
    seen = set(features)
    out = [f.value for f in _FEATURE_ORDER if f in seen]
    for f in sorted(seen, key=lambda x: x.value):
        if f.value not in out:
            out.append(f.value)
    return out


def _should_omit_compose_facade(*, has_compose: bool) -> bool:
    if not has_compose:
        return False
    return not (is_gateway_enhance_enabled() and is_vision_compose_enabled())


# Stable protocol order for serialisation
_PROTOCOL_ORDER = (
    ApiProtocol.OPENAI_CHAT,
    ApiProtocol.OPENAI_RESPONSES,
    ApiProtocol.ANTHROPIC_MESSAGES,
)


def _ordered_protocol_values(protocols: Iterable[ApiProtocol]) -> list[str]:
    seen = set(protocols)
    out = [p.value for p in _PROTOCOL_ORDER if p in seen]
    for p in sorted(seen, key=lambda x: x.value):
        if p.value not in out:
            out.append(p.value)
    return out


def _parse_public_protocols_field(raw: Any) -> frozenset[ApiProtocol]:
    """Parse model_info.public_protocols; invalid entries are skipped (not all)."""
    if raw is None:
        return frozenset()
    if isinstance(raw, str):
        try:
            return frozenset({parse_api_protocol(raw)})
        except ValueError:
            return frozenset()
    if not isinstance(raw, (list, tuple, set, frozenset)):
        return frozenset()
    out: set[ApiProtocol] = set()
    for item in raw:
        try:
            out.add(parse_api_protocol(item))
        except ValueError:
            # Unknown protocol values must not appear in discovery
            continue
    return frozenset(out)


@dataclass(frozen=True, slots=True)
class ModelCapability:
    """Discovery record for one logical model."""

    model_group: str
    public_protocols: frozenset[ApiProtocol] = frozenset()
    advertised_features: frozenset[Feature] = frozenset()

    def to_openai_style_dict(self) -> dict[str, Any]:
        """OpenAI-ish model object with metadata.public_protocols only."""
        metadata: dict[str, Any] = {
            "public_protocols": _ordered_protocol_values(self.public_protocols),
        }
        if self.advertised_features:
            metadata["features"] = _ordered_feature_values(self.advertised_features)
        return {
            "id": self.model_group,
            "object": "model",
            "metadata": metadata,
        }

    def to_capability_dict(self) -> dict[str, Any]:
        body: dict[str, Any] = {
            "id": self.model_group,
            "object": "model_capability",
            "public_protocols": _ordered_protocol_values(self.public_protocols),
        }
        if self.advertised_features:
            body["features"] = _ordered_feature_values(self.advertised_features)
        return body


@dataclass(slots=True)
class CapabilityCatalog:
    """Aggregated logical-model capabilities (one row per model_group)."""

    models: list[ModelCapability] = field(default_factory=list)
    disclaimer: str = _DISCLAIMER

    def get(self, model_group: str) -> ModelCapability | None:
        for m in self.models:
            if m.model_group == model_group:
                return m
        return None

    def to_list_response(self, *, style: str = "openai") -> dict[str, Any]:
        """
        style:
          - ``openai``: data[].metadata.public_protocols (M1-05 preferred shape)
          - ``capability``: data[].public_protocols top-level
        """
        if style == "capability":
            data = [m.to_capability_dict() for m in self.models]
        else:
            data = [m.to_openai_style_dict() for m in self.models]
        return {
            "object": "list",
            "data": data,
            "disclaimer": self.disclaimer,
        }


def catalog_from_model_list(model_list: Sequence[Mapping[str, Any]]) -> CapabilityCatalog:
    """Build catalog from LiteLLM router ``model_list`` entries.

    Aggregates by ``model_name``. Unions ``model_info.public_protocols`` across
    deployments. Models with no public_protocols after union are omitted.
    Does not invent protocols from provider or model name.
    """
    by_group: dict[str, set[ApiProtocol]] = {}
    by_features: dict[str, set[Feature]] = {}
    compose_groups: set[str] = set()
    order: list[str] = []

    for entry in model_list:
        if not isinstance(entry, Mapping):
            continue
        name = entry.get("model_name") or (entry.get("model_info") or {}).get(
            "model_group"
        )
        if not name:
            continue
        model_group = str(name)
        info = entry.get("model_info") or {}
        if not isinstance(info, Mapping):
            info = {}
        protocols = _parse_public_protocols_field(info.get("public_protocols"))
        if model_group not in by_group:
            by_group[model_group] = set()
            by_features[model_group] = set()
            order.append(model_group)
        by_group[model_group] |= set(protocols)
        try:
            by_features[model_group] |= set(
                parse_feature_set(info.get("advertised_features"))
            )
        except ValueError:
            pass
        if info.get("compose"):
            compose_groups.add(model_group)

    models: list[ModelCapability] = []
    for mg in order:
        if _should_omit_compose_facade(has_compose=mg in compose_groups):
            continue
        protos = frozenset(by_group.get(mg) or ())
        if not protos:
            # No public opt-in ⇒ unavailable everywhere; omit from discovery
            continue
        models.append(
            ModelCapability(
                model_group=mg,
                public_protocols=protos,
                advertised_features=frozenset(by_features.get(mg) or ()),
            )
        )
    return CapabilityCatalog(models=models)


def catalog_from_logical_models(
    logical_models: Mapping[str, LogicalModelProtocols],
) -> CapabilityCatalog:
    """Build catalog from validated plans logical_models map."""
    models: list[ModelCapability] = []
    for mg in sorted(logical_models.keys()):
        lm = logical_models[mg]
        if not lm.public_protocols:
            continue
        if _should_omit_compose_facade(has_compose=lm.compose is not None):
            continue
        models.append(
            ModelCapability(
                model_group=lm.model_group,
                public_protocols=frozenset(lm.public_protocols),
                advertised_features=frozenset(lm.advertised_features),
            )
        )
    return CapabilityCatalog(models=models)


def catalog_from_router(router: Any) -> CapabilityCatalog:
    """Read ``router.model_list`` if present."""
    ml = getattr(router, "model_list", None)
    if ml is None and hasattr(router, "get_model_list"):
        try:
            ml = router.get_model_list()
        except Exception:  # noqa: BLE001
            ml = None
    if not ml:
        return CapabilityCatalog(models=[])
    return catalog_from_model_list(list(ml))


def enrich_openai_models_list(
    base_data: Sequence[Mapping[str, Any]],
    catalog: CapabilityCatalog,
) -> list[dict[str, Any]]:
    """Optionally merge public_protocols into an existing /v1/models data array.

    Only attaches metadata for models present in *catalog*. Never adds protocols
    for models not in the catalog. Does not invent all-protocol support.
    """
    by_id = {m.model_group: m for m in catalog.models}
    out: list[dict[str, Any]] = []
    for item in base_data:
        row = dict(item)
        mid = str(row.get("id") or "")
        cap = by_id.get(mid)
        if cap is not None:
            meta = dict(row.get("metadata") or {}) if isinstance(row.get("metadata"), dict) else {}
            meta["public_protocols"] = _ordered_protocol_values(cap.public_protocols)
            row["metadata"] = meta
        out.append(row)
    return out

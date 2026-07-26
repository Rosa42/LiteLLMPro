"""Deployment registry built from LiteLLM model_list / model_info custom fields."""

from __future__ import annotations

from typing import Any, Iterable

from shared_quota_router.models import (
    ApiProtocol,
    ConversionCapability,
    Deployment,
    Feature,
    FidelityClass,
    parse_api_protocol,
    parse_feature_set,
    parse_fidelity_class,
)


class DeploymentRegistry:
    def __init__(self, deployments: Iterable[Deployment] | None = None) -> None:
        self._by_id: dict[str, Deployment] = {}
        self._by_model_group: dict[str, list[str]] = {}
        self._by_quota_group: dict[str, list[str]] = {}
        if deployments:
            for d in deployments:
                self.add(d)

    def add(self, deployment: Deployment) -> None:
        self._by_id[deployment.deployment_id] = deployment
        self._by_model_group.setdefault(deployment.model_group, []).append(
            deployment.deployment_id
        )
        self._by_quota_group.setdefault(deployment.quota_group_id, []).append(
            deployment.deployment_id
        )

    def get(self, deployment_id: str) -> Deployment | None:
        return self._by_id.get(deployment_id)

    def get_by_model_group(self, model_group: str) -> list[Deployment]:
        ids = self._by_model_group.get(model_group, [])
        return [self._by_id[i] for i in ids if i in self._by_id]

    def get_by_quota_group(self, quota_group_id: str) -> list[Deployment]:
        ids = self._by_quota_group.get(quota_group_id, [])
        return [self._by_id[i] for i in ids if i in self._by_id]

    def all_deployments(self) -> list[Deployment]:
        return list(self._by_id.values())

    def filter_by_protocol(
        self,
        model_group: str,
        protocol: ApiProtocol,
    ) -> list[Deployment]:
        """Enabled deployments in the model group that explicitly support protocol."""
        return [
            d
            for d in self.get_by_model_group(model_group)
            if d.enabled and d.supports_protocol(protocol)
        ]

    def public_protocols_for_model(self, model_group: str) -> frozenset[ApiProtocol]:
        """Union of public_protocols across enabled deployments for a logical model."""
        out: set[ApiProtocol] = set()
        for d in self.get_by_model_group(model_group):
            if d.enabled:
                out |= set(d.public_protocols)
        return frozenset(out)

    def model_opts_into_public(self, model_group: str, protocol: ApiProtocol) -> bool:
        return protocol in self.public_protocols_for_model(model_group)

    def has_verified_upstream(
        self,
        model_group: str,
        protocol: ApiProtocol,
    ) -> bool:
        """Enabled deployment with matching upstream_protocol for the model group."""
        return bool(self.filter_by_protocol(model_group, protocol))

    def any_verified_upstream(self, protocol: ApiProtocol) -> bool:
        """True if any enabled deployment in the registry speaks protocol."""
        return any(
            d.enabled and d.supports_protocol(protocol) for d in self.all_deployments()
        )

    def pick_probe_deployment(
        self,
        quota_group_id: str,
        *,
        preferred_model_groups: list[str] | None = None,
    ) -> Deployment | None:
        """Pick an enabled deployment for recovery probes (not via user Router)."""
        members = [d for d in self.get_by_quota_group(quota_group_id) if d.enabled]
        if not members:
            return None
        if preferred_model_groups:
            for mg in preferred_model_groups:
                for d in members:
                    if d.model_group == mg:
                        return d
        # Stable: lowest priority then deployment_id
        members.sort(key=lambda d: (d.priority, d.deployment_id))
        return members[0]


def _parse_upstream_protocol(info: dict[str, Any]) -> ApiProtocol | None:
    """Parse optional upstream_protocol. Missing → None (not universal).

    Unknown non-empty values raise ValueError (configuration-invalid).
    """
    raw = info.get("upstream_protocol")
    if raw is None or raw == "":
        return None
    return parse_api_protocol(raw)


def _parse_supports_streaming(info: dict[str, Any], features: frozenset[Feature]) -> bool:
    if "supports_streaming" in info:
        return bool(info.get("supports_streaming"))
    return Feature.STREAMING in features


def _parse_public_protocols(info: dict[str, Any]) -> frozenset[ApiProtocol]:
    """Parse model_info.public_protocols. Missing → empty (not universal)."""
    raw = info.get("public_protocols")
    if raw is None:
        return frozenset()
    if isinstance(raw, str):
        return frozenset({parse_api_protocol(raw)})
    if not isinstance(raw, (list, tuple, set, frozenset)):
        raise ValueError(
            f"public_protocols must be a list, got {type(raw).__name__}"
        )
    return frozenset(parse_api_protocol(p) for p in raw)


def _parse_conversions(info: dict[str, Any]) -> tuple[ConversionCapability, ...]:
    """Parse model_info.conversions or nested model_info.protocol.conversions."""
    raw = info.get("conversions")
    if raw is None:
        nested = info.get("protocol")
        if isinstance(nested, dict):
            raw = nested.get("conversions")
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ValueError(f"conversions must be a list, got {type(raw).__name__}")
    out: list[ConversionCapability] = []
    seen: set[tuple[ApiProtocol, ApiProtocol]] = set()
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("each conversion entry must be a mapping")
        source = parse_api_protocol(item.get("from") or item.get("source"))
        target = parse_api_protocol(item.get("to") or item.get("target"))
        direction = (source, target)
        if direction in seen:
            raise ValueError(
                f"duplicate conversion direction {source.value} -> {target.value}"
            )
        seen.add(direction)
        fidelity = parse_fidelity_class(item.get("fidelity", FidelityClass.EQUIVALENT.value))
        streaming = bool(item.get("streaming", False))
        feat_block = item.get("features") if isinstance(item.get("features"), dict) else {}
        req = parse_feature_set(feat_block.get("request") if feat_block else None)
        resp = parse_feature_set(feat_block.get("response") if feat_block else None)
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
                streaming=streaming,
                fidelity=fidelity,
            )
        )
    return tuple(out)


def deployment_from_model_entry(entry: dict[str, Any]) -> Deployment:
    """Map one LiteLLM model_list item to Deployment."""
    info = entry.get("model_info") or {}
    params = entry.get("litellm_params") or {}
    model_group = entry.get("model_name") or info.get("model_group")
    deployment_id = info.get("deployment_id")
    if not deployment_id:
        raise ValueError("model_info.deployment_id is required")
    if not model_group:
        raise ValueError("model_name / model_group is required")

    features = parse_feature_set(info.get("supported_features"))
    upstream_protocol = _parse_upstream_protocol(info)
    supports_streaming = _parse_supports_streaming(info, features)
    public_protocols = _parse_public_protocols(info)
    conversions = _parse_conversions(info)

    upstream = params.get("model") or model_group
    return Deployment(
        deployment_id=str(deployment_id),
        model_group=str(model_group),
        upstream_model=str(upstream),
        provider_id=str(info.get("provider_id") or "unknown"),
        quota_group_id=str(info.get("quota_group_id") or info.get("account_id") or "unknown"),
        priority=int(info.get("priority") or 100),
        weight=int(info.get("weight") or 1),
        enabled=bool(info.get("enabled", True)),
        api_base=params.get("api_base"),
        api_key_env=_api_key_env_name(params.get("api_key")),
        upstream_protocol=upstream_protocol,
        supported_features=features,
        supports_streaming=supports_streaming,
        public_protocols=public_protocols,
        conversions=conversions,
        extra={"account_id": info.get("account_id")},
    )

def registry_from_model_list(model_list: list[dict[str, Any]]) -> DeploymentRegistry:
    reg = DeploymentRegistry()
    for entry in model_list:
        reg.add(deployment_from_model_entry(entry))
    return reg


def _api_key_env_name(api_key_field: Any) -> str | None:
    if not isinstance(api_key_field, str):
        return None
    # LiteLLM style: os.environ/OPENCODE_GO_KEY_A
    prefix = "os.environ/"
    if api_key_field.startswith(prefix):
        return api_key_field[len(prefix) :]
    return None

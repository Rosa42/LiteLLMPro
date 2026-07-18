"""Deployment registry built from LiteLLM model_list / model_info custom fields."""

from __future__ import annotations

from typing import Any, Iterable

from shared_quota_router.models import Deployment


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

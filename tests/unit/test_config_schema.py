"""M1-03: plans.yaml schema validation."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from shared_quota_router.config_schema import (
    ConfigValidationError,
    load_plans_dict,
    load_plans_file,
    public_protocols_for,
)
from shared_quota_router.models import ApiProtocol

_ROOT = Path(__file__).resolve().parents[2]


def _base_plan(**overrides):
    p = {
        "id": "opencode-a",
        "display_name": "OpenCode Go A",
        "provider_id": "opencode-go",
        "priority": 10,
        "base_url_env": "OPENCODE_GO_BASE_URL",
        "api_key_env": "OPENCODE_GO_KEY_A",
        "upstream_protocol": "openai_chat",
        "supported_features": ["text", "streaming", "tools"],
        "supports_streaming": True,
        "models": ["kimi-k3", "glm-5.2"],
    }
    p.update(overrides)
    return p


def test_opencode_and_volc_validate_as_openai_chat() -> None:
    data = {
        "plans": [
            _base_plan(),
            {
                "id": "volc-c",
                "display_name": "Volc C",
                "provider_id": "volcengine",
                "priority": 20,
                "base_url_env": "VOLC_CODING_BASE_URL",
                "api_key_env": "VOLC_CODING_KEY_C",
                "upstream_protocol": "openai_chat",
                "supported_features": ["text", "streaming", "tools"],
                "supports_streaming": True,
                "models": ["glm-5.2", "ark-code-latest"],
            },
        ],
        "logical_models": {
            "kimi-k3": {"public_protocols": ["openai_chat"]},
            "glm-5.2": {"public_protocols": ["openai_chat"]},
            "ark-code-latest": {"public_protocols": ["openai_chat"]},
        },
    }
    doc = load_plans_dict(data)
    assert doc.plans[0].upstream_protocol is ApiProtocol.OPENAI_CHAT
    assert doc.plans[1].upstream_protocol is ApiProtocol.OPENAI_CHAT
    assert all(
        m.model and doc.plans[0].resolved_protocol(m) is ApiProtocol.OPENAI_CHAT
        for m in doc.plans[0].models
    )


def test_newapi_disabled_without_protocol() -> None:
    data = {
        "plans": [
            _base_plan(),
            {
                "id": "newapi-a",
                "display_name": "NewAPI A",
                "provider_id": "newapi",
                "priority": 30,
                "base_url_env": "PLAN_NEWAPI_A_BASE_URL",
                "api_key_env": "PLAN_NEWAPI_A_API_KEY",
                "enabled": False,
                "models": ["claude-opus-4-8"],
            },
        ],
        "logical_models": {
            "kimi-k3": {"public_protocols": ["openai_chat"]},
            "glm-5.2": {"public_protocols": ["openai_chat"]},
        },
    }
    doc = load_plans_dict(data)
    newapi = next(p for p in doc.plans if p.id == "newapi-a")
    assert newapi.upstream_protocol is None
    assert newapi.enabled is False
    assert newapi.resolved_enabled(newapi.models[0]) is False
    # Claude not in logical_models => no public exposure
    assert public_protocols_for(doc, "claude-opus-4-8") == frozenset()


def test_responses_public_opt_in_fails_without_responses_deployment() -> None:
    data = {
        "plans": [_base_plan()],
        "logical_models": {
            "kimi-k3": {"public_protocols": ["openai_chat", "openai_responses"]},
            "glm-5.2": {"public_protocols": ["openai_chat"]},
        },
    }
    with pytest.raises(ConfigValidationError, match="openai_responses"):
        load_plans_dict(data)


def test_logical_model_without_public_protocols_unavailable() -> None:
    data = {
        "plans": [_base_plan()],
        "logical_models": {
            "kimi-k3": {"public_protocols": ["openai_chat"]},
            # glm-5.2 omitted
        },
    }
    doc = load_plans_dict(data)
    assert public_protocols_for(doc, "glm-5.2") == frozenset()
    assert ApiProtocol.OPENAI_CHAT in public_protocols_for(doc, "kimi-k3")


def test_empty_public_protocols_list_rejected() -> None:
    data = {
        "plans": [_base_plan()],
        "logical_models": {
            "kimi-k3": {"public_protocols": []},
        },
    }
    with pytest.raises(ConfigValidationError, match="empty list"):
        load_plans_dict(data)


def test_unknown_protocol_rejected() -> None:
    data = {
        "plans": [_base_plan(upstream_protocol="soap")],
        "logical_models": {},
    }
    with pytest.raises(ConfigValidationError, match="unknown protocol"):
        load_plans_dict(data)


def test_duplicate_plan_ids_rejected() -> None:
    data = {
        "plans": [_base_plan(), _base_plan(id="opencode-a", models=["other"])],
        "logical_models": {},
    }
    with pytest.raises(ConfigValidationError, match="duplicate plan id"):
        load_plans_dict(data)


def test_plan_protocol_does_not_auto_expose_public() -> None:
    """Plan has upstream_protocol but no logical_models => no public opt-in."""
    data = {
        "plans": [_base_plan()],
        "logical_models": {},
    }
    doc = load_plans_dict(data)
    assert public_protocols_for(doc, "kimi-k3") == frozenset()
    # capability still present for deployments
    assert doc.plans[0].upstream_protocol is ApiProtocol.OPENAI_CHAT


def test_secret_values_rejected() -> None:
    data = {
        "plans": [
            _base_plan(api_key_env="sk-thisisarealsecretvalue12345"),
        ],
        "logical_models": {},
    }
    with pytest.raises(ConfigValidationError, match="secret|env var name"):
        load_plans_dict(data)


def test_repo_plans_yaml_loads() -> None:
    path = _ROOT / "config" / "plans.yaml"
    if not path.is_file():
        pytest.skip("config/plans.yaml missing")
    doc = load_plans_file(path)
    opencode = next(p for p in doc.plans if p.id == "opencode-a")
    volc = next(p for p in doc.plans if p.id == "volc-c")
    assert opencode.upstream_protocol is ApiProtocol.OPENAI_CHAT
    assert volc.upstream_protocol is ApiProtocol.OPENAI_CHAT
    newapi = next(p for p in doc.plans if p.id == "newapi-a")
    assert newapi.upstream_protocol is None
    assert newapi.enabled is False
    assert public_protocols_for(doc, "kimi-k3") == frozenset({ApiProtocol.OPENAI_CHAT})
    assert public_protocols_for(doc, "claude-opus-4-8") == frozenset()


def test_repo_plans_example_loads() -> None:
    path = _ROOT / "config" / "plans.example.yaml"
    doc = load_plans_file(path)
    assert any(p.upstream_protocol is ApiProtocol.OPENAI_CHAT for p in doc.plans)

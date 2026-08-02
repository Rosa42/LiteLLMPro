"""M1-03: plans.yaml schema validation."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from shared_quota_router.config_schema import (
    DEFAULT_CHAT_FEATURES,
    ConfigValidationError,
    load_plans_dict,
    load_plans_file,
    public_protocols_for,
)
from shared_quota_router.models import ApiProtocol, Feature

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


def test_default_chat_features_text_only() -> None:
    """P1-CAP：DEFAULT_CHAT_FEATURES 收窄为仅 TEXT，禁止静默打开 stream/tools。"""
    assert DEFAULT_CHAT_FEATURES == frozenset({Feature.TEXT})
    assert Feature.STREAMING not in DEFAULT_CHAT_FEATURES
    assert Feature.TOOLS not in DEFAULT_CHAT_FEATURES


def test_openai_chat_omitted_features_uses_text_default() -> None:
    plan = _base_plan()
    del plan["supported_features"]
    del plan["supports_streaming"]
    doc = load_plans_dict({"plans": [plan], "logical_models": {}})
    assert doc.plans[0].supported_features == frozenset({Feature.TEXT})
    assert doc.plans[0].supports_streaming is False


def test_quota_group_id_defaults_to_plan_id() -> None:
    doc = load_plans_dict({"plans": [_base_plan()], "logical_models": {}})
    assert doc.plans[0].quota_group_id == "opencode-a"


def test_quota_group_id_explicit_accepted() -> None:
    doc = load_plans_dict(
        {
            "plans": [_base_plan(id="opencode-a-msg", quota_group_id="opencode-a")],
            "logical_models": {},
        }
    )
    assert doc.plans[0].id == "opencode-a-msg"
    assert doc.plans[0].quota_group_id == "opencode-a"


def test_quota_group_id_invalid_format_rejected() -> None:
    with pytest.raises(ConfigValidationError, match="quota_group_id"):
        load_plans_dict(
            {
                "plans": [_base_plan(quota_group_id="OpenCode_A")],
                "logical_models": {},
            }
        )


def test_plan_id_invalid_format_rejected() -> None:
    with pytest.raises(ConfigValidationError, match="plan id"):
        load_plans_dict(
            {
                "plans": [_base_plan(id="OpenCode_A")],
                "logical_models": {},
            }
        )


def test_same_api_key_env_requires_same_quota_group_id() -> None:
    data = {
        "plans": [
            _base_plan(id="opencode-a-msg", quota_group_id="opencode-a"),
            _base_plan(
                id="opencode-a-chat",
                quota_group_id="opencode-b",
                models=["kimi-k3"],
            ),
        ],
        "logical_models": {},
    }
    with pytest.raises(ConfigValidationError, match="quota_group_id"):
        load_plans_dict(data)


def test_same_api_key_env_same_quota_group_id_ok() -> None:
    data = {
        "plans": [
            _base_plan(id="opencode-a-msg", quota_group_id="opencode-a", models=["glm-5.2"]),
            _base_plan(
                id="opencode-a-chat",
                quota_group_id="opencode-a",
                models=["kimi-k3"],
            ),
        ],
        "logical_models": {},
    }
    doc = load_plans_dict(data)
    assert {p.quota_group_id for p in doc.plans} == {"opencode-a"}


def test_disabled_plan_skips_same_key_quota_group_check() -> None:
    data = {
        "plans": [
            _base_plan(id="opencode-a-msg", quota_group_id="opencode-a"),
            _base_plan(
                id="opencode-a-legacy",
                enabled=False,
                quota_group_id="other-qg",
                models=["legacy-model"],
            ),
        ],
        "logical_models": {},
    }
    doc = load_plans_dict(data)
    assert doc.plans[0].quota_group_id == "opencode-a"
    assert doc.plans[1].quota_group_id == "other-qg"


def test_anthropic_plan_requires_supported_features() -> None:
    plan = _base_plan(upstream_protocol="anthropic_messages")
    del plan["supported_features"]
    with pytest.raises(ConfigValidationError, match="supported_features"):
        load_plans_dict({"plans": [plan], "logical_models": {}})


def test_conversion_plan_requires_supported_features() -> None:
    plan = _base_plan(
        models=["claude-pilot"],
        conversions=[
            {
                "from": "anthropic_messages",
                "to": "openai_chat",
                "fidelity": "equivalent",
                "streaming": False,
            }
        ],
    )
    del plan["supported_features"]
    with pytest.raises(ConfigValidationError, match="supported_features"):
        load_plans_dict(
            {
                "plans": [plan],
                "logical_models": {
                    "claude-pilot": {
                        "public_protocols": ["openai_chat"],
                        "allow_conversion": True,
                        "conversion_policy": {
                            "allowed": [
                                {"from": "anthropic_messages", "to": "openai_chat"}
                            ]
                        },
                    }
                },
            }
        )


def test_opencode_and_volc_validate_as_anthropic_messages() -> None:
    """当前合法语义：OpenCode/Volc 可为 anthropic_messages（不强制 Chat-only）。"""
    data = {
        "plans": [
            _base_plan(
                id="opencode-a",
                upstream_protocol="anthropic_messages",
                supported_features=["text"],
                supports_streaming=False,
                models=["kimi-k3", "glm-5.2"],
            ),
            {
                "id": "volc-c",
                "display_name": "Volc C",
                "provider_id": "volcengine",
                "priority": 20,
                "base_url_env": "VOLC_CODING_ANTHROPIC_BASE_URL",
                "api_key_env": "VOLC_CODING_KEY_C",
                "upstream_protocol": "anthropic_messages",
                "supported_features": ["text"],
                "supports_streaming": False,
                "models": ["glm-5.2"],
            },
        ],
        "logical_models": {
            "kimi-k3": {"public_protocols": ["anthropic_messages"]},
            "glm-5.2": {"public_protocols": ["anthropic_messages"]},
        },
    }
    doc = load_plans_dict(data)
    assert doc.plans[0].upstream_protocol is ApiProtocol.ANTHROPIC_MESSAGES
    assert doc.plans[1].upstream_protocol is ApiProtocol.ANTHROPIC_MESSAGES
    assert all(
        m.model and doc.plans[0].resolved_protocol(m) is ApiProtocol.ANTHROPIC_MESSAGES
        for m in doc.plans[0].models
    )


def test_openai_chat_plans_still_legal_for_convert_leg() -> None:
    """Chat upstream 仍合法（convert 腿）；不再把公网语义钉死为 Chat-only。"""
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
    opencode = next(p for p in doc.plans if p.id == "opencode-a-msg")
    volc = next(p for p in doc.plans if p.id == "volc-c-msg")
    assert opencode.upstream_protocol is ApiProtocol.ANTHROPIC_MESSAGES
    assert volc.upstream_protocol is ApiProtocol.ANTHROPIC_MESSAGES
    assert opencode.quota_group_id == "opencode-a"
    assert opencode.supported_features  # anthropic 须显式 features
    chat = next(p for p in doc.plans if p.id == "opencode-a-chat")
    assert chat.upstream_protocol is ApiProtocol.OPENAI_CHAT
    chat_model_names = {m.model for m in chat.models}
    assert "kimi-k3" in chat_model_names
    assert "deepseek-v4-flash" not in chat_model_names
    ds_chat = next(p for p in doc.plans if p.id == "deepseek-official-chat")
    ds_msg = next(p for p in doc.plans if p.id == "deepseek-official-msg")
    assert ds_chat.upstream_protocol is ApiProtocol.OPENAI_CHAT
    assert ds_msg.upstream_protocol is ApiProtocol.ANTHROPIC_MESSAGES
    assert ds_chat.quota_group_id == "deepseek-official"
    assert ds_msg.quota_group_id == "deepseek-official"
    ds_models = {m.model for m in ds_chat.models}
    assert ds_models == {"deepseek-v4-flash", "deepseek-v4-pro"}
    newapi = next(p for p in doc.plans if p.id == "newapi-a")
    assert newapi.upstream_protocol is ApiProtocol.ANTHROPIC_MESSAGES
    assert newapi.enabled is True
    assert public_protocols_for(doc, "kimi-k3") == frozenset(
        {ApiProtocol.OPENAI_CHAT, ApiProtocol.ANTHROPIC_MESSAGES}
    )
    assert public_protocols_for(doc, "deepseek-v4-flash") == frozenset(
        {ApiProtocol.OPENAI_CHAT, ApiProtocol.ANTHROPIC_MESSAGES}
    )
    assert public_protocols_for(doc, "deepseek-v4-pro") == frozenset(
        {ApiProtocol.OPENAI_CHAT, ApiProtocol.ANTHROPIC_MESSAGES}
    )
    assert doc.logical_models["deepseek-v4-flash"].allow_conversion is False
    assert doc.logical_models["deepseek-v4-pro"].allow_conversion is False
    assert public_protocols_for(doc, "claude-opus-4-8") == frozenset(
        {ApiProtocol.ANTHROPIC_MESSAGES}
    )


def test_repo_plans_example_loads() -> None:
    path = _ROOT / "config" / "plans.example.yaml"
    doc = load_plans_file(path)
    assert any(p.upstream_protocol is ApiProtocol.OPENAI_CHAT for p in doc.plans)


def test_streaming_flag_without_feature_rejected() -> None:
    plan = _base_plan(
        supported_features=["text"],
        supports_streaming=True,
    )
    with pytest.raises(ConfigValidationError, match="sole source of truth"):
        load_plans_dict({"plans": [plan], "logical_models": {}})


def test_streaming_feature_without_flag_derives_false_compat_field() -> None:
    plan = _base_plan()
    del plan["supports_streaming"]
    doc = load_plans_dict({"plans": [plan], "logical_models": {}})
    assert Feature.STREAMING in doc.plans[0].supported_features
    assert doc.plans[0].supports_streaming is True


def test_model_level_streaming_flag_must_match_features() -> None:
    plan = _base_plan(
        supported_features=["text"],
        supports_streaming=False,
        models=[
            {
                "model": "kimi-k3",
                "supported_features": ["text"],
                "supports_streaming": True,
            }
        ],
    )
    with pytest.raises(ConfigValidationError, match="sole source of truth"):
        load_plans_dict({"plans": [plan], "logical_models": {}})


def test_reject_conversion_while_allow_conversion_false() -> None:
    data = {
        "plans": [_base_plan(models=["claude-pilot"])],
        "logical_models": {
            "claude-pilot": {
                "public_protocols": ["anthropic_messages", "openai_chat"],
                "allow_conversion": False,
                "conversion_policy": {
                    "allowed": [
                        {
                            "from": "anthropic_messages",
                            "to": "openai_chat",
                            "fidelity": "equivalent",
                        }
                    ]
                },
            }
        },
    }
    with pytest.raises(ConfigValidationError, match="allow_conversion is false"):
        load_plans_dict(data)


def test_reject_duplicate_conversion_directions_on_plan() -> None:
    conv = {
        "from": "anthropic_messages",
        "to": "openai_chat",
        "fidelity": "equivalent",
        "streaming": False,
        "features": {"request": ["text"], "response": ["text"]},
    }
    data = {
        "plans": [
            _base_plan(
                models=[
                    {
                        "model": "claude-pilot",
                        "conversions": [conv, dict(conv)],
                    }
                ]
            )
        ],
        "logical_models": {
            "claude-pilot": {
                "public_protocols": ["openai_chat"],
                "allow_conversion": True,
                "conversion_policy": {
                    "allowed": [{"from": "anthropic_messages", "to": "openai_chat"}]
                },
            }
        },
    }
    with pytest.raises(ConfigValidationError, match="duplicate conversion"):
        load_plans_dict(data)


def test_accept_explicit_conversion_allowlist() -> None:
    data = {
        "plans": [
            _base_plan(
                models=[
                    {
                        "model": "claude-pilot",
                        "conversions": [
                            {
                                "from": "anthropic_messages",
                                "to": "openai_chat",
                                "fidelity": "equivalent",
                                "streaming": False,
                                "features": {
                                    "request": ["text"],
                                    "response": ["text"],
                                },
                            }
                        ],
                    }
                ]
            )
        ],
        "logical_models": {
            "claude-pilot": {
                "public_protocols": ["anthropic_messages", "openai_chat"],
                "allow_conversion": True,
                "conversion_policy": {
                    "allowed": [
                        {
                            "from": "anthropic_messages",
                            "to": "openai_chat",
                            "fidelity": "equivalent",
                        }
                    ]
                },
            }
        },
    }
    doc = load_plans_dict(data)
    lm = doc.logical_models["claude-pilot"]
    assert lm.allow_conversion is True
    assert (
        ApiProtocol.ANTHROPIC_MESSAGES,
        ApiProtocol.OPENAI_CHAT,
    ) in lm.allowed_conversions
    caps = doc.plans[0].resolved_conversions(doc.plans[0].models[0])
    assert len(caps) == 1
    assert caps[0].source is ApiProtocol.ANTHROPIC_MESSAGES
    assert caps[0].streaming is False


def test_reject_streaming_true_on_conversion() -> None:
    data = {
        "plans": [
            _base_plan(
                models=[
                    {
                        "model": "claude-pilot",
                        "conversions": [
                            {
                                "from": "anthropic_messages",
                                "to": "openai_chat",
                                "fidelity": "equivalent",
                                "streaming": True,
                            }
                        ],
                    }
                ]
            )
        ],
        "logical_models": {
            "claude-pilot": {
                "public_protocols": ["openai_chat"],
                "allow_conversion": True,
                "conversion_policy": {
                    "allowed": [{"from": "anthropic_messages", "to": "openai_chat"}]
                },
            }
        },
    }
    with pytest.raises(ConfigValidationError, match="streaming"):
        load_plans_dict(data)

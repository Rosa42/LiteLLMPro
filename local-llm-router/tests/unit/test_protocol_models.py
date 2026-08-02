"""M1-01 / M1-02: protocol enums, features, and registry capability parsing."""

from __future__ import annotations

import pytest

from shared_quota_router.models import (
    ApiProtocol,
    Deployment,
    Feature,
    LogicalModelProtocols,
    parse_api_protocol,
    parse_feature,
    parse_feature_set,
)
from shared_quota_router.registry import (
    DeploymentRegistry,
    deployment_from_model_entry,
)


def test_api_protocol_values_are_stable_strings() -> None:
    assert ApiProtocol.OPENAI_CHAT.value == "openai_chat"
    assert ApiProtocol.OPENAI_RESPONSES.value == "openai_responses"
    assert ApiProtocol.ANTHROPIC_MESSAGES.value == "anthropic_messages"
    assert ApiProtocol.OPENAI_CHAT == "openai_chat"
    assert list(ApiProtocol) == [
        ApiProtocol.OPENAI_CHAT,
        ApiProtocol.OPENAI_RESPONSES,
        ApiProtocol.ANTHROPIC_MESSAGES,
    ]


def test_feature_values_are_stable_strings() -> None:
    assert Feature.TEXT.value == "text"
    assert Feature.STREAMING.value == "streaming"
    assert Feature.TOOLS.value == "tools"


def test_unknown_protocol_fails_validation() -> None:
    with pytest.raises(ValueError, match="unknown protocol"):
        parse_api_protocol("graphql")
    with pytest.raises(ValueError, match="invalid protocol"):
        parse_api_protocol("")
    with pytest.raises(ValueError, match="invalid protocol"):
        parse_api_protocol(None)


def test_parse_api_protocol_accepts_enum_and_casefold() -> None:
    assert parse_api_protocol("OpenAI_Chat") is ApiProtocol.OPENAI_CHAT
    assert parse_api_protocol(ApiProtocol.ANTHROPIC_MESSAGES) is ApiProtocol.ANTHROPIC_MESSAGES


def test_unknown_feature_fails_validation() -> None:
    with pytest.raises(ValueError, match="unknown feature"):
        parse_feature("not_a_real_feature")
    with pytest.raises(ValueError, match="invalid feature"):
        parse_feature("")


def test_post_mvp_features_parse() -> None:
    assert parse_feature("reasoning") is Feature.REASONING
    assert Feature.PROMPT_CACHE in parse_feature_set(["prompt_cache", "text"])

def test_missing_protocol_does_not_imply_universal_support() -> None:
    d = Deployment(
        deployment_id="d1",
        model_group="m",
        upstream_model="openai/m",
        provider_id="p",
        quota_group_id="q",
    )
    assert d.upstream_protocol is None
    assert d.supports_protocol(ApiProtocol.OPENAI_CHAT) is False
    assert d.supports_protocol(ApiProtocol.ANTHROPIC_MESSAGES) is False
    assert d.supported_features == frozenset()
    assert d.supports_streaming is False


def test_logical_model_missing_public_protocols_unavailable() -> None:
    lm = LogicalModelProtocols.from_config("kimi-k3", None)
    assert lm.public_protocols == frozenset()
    assert lm.supports(ApiProtocol.OPENAI_CHAT) is False

    lm2 = LogicalModelProtocols.from_config("kimi-k3", [])
    assert lm2.supports(ApiProtocol.OPENAI_CHAT) is False


def test_logical_model_public_protocols_opt_in() -> None:
    lm = LogicalModelProtocols.from_config(
        "kimi-k3",
        ["openai_chat"],
    )
    assert lm.supports(ApiProtocol.OPENAI_CHAT) is True
    assert lm.supports(ApiProtocol.OPENAI_RESPONSES) is False


def test_logical_model_unknown_protocol_rejected() -> None:
    with pytest.raises(ValueError, match="unknown protocol"):
        LogicalModelProtocols.from_config("m", ["openai_chat", "soap"])


def test_parse_feature_set_empty_on_none() -> None:
    assert parse_feature_set(None) == frozenset()
    assert parse_feature_set([]) == frozenset()
    assert parse_feature_set(["text", "tools"]) == frozenset(
        {Feature.TEXT, Feature.TOOLS}
    )


# --- M1-02 registry ---


def test_registry_parses_explicit_protocol_and_features() -> None:
    entry = {
        "model_name": "kimi-k3",
        "litellm_params": {
            "model": "openai/kimi-k3",
            "api_base": "https://example.invalid",
            "api_key": "os.environ/KEY",
        },
        "model_info": {
            "deployment_id": "opencode-a-kimi",
            "provider_id": "opencode-go",
            "quota_group_id": "opencode-a",
            "priority": 10,
            "upstream_protocol": "openai_chat",
            "supported_features": ["text", "streaming", "tools"],
            "supports_streaming": True,
        },
    }
    d = deployment_from_model_entry(entry)
    assert d.upstream_protocol is ApiProtocol.OPENAI_CHAT
    assert d.supported_features == frozenset(
        {Feature.TEXT, Feature.STREAMING, Feature.TOOLS}
    )
    assert d.supports_streaming is True
    assert d.supports_feature(Feature.TOOLS) is True
    assert d.model_group == "kimi-k3"
    assert d.quota_group_id == "opencode-a"


def test_registry_missing_protocol_is_not_universal() -> None:
    entry = {
        "model_name": "legacy",
        "litellm_params": {"model": "openai/legacy"},
        "model_info": {
            "deployment_id": "legacy-1",
            "provider_id": "p",
            "quota_group_id": "q",
        },
    }
    d = deployment_from_model_entry(entry)
    assert d.upstream_protocol is None
    assert d.supports_protocol(ApiProtocol.OPENAI_CHAT) is False
    assert d.supported_features == frozenset()
    assert d.supports_streaming is False


def test_registry_unknown_protocol_rejected() -> None:
    entry = {
        "model_name": "m",
        "litellm_params": {"model": "openai/m"},
        "model_info": {
            "deployment_id": "d",
            "upstream_protocol": "not-a-protocol",
        },
    }
    with pytest.raises(ValueError, match="unknown protocol"):
        deployment_from_model_entry(entry)


def test_one_model_group_may_have_mixed_upstream_protocols() -> None:
    reg = DeploymentRegistry()
    reg.add(
        deployment_from_model_entry(
            {
                "model_name": "shared-model",
                "litellm_params": {"model": "openai/shared"},
                "model_info": {
                    "deployment_id": "chat-dep",
                    "provider_id": "opencode-go",
                    "quota_group_id": "a",
                    "priority": 10,
                    "upstream_protocol": "openai_chat",
                    "supported_features": ["text", "streaming"],
                    "supports_streaming": True,
                },
            }
        )
    )
    reg.add(
        deployment_from_model_entry(
            {
                "model_name": "shared-model",
                "litellm_params": {"model": "anthropic/shared"},
                "model_info": {
                    "deployment_id": "msg-dep",
                    "provider_id": "newapi",
                    "quota_group_id": "b",
                    "priority": 20,
                    "upstream_protocol": "anthropic_messages",
                    "supported_features": ["text", "streaming"],
                    "supports_streaming": True,
                },
            }
        )
    )
    group = reg.get_by_model_group("shared-model")
    assert len(group) == 2
    protocols = {d.upstream_protocol for d in group}
    assert protocols == {ApiProtocol.OPENAI_CHAT, ApiProtocol.ANTHROPIC_MESSAGES}

    chat_only = reg.filter_by_protocol("shared-model", ApiProtocol.OPENAI_CHAT)
    assert [d.deployment_id for d in chat_only] == ["chat-dep"]

    msg_only = reg.filter_by_protocol("shared-model", ApiProtocol.ANTHROPIC_MESSAGES)
    assert [d.deployment_id for d in msg_only] == ["msg-dep"]


def test_supports_streaming_inferred_from_features_when_flag_absent() -> None:
    d = deployment_from_model_entry(
        {
            "model_name": "m",
            "litellm_params": {"model": "openai/m"},
            "model_info": {
                "deployment_id": "d",
                "upstream_protocol": "openai_chat",
                "supported_features": ["text", "streaming"],
            },
        }
    )
    assert d.supports_streaming is True
    assert d.supports_feature(Feature.STREAMING) is True


def test_supports_feature_streaming_ignores_compat_flag_without_feature() -> None:
    """P0-SOT: runtime only checks supported_features, not supports_streaming OR."""
    d = Deployment(
        deployment_id="d1",
        model_group="m",
        upstream_model="openai/m",
        provider_id="p",
        quota_group_id="q",
        upstream_protocol=ApiProtocol.OPENAI_CHAT,
        supported_features=frozenset({Feature.TEXT}),
        supports_streaming=True,
    )
    assert d.supports_streaming is True
    assert d.supports_feature(Feature.STREAMING) is False

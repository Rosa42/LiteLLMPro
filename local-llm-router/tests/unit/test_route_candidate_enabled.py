"""route_candidate_enabled / readiness — 禁止退化为全局布尔。"""

from __future__ import annotations

import pytest

from shared_quota_router.feature_flags import clear_flag_cache, set_g0a_messages_mount_ready
from shared_quota_router.models import (
    ApiProtocol,
    ConversionCapability,
    Deployment,
    Feature,
    FidelityClass,
    RouteCandidate,
    RouteMode,
    TransformOwner,
)
from shared_quota_router.route_readiness import (
    litellm_native_responses_to_chat_enabled,
    readiness,
    route_candidate_enabled,
)


@pytest.fixture(autouse=True)
def _reset(monkeypatch: pytest.MonkeyPatch) -> None:
    set_g0a_messages_mount_ready(False)
    monkeypatch.delenv("SHARED_QUOTA_ENV_PROFILE", raising=False)
    monkeypatch.delenv("SHARED_QUOTA_PRODUCTION_APPROVED_DIRECTIONS", raising=False)
    monkeypatch.delenv("PROTOCOL_AWARE_GATEWAY_ENABLED", raising=False)
    monkeypatch.delenv("PROTOCOL_CONVERSION_ENABLED", raising=False)
    monkeypatch.delenv(
        "LITELLM_USE_CHAT_COMPLETIONS_URL_FOR_ANTHROPIC_MESSAGES", raising=False
    )
    try:
        import litellm

        monkeypatch.setattr(
            litellm, "use_chat_completions_url_for_anthropic_messages", False
        )
    except ImportError:
        pass
    clear_flag_cache()
    yield
    set_g0a_messages_mount_ready(False)
    clear_flag_cache()


def _dep() -> Deployment:
    return Deployment(
        deployment_id="chat-1",
        model_group="glm-5.2",
        upstream_model="openai/glm-5.2",
        provider_id="p",
        quota_group_id="q1",
        priority=10,
        enabled=True,
        upstream_protocol=ApiProtocol.OPENAI_CHAT,
        supported_features=frozenset({Feature.TEXT, Feature.STREAMING}),
        supports_streaming=True,
        public_protocols=frozenset({ApiProtocol.OPENAI_RESPONSES}),
    )


def test_messages_native_flag_does_not_enable_responses_project_adapter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PROTOCOL_AWARE_GATEWAY_ENABLED", "true")
    monkeypatch.setenv("PROTOCOL_CONVERSION_ENABLED", "true")
    monkeypatch.setenv("SHARED_QUOTA_ENV_PROFILE", "staging")
    try:
        import litellm

        monkeypatch.setattr(
            litellm, "use_chat_completions_url_for_anthropic_messages", True
        )
    except ImportError:
        monkeypatch.setenv(
            "LITELLM_USE_CHAT_COMPLETIONS_URL_FOR_ANTHROPIC_MESSAGES", "true"
        )
    clear_flag_cache()

    assert (
        readiness(
            ApiProtocol.OPENAI_RESPONSES,
            ApiProtocol.OPENAI_CHAT,
            TransformOwner.PROJECT_ADAPTER,
        )
        is False
    )
    assert (
        readiness(
            ApiProtocol.OPENAI_RESPONSES,
            ApiProtocol.OPENAI_CHAT,
            TransformOwner.LITELLM_NATIVE,
        )
        is True
    )


def test_responses_chat_native_enabled_in_staging(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PROTOCOL_AWARE_GATEWAY_ENABLED", "true")
    monkeypatch.setenv("PROTOCOL_CONVERSION_ENABLED", "true")
    monkeypatch.setenv("SHARED_QUOTA_ENV_PROFILE", "staging")
    clear_flag_cache()
    assert litellm_native_responses_to_chat_enabled() is True

    cap = ConversionCapability(
        source=ApiProtocol.OPENAI_RESPONSES,
        target=ApiProtocol.OPENAI_CHAT,
        request_features=frozenset({Feature.TEXT}),
        response_features=frozenset({Feature.TEXT}),
        streaming=True,
        fidelity=FidelityClass.LOSSY_SAFE,
    )
    route = RouteCandidate(
        deployment=_dep(),
        route_mode=RouteMode.CONVERT,
        conversion=cap,
        transform_owner=TransformOwner.LITELLM_NATIVE,
    )
    assert route_candidate_enabled(route) is True


def test_responses_chat_native_blocked_in_production_without_approve(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PROTOCOL_AWARE_GATEWAY_ENABLED", "true")
    monkeypatch.setenv("PROTOCOL_CONVERSION_ENABLED", "true")
    monkeypatch.setenv("SHARED_QUOTA_ENV_PROFILE", "production")
    clear_flag_cache()
    assert litellm_native_responses_to_chat_enabled() is False

    monkeypatch.setenv(
        "SHARED_QUOTA_PRODUCTION_APPROVED_DIRECTIONS",
        "openai_responses>openai_chat:litellm_native",
    )
    clear_flag_cache()
    assert litellm_native_responses_to_chat_enabled() is True


def test_p0_g0a_project_adapter_messages_chat_always_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """本期 PROJECT_ADAPTER Messages→Chat readiness 恒 False（即使 g0a mount）。"""
    monkeypatch.setenv("PROTOCOL_AWARE_GATEWAY_ENABLED", "true")
    monkeypatch.setenv("PROTOCOL_CONVERSION_ENABLED", "true")
    set_g0a_messages_mount_ready(True)
    clear_flag_cache()
    assert (
        readiness(
            ApiProtocol.ANTHROPIC_MESSAGES,
            ApiProtocol.OPENAI_CHAT,
            TransformOwner.PROJECT_ADAPTER,
        )
        is False
    )


def test_p0_g0a_l2_native_off_resolve_no_adapter_candidate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """L2/A10：native off ⇒ resolve_route convert 候选为 0，且无 PROJECT_ADAPTER。"""
    from shared_quota_router.conversion.registry import resolve_route
    from shared_quota_router.models import LogicalModelProtocols

    set_g0a_messages_mount_ready(True)
    clear_flag_cache()

    cap = ConversionCapability(
        source=ApiProtocol.ANTHROPIC_MESSAGES,
        target=ApiProtocol.OPENAI_CHAT,
        request_features=frozenset({Feature.TEXT}),
        response_features=frozenset({Feature.TEXT}),
        streaming=False,
        fidelity=FidelityClass.EQUIVALENT,
    )
    dep = Deployment(
        deployment_id="chat-convert",
        model_group="kimi-k3",
        upstream_model="openai/kimi-k3",
        provider_id="p",
        quota_group_id="q1",
        priority=10,
        enabled=True,
        upstream_protocol=ApiProtocol.OPENAI_CHAT,
        supported_features=frozenset({Feature.TEXT}),
        supports_streaming=False,
        public_protocols=frozenset({ApiProtocol.OPENAI_CHAT}),
        conversions=(cap,),
    )
    logical = LogicalModelProtocols(
        model_group="kimi-k3",
        public_protocols=frozenset({ApiProtocol.ANTHROPIC_MESSAGES}),
        allow_conversion=True,
        allowed_conversions=frozenset(
            {(ApiProtocol.ANTHROPIC_MESSAGES, ApiProtocol.OPENAI_CHAT)}
        ),
    )
    route = resolve_route(
        dep,
        public_protocol=ApiProtocol.ANTHROPIC_MESSAGES,
        required_features=frozenset({Feature.TEXT}),
        stream=False,
        logical=logical,
        conversion_enabled=True,
    )
    assert route is None

"""F2: compose recipe on logical_models (glm-5.2-vision facade)."""

from __future__ import annotations

import pytest

from shared_quota_router.composed_vision import defers_image_gate, is_vision_compose
from shared_quota_router.config_schema import ConfigValidationError, load_plans_dict
from shared_quota_router.feature_flags import clear_flag_cache
from shared_quota_router.generator import render_litellm_yaml
from shared_quota_router.models import Feature


def _msg_plan(
    *,
    plan_id: str,
    quota_group_id: str,
    provider_id: str,
    base_url_env: str,
    api_key_env: str,
    models: list,
    features: list[str] | None = None,
) -> dict:
    return {
        "id": plan_id,
        "display_name": plan_id,
        "provider_id": provider_id,
        "priority": 10,
        "quota_group_id": quota_group_id,
        "base_url_env": base_url_env,
        "api_key_env": api_key_env,
        "upstream_protocol": "anthropic_messages",
        "supported_features": features
        or ["text", "streaming", "tools", "reasoning"],
        "supports_streaming": True,
        "models": models,
    }


def _valid_compose_doc(**lm_extra) -> dict:
    vision_lm = {
        "public_protocols": ["anthropic_messages"],
        "advertised_features": ["text", "streaming", "tools", "reasoning", "image"],
        "compose": {
            "execute_model": "glm-5.2",
            "translate_model": "MiniMax-M3",
        },
    }
    vision_lm.update(lm_extra)
    return {
        "plans": [
            _msg_plan(
                plan_id="volc-c",
                quota_group_id="volc-c",
                provider_id="volcengine",
                base_url_env="VOLC_CODING_ANTHROPIC_BASE_URL",
                api_key_env="VOLC_CODING_KEY_C",
                models=["glm-5.2", "glm-5.2-vision"],
            ),
            _msg_plan(
                plan_id="minimax-official",
                quota_group_id="minimax-official",
                provider_id="minimax",
                base_url_env="MINIMAX_ANTHROPIC_BASE_URL",
                api_key_env="MINIMAX_API_KEY",
                models=[
                    {
                        "model": "MiniMax-M3",
                        "supported_features": [
                            "text",
                            "streaming",
                            "tools",
                            "reasoning",
                            "image",
                        ],
                    }
                ],
            ),
        ],
        "logical_models": {
            "glm-5.2": {"public_protocols": ["anthropic_messages"]},
            "glm-5.2-vision": vision_lm,
            "MiniMax-M3": {"public_protocols": ["anthropic_messages"]},
        },
    }


def test_compose_recipe_parses_and_defers_image_gate() -> None:
    doc = load_plans_dict(_valid_compose_doc())
    lm = doc.logical_models["glm-5.2-vision"]
    assert lm.compose is not None
    assert lm.compose.execute_model == "glm-5.2"
    assert lm.compose.translate_model == "MiniMax-M3"
    assert Feature.IMAGE in lm.advertised_features
    assert defers_image_gate("glm-5.2-vision", lm) is True
    glm = doc.logical_models["glm-5.2"]
    assert glm.compose is None
    assert defers_image_gate("glm-5.2", glm) is False


def test_compose_same_translate_and_execute_rejected() -> None:
    data = _valid_compose_doc()
    data["logical_models"]["glm-5.2-vision"]["compose"] = {
        "execute_model": "glm-5.2",
        "translate_model": "glm-5.2",
    }
    with pytest.raises(ConfigValidationError, match="must differ"):
        load_plans_dict(data)


def test_compose_same_quota_group_rejected() -> None:
    data = _valid_compose_doc()
    data["plans"][0]["models"].append(
        {
            "model": "MiniMax-M3",
            "supported_features": [
                "text",
                "streaming",
                "tools",
                "reasoning",
                "image",
            ],
        }
    )
    data["plans"][1]["enabled"] = False
    with pytest.raises(ConfigValidationError, match="quota_group_id"):
        load_plans_dict(data)


def test_compose_facade_must_not_have_image_feature() -> None:
    data = _valid_compose_doc()
    data["plans"][0]["models"] = [
        "glm-5.2",
        {
            "model": "glm-5.2-vision",
            "supported_features": [
                "text",
                "streaming",
                "tools",
                "reasoning",
                "image",
            ],
        },
    ]
    with pytest.raises(ConfigValidationError, match="must not declare image"):
        load_plans_dict(data)


def test_generator_emits_compose_section() -> None:
    doc = load_plans_dict(_valid_compose_doc())
    text = render_litellm_yaml(doc)
    assert "glm-5.2-vision:" in text
    assert "execute_model: glm-5.2" in text
    assert "translate_model: MiniMax-M3" in text
    assert "advertised_features:" in text
    assert "timeout: 480" in text


def test_generator_sends_execute_model_to_volc_not_facade_name() -> None:
    """Facade model_name stays glm-5.2-vision; Volc upstream must be glm-5.2."""
    doc = load_plans_dict(_valid_compose_doc())
    text = render_litellm_yaml(doc)
    assert "model_name: glm-5.2-vision" in text
    assert "anthropic/glm-5.2-vision" not in text
    assert "model: anthropic/glm-5.2" in text


def test_discovery_omits_compose_when_vision_flag_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from shared_quota_router.discovery import catalog_from_logical_models

    monkeypatch.delenv("VISION_COMPOSE_ENABLED", raising=False)
    clear_flag_cache()
    doc = load_plans_dict(_valid_compose_doc())
    cat = catalog_from_logical_models(doc.logical_models)
    ids = {m.model_group for m in cat.models}
    assert "glm-5.2" in ids
    assert "glm-5.2-vision" not in ids


def test_discovery_omits_compose_when_enhance_flag_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from shared_quota_router.discovery import catalog_from_logical_models

    monkeypatch.delenv("GATEWAY_ENHANCE_ENABLED", raising=False)
    monkeypatch.setenv("VISION_COMPOSE_ENABLED", "true")
    clear_flag_cache()
    doc = load_plans_dict(_valid_compose_doc())
    cat = catalog_from_logical_models(doc.logical_models)
    ids = {m.model_group for m in cat.models}
    assert "glm-5.2" in ids
    assert "glm-5.2-vision" not in ids


def test_discovery_lists_compose_features_when_vision_flag_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from shared_quota_router.discovery import catalog_from_logical_models

    monkeypatch.setenv("GATEWAY_ENHANCE_ENABLED", "true")
    monkeypatch.setenv("VISION_COMPOSE_ENABLED", "true")
    clear_flag_cache()
    doc = load_plans_dict(_valid_compose_doc())
    cat = catalog_from_logical_models(doc.logical_models)
    ids = {m.model_group for m in cat.models}
    assert "glm-5.2-vision" in ids
    cap = cat.get("glm-5.2-vision")
    assert cap is not None
    assert Feature.IMAGE in cap.advertised_features
    glm = cat.get("glm-5.2")
    assert glm is not None
    assert Feature.IMAGE not in glm.advertised_features
    body = cat.to_list_response()
    vision_row = next(r for r in body["data"] if r["id"] == "glm-5.2-vision")
    assert "image" in vision_row["metadata"]["features"]
    glm_row = next(r for r in body["data"] if r["id"] == "glm-5.2")
    assert "features" not in glm_row["metadata"]


def test_compose_template_defaults_to_vision() -> None:
    doc = load_plans_dict(_valid_compose_doc())
    lm = doc.logical_models["glm-5.2-vision"]
    assert lm.compose is not None
    assert lm.compose.template == "vision"
    assert is_vision_compose(lm) is True


def test_compose_template_rethink_rejected() -> None:
    data = _valid_compose_doc()
    data["logical_models"]["glm-5.2-vision"]["compose"]["template"] = "rethink"
    with pytest.raises(ConfigValidationError, match="template"):
        load_plans_dict(data)


def test_compose_unknown_key_rejected() -> None:
    data = _valid_compose_doc()
    data["logical_models"]["glm-5.2-vision"]["compose"]["fallback"] = "glm-5.2"
    with pytest.raises(ConfigValidationError, match="fallback"):
        load_plans_dict(data)


def test_compose_aliases_reasoning_and_vision() -> None:
    data = _valid_compose_doc()
    data["logical_models"]["glm-5.2-vision"]["compose"] = {
        "reasoning": "glm-5.2",
        "vision": "MiniMax-M3",
    }
    doc = load_plans_dict(data)
    recipe = doc.logical_models["glm-5.2-vision"].compose
    assert recipe is not None
    assert recipe.execute_model == "glm-5.2"
    assert recipe.translate_model == "MiniMax-M3"
    assert recipe.template == "vision"


def test_translate_without_image_feature_rejected() -> None:
    data = _valid_compose_doc()
    data["plans"][1]["models"] = [
        {
            "model": "MiniMax-M2.7",
            "supported_features": ["text", "streaming", "tools", "reasoning"],
        }
    ]
    data["logical_models"]["glm-5.2-vision"]["compose"]["translate_model"] = "MiniMax-M2.7"
    data["logical_models"]["MiniMax-M2.7"] = {"public_protocols": ["anthropic_messages"]}
    del data["logical_models"]["MiniMax-M3"]
    with pytest.raises(ConfigValidationError, match="image"):
        load_plans_dict(data)


def test_facade_row_on_translator_plan_rejected() -> None:
    data = _valid_compose_doc()
    data["plans"][1]["models"].append("glm-5.2-vision")
    with pytest.raises(ConfigValidationError, match="lineage"):
        load_plans_dict(data)


def test_nested_execute_facade_rejected() -> None:
    data = _valid_compose_doc()
    data["logical_models"]["glm-5.2-vision"]["compose"]["execute_model"] = "glm-5.2-vision"
    with pytest.raises(ConfigValidationError, match="compose"):
        load_plans_dict(data)


def test_execute_glm53_with_minimax_translator_ok() -> None:
    data = _valid_compose_doc()
    data["plans"][0]["models"] = ["glm-5.2", "glm-5.3", "glm-5.2-vision"]
    data["logical_models"]["glm-5.3"] = {"public_protocols": ["anthropic_messages"]}
    data["logical_models"]["glm-5.2-vision"]["compose"]["execute_model"] = "glm-5.3"
    doc = load_plans_dict(data)
    recipe = doc.logical_models["glm-5.2-vision"].compose
    assert recipe is not None
    assert recipe.execute_model == "glm-5.3"
    assert recipe.translate_model == "MiniMax-M3"

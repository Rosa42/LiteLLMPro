"""Host mutator for configurable vision facades."""

from __future__ import annotations

from pathlib import Path

import pytest

from shared_quota_router.compose_mutator import (
    compose_vision_add,
    compose_vision_remove,
    compose_vision_update,
    list_vision_slot_options,
)
from shared_quota_router.config_schema import ConfigValidationError, load_plans_dict
from tests.unit.test_compose_recipe_config import _valid_compose_doc


def _inventory() -> dict:
    data = _valid_compose_doc()
    data["plans"][0]["models"] = ["glm-5.2", "glm-5.3", "glm-5.2-vision"]
    data["logical_models"]["glm-5.3"] = {"public_protocols": ["anthropic_messages"]}
    return data


def test_slot_options_exclude_m27_without_image() -> None:
    data = _inventory()
    data["plans"][1]["models"].append(
        {
            "model": "MiniMax-M2.7",
            "supported_features": ["text", "streaming", "tools", "reasoning"],
        }
    )
    doc = load_plans_dict(data)
    slots = list_vision_slot_options(doc, execute_model="glm-5.3")
    assert "glm-5.3" in slots["execute"]
    assert "MiniMax-M3" in slots["translate"]
    assert "MiniMax-M2.7" not in slots["translate"]
    assert "glm-5.2-vision" not in slots["execute"]


def test_add_facade_execute_glm53_translate_minimax() -> None:
    data = _inventory()
    # Start without extra facade; inventory already has glm-5.2-vision.
    out = compose_vision_add(
        data,
        facade_id="lab-vision",
        execute_model="glm-5.3",
        translate_model="MiniMax-M3",
    )
    doc = load_plans_dict(out)
    recipe = doc.logical_models["lab-vision"].compose
    assert recipe is not None
    assert recipe.execute_model == "glm-5.3"
    assert recipe.translate_model == "MiniMax-M3"
    volc = next(p for p in doc.plans if p.id == "volc-c")
    assert any(m.model == "lab-vision" and m.facade_role == "vision" for m in volc.models)
    minimax = next(p for p in doc.plans if p.id == "minimax-official")
    assert all(m.model != "lab-vision" for m in minimax.models)


def test_update_preset_execute_to_glm53() -> None:
    data = _inventory()
    out = compose_vision_update(
        data,
        facade_id="glm-5.2-vision",
        execute_model="glm-5.3",
        translate_model="MiniMax-M3",
        force_preset=True,
    )
    doc = load_plans_dict(out)
    recipe = doc.logical_models["glm-5.2-vision"].compose
    assert recipe is not None
    assert recipe.execute_model == "glm-5.3"
    text = __import__(
        "shared_quota_router.generator", fromlist=["render_litellm_yaml"]
    ).render_litellm_yaml(doc)
    assert "execute_model: glm-5.3" in text
    assert "translate_model: MiniMax-M3" in text
    assert "model: anthropic/glm-5.3" in text
    assert "MINIMAX_ANTHROPIC_BASE_URL" not in text.split("model_name: glm-5.2-vision")[1].split("model_name:")[0]


def test_remove_plain_model_rejected() -> None:
    data = _inventory()
    with pytest.raises(ConfigValidationError, match="not a vision facade"):
        compose_vision_remove(data, facade_id="glm-5.2")


def test_add_onto_existing_logical_without_compose_rejected() -> None:
    data = _inventory()
    with pytest.raises(ConfigValidationError, match="already exists"):
        compose_vision_add(
            data,
            facade_id="glm-5.3",
            execute_model="glm-5.2",
            translate_model="MiniMax-M3",
        )


def test_persist_rolls_back_plans_when_apply_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import yaml
    from shared_quota_router.compose_mutator import persist_plans_and_apply

    data = _inventory()
    plans = tmp_path / "plans.yaml"
    litellm = tmp_path / "litellm.yaml"
    plans.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    original = plans.read_bytes()
    mutated = compose_vision_add(
        data,
        facade_id="lab-vision",
        execute_model="glm-5.3",
        translate_model="MiniMax-M3",
    )

    def boom(*_a: object, **_k: object) -> dict:
        raise RuntimeError("apply exploded")

    monkeypatch.setattr(
        "shared_quota_router.compose_mutator.apply_plans_to_litellm", boom
    )
    with pytest.raises(RuntimeError, match="apply exploded"):
        persist_plans_and_apply(
            plans,
            mutated,
            litellm_path=litellm,
            backup_dir=tmp_path / "backups",
        )
    assert plans.read_bytes() == original


def test_cli_compose_update_parser_sets_action() -> None:
    from shared_quota_router.cli_config import build_parser

    args = build_parser().parse_args(
        [
            "compose-vision-update",
            "--id",
            "glm-5.2-vision",
            "--execute",
            "glm-5.3",
            "--vision",
            "MiniMax-M3",
            "--force",
        ]
    )
    assert args.compose_action == "update"
    assert args.execute == "glm-5.3"
    assert args.vision == "MiniMax-M3"
    assert args.force is True

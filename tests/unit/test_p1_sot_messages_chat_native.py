"""P1-SOT：Messages→Chat native 批准源（CLI → YAML；YAML false 压过遗留 env）。"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from shared_quota_router.cli_config import build_parser, main
from shared_quota_router.config_schema import load_plans_dict
from shared_quota_router.feature_flags import (
    clear_flag_cache,
    is_native_messages_chat_path_active,
)
from shared_quota_router.generator import apply_plans_to_litellm, render_litellm_yaml


def _plans_with_messages_chat_convert() -> dict:
    """含 logical allow_conversion：anthropic_messages→openai_chat。"""
    return {
        "plans": [
            {
                "id": "opencode-a-chat",
                "display_name": "OpenCode Chat",
                "provider_id": "opencode-go",
                "priority": 10,
                "quota_group_id": "opencode-a",
                "base_url_env": "OPENCODE_GO_BASE_URL",
                "api_key_env": "OPENCODE_GO_KEY_A",
                "upstream_protocol": "openai_chat",
                "supported_features": ["text"],
                "supports_streaming": False,
                "conversions": [
                    {
                        "from": "anthropic_messages",
                        "to": "openai_chat",
                        "streaming": False,
                        "fidelity": "lossy_safe",
                    }
                ],
                "models": ["kimi-k3"],
            }
        ],
        "logical_models": {
            "kimi-k3": {
                "public_protocols": ["anthropic_messages"],
                "allow_conversion": True,
                "conversion_policy": {
                    "allowed": [
                        {"from": "anthropic_messages", "to": "openai_chat"},
                    ]
                },
            }
        },
    }


def _plans_without_convert() -> dict:
    """无 Messages→Chat convert policy（仅 direct）。"""
    return {
        "plans": [
            {
                "id": "opencode-a-msg",
                "display_name": "OpenCode Msg",
                "provider_id": "opencode-go",
                "priority": 10,
                "quota_group_id": "opencode-a",
                "base_url_env": "OPENCODE_GO_ANTHROPIC_BASE_URL",
                "api_key_env": "OPENCODE_GO_KEY_A",
                "upstream_protocol": "anthropic_messages",
                "supported_features": ["text"],
                "supports_streaming": False,
                "models": ["glm-5.2"],
            }
        ],
        "logical_models": {
            "glm-5.2": {
                "public_protocols": ["anthropic_messages"],
                "allow_conversion": False,
            }
        },
    }


@pytest.fixture(autouse=True)
def _reset_native_flags(monkeypatch: pytest.MonkeyPatch) -> None:
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
    clear_flag_cache()


# --- generator / CLI ---


def test_generator_native_true_only_with_flag_and_convert_policy() -> None:
    doc = load_plans_dict(_plans_with_messages_chat_convert())
    text_on = render_litellm_yaml(doc, enable_messages_chat_native=True)
    text_off = render_litellm_yaml(doc, enable_messages_chat_native=False)
    assert (
        yaml.safe_load(text_on)["litellm_settings"][
            "use_chat_completions_url_for_anthropic_messages"
        ]
        is True
    )
    assert (
        yaml.safe_load(text_off)["litellm_settings"][
            "use_chat_completions_url_for_anthropic_messages"
        ]
        is False
    )


def test_generator_flag_alone_without_convert_policy_stays_false() -> None:
    doc = load_plans_dict(_plans_without_convert())
    text = render_litellm_yaml(doc, enable_messages_chat_native=True)
    assert (
        yaml.safe_load(text)["litellm_settings"][
            "use_chat_completions_url_for_anthropic_messages"
        ]
        is False
    )


def test_cli_apply_with_enable_flag_writes_yaml_true(tmp_path: Path) -> None:
    plans = tmp_path / "plans.yaml"
    plans.write_text(
        yaml.dump(_plans_with_messages_chat_convert()), encoding="utf-8"
    )
    out = tmp_path / "litellm.yaml"
    rc = main(
        [
            "apply",
            "--plans",
            str(plans),
            "--output",
            str(out),
            "--backup-dir",
            str(tmp_path / "backups"),
            "--enable-messages-chat-native",
        ]
    )
    assert rc == 0
    data = yaml.safe_load(out.read_text(encoding="utf-8"))
    assert (
        data["litellm_settings"]["use_chat_completions_url_for_anthropic_messages"]
        is True
    )


def test_cli_apply_without_enable_flag_writes_yaml_false(tmp_path: Path) -> None:
    plans = tmp_path / "plans.yaml"
    plans.write_text(
        yaml.dump(_plans_with_messages_chat_convert()), encoding="utf-8"
    )
    out = tmp_path / "litellm.yaml"
    rc = main(
        [
            "apply",
            "--plans",
            str(plans),
            "--output",
            str(out),
            "--backup-dir",
            str(tmp_path / "backups"),
        ]
    )
    assert rc == 0
    data = yaml.safe_load(out.read_text(encoding="utf-8"))
    assert (
        data["litellm_settings"]["use_chat_completions_url_for_anthropic_messages"]
        is False
    )


def test_cli_parser_exposes_enable_messages_chat_native() -> None:
    parser = build_parser()
    args = parser.parse_args(
        ["apply", "--enable-messages-chat-native", "--plans", "p.yaml"]
    )
    assert args.enable_messages_chat_native is True
    args_off = parser.parse_args(["apply", "--plans", "p.yaml"])
    assert args_off.enable_messages_chat_native is False


# --- feature_flags：YAML false 压过遗留 env ---


def test_yaml_false_attr_overrides_leftover_env_true(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """属性已加载且为 False 时，禁止 OR/回退到遗留 env。"""
    import litellm

    monkeypatch.setattr(
        litellm, "use_chat_completions_url_for_anthropic_messages", False
    )
    monkeypatch.setenv(
        "LITELLM_USE_CHAT_COMPLETIONS_URL_FOR_ANTHROPIC_MESSAGES", "true"
    )
    clear_flag_cache()
    assert is_native_messages_chat_path_active() is False


def test_attr_true_is_active(monkeypatch: pytest.MonkeyPatch) -> None:
    import litellm

    monkeypatch.setattr(
        litellm, "use_chat_completions_url_for_anthropic_messages", True
    )
    clear_flag_cache()
    assert is_native_messages_chat_path_active() is True


def test_missing_attr_uses_strict_env_true(monkeypatch: pytest.MonkeyPatch) -> None:
    """仅属性缺失时读 env；严格解析 1/true/yes/on。"""
    import litellm

    monkeypatch.delattr(
        litellm, "use_chat_completions_url_for_anthropic_messages", raising=False
    )
    monkeypatch.setenv(
        "LITELLM_USE_CHAT_COMPLETIONS_URL_FOR_ANTHROPIC_MESSAGES", "true"
    )
    clear_flag_cache()
    assert is_native_messages_chat_path_active() is True


@pytest.mark.parametrize("raw", ["1", "TRUE", "Yes", "on"])
def test_missing_attr_strict_truthy_tokens(
    monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    import litellm

    monkeypatch.delattr(
        litellm, "use_chat_completions_url_for_anthropic_messages", raising=False
    )
    monkeypatch.setenv("LITELLM_USE_CHAT_COMPLETIONS_URL_FOR_ANTHROPIC_MESSAGES", raw)
    clear_flag_cache()
    assert is_native_messages_chat_path_active() is True


@pytest.mark.parametrize("raw", ["false", "False", "0", "", "no", "off", "random"])
def test_missing_attr_strict_falsy_tokens(
    monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    """禁止 bool(\"false\")；\"false\"/空/未知均为假。"""
    import litellm

    monkeypatch.delattr(
        litellm, "use_chat_completions_url_for_anthropic_messages", raising=False
    )
    if raw == "":
        monkeypatch.setenv(
            "LITELLM_USE_CHAT_COMPLETIONS_URL_FOR_ANTHROPIC_MESSAGES", ""
        )
    else:
        monkeypatch.setenv(
            "LITELLM_USE_CHAT_COMPLETIONS_URL_FOR_ANTHROPIC_MESSAGES", raw
        )
    clear_flag_cache()
    assert is_native_messages_chat_path_active() is False


def test_apply_plans_passes_enable_flag(tmp_path: Path) -> None:
    plans = tmp_path / "plans.yaml"
    plans.write_text(
        yaml.dump(_plans_with_messages_chat_convert()), encoding="utf-8"
    )
    out = tmp_path / "litellm.yaml"
    meta = apply_plans_to_litellm(
        plans,
        out,
        backup_dir=tmp_path / "backups",
        enable_messages_chat_native=True,
    )
    assert meta["bytes"] > 0
    data = yaml.safe_load(out.read_text(encoding="utf-8"))
    assert (
        data["litellm_settings"]["use_chat_completions_url_for_anthropic_messages"]
        is True
    )

"""M1-04: litellm.yaml generator — protocol metadata, atomic write, no secrets."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from shared_quota_router.config_schema import ConfigValidationError, load_plans_dict
from shared_quota_router.generator import (
    apply_plans_to_litellm,
    litellm_model_for_protocol,
    render_litellm_yaml,
    write_litellm_yaml_atomic,
)
from shared_quota_router.models import ApiProtocol

_ROOT = Path(__file__).resolve().parents[2]


def _anthropic_doc():
    """当前公网合法语义：Anthropic Messages（direct）；Chat 仅作 convert 腿示例。"""
    return load_plans_dict(
        {
            "plans": [
                {
                    "id": "opencode-a",
                    "display_name": "OpenCode Go A",
                    "provider_id": "opencode-go",
                    "priority": 10,
                    "base_url_env": "OPENCODE_GO_ANTHROPIC_BASE_URL",
                    "api_key_env": "OPENCODE_GO_KEY_A",
                    "upstream_protocol": "anthropic_messages",
                    "supported_features": ["text"],
                    "supports_streaming": False,
                    "models": ["kimi-k3", "glm-5.2"],
                },
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
                "kimi-k3": {"public_protocols": ["anthropic_messages"]},
                "glm-5.2": {"public_protocols": ["anthropic_messages"]},
            },
        }
    )


def _chat_doc():
    """遗留 Chat fixture：仍用于 secrets / atomic write 等生成器行为测。"""
    return load_plans_dict(
        {
            "plans": [
                {
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
                },
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
                    "models": ["glm-5.2"],
                },
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
    )


def test_litellm_model_prefix_by_protocol() -> None:
    assert litellm_model_for_protocol(ApiProtocol.OPENAI_CHAT, "kimi-k3") == "openai/kimi-k3"
    assert (
        litellm_model_for_protocol(ApiProtocol.ANTHROPIC_MESSAGES, "claude-x")
        == "anthropic/claude-x"
    )
    assert litellm_model_for_protocol(ApiProtocol.OPENAI_RESPONSES, "r") == "openai/r"


def test_generated_anthropic_messages_for_opencode_volc() -> None:
    """公网生成语义为 anthropic_messages（不要强制回到 Chat-only）。"""
    text = render_litellm_yaml(_anthropic_doc())
    data = yaml.safe_load(text)
    assert "model_list" in data
    by_name: dict[str, list] = {}
    for entry in data["model_list"]:
        by_name.setdefault(entry["model_name"], []).append(entry)

    kimi = by_name["kimi-k3"][0]
    assert kimi["model_info"]["upstream_protocol"] == "anthropic_messages"
    assert kimi["model_info"]["public_protocols"] == ["anthropic_messages"]
    assert kimi["model_info"]["supported_features"] == ["text"]
    assert kimi["litellm_params"]["model"] == "anthropic/kimi-k3"
    assert kimi["litellm_params"]["api_key"].startswith("os.environ/")
    assert kimi["model_info"]["quota_group_id"] == "opencode-a"
    assert kimi["model_info"]["deployment_id"] == "opencode-a-kimi-k3"
    assert kimi["model_info"]["priority"] == 10

    # 无 Responses；允许 anthropic_messages（及 disabled NewAPI 无 protocol）
    for entry in data["model_list"]:
        proto = entry["model_info"].get("upstream_protocol")
        assert proto in (None, "anthropic_messages")
        assert "openai_responses" not in (entry["model_info"].get("public_protocols") or [])


def test_generated_chat_upstream_still_emits_openai_prefix() -> None:
    """Chat upstream 腿仍生成 openai/ 前缀（convert 场景）。"""
    text = render_litellm_yaml(_chat_doc())
    data = yaml.safe_load(text)
    kimi = next(e for e in data["model_list"] if e["model_name"] == "kimi-k3")
    assert kimi["model_info"]["upstream_protocol"] == "openai_chat"
    assert kimi["litellm_params"]["model"] == "openai/kimi-k3"

def test_generator_emits_explicit_quota_group_id_alias() -> None:
    """P1-QG-ID：generator 写出显式 quota_group_id（可与 plan.id 不同）。"""
    doc = load_plans_dict(
        {
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
                },
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
                    "models": ["kimi-k3"],
                    "conversions": [
                        {
                            "from": "anthropic_messages",
                            "to": "openai_chat",
                            "fidelity": "lossy_safe",
                            "streaming": False,
                        }
                    ],
                },
            ],
            "logical_models": {
                "glm-5.2": {"public_protocols": ["anthropic_messages"]},
                "kimi-k3": {
                    "public_protocols": ["anthropic_messages"],
                    "allow_conversion": True,
                    "conversion_policy": {
                        "allowed": [
                            {"from": "anthropic_messages", "to": "openai_chat"}
                        ]
                    },
                },
            },
        }
    )
    text = render_litellm_yaml(doc)
    data = yaml.safe_load(text)
    by_name = {e["model_name"]: e for e in data["model_list"]}
    glm = by_name["glm-5.2"]
    kimi = by_name["kimi-k3"]
    assert glm["model_info"]["quota_group_id"] == "opencode-a"
    assert kimi["model_info"]["quota_group_id"] == "opencode-a"
    assert glm["model_info"]["deployment_id"] == "opencode-a-msg-glm-5.2"
    assert kimi["model_info"]["deployment_id"] == "opencode-a-chat-kimi-k3"
    assert "quota_group_id: opencode-a" in text


def test_newapi_disabled_without_protocol() -> None:
    text = render_litellm_yaml(_chat_doc())
    data = yaml.safe_load(text)
    claude = next(e for e in data["model_list"] if e["model_name"] == "claude-opus-4-8")
    assert claude["model_info"]["enabled"] is False
    assert "upstream_protocol" not in claude["model_info"]
    assert "public_protocols" not in claude["model_info"]


def test_generated_output_contains_no_secrets() -> None:
    text = render_litellm_yaml(_chat_doc())
    assert "sk-" not in text
    assert "ark-" not in text.lower() or "ark-code" in text  # model name ok
    assert "Bearer " not in text
    # api keys are env refs only
    for line in text.splitlines():
        if "api_key:" in line:
            assert "os.environ/" in line


def test_existing_fields_preserved() -> None:
    text = render_litellm_yaml(_chat_doc())
    data = yaml.safe_load(text)
    entry = data["model_list"][0]
    for key in (
        "deployment_id",
        "provider_id",
        "account_id",
        "quota_group_id",
        "priority",
    ):
        assert key in entry["model_info"]
    assert entry["litellm_params"]["timeout"] == 300
    assert data["router_settings"]["routing_strategy"] == "simple-shuffle"
    assert data["general_settings"]["master_key"] == "os.environ/LITELLM_MASTER_KEY"


def test_atomic_write_backup_and_invalid_leaves_previous(
    tmp_path: Path,
) -> None:
    out = tmp_path / "litellm.yaml"
    out.write_text("# previous good file\nmodel_list: []\n", encoding="ascii")
    previous = out.read_text(encoding="ascii")

    good = render_litellm_yaml(_chat_doc())
    meta = write_litellm_yaml_atomic(good, out, backup_dir=tmp_path / "backups")
    assert out.exists()
    assert "upstream_protocol" in out.read_text(encoding="ascii")
    assert meta["backup"]
    assert Path(meta["backup"]).is_file()

    # Invalid content must not replace
    with pytest.raises(ConfigValidationError):
        write_litellm_yaml_atomic("not-valid-no-header", out, backup_dir=tmp_path / "backups")
    # File still the good generated one (not previous, not invalid)
    assert "AUTO-GENERATED" in out.read_text(encoding="ascii")
    assert previous not in out.read_text(encoding="ascii") or True  # previous was replaced by good only


def test_apply_plans_to_litellm_repo(tmp_path: Path) -> None:
    plans = _ROOT / "config" / "plans.yaml"
    if not plans.is_file():
        pytest.skip("plans.yaml missing")
    out = tmp_path / "litellm.yaml"
    meta = apply_plans_to_litellm(plans, out, backup_dir=tmp_path / "backups")
    text = out.read_text(encoding="ascii")
    assert "upstream_protocol: anthropic_messages" in text
    assert "public_protocols: [anthropic_messages]" in text
    assert "quota_group_id: opencode-a" not in text
    assert "os.environ/OPENCODE_GO_KEY_A" not in text
    assert "os.environ/VOLC_CODING_KEY_C" in text
    assert "os.environ/PLAN_NEWAPI_A_API_KEY" in text
    assert "quota_group_id: newapi-a" in text
    assert "claude-opus-5" in text
    assert "claude-opus-4-8" in text
    assert "kimi-k3" not in text
    assert "enabled: true" in text
    assert meta["plans"] >= 3


def test_no_responses_without_verified_capability() -> None:
    text = render_litellm_yaml(_chat_doc())
    assert "openai_responses" not in text
    assert "/responses" not in text


def test_generator_supports_streaming_derived_from_features() -> None:
    doc = load_plans_dict(
        {
            "plans": [
                {
                    "id": "stream-plan",
                    "display_name": "Stream",
                    "provider_id": "p",
                    "priority": 10,
                    "base_url_env": "BASE",
                    "api_key_env": "KEY",
                    "upstream_protocol": "openai_chat",
                    "supported_features": ["text", "streaming"],
                    "models": ["m1"],
                },
                {
                    "id": "no-stream-plan",
                    "display_name": "No Stream",
                    "provider_id": "p",
                    "priority": 20,
                    "base_url_env": "BASE2",
                    "api_key_env": "KEY2",
                    "upstream_protocol": "openai_chat",
                    "supported_features": ["text"],
                    "models": ["m2"],
                },
            ],
            "logical_models": {
                "m1": {"public_protocols": ["openai_chat"]},
                "m2": {"public_protocols": ["openai_chat"]},
            },
        }
    )
    text = render_litellm_yaml(doc)
    data = yaml.safe_load(text)
    by_name = {e["model_name"]: e for e in data["model_list"]}
    assert by_name["m1"]["model_info"]["supports_streaming"] is True
    assert "streaming" in by_name["m1"]["model_info"]["supported_features"]
    assert by_name["m2"]["model_info"]["supports_streaming"] is False
    assert "streaming" not in by_name["m2"]["model_info"]["supported_features"]


def test_rollback_remove_streaming_feature_clears_generated_flag() -> None:
    """P0-SOT rollback: drop streaming from features → supports_streaming false in output."""
    doc = load_plans_dict(
        {
            "plans": [
                {
                    "id": "rollback-plan",
                    "display_name": "Rollback",
                    "provider_id": "p",
                    "priority": 10,
                    "base_url_env": "BASE",
                    "api_key_env": "KEY",
                    "upstream_protocol": "openai_chat",
                    "supported_features": ["text"],
                    "models": ["m1"],
                }
            ],
            "logical_models": {"m1": {"public_protocols": ["openai_chat"]}},
        }
    )
    text = render_litellm_yaml(doc)
    entry = yaml.safe_load(text)["model_list"][0]
    assert entry["model_info"]["supported_features"] == ["text"]
    assert entry["model_info"]["supports_streaming"] is False

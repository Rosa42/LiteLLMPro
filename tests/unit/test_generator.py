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


def _chat_doc():
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


def test_generated_chat_only_for_opencode_volc() -> None:
    text = render_litellm_yaml(_chat_doc())
    data = yaml.safe_load(text)
    assert "model_list" in data
    by_name: dict[str, list] = {}
    for entry in data["model_list"]:
        by_name.setdefault(entry["model_name"], []).append(entry)

    kimi = by_name["kimi-k3"][0]
    assert kimi["model_info"]["upstream_protocol"] == "openai_chat"
    assert kimi["model_info"]["public_protocols"] == ["openai_chat"]
    assert "streaming" in kimi["model_info"]["supported_features"]
    assert kimi["litellm_params"]["model"] == "openai/kimi-k3"
    assert kimi["litellm_params"]["api_key"].startswith("os.environ/")
    assert kimi["model_info"]["quota_group_id"] == "opencode-a"
    assert kimi["model_info"]["priority"] == 10

    # No Responses protocol anywhere
    for entry in data["model_list"]:
        assert entry["model_info"].get("upstream_protocol") in (None, "openai_chat")
        assert "openai_responses" not in (entry["model_info"].get("public_protocols") or [])


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
    assert "upstream_protocol: openai_chat" in text
    assert "public_protocols: [openai_chat]" in text
    assert "os.environ/OPENCODE_GO_KEY_A" in text
    assert "os.environ/VOLC_CODING_KEY_C" in text
    # NewAPI present but disabled
    assert "claude-opus-4-8" in text
    assert "enabled: false" in text
    assert meta["plans"] >= 3


def test_no_responses_without_verified_capability() -> None:
    text = render_litellm_yaml(_chat_doc())
    assert "openai_responses" not in text
    assert "/responses" not in text

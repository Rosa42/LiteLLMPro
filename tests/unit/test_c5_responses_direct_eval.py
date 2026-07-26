"""C5: Responses remains controlled-disabled without a verified direct upstream."""

from __future__ import annotations

from pathlib import Path

import pytest

from shared_quota_router.config_schema import ConfigValidationError, load_plans_dict
from shared_quota_router.models import ApiProtocol


def test_c5_responses_opt_in_still_requires_direct_upstream() -> None:
    data = {
        "plans": [
            {
                "id": "opencode-a",
                "display_name": "A",
                "provider_id": "opencode-go",
                "priority": 10,
                "base_url_env": "OPENCODE_GO_BASE_URL",
                "api_key_env": "OPENCODE_GO_KEY_A",
                "upstream_protocol": "openai_chat",
                "supported_features": ["text", "streaming", "tools"],
                "models": ["kimi-k3"],
            }
        ],
        "logical_models": {
            "kimi-k3": {"public_protocols": ["openai_chat", "openai_responses"]},
        },
    }
    with pytest.raises(ConfigValidationError, match="openai_responses"):
        load_plans_dict(data)


def test_c5_repo_plans_have_no_responses_upstream() -> None:
    root = Path(__file__).resolve().parents[2]
    path = root / "config" / "plans.yaml"
    if not path.is_file():
        pytest.skip("plans.yaml missing")
    from shared_quota_router.config_schema import load_plans_file

    doc = load_plans_file(path)
    available = doc.enabled_deployments_protocols()
    assert ApiProtocol.OPENAI_RESPONSES not in available

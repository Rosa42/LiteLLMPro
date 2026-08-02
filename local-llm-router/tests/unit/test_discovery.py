"""M1-05: capability discovery — public_protocols only, one entry per model."""

from __future__ import annotations

from shared_quota_router.discovery import (
    CapabilityCatalog,
    ModelCapability,
    catalog_from_logical_models,
    catalog_from_model_list,
    catalog_from_router,
    enrich_openai_models_list,
)
from shared_quota_router.discovery_routes import build_capabilities_payload
from shared_quota_router.models import ApiProtocol, LogicalModelProtocols


def _entry(
    name: str,
    *,
    public: list[str] | None = None,
    protocol: str | None = "openai_chat",
    enabled: bool = True,
    dep: str | None = None,
) -> dict:
    info: dict = {
        "deployment_id": dep or f"{name}-dep",
        "enabled": enabled,
    }
    if protocol:
        info["upstream_protocol"] = protocol
    if public is not None:
        info["public_protocols"] = public
    return {
        "model_name": name,
        "model_info": info,
        "litellm_params": {"model": f"openai/{name}"},
    }


def test_one_entry_per_logical_model_across_deployments() -> None:
    ml = [
        _entry("glm-5.2", public=["openai_chat"], dep="opencode-a-glm"),
        _entry("glm-5.2", public=["openai_chat"], dep="volc-c-glm"),
        _entry("kimi-k3", public=["openai_chat"], dep="opencode-a-kimi"),
    ]
    cat = catalog_from_model_list(ml)
    ids = [m.model_group for m in cat.models]
    assert ids == ["glm-5.2", "kimi-k3"]
    assert len(cat.models) == 2


def test_chat_only_model_lists_only_chat() -> None:
    cat = catalog_from_model_list(
        [_entry("kimi-k3", public=["openai_chat"])]
    )
    assert len(cat.models) == 1
    assert cat.models[0].public_protocols == frozenset({ApiProtocol.OPENAI_CHAT})
    body = cat.to_list_response(style="openai")
    assert body["data"][0]["metadata"]["public_protocols"] == ["openai_chat"]
    assert "openai_responses" not in body["data"][0]["metadata"]["public_protocols"]
    assert "anthropic_messages" not in body["data"][0]["metadata"]["public_protocols"]


def test_listing_never_implies_all_protocols() -> None:
    cat = catalog_from_model_list(
        [
            _entry("kimi-k3", public=["openai_chat"]),
            _entry("other", public=["openai_chat"]),
        ]
    )
    body = cat.to_list_response()
    for row in body["data"]:
        protos = row["metadata"]["public_protocols"]
        assert protos == ["openai_chat"]
        assert set(protos) != {
            "openai_chat",
            "openai_responses",
            "anthropic_messages",
        }


def test_unknown_protocol_values_absent() -> None:
    cat = catalog_from_model_list(
        [
            _entry(
                "kimi-k3",
                public=["openai_chat", "not-a-protocol", "soap"],
            )
        ]
    )
    assert cat.models[0].public_protocols == frozenset({ApiProtocol.OPENAI_CHAT})
    assert "soap" not in cat.to_list_response()["data"][0]["metadata"]["public_protocols"]


def test_disabled_protocol_absent_from_discovery() -> None:
    """Model without public_protocols opt-in is omitted entirely."""
    cat = catalog_from_model_list(
        [
            _entry("kimi-k3", public=["openai_chat"]),
            _entry("claude-opus-4-8", public=None),  # no public opt-in
            _entry("ghost", public=[]),
        ]
    )
    ids = {m.model_group for m in cat.models}
    assert ids == {"kimi-k3"}
    assert "claude-opus-4-8" not in ids
    assert "ghost" not in ids


def test_union_public_protocols_across_deployments() -> None:
    ml = [
        {
            "model_name": "multi",
            "model_info": {
                "deployment_id": "chat-dep",
                "public_protocols": ["openai_chat"],
            },
        },
        {
            "model_name": "multi",
            "model_info": {
                "deployment_id": "msg-dep",
                "public_protocols": ["anthropic_messages"],
            },
        },
    ]
    cat = catalog_from_model_list(ml)
    assert len(cat.models) == 1
    assert cat.models[0].public_protocols == frozenset(
        {ApiProtocol.OPENAI_CHAT, ApiProtocol.ANTHROPIC_MESSAGES}
    )
    ordered = cat.to_list_response()["data"][0]["metadata"]["public_protocols"]
    assert ordered[0] == "openai_chat"
    assert "anthropic_messages" in ordered


def test_disclaimer_present() -> None:
    cat = catalog_from_model_list([_entry("kimi-k3", public=["openai_chat"])])
    body = cat.to_list_response()
    assert "does not prove" in body["disclaimer"].lower() or "opt-in" in body[
        "disclaimer"
    ].lower()


def test_catalog_from_logical_models() -> None:
    logical = {
        "kimi-k3": LogicalModelProtocols.from_config("kimi-k3", ["openai_chat"]),
        "empty": LogicalModelProtocols(model_group="empty", public_protocols=frozenset()),
    }
    cat = catalog_from_logical_models(logical)
    assert [m.model_group for m in cat.models] == ["kimi-k3"]


def test_enrich_openai_models_list_only_known() -> None:
    cat = CapabilityCatalog(
        models=[
            ModelCapability(
                model_group="kimi-k3",
                public_protocols=frozenset({ApiProtocol.OPENAI_CHAT}),
            )
        ]
    )
    base = [
        {"id": "kimi-k3", "object": "model"},
        {"id": "unknown-model", "object": "model"},
    ]
    out = enrich_openai_models_list(base, cat)
    assert out[0]["metadata"]["public_protocols"] == ["openai_chat"]
    assert "metadata" not in out[1] or "public_protocols" not in (
        out[1].get("metadata") or {}
    )


def test_build_capabilities_payload_from_router_stub() -> None:
    class R:
        model_list = [
            _entry("kimi-k3", public=["openai_chat"]),
            _entry("claude-x", public=None),
        ]

    body = build_capabilities_payload(router=R(), style="openai")
    assert body["object"] == "list"
    assert body["source"] == "shared_quota_router.discovery"
    assert len(body["data"]) == 1
    assert body["data"][0]["id"] == "kimi-k3"
    assert body["data"][0]["metadata"]["public_protocols"] == ["openai_chat"]


def test_catalog_from_router_empty() -> None:
    class Empty:
        model_list = []

    assert catalog_from_router(Empty()).models == []


def test_capability_style_response() -> None:
    cat = catalog_from_model_list([_entry("kimi-k3", public=["openai_chat"])])
    body = cat.to_list_response(style="capability")
    assert body["data"][0]["public_protocols"] == ["openai_chat"]
    assert "metadata" not in body["data"][0]

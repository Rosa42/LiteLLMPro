"""HTTP routes for protocol capability discovery (M1-05).

Mounted on the LiteLLM proxy app at startup (no upstream business patch).

Endpoints (proxy key auth when LiteLLM auth is available):
  GET /v1/router/model-capabilities
  GET /shared-quota/v1/model-capabilities

Response: one entry per logical model with ``metadata.public_protocols``.
"""

from __future__ import annotations

import logging
from typing import Any

from shared_quota_router.discovery import CapabilityCatalog, catalog_from_router

logger = logging.getLogger(__name__)

_ROUTER_MOUNTED = False


def _get_llm_router() -> Any:
    try:
        from litellm.proxy import proxy_server

        return getattr(proxy_server, "llm_router", None)
    except Exception:  # noqa: BLE001
        return None


def build_capabilities_payload(
    router: Any = None,
    *,
    style: str = "openai",
) -> dict[str, Any]:
    """Build JSON body for capability listing (testable without FastAPI)."""
    r = router if router is not None else _get_llm_router()
    catalog: CapabilityCatalog = (
        catalog_from_router(r) if r is not None else CapabilityCatalog()
    )
    if style not in {"openai", "capability"}:
        style = "openai"
    body = catalog.to_list_response(style=style)
    body["source"] = "shared_quota_router.discovery"
    return body


def create_discovery_router() -> Any:
    """Create a FastAPI APIRouter with discovery endpoints."""
    from fastapi import APIRouter, Depends, Query

    auth_dep = None
    try:
        from litellm.proxy.auth.user_api_key_auth import user_api_key_auth as auth_dep
    except Exception:  # pragma: no cover
        auth_dep = None

    router = APIRouter(tags=["shared-quota discovery"])
    dependencies = [Depends(auth_dep)] if auth_dep is not None else []

    @router.get(
        "/v1/router/model-capabilities",
        dependencies=dependencies,
        summary="List logical models with public_protocols opt-in",
    )
    @router.get(
        "/shared-quota/v1/model-capabilities",
        dependencies=dependencies,
        summary="Alias for model-capabilities discovery",
    )
    async def model_capabilities(
        style: str = Query(
            "openai",
            description="openai: metadata.public_protocols; capability: top-level field",
        ),
    ) -> dict[str, Any]:
        return build_capabilities_payload(style=style)

    return router


def mount_discovery_routes(app: Any = None) -> bool:
    """Include discovery router on LiteLLM FastAPI app. Idempotent."""
    global _ROUTER_MOUNTED
    if _ROUTER_MOUNTED:
        return True
    try:
        if app is None:
            from litellm.proxy.proxy_server import app as proxy_app

            app = proxy_app
        api = create_discovery_router()
        app.include_router(api)
        _ROUTER_MOUNTED = True
        logger.info(
            "shared_quota_router discovery routes mounted: "
            "/v1/router/model-capabilities , /shared-quota/v1/model-capabilities"
        )
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("failed to mount discovery routes: %s", exc)
        return False


def is_mounted() -> bool:
    return _ROUTER_MOUNTED

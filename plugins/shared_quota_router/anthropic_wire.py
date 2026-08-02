"""P1-A5：保证 ``POST /v1/messages`` 协议门控错误的 Anthropic wire。

LiteLLM ``anthropic_endpoints`` 会把异常剥成 ``ProxyException``，全局
``openai_exception_handler`` 再输出 OpenAI ``{"error":...}``，丢失
``{"type":"error",...}``。

注意：Starlette ``ExceptionMiddleware`` 在构建时**拷贝**
``app.exception_handlers`` 到 ``_exception_handlers``；仅改 app 字典
不会影响已构建的中间件。挂载时必须同步打补丁。
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

_WIRE_MOUNTED = False
_ORIGINAL_PROXY_EXC_HANDLER: Optional[Callable[..., Any]] = None
_HANDLER_NAME = "shared_quota_anthropic_proxy_exception_handler"


def is_anthropic_messages_path(path: str) -> bool:
    """True for unified Anthropic Messages public paths."""
    p = (path or "").rstrip("/")
    return p.endswith("/v1/messages") or p == "/messages"


def anthropic_error_body_from_proxy_exc(exc: Any) -> dict[str, Any]:
    """从 ProxyException / 协议错误还原 Anthropic §8.1 body。"""
    fields = getattr(exc, "provider_specific_fields", None)
    if isinstance(fields, dict) and fields.get("type") == "error":
        err = fields.get("error")
        if isinstance(err, dict) and err.get("type"):
            return {
                "type": "error",
                "error": {
                    "type": str(err.get("type") or "invalid_request_error"),
                    "message": str(err.get("message") or getattr(exc, "message", "")),
                },
            }

    raw_type = getattr(exc, "type", None)
    err_type = (
        "invalid_request_error"
        if raw_type in (None, "None", "", "null")
        else str(raw_type)
    )
    return {
        "type": "error",
        "error": {
            "type": err_type,
            "message": str(getattr(exc, "message", None) or str(exc)),
        },
    }


def resolve_messages_error_status(exc: Any) -> int:
    """Messages 协议门控：固定 400；其它异常尽量保留原 code。"""
    code = getattr(exc, "code", None)
    try:
        status = int(code) if code is not None else 400
    except (TypeError, ValueError):
        status = 400
    body = anthropic_error_body_from_proxy_exc(exc)
    err_type = body.get("error", {}).get("type")
    if err_type in {"invalid_request_error", "api_error"} and status >= 500:
        return 400
    if status < 400 or status > 599:
        return 400
    return status


def _patch_exception_middleware(app: Any, exc_class: type, handler: Callable[..., Any]) -> int:
    """把 handler 写入已构建的 ExceptionMiddleware 拷贝。返回打补丁层数。"""
    patched = 0
    # 1) middleware_stack 链（运行时生效的路径）
    current = getattr(app, "middleware_stack", None)
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        handlers = getattr(current, "_exception_handlers", None)
        if isinstance(handlers, dict):
            handlers[exc_class] = handler
            patched += 1
        current = getattr(current, "app", None)

    # 2) 部分 Starlette 版本把拷贝挂在 router 上
    router = getattr(app, "router", None)
    handlers = getattr(router, "_exception_handlers", None) if router is not None else None
    if isinstance(handlers, dict):
        handlers[exc_class] = handler
        patched += 1
    return patched


def mount_anthropic_wire_guard(app: Any = None, *, force: bool = False) -> bool:
    """在 LiteLLM FastAPI app 上挂 Messages Anthropic wire handler。"""
    global _WIRE_MOUNTED, _ORIGINAL_PROXY_EXC_HANDLER
    try:
        if app is None:
            from litellm.proxy.proxy_server import app as proxy_app

            app = proxy_app

        try:
            from litellm.proxy._types import ProxyException
        except ImportError:
            logger.warning("ProxyException unavailable; anthropic wire guard skipped")
            return False

        from fastapi import Request
        from fastapi.responses import JSONResponse

        existing = None
        try:
            existing = app.exception_handlers.get(ProxyException)
        except Exception:  # noqa: BLE001
            existing = None

        if existing is not None and getattr(existing, "__name__", "") != _HANDLER_NAME:
            _ORIGINAL_PROXY_EXC_HANDLER = existing
        elif _ORIGINAL_PROXY_EXC_HANDLER is None and existing is not None:
            _ORIGINAL_PROXY_EXC_HANDLER = existing

        async def shared_quota_anthropic_proxy_exception_handler(
            request: Request, exc: ProxyException
        ):
            path = request.url.path if request is not None else ""
            if is_anthropic_messages_path(path):
                body = anthropic_error_body_from_proxy_exc(exc)
                status = resolve_messages_error_status(exc)
                headers = getattr(exc, "headers", None) or {}
                return JSONResponse(
                    status_code=status,
                    content=body,
                    headers=headers,
                    media_type="application/json",
                )
            if _ORIGINAL_PROXY_EXC_HANDLER is not None:
                return await _ORIGINAL_PROXY_EXC_HANDLER(request, exc)
            status_code = 500
            try:
                status_code = int(exc.code) if exc.code else 500
            except (TypeError, ValueError):
                status_code = 500
            return JSONResponse(
                status_code=status_code,
                content={"error": exc.to_dict()},
                headers=getattr(exc, "headers", None) or {},
                media_type="application/json",
            )

        # 已是我们的 handler 且非 force：仍需确保 middleware 拷贝同步
        already_ours = (
            existing is not None
            and getattr(existing, "__name__", "") == _HANDLER_NAME
        )
        if not already_ours or force or not _WIRE_MOUNTED:
            app.add_exception_handler(
                ProxyException, shared_quota_anthropic_proxy_exception_handler
            )

        n = _patch_exception_middleware(
            app, ProxyException, shared_quota_anthropic_proxy_exception_handler
        )
        _WIRE_MOUNTED = True
        logger.info(
            "shared_quota_router anthropic wire guard mounted "
            "(force=%s middleware_patches=%s)",
            force,
            n,
        )
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("failed to mount anthropic wire guard: %s", exc)
        return False


def is_mounted() -> bool:
    return _WIRE_MOUNTED


def reset_mount_state_for_tests() -> None:
    """单测重置幂等标志（勿用于生产）。"""
    global _WIRE_MOUNTED, _ORIGINAL_PROXY_EXC_HANDLER
    _WIRE_MOUNTED = False
    _ORIGINAL_PROXY_EXC_HANDLER = None

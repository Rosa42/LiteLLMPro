"""Shared quota routing extension for LiteLLM Proxy.

Import name (runtime): shared_quota_router
"""

__version__ = "0.1.1"

from shared_quota_router.bootstrap import (  # noqa: E402
    callback_instance,
    get_callback,
    get_store,
    register,
    register_proxy_startup,
)

__all__ = [
    "callback_instance",
    "get_callback",
    "get_store",
    "register",
    "register_proxy_startup",
    "__version__",
]

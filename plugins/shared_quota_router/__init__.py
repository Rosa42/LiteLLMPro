"""Shared quota routing extension for LiteLLM Proxy.

Import name (runtime): shared_quota_router
Source path: plugins/shared_quota_router/

Registration (phase 7+):
  from shared_quota_router.bootstrap import register
  register(router)

Proxy: LITELLM_WORKER_STARTUP_HOOKS=shared_quota_router.bootstrap:register_proxy_startup
"""

__version__ = "0.1.0"

# callback_instance is delivered in phase 8; keep package importable from M0.
callback_instance = None

from shared_quota_router.bootstrap import register as register  # noqa: E402

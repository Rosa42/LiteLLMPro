"""Shared quota routing extension for LiteLLM Proxy.

Import name (runtime): shared_quota_router
Source path: plugins/shared_quota_router/
"""

__version__ = "0.1.0"

# callback_instance is delivered in phase 8; keep package importable from M0.
callback_instance = None

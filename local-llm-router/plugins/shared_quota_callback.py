"""LiteLLM config-side shim for custom callback import.

LiteLLM v1.90.5 `get_instance_fn` resolves callbacks relative to the config
file as a *single* `.py` path (not a package directory). Keep the real
implementation in the `shared_quota_router` package; this file only re-exports
the instance LiteLLM needs:

  litellm_settings.callbacks:
    - shared_quota_callback.callback_instance
"""

from __future__ import annotations

from shared_quota_router.bootstrap import callback_instance, get_callback

__all__ = ["callback_instance", "get_callback"]

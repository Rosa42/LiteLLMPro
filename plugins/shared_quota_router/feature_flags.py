"""Runtime feature flags for protocol-aware gateway (M4-02).

Primary rollback: set ``PROTOCOL_AWARE_GATEWAY_ENABLED=false``.
Catastrophic fallback: restore timestamped ``config/backups/litellm.yaml.*.bak``.
Redis quota keys are never flushed by these flags.
"""

from __future__ import annotations

import os
from functools import lru_cache

# G0-A mount readiness（未实现前恒为 False；mount 成功后由 g0a_route_mount 置位）
_G0A_MESSAGES_MOUNT_READY: bool = False


def _env_bool(name: str, *, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or str(raw).strip() == "":
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def is_protocol_aware_gateway_enabled() -> bool:
    """When False: legacy Chat selection (no capability/public gates).

    Messages/Responses still return controlled not-enabled responses.
    Default ``false`` for safe rollout; set ``true`` for MVP protocol-aware path.
    """
    return _env_bool("PROTOCOL_AWARE_GATEWAY_ENABLED", default=False)


def is_protocol_conversion_enabled() -> bool:
    """Raw ``PROTOCOL_CONVERSION_ENABLED`` env (default false).

    Prefer :func:`is_conversion_routing_active` for selection / dispatch —
    conversion must not activate when the protocol gateway flag is off.
    """
    return _env_bool("PROTOCOL_CONVERSION_ENABLED", default=False)


def is_native_messages_chat_path_active() -> bool:
    """LiteLLM Messages→Chat URL switch (G0-Native).

    Prefer live ``litellm.use_chat_completions_url_for_anthropic_messages``
    (proxy yaml may set it after import). Also honor env for early/tests.
    """
    try:
        import litellm

        if bool(
            getattr(litellm, "use_chat_completions_url_for_anthropic_messages", False)
        ):
            return True
    except ImportError:
        pass
    return _env_bool(
        "LITELLM_USE_CHAT_COMPLETIONS_URL_FOR_ANTHROPIC_MESSAGES", default=False
    )


def is_g0a_messages_mount_ready() -> bool:
    """True when thin G0-A ``/v1/messages`` swap mounted successfully."""
    return _G0A_MESSAGES_MOUNT_READY


def set_g0a_messages_mount_ready(ready: bool) -> None:
    """供 G0-A mount / 测试设置 mount 就绪位。"""
    global _G0A_MESSAGES_MOUNT_READY
    _G0A_MESSAGES_MOUNT_READY = bool(ready)
    clear_flag_cache()


def is_conversion_path_ready() -> bool:
    """Proven convert upstream path: native switch OR G0-A mount."""
    return is_native_messages_chat_path_active() or is_g0a_messages_mount_ready()


def is_conversion_routing_active() -> bool:
    """True only when gateway ∧ conversion ∧ proven path (ops AND matrix).

    ``GATEWAY=false`` + ``CONVERSION=true`` is a misconfig: never select or
    apply convert routes. ``CONVERSION=true`` without native/G0-A path is also
    inactive — avoids conversion-only traffic on stock Responses misroute.
    """
    return (
        is_protocol_aware_gateway_enabled()
        and is_protocol_conversion_enabled()
        and is_conversion_path_ready()
    )


def metrics_label_salt() -> str | None:
    salt = os.environ.get("SHARED_QUOTA_METRICS_LABEL_SALT") or os.environ.get(
        "METRICS_LABEL_SALT"
    )
    if salt is None or not str(salt).strip():
        return None
    return str(salt).strip()


def metrics_raw_labels_allowed() -> bool:
    """Local/dev only: allow raw model/deployment labels without salt."""
    return _env_bool("SHARED_QUOTA_METRICS_RAW_LABELS", default=False)


@lru_cache(maxsize=1)
def flag_snapshot() -> dict[str, object]:
    """Cached view for docs/tests; call ``clear_flag_cache`` after env changes in tests."""
    return {
        "PROTOCOL_AWARE_GATEWAY_ENABLED": is_protocol_aware_gateway_enabled(),
        "PROTOCOL_CONVERSION_ENABLED": is_protocol_conversion_enabled(),
        "native_messages_chat_path": is_native_messages_chat_path_active(),
        "g0a_messages_mount_ready": is_g0a_messages_mount_ready(),
        "conversion_path_ready": is_conversion_path_ready(),
        "conversion_routing_active": is_conversion_routing_active(),
        "has_metrics_salt": metrics_label_salt() is not None,
        "metrics_raw_labels": metrics_raw_labels_allowed(),
    }


def clear_flag_cache() -> None:
    flag_snapshot.cache_clear()

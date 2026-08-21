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
    """LiteLLM Messages→Chat URL switch（G0-Native；P1-SOT）。

    1. 若 litellm 模块属性已加载：以 bool(attr) 为准；False 时禁止再 OR/回退 env。
    2. 仅属性缺失（单测 / 未启动 proxy）时，才严格解析 env
       （仅 1/true/yes/on 为真；字符串 false 为假，禁止 Python bool("false")）。
    """
    try:
        import litellm
    except ImportError:
        return _env_bool(
            "LITELLM_USE_CHAT_COMPLETIONS_URL_FOR_ANTHROPIC_MESSAGES", default=False
        )

    # 属性已存在（含 YAML setattr / 模块默认值）→ YAML/attr 优先，不回退 env
    if hasattr(litellm, "use_chat_completions_url_for_anthropic_messages"):
        return bool(litellm.use_chat_completions_url_for_anthropic_messages)

    # 仅属性缺失时读 env（严格解析由 _env_bool 保证）
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


def is_messages_chat_native_path_ready() -> bool:
    """Messages→Chat path ready（P0-G0A：本期 native-only；g0a_mount 不计入）。"""
    return is_native_messages_chat_path_active()


def is_conversion_path_ready() -> bool:
    """Proven Messages→Chat convert upstream path（本期 = native only）。

    历史曾为 ``native ∨ g0a_mount``；L2 仅关 native 时 convert 仍可能存活，
    故本期 g0a_mount **不计入** readiness（见设计方案 P0-G0A）。
    """
    return is_messages_chat_native_path_ready()


def is_conversion_routing_active() -> bool:
    """True only when gateway ∧ conversion ∧ Messages→Chat native path ready.

    ``GATEWAY=false`` + ``CONVERSION=true`` is a misconfig: never select or
    apply convert routes. ``CONVERSION=true`` without native path is also
    inactive — g0a_mount alone must not activate Messages→Chat conversion.
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


def p0_probe_b_marker() -> str:
    """Non-empty => inject this exact token into messages in pre-call. Default empty."""
    return (os.environ.get("P0_PROBE_B_MARKER") or "").strip()


@lru_cache(maxsize=1)
def flag_snapshot() -> dict[str, object]:
    """Cached view for docs/tests; call ``clear_flag_cache`` after env changes in tests."""
    return {
        "PROTOCOL_AWARE_GATEWAY_ENABLED": is_protocol_aware_gateway_enabled(),
        "PROTOCOL_CONVERSION_ENABLED": is_protocol_conversion_enabled(),
        "native_messages_chat_path": is_native_messages_chat_path_active(),
        "g0a_messages_mount_ready": is_g0a_messages_mount_ready(),
        "messages_chat_native_path_ready": is_messages_chat_native_path_ready(),
        "conversion_path_ready": is_conversion_path_ready(),
        "conversion_routing_active": is_conversion_routing_active(),
        "has_metrics_salt": metrics_label_salt() is not None,
        "metrics_raw_labels": metrics_raw_labels_allowed(),
    }


def clear_flag_cache() -> None:
    flag_snapshot.cache_clear()

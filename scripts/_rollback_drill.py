#!/usr/bin/env python3
"""A7/A10 rollback drill checks. No secrets printed."""
from __future__ import annotations

import argparse
import json
import os
import urllib.error
import urllib.request
from pathlib import Path


def load_env(path: str = ".env") -> dict[str, str]:
    out: dict[str, str] = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k] = v
    return out


def post_messages(mk: str, model: str) -> tuple[int, str]:
    body = {
        "model": model,
        "max_tokens": 16,
        "messages": [{"role": "user", "content": "Reply with exactly: pong"}],
    }
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        "http://127.0.0.1:4000/v1/messages",
        data=data,
        method="POST",
        headers={
            "x-api-key": mk,
            "anthropic-version": "2023-06-01",
            "User-Agent": "Mozilla/5.0",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def discovery_ids(mk: str) -> list[str]:
    req = urllib.request.Request(
        "http://127.0.0.1:4000/v1/router/model-capabilities",
        headers={"Authorization": f"Bearer {mk}"},
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        cap = json.load(r)
    return [x.get("id") for x in cap.get("data", [])]


def check_a7(mk: str) -> None:
    print("=== A7 / L1 (PROTOCOL_CONVERSION_ENABLED=false) ===")
    for m in ("glm-5.2", "claude-opus-4-8"):
        code, raw = post_messages(mk, m)
        ok = code == 200 and "message" in raw
        print(f"  direct {m}: HTTP={code} ok={ok}")
        if not ok:
            raise SystemExit(f"A7 FAIL: direct {m} should remain reachable")
    for m in ("kimi-k3", "deepseek-v4-flash"):
        code, raw = post_messages(mk, m)
        print(
            f"  convert {m}: HTTP={code} body={raw[:180].replace(chr(10), ' ')}"
        )
        if code == 200:
            raise SystemExit(f"A7 FAIL: {m} must be runtime-unreachable")
    ids = discovery_ids(mk)
    print(f"  discovery ids={ids}")
    for need in ("glm-5.2", "claude-opus-4-8", "kimi-k3", "deepseek-v4-flash"):
        if need not in ids:
            raise SystemExit(f"A7 FAIL: discovery should still list {need}")
    print("A7 PASS")


def check_a10() -> None:
    print("=== A10 / L2 (native YAML false + env deleted) ===")
    # Ensure this process does not inherit leftover native env
    os.environ.pop("LITELLM_USE_CHAT_COMPLETIONS_URL_FOR_ANTHROPIC_MESSAGES", None)

    import yaml
    from shared_quota_router.config_schema import load_plans_file
    from shared_quota_router.conversion.registry import resolve_route
    from shared_quota_router.feature_flags import (
        clear_flag_cache,
        is_conversion_routing_active,
        is_g0a_messages_mount_ready,
        is_messages_chat_native_path_ready,
        is_native_messages_chat_path_active,
    )
    from shared_quota_router.models import ApiProtocol, Feature, TransformOwner
    from shared_quota_router.registry import registry_from_model_list
    from shared_quota_router.route_readiness import readiness

    clear_flag_cache()

    yaml_text = Path("config/litellm.yaml").read_text(encoding="utf-8")
    yaml_native_true = (
        "use_chat_completions_url_for_anthropic_messages: true" in yaml_text
    )
    env_file_has = any(
        line.startswith("LITELLM_USE_CHAT_COMPLETIONS_URL_FOR_ANTHROPIC_MESSAGES=")
        for line in Path(".env").read_text(encoding="utf-8").splitlines()
    )

    print(f"  yaml_native_true={yaml_native_true}")
    print(f"  env_file_has_native={env_file_has}")
    print(f"  native_active={is_native_messages_chat_path_active()}")
    print(f"  messages_chat_native_ready={is_messages_chat_native_path_ready()}")
    print(f"  g0a_mount={is_g0a_messages_mount_ready()}")
    print(f"  conversion_routing_active={is_conversion_routing_active()}")

    if yaml_native_true:
        raise SystemExit("A10 FAIL: litellm.yaml must have native=false")
    if env_file_has:
        raise SystemExit("A10 FAIL: delete LITELLM_USE_CHAT... from .env")
    if is_native_messages_chat_path_active() or is_messages_chat_native_path_ready():
        raise SystemExit("A10 FAIL: native path still active")
    if is_g0a_messages_mount_ready():
        raise SystemExit("A10 FAIL: g0a_mount_ready must be False")

    ready_native = readiness(
        ApiProtocol.ANTHROPIC_MESSAGES,
        ApiProtocol.OPENAI_CHAT,
        TransformOwner.LITELLM_NATIVE,
    )
    ready_adapter = readiness(
        ApiProtocol.ANTHROPIC_MESSAGES,
        ApiProtocol.OPENAI_CHAT,
        TransformOwner.PROJECT_ADAPTER,
    )
    print(f"  readiness native={ready_native} adapter={ready_adapter}")
    if ready_native or ready_adapter:
        raise SystemExit("A10 FAIL: Messages→Chat readiness must be False for both owners")

    doc = load_plans_file("config/plans.yaml")
    data = yaml.safe_load(yaml_text) or {}
    reg = registry_from_model_list(list(data.get("model_list") or []))
    convert_n = 0
    adapter_n = 0
    for mg in ("kimi-k3", "deepseek-v4-flash"):
        lm = doc.logical_models.get(mg)
        for dep in reg.get_by_model_group(mg):
            route = resolve_route(
                dep,
                public_protocol=ApiProtocol.ANTHROPIC_MESSAGES,
                required_features=frozenset({Feature.TEXT}),
                stream=False,
                logical=lm,
                conversion_enabled=True,
            )
            if route is not None and route.conversion is not None:
                convert_n += 1
                if route.transform_owner is TransformOwner.PROJECT_ADAPTER:
                    adapter_n += 1
                print(
                    f"  unexpected route {dep.deployment_id} owner={route.transform_owner}"
                )
    print(f"  convert_candidates={convert_n} project_adapter={adapter_n}")
    if convert_n != 0:
        raise SystemExit("A10 FAIL: convert candidates must be 0")
    if adapter_n != 0:
        raise SystemExit("A10 FAIL: PROJECT_ADAPTER candidates must be 0")

    # Container runtime flags
    mk = load_env()["LITELLM_MASTER_KEY"]
    for m in ("kimi-k3", "deepseek-v4-flash"):
        code, _ = post_messages(mk, m)
        print(f"  runtime {m}: HTTP={code}")
        if code == 200:
            raise SystemExit(f"A10 FAIL: {m} must not succeed after L2")
    for m in ("glm-5.2", "claude-opus-4-8"):
        code, raw = post_messages(mk, m)
        ok = code == 200 and "message" in raw
        print(f"  direct {m}: HTTP={code} ok={ok}")
        if not ok:
            raise SystemExit(f"A10 FAIL: direct {m} should still work")
    print("A10 PASS")


def check_restore(mk: str) -> None:
    print("=== restore smoke A1-A4 ===")
    for m in ("glm-5.2", "claude-opus-4-8", "kimi-k3", "deepseek-v4-flash"):
        code, raw = post_messages(mk, m)
        ok = code == 200 and "message" in raw
        print(f"  {m}: HTTP={code} ok={ok}")
        if not ok:
            raise SystemExit(f"restore FAIL: {m} body={raw[:160]}")
    print("RESTORE PASS")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=("a7", "a10", "restore"), required=True)
    args = ap.parse_args()
    mk = load_env()["LITELLM_MASTER_KEY"]
    if args.mode == "a7":
        check_a7(mk)
    elif args.mode == "a10":
        check_a10()
    else:
        check_restore(mk)


if __name__ == "__main__":
    main()

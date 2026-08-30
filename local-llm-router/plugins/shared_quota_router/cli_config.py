"""CLI for plans validation and litellm.yaml generation.

Examples:
  python -m shared_quota_router.cli_config validate --plans config/plans.yaml
  python -m shared_quota_router.cli_config apply --plans config/plans.yaml --output config/litellm.yaml
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from shared_quota_router.compose_mutator import (
    compose_vision_add,
    compose_vision_remove,
    compose_vision_update,
    list_vision_slot_options,
    persist_plans_and_apply,
)
from shared_quota_router.config_schema import ConfigValidationError, load_plans_file
from shared_quota_router.generator import apply_plans_to_litellm, render_litellm_yaml


def _load_plans_mapping(path: Path) -> dict:
    import yaml

    if not path.is_file():
        raise ConfigValidationError(f"plans file not found: {path}")
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ConfigValidationError(f"{path}: root must be a mapping")
    return data


def _cmd_compose_slots(args: argparse.Namespace) -> int:
    try:
        doc = load_plans_file(args.plans)
        slots = list_vision_slot_options(doc, execute_model=args.execute)
    except ConfigValidationError as exc:
        print(f"INVALID: {exc}", file=sys.stderr)
        return 1
    print("execute:")
    for name in slots["execute"]:
        print(f"  - {name}")
    print("translate:")
    for name in slots["translate"]:
        print(f"  - {name}")
    return 0


def _cmd_compose_write(args: argparse.Namespace) -> int:
    try:
        data = _load_plans_mapping(args.plans)
        if args.compose_action == "add":
            data = compose_vision_add(
                data,
                facade_id=args.id,
                execute_model=args.execute,
                translate_model=args.vision,
            )
        elif args.compose_action == "update":
            data = compose_vision_update(
                data,
                facade_id=args.id,
                execute_model=args.execute,
                translate_model=args.vision,
                force_preset=bool(args.force),
            )
        else:
            data = compose_vision_remove(
                data,
                facade_id=args.id,
                force_preset=bool(args.force),
            )
        meta = persist_plans_and_apply(
            args.plans,
            data,
            litellm_path=args.output,
            backup_dir=args.backup_dir,
            enable_messages_chat_native=True,
        )
    except ConfigValidationError as exc:
        print(f"COMPOSE FAILED: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"COMPOSE FAILED: {exc}", file=sys.stderr)
        return 1
    print(f"OK: wrote {meta['output']} ({meta['bytes']} bytes)")
    print("Restart the litellm container to load the new facade.")
    return 0


def _cmd_validate(args: argparse.Namespace) -> int:
    try:
        doc = load_plans_file(args.plans)
    except ConfigValidationError as exc:
        print(f"INVALID: {exc}", file=sys.stderr)
        return 1
    print(
        f"OK: {len(doc.plans)} plan(s), {len(doc.logical_models)} logical model(s) "
        f"with public_protocols"
    )
    for p in doc.plans:
        proto = p.upstream_protocol.value if p.upstream_protocol else "unset"
        print(
            f"  - {p.id}: provider={p.provider_id} protocol={proto} "
            f"enabled={p.enabled} models={len(p.models)}"
        )
    return 0


def _cmd_apply(args: argparse.Namespace) -> int:
    try:
        meta = apply_plans_to_litellm(
            args.plans,
            args.output,
            backup_dir=args.backup_dir,
            # P1-SOT：唯一批准输入 → 写入 YAML（非裸 env）
            enable_messages_chat_native=bool(
                getattr(args, "enable_messages_chat_native", False)
            ),
        )
    except ConfigValidationError as exc:
        print(f"APPLY FAILED (previous litellm.yaml untouched): {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"APPLY FAILED (previous litellm.yaml untouched): {exc}", file=sys.stderr)
        return 1
    print(f"OK: wrote {meta['output']} ({meta['bytes']} bytes)")
    if meta.get("backup"):
        print(f"backup: {meta['backup']}")
    print(
        f"plans={meta['plans']} deployments={meta['deployments']} "
        f"logical_models={meta['logical_models']}"
    )
    if args.json:
        print(json.dumps(meta, indent=2))
    return 0


def _cmd_render(args: argparse.Namespace) -> int:
    """Render to stdout (tests / dry-run). Does not write files."""
    try:
        doc = load_plans_file(args.plans)
        text = render_litellm_yaml(doc)
    except ConfigValidationError as exc:
        print(f"RENDER FAILED: {exc}", file=sys.stderr)
        return 1
    sys.stdout.write(text)
    if not text.endswith("\n"):
        sys.stdout.write("\n")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="shared_quota_router.cli_config")
    sub = p.add_subparsers(dest="command", required=True)

    v = sub.add_parser("validate", help="Validate plans.yaml")
    v.add_argument(
        "--plans",
        type=Path,
        default=Path("config/plans.yaml"),
        help="Path to plans.yaml",
    )
    v.set_defaults(func=_cmd_validate)

    a = sub.add_parser("apply", help="Validate + generate litellm.yaml atomically")
    a.add_argument("--plans", type=Path, default=Path("config/plans.yaml"))
    a.add_argument("--output", type=Path, default=Path("config/litellm.yaml"))
    a.add_argument(
        "--backup-dir",
        type=Path,
        default=None,
        help="Backup directory (default: config/backups)",
    )
    a.add_argument(
        "--enable-messages-chat-native",
        action="store_true",
        default=False,
        help=(
            "P1-SOT: 批准 Messages→Chat G0-Native；仅当 plans 存在 "
            "anthropic_messages→openai_chat convert policy 时写入 YAML true"
        ),
    )
    a.add_argument("--json", action="store_true", help="Also print machine-readable meta")
    a.set_defaults(func=_cmd_apply)

    r = sub.add_parser("render", help="Render litellm.yaml to stdout")
    r.add_argument("--plans", type=Path, default=Path("config/plans.yaml"))
    r.set_defaults(func=_cmd_render)

    slots = sub.add_parser(
        "compose-vision-slots",
        help="List eligible execute / translate models for a vision facade",
    )
    slots.add_argument("--plans", type=Path, default=Path("config/plans.yaml"))
    slots.add_argument(
        "--execute",
        default=None,
        help="If set, translate candidates exclude overlapping quota groups",
    )
    slots.set_defaults(func=_cmd_compose_slots)

    for action, help_text in (
        ("compose-vision-add", "Create a vision facade and regenerate litellm.yaml"),
        ("compose-vision-update", "Change vision facade slots and regenerate litellm.yaml"),
        ("compose-vision-remove", "Delete a vision facade and regenerate litellm.yaml"),
    ):
        c = sub.add_parser(action, help=help_text)
        c.add_argument("--plans", type=Path, default=Path("config/plans.yaml"))
        c.add_argument("--output", type=Path, default=Path("config/litellm.yaml"))
        c.add_argument("--backup-dir", type=Path, default=None)
        c.add_argument("--id", required=True, help="Facade logical model id")
        if action != "compose-vision-remove":
            c.add_argument("--execute", required=True, help="Reasoning / execute model")
            c.add_argument("--vision", required=True, help="Translate / vision model")
        if action != "compose-vision-add":
            c.add_argument(
                "--force",
                action="store_true",
                help="Required to change or delete preset glm-5.2-vision",
            )
        verb = action.rsplit("-", 1)[-1]
        c.set_defaults(func=_cmd_compose_write, compose_action=verb)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())

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

from shared_quota_router.config_schema import ConfigValidationError, load_plans_file
from shared_quota_router.generator import apply_plans_to_litellm, render_litellm_yaml


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
    a.add_argument("--json", action="store_true", help="Also print machine-readable meta")
    a.set_defaults(func=_cmd_apply)

    r = sub.add_parser("render", help="Render litellm.yaml to stdout")
    r.add_argument("--plans", type=Path, default=Path("config/plans.yaml"))
    r.set_defaults(func=_cmd_render)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())

"""Host-side vision-facade mutation (plans.yaml dual-write, no in-proxy PUT)."""

from __future__ import annotations

import copy
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

import yaml

from shared_quota_router.config_schema import (
    ConfigValidationError,
    PlansDocument,
    eligible_routes,
    load_plans_dict,
)
from shared_quota_router.generator import apply_plans_to_litellm
from shared_quota_router.models import ApiProtocol, Feature

_TEXT = frozenset({Feature.TEXT})
_TEXT_IMAGE = frozenset({Feature.TEXT, Feature.IMAGE})


def compose_vision_add(
    data: Mapping[str, Any],
    *,
    facade_id: str,
    execute_model: str,
    translate_model: str,
) -> dict[str, Any]:
    """Return a new plans dict with a vision facade injected onto execute plans."""
    out = copy.deepcopy(dict(data))
    logical = out.setdefault("logical_models", {})
    if not isinstance(logical, dict):
        raise ConfigValidationError("logical_models must be a mapping")
    if facade_id in logical and isinstance(logical[facade_id], Mapping):
        existing = logical[facade_id]
        if existing.get("compose"):
            raise ConfigValidationError(
                f"logical model {facade_id!r} already has compose; use update"
            )
        raise ConfigValidationError(
            f"logical model {facade_id!r} already exists without compose"
        )
    _assert_id_free_in_plans(out, facade_id)
    _inject_facade_rows(
        out,
        facade_id=facade_id,
        execute_model=execute_model,
    )
    logical[facade_id] = {
        "public_protocols": ["anthropic_messages"],
        "advertised_features": _advertised_for_execute(out, execute_model),
        "compose": {
            "template": "vision",
            "execute_model": execute_model,
            "translate_model": translate_model,
        },
    }
    load_plans_dict(out)
    return out


def compose_vision_update(
    data: Mapping[str, Any],
    *,
    facade_id: str,
    execute_model: str,
    translate_model: str,
    force_preset: bool = False,
) -> dict[str, Any]:
    out = copy.deepcopy(dict(data))
    logical = out.setdefault("logical_models", {})
    if not isinstance(logical, dict):
        raise ConfigValidationError("logical_models must be a mapping")
    entry = logical.get(facade_id)
    if not isinstance(entry, Mapping) or not entry.get("compose"):
        raise ConfigValidationError(
            f"logical model {facade_id!r} is not a vision facade"
        )
    if facade_id == "glm-5.2-vision" and not force_preset:
        raise ConfigValidationError(
            "glm-5.2-vision is a preset; pass force_preset=True to change slots"
        )
    _strip_facade_rows(out, facade_id)
    _inject_facade_rows(
        out,
        facade_id=facade_id,
        execute_model=execute_model,
    )
    logical[facade_id] = {
        **dict(entry),
        "public_protocols": ["anthropic_messages"],
        "advertised_features": _advertised_for_execute(out, execute_model),
        "compose": {
            "template": "vision",
            "execute_model": execute_model,
            "translate_model": translate_model,
        },
    }
    load_plans_dict(out)
    return out


def compose_vision_remove(
    data: Mapping[str, Any],
    *,
    facade_id: str,
    force_preset: bool = False,
) -> dict[str, Any]:
    out = copy.deepcopy(dict(data))
    logical = out.setdefault("logical_models", {})
    if not isinstance(logical, dict):
        raise ConfigValidationError("logical_models must be a mapping")
    entry = logical.get(facade_id)
    if not isinstance(entry, Mapping) or not entry.get("compose"):
        raise ConfigValidationError(
            f"cannot remove {facade_id!r}: not a vision facade"
        )
    if facade_id == "glm-5.2-vision" and not force_preset:
        raise ConfigValidationError(
            "refusing to remove preset glm-5.2-vision without force_preset"
        )
    for name, other in logical.items():
        if name == facade_id or not isinstance(other, Mapping):
            continue
        compose = other.get("compose")
        if not isinstance(compose, Mapping):
            continue
        slots = {
            str(compose.get("execute_model") or ""),
            str(compose.get("translate_model") or ""),
            str(compose.get("reasoning") or ""),
            str(compose.get("vision") or ""),
        }
        if facade_id in slots:
            raise ConfigValidationError(
                f"cannot remove {facade_id!r}: still referenced by {name!r}"
            )
    _strip_facade_rows(out, facade_id)
    del logical[facade_id]
    load_plans_dict(out)
    return out


def list_vision_slot_options(
    doc: PlansDocument,
    *,
    execute_model: str | None = None,
) -> dict[str, list[str]]:
    """Eligible execute / translate logical names (not themselves facades)."""
    compose_ids = {
        mg for mg, lm in doc.logical_models.items() if lm.compose is not None
    }
    execute: list[str] = []
    translate: list[str] = []
    names: set[str] = set()
    for plan in doc.plans:
        for m in plan.models:
            names.add(m.model)
    for name in sorted(names):
        if name in compose_ids:
            continue
        if eligible_routes(
            doc, name, ApiProtocol.ANTHROPIC_MESSAGES, _TEXT
        ):
            execute.append(name)
        if eligible_routes(
            doc, name, ApiProtocol.ANTHROPIC_MESSAGES, _TEXT_IMAGE
        ):
            translate.append(name)
    if execute_model:
        exec_qgs = {
            qg
            for _, qg in eligible_routes(
                doc, execute_model, ApiProtocol.ANTHROPIC_MESSAGES, _TEXT
            )
        }
        translate = [
            name
            for name in translate
            if not (
                {
                    qg
                    for _, qg in eligible_routes(
                        doc, name, ApiProtocol.ANTHROPIC_MESSAGES, _TEXT_IMAGE
                    )
                }
                & exec_qgs
            )
        ]
    return {"execute": execute, "translate": translate}


def _assert_id_free_in_plans(data: dict[str, Any], facade_id: str) -> None:
    for plan in data.get("plans") or []:
        if not isinstance(plan, Mapping):
            continue
        for raw in plan.get("models") or []:
            name = raw if isinstance(raw, str) else (raw or {}).get("model")
            if name == facade_id:
                raise ConfigValidationError(
                    f"model {facade_id!r} already listed in plan "
                    f"{plan.get('id')!r}"
                )


def _strip_facade_rows(data: dict[str, Any], facade_id: str) -> None:
    for plan in data.get("plans") or []:
        if not isinstance(plan, dict):
            continue
        models = plan.get("models")
        if not isinstance(models, list):
            continue
        kept: list[Any] = []
        for raw in models:
            name = raw if isinstance(raw, str) else (raw or {}).get("model")
            if name != facade_id:
                kept.append(raw)
        plan["models"] = kept


def _inject_facade_rows(
    data: dict[str, Any],
    *,
    facade_id: str,
    execute_model: str,
) -> None:
    for plan in data.get("plans") or []:
        if not isinstance(plan, dict):
            continue
        if plan.get("enabled") is False:
            continue
        proto = str(plan.get("upstream_protocol") or "")
        source = _find_model_entry(plan, execute_model)
        if source is None:
            continue
        src_proto = ""
        src_feats: list[str] | None = None
        src_stream = None
        if isinstance(source, Mapping):
            src_proto = str(source.get("upstream_protocol") or "")
            if "supported_features" in source:
                src_feats = [str(x) for x in source.get("supported_features") or []]
            if "supports_streaming" in source:
                src_stream = source.get("supports_streaming")
        row_proto = src_proto or proto
        if row_proto != "anthropic_messages":
            continue
        feats = src_feats
        if feats is None:
            feats = [str(x) for x in plan.get("supported_features") or []]
        feats = [f for f in feats if f != "image"]
        entry: dict[str, Any] = {
            "model": facade_id,
            "facade_role": "vision",
            "upstream_protocol": "anthropic_messages",
            "supported_features": feats,
        }
        streaming = src_stream if src_stream is not None else plan.get("supports_streaming")
        if streaming is not None:
            entry["supports_streaming"] = bool(streaming)
        models = plan.setdefault("models", [])
        if not isinstance(models, list):
            raise ConfigValidationError("plan models must be a list")
        models.append(entry)


def _find_model_entry(plan: Mapping[str, Any], model_name: str) -> Any:
    for raw in plan.get("models") or []:
        name = raw if isinstance(raw, str) else (raw or {}).get("model")
        if name == model_name:
            return raw
    return None


def _advertised_for_execute(data: Mapping[str, Any], execute_model: str) -> list[str]:
    logical = data.get("logical_models") or {}
    entry = logical.get(execute_model) if isinstance(logical, Mapping) else None
    base: list[str] = []
    if isinstance(entry, Mapping) and entry.get("advertised_features"):
        base = [str(x) for x in entry["advertised_features"]]
    else:
        inter: set[str] | None = None
        for plan in data.get("plans") or []:
            if not isinstance(plan, Mapping) or plan.get("enabled") is False:
                continue
            raw = _find_model_entry(plan, execute_model)
            if raw is None:
                continue
            proto = (
                str(raw.get("upstream_protocol") or "")
                if isinstance(raw, Mapping)
                else ""
            ) or str(plan.get("upstream_protocol") or "")
            if proto != "anthropic_messages":
                continue
            if isinstance(raw, Mapping) and "supported_features" in raw:
                feats = {str(x) for x in raw.get("supported_features") or []}
            else:
                feats = {str(x) for x in plan.get("supported_features") or []}
            inter = feats if inter is None else inter & feats
        base = sorted(inter or [])
    if "image" not in base:
        base = [*base, "image"]
    order = ["text", "streaming", "tools", "reasoning", "image"]
    seen = set(base)
    return [f for f in order if f in seen] + sorted(seen - set(order))


def persist_plans_and_apply(
    plans_path: str | Path,
    data: Mapping[str, Any],
    *,
    litellm_path: str | Path,
    backup_dir: str | Path | None = None,
    enable_messages_chat_native: bool = True,
) -> dict[str, Any]:
    """Lock, backup plans, atomic-write plans, apply litellm; rollback plans on apply failure."""
    plans = Path(plans_path)
    litellm = Path(litellm_path)
    bdir = Path(backup_dir) if backup_dir else plans.parent / "backups"
    bdir.mkdir(parents=True, exist_ok=True)
    lock_path = plans.with_name(plans.name + ".lock")
    load_plans_dict(data)
    text = yaml.safe_dump(dict(data), sort_keys=False, allow_unicode=True)
    if not text.endswith("\n"):
        text += "\n"
    original = plans.read_bytes() if plans.is_file() else None
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    plans_backup = bdir / f"plans.yaml.{stamp}.bak"
    with _plans_lock(lock_path):
        if original is not None:
            plans_backup.write_bytes(original)
        _atomic_write_text(plans, text)
        try:
            meta = apply_plans_to_litellm(
                plans,
                litellm,
                backup_dir=bdir,
                enable_messages_chat_native=enable_messages_chat_native,
            )
        except Exception:
            if original is not None:
                plans.write_bytes(original)
            elif plans.is_file():
                plans.unlink()
            raise
    meta["plans_backup"] = str(plans_backup) if original is not None else None
    return meta


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=path.name + ".",
        suffix=".tmp",
        dir=str(path.parent),
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        tmp_path.replace(path)
    except Exception:
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)
        raise


class _plans_lock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._fh: Any = None

    def __enter__(self) -> _plans_lock:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self.path, "a+b")
        if os.name == "nt":
            import msvcrt

            self._fh.seek(0)
            if self._fh.read(1) == b"":
                self._fh.write(b"0")
                self._fh.flush()
            self._fh.seek(0)
            msvcrt.locking(self._fh.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc: object) -> None:
        if self._fh is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                self._fh.seek(0)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
        finally:
            self._fh.close()
            self._fh = None

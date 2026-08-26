"""Workspace root normalization for gateway memory."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Mapping

_ABS_WIN = re.compile(r"^[A-Za-z]:[\\/][^\x00]*$")
_ABS_POSIX = re.compile(r"^/[^\x00]*$")


def _norm_windows_abs(text: str) -> str | None:
    """Canonicalize a Windows absolute path without requiring it to exist.

    The gateway often runs in Linux Docker while clients send ``E:\\repo``.
    ``Path.is_absolute()`` is false for that string on POSIX, so we must not
    use the host filesystem to resolve it.
    """
    raw = text.strip().replace("/", "\\")
    if not re.match(r"^[A-Za-z]:\\", raw):
        return None
    match = re.match(r"^([A-Za-z]):\\(.*)$", raw)
    if match is None:
        return None
    drive = match.group(1).upper()
    parts: list[str] = []
    for piece in match.group(2).split("\\"):
        if piece in {"", "."}:
            continue
        if piece == "..":
            if not parts:
                return None
            parts.pop()
            continue
        parts.append(piece)
    if not parts:
        return None
    return drive + ":\\" + "\\".join(parts)


def _path_parts(norm: str) -> tuple[str, ...]:
    win = _norm_windows_abs(norm)
    if win is not None:
        drive, _, rest = win.partition("\\")
        return (drive, *[p for p in rest.split("\\") if p])
    return Path(norm).parts


def normalize_workspace(raw: str | None) -> str | None:
    if raw is None:
        return None
    text = str(raw).strip()
    if not text or "\x00" in text or "://" in text:
        return None
    if re.match(r"^[A-Za-z]:[\\/]", text):
        return _norm_windows_abs(text)
    try:
        path = Path(text).expanduser()
    except Exception:
        return None
    if not path.is_absolute():
        return None
    try:
        resolved = path.resolve()
    except (OSError, RuntimeError):
        return None
    as_str = str(resolved)
    if ".." in Path(as_str).parts:
        return None
    if not resolved.is_absolute():
        return None
    return as_str


def collect_request_headers(request_kwargs: Mapping[str, Any] | None) -> dict[str, Any]:
    """Merge LiteLLM proxy headers for workspace lookup.

    Messages/Chat kwargs usually have no top-level ``headers``. The proxy
    stores cleaned inbound headers on ``proxy_server_request.headers``.
    """
    out: dict[str, Any] = {}
    if not isinstance(request_kwargs, Mapping):
        return out

    def _absorb(mapping: Mapping[str, Any]) -> None:
        for key, value in mapping.items():
            out[str(key).lower()] = value

    psr = request_kwargs.get("proxy_server_request")
    if isinstance(psr, Mapping):
        nested = psr.get("headers")
        if isinstance(nested, Mapping):
            _absorb(nested)
    top = request_kwargs.get("headers")
    if isinstance(top, Mapping):
        _absorb(top)
    return out


def workspace_from_headers(headers: Mapping[str, Any] | None) -> str | None:
    if not isinstance(headers, Mapping):
        return None
    for key, value in headers.items():
        if str(key).lower() == "x-workspace-root":
            return normalize_workspace(str(value) if value is not None else None)
    return None


def workspace_from_metadata(meta: Mapping[str, Any] | None) -> str | None:
    if not isinstance(meta, Mapping):
        return None
    return normalize_workspace(
        str(meta["workspace_root"]) if meta.get("workspace_root") else None
    )


def _absolute_paths_in_text(text: str) -> list[str]:
    found: list[str] = []
    for token in re.findall(r"[^\s\"'`]+", text):
        if len(token) < 3:
            continue
        if _ABS_WIN.match(token) or _ABS_POSIX.match(token):
            found.append(token)
    return found


def _collect_message_text(messages: Any) -> str:
    chunks: list[str] = []

    def walk(obj: Any) -> None:
        if isinstance(obj, str):
            chunks.append(obj)
            return
        if isinstance(obj, dict):
            for v in obj.values():
                walk(v)
            return
        if isinstance(obj, list):
            for item in obj:
                walk(item)

    walk(messages)
    return "\n".join(chunks)


def infer_workspace_from_messages(messages: Any) -> str | None:
    paths = _absolute_paths_in_text(_collect_message_text(messages))
    resolved: list[str] = []
    for raw in paths:
        norm = normalize_workspace(raw)
        if norm:
            resolved.append(norm)
    if len(resolved) < 2:
        return None
    try:
        common = _path_parts(resolved[0])
        for item in resolved[1:]:
            other = _path_parts(item)
            n = 0
            for a, b in zip(common, other):
                if a != b:
                    break
                n += 1
            common = common[:n]
            if not common:
                return None
    except Exception:
        return None
    if len(common) <= 1:
        return None
    if common[0].endswith(":"):
        return normalize_workspace(common[0] + "\\" + "\\".join(common[1:]))
    return normalize_workspace(str(Path(*common)))


def resolve_workspace(
    *,
    headers: Mapping[str, Any] | None = None,
    metadata: Mapping[str, Any] | None = None,
    messages: Any = None,
) -> str | None:
    trusted = workspace_from_headers(headers)
    if trusted:
        return trusted
    trusted = workspace_from_metadata(metadata)
    if trusted:
        return trusted
    return infer_workspace_from_messages(messages)

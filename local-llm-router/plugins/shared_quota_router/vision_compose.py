"""Vision compose stage: hash, cache, translate, replace image blocks.

Live MiniMax HTTP uses a nested select + httpx. Tests may inject a fake
translator or ``select_deployment`` / ``http_post`` on the envelope.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import threading
import time
from typing import Any, Awaitable, Callable

from shared_quota_router.anthropic_direct import (
    anthropic_headers,
    extract_text_from_messages_response,
    httpx_post_json,
    messages_url,
    resolve_env_ref,
    upstream_model_name,
)
from shared_quota_router.composed_vision import defers_image_gate, messages_have_image
from shared_quota_router.feature_flags import is_vision_compose_enabled
from shared_quota_router.internal_call import assert_quota_exclusive, child_request_id
from shared_quota_router.metrics import inc
from shared_quota_router.models import ApiProtocol, LogicalModelProtocols
from shared_quota_router.pipeline import EnhanceEnvelope
from shared_quota_router.protocol_errors import (
    ProtocolAwareRoutingError,
    ProtocolRoutingReason,
)
from shared_quota_router.vision_cache import SCHEMA_VER, get_cached, put_cached
from shared_quota_router.vision_ir import validate_visual_evidence

logger = logging.getLogger(__name__)

Translator = Callable[[bytes], Awaitable[str]]

_IMAGE_TYPES = frozenset({"image", "image_url"})

_PREFIX = (
    "[gateway visual translation — not repository source; do not write this as a new file]\n"
)

TRANSLATE_SYSTEM = """You translate coding-agent screenshots into a structured working memory for a text-only model.
Output ONLY one XML fragment. Root element must be <visual-evidence>.
Do not output <html>, <script>, markdown fences, or a solution to the user's problem.
Choose a carrier:
- terminal / traceback / logs → <pre> with exact visible text
- editor / code → <pre><code> and data-file if a path is visible
- IDE / browser / settings UI → semantic HTML sketch, almost no CSS
- table → <table>
If the image is a landscape, portrait, handwriting, or too blurry to read, output:
<visual-evidence data-reject="out-of-scope"></visual-evidence>
Unreadable glyphs: wrap in <span data-uncertain="1">...</span>. Do not guess.
"""

MAX_IMAGES = 6
MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_TOTAL_IMAGE_BYTES = 12 * 1024 * 1024
MAX_IR_TOKENS = 4000
CIRCUIT_FAILURES = 3
CIRCUIT_WINDOW_S = 60.0

_fail_streak = 0
_open_until = 0.0
_circuit_lock = threading.Lock()


def reset_circuit_for_tests() -> None:
    global _fail_streak, _open_until
    with _circuit_lock:
        _fail_streak = 0
        _open_until = 0.0


def _circuit_open() -> bool:
    with _circuit_lock:
        return time.monotonic() < _open_until


def _note_success() -> None:
    global _fail_streak, _open_until
    with _circuit_lock:
        _fail_streak = 0
        _open_until = 0.0


def _note_failure() -> None:
    global _fail_streak, _open_until
    with _circuit_lock:
        _fail_streak += 1
        if _fail_streak >= CIRCUIT_FAILURES:
            _open_until = time.monotonic() + CIRCUIT_WINDOW_S


def _raise_if_circuit_open() -> None:
    if not _circuit_open():
        return
    inc("enhance_vision_circuit_open")
    raise ProtocolAwareRoutingError(
        "vision composer temporarily unavailable",
        reason=ProtocolRoutingReason.FEATURE_UNSUPPORTED,
        protocol=ApiProtocol.ANTHROPIC_MESSAGES,
        details={"vision": "circuit_open"},
    )


def _unsupported(message: str, **details: Any) -> ProtocolAwareRoutingError:
    return ProtocolAwareRoutingError(
        message,
        reason=ProtocolRoutingReason.FEATURE_UNSUPPORTED,
        protocol=ApiProtocol.ANTHROPIC_MESSAGES,
        details=details,
    )


def _image_bytes(block: dict[str, Any]) -> bytes:
    source = block.get("source")
    data = None
    if isinstance(source, dict):
        data = source.get("data")
    if data is None:
        data = block.get("data") or block.get("url")
    if not isinstance(data, str) or not data.strip():
        raise _unsupported(
            "composed model image block is missing data",
            vision="missing_bytes",
        )
    payload = data.strip()
    if "base64," in payload:
        payload = payload.split("base64,", 1)[1]
    try:
        return base64.b64decode(payload, validate=False)
    except Exception as exc:
        raise _unsupported(
            "composed model image block is not valid base64",
            vision="bad_base64",
        ) from exc


def _text_block(ir: str) -> dict[str, str]:
    body = ir.strip()
    if "<visual-evidence" not in body:
        raise _unsupported(
            "vision translator returned no visual-evidence",
            vision="empty_ir",
        )
    return {"type": "text", "text": _PREFIX + body}


def _counts_toward_circuit(exc: ProtocolAwareRoutingError) -> bool:
    details = exc.details or {}
    if details.get("vision_limit"):
        return False
    if details.get("vision") in {"no_translator", "circuit_open"}:
        return False
    return True


async def _translate_one(png: bytes, translator: Translator | None) -> str:
    digest = hashlib.sha256(png).hexdigest()
    hit = get_cached(digest, schema_ver=SCHEMA_VER)
    if hit is not None:
        inc("enhance_vision_cache_hit")
        return hit
    _raise_if_circuit_open()
    if translator is None:
        raise _unsupported(
            "composed model still has image blocks; vision translator is not mounted",
            composed_peel="disabled",
            vision="no_translator",
        )
    try:
        ir = await translator(png)
        fragment = validate_visual_evidence(ir if isinstance(ir, str) else "")
        if max(1, len(fragment) // 4) > MAX_IR_TOKENS:
            raise _unsupported(
                "vision IR exceeds token budget",
                vision_limit="tokens",
            )
        put_cached(digest, fragment, schema_ver=SCHEMA_VER)
        _note_success()
        inc("enhance_vision_ok")
        return fragment
    except ProtocolAwareRoutingError as exc:
        if _counts_toward_circuit(exc):
            _note_failure()
            inc("enhance_vision_fail")
        raise
    except Exception as exc:
        _note_failure()
        inc("enhance_vision_fail")
        raise _unsupported(
            "vision translator failed",
            vision="upstream",
        ) from exc


async def _rewrite_content_list(
    blocks: list[Any],
    translator: Translator | None,
    evidence: list[str],
) -> None:
    for i, block in enumerate(blocks):
        if not isinstance(block, dict):
            continue
        btype = str(block.get("type") or "")
        if btype in _IMAGE_TYPES:
            png = _image_bytes(block)
            ir = await _translate_one(png, translator)
            evidence.append(ir)
            blocks[i] = _text_block(ir)
            continue
        if btype == "tool_result":
            inner = block.get("content")
            if isinstance(inner, list):
                await _rewrite_content_list(inner, translator, evidence)


def _iter_image_blocks(content: Any) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    if not isinstance(content, list):
        return found
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = str(block.get("type") or "")
        if btype in _IMAGE_TYPES:
            found.append(block)
        elif btype == "tool_result":
            found.extend(_iter_image_blocks(block.get("content")))
    return found


def assert_vision_limits(messages: Any) -> None:
    blocks: list[dict[str, Any]] = []
    if isinstance(messages, list):
        for msg in messages:
            if isinstance(msg, dict):
                blocks.extend(_iter_image_blocks(msg.get("content")))
    if len(blocks) > MAX_IMAGES:
        raise _unsupported(
            f"too many images ({len(blocks)} > {MAX_IMAGES})",
            vision_limit="images",
        )
    total = 0
    for block in blocks:
        n = len(_image_bytes(block))
        if n > MAX_IMAGE_BYTES:
            raise _unsupported(
                "image exceeds 5 MiB",
                vision_limit="bytes",
            )
        total += n
    if total > MAX_TOTAL_IMAGE_BYTES:
        raise _unsupported(
            "images exceed 12 MiB total",
            vision_limit="bytes",
        )


async def _rewrite_images_in_messages(
    messages: Any,
    translator: Translator | None,
) -> list[str]:
    evidence: list[str] = []
    if not isinstance(messages, list):
        return evidence
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if isinstance(content, list):
            await _rewrite_content_list(content, translator, evidence)
    return evidence


class _MiniMaxTranslator:
    def __init__(self, env: EnhanceEnvelope, translate_model: str) -> None:
        self.env = env
        self.translate_model = translate_model

    async def __call__(self, png: bytes) -> str:
        digest = hashlib.sha256(png).hexdigest()
        child_id = child_request_id(self.env.parent_request_id, "vision", digest)
        child_kwargs = {
            "litellm_call_id": child_id,
            "messages": [{"role": "user", "content": "Translate this screenshot."}],
            "litellm_metadata": {
                "protocol": ApiProtocol.ANTHROPIC_MESSAGES.value,
                "internal_call": True,
                "internal_kind": "vision",
            },
            "metadata": {
                "protocol": ApiProtocol.ANTHROPIC_MESSAGES.value,
                "internal_call": True,
                "internal_kind": "vision",
            },
        }
        try:
            entry = self.env.select_deployment(
                self.translate_model,
                messages=child_kwargs["messages"],
                request_kwargs=child_kwargs,
            )
        except ProtocolAwareRoutingError:
            raise
        except Exception as exc:
            raise _unsupported(
                "vision translate model has no available deployment",
                vision="upstream",
            ) from exc
        if not isinstance(entry, dict):
            raise _unsupported(
                "vision translate select returned no deployment",
                vision="upstream",
            )
        info = entry.get("model_info") if isinstance(entry.get("model_info"), dict) else {}
        child_qg = str(info.get("quota_group_id") or "")
        try:
            assert_quota_exclusive(
                self.env.parent_quota_group_id,
                child_qg,
                protocol=ApiProtocol.ANTHROPIC_MESSAGES,
                model_group=self.env.model_group,
            )
            params = entry.get("litellm_params") if isinstance(entry.get("litellm_params"), dict) else {}
            api_base = resolve_env_ref(params.get("api_base"))
            api_key = resolve_env_ref(params.get("api_key"))
            if not api_base or not api_key:
                raise _unsupported(
                    "vision translate deployment is missing api_base or api_key",
                    vision="upstream",
                )
            model = upstream_model_name(params.get("model") or self.translate_model)
            url = messages_url(api_base)
            body = {
                "model": model,
                "max_tokens": 4096,
                "temperature": 0,
                "system": TRANSLATE_SYSTEM,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": "image/png",
                                    "data": base64.b64encode(png).decode("ascii"),
                                },
                            },
                            {"type": "text", "text": "Translate this screenshot."},
                        ],
                    }
                ],
            }
            post = self.env.http_post or httpx_post_json
            try:
                resp = await post(
                    url,
                    headers=anthropic_headers(api_key),
                    json=body,
                    timeout=60.0,
                )
            except ProtocolAwareRoutingError:
                raise
            except Exception as exc:
                logger.warning(
                    "vision translate http failed type=%s",
                    type(exc).__name__,
                )
                raise _unsupported(
                    "vision translate upstream request failed",
                    vision="timeout" if "timeout" in str(exc).lower() else "upstream",
                ) from exc
            status = int(getattr(resp, "status_code", 200) or 200)
            if status >= 400:
                raise _unsupported(
                    "vision translate upstream returned an error",
                    vision="upstream",
                )
            raise_for_status = getattr(resp, "raise_for_status", None)
            if callable(raise_for_status):
                try:
                    raise_for_status()
                except Exception as exc:
                    raise _unsupported(
                        "vision translate upstream returned an error",
                        vision="upstream",
                    ) from exc
            payload = resp.json() if hasattr(resp, "json") else {}
            if callable(payload):
                payload = payload()
            text = extract_text_from_messages_response(payload)
            if not text.strip():
                raise _unsupported(
                    "vision translator returned empty IR",
                    vision="empty_ir",
                )
            return text
        finally:
            release = self.env.release_lease
            if callable(release) and child_qg:
                try:
                    release(child_qg, child_id)
                except Exception as exc:  # noqa: BLE001 — must not leak MiniMax lease
                    logger.warning("vision translate lease release failed: %s", exc)


def _resolve_logical(model_group: str) -> LogicalModelProtocols | None:
    try:
        from shared_quota_router.logical_policy import resolve_runtime_logical_models

        return resolve_runtime_logical_models().get(model_group)
    except Exception:  # noqa: BLE001 — tests may have no plans file
        return None


def _translate_model_name(env: EnhanceEnvelope, logical: LogicalModelProtocols | None) -> str:
    if logical is not None and logical.compose is not None:
        return logical.compose.translate_model
    return "MiniMax-M3"


class VisionComposeStage:
    name = "vision"

    def enabled(self) -> bool:
        return is_vision_compose_enabled()

    async def run(self, env: EnhanceEnvelope) -> None:
        logical = _resolve_logical(env.model_group)
        composed = defers_image_gate(env.model_group, logical)
        if not composed and env.translator is None:
            return
        if not messages_have_image(env.messages):
            if composed:
                inc("vision_translate_skipped")
            return
        assert_vision_limits(env.messages)
        translator: Translator | None = env.translator
        if translator is None:
            if env.select_deployment is None:
                translator = None
            else:
                translator = _MiniMaxTranslator(env, _translate_model_name(env, logical))
        evidence = await _rewrite_images_in_messages(env.messages, translator)
        env.visual_evidence.extend(evidence)
        if messages_have_image(env.messages):
            raise ProtocolAwareRoutingError(
                f"composed model {env.model_group!r} still has image blocks after vision stage",
                reason=ProtocolRoutingReason.FEATURE_UNSUPPORTED,
                protocol=env.protocol or ApiProtocol.ANTHROPIC_MESSAGES,
                model_group=env.model_group,
                details={"vision": "residual_image"},
            )
        logger.info(
            "enhance_vision model=%s outbound_has_image=false evidence=%s",
            env.model_group,
            len(env.visual_evidence),
        )

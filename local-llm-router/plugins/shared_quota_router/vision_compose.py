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
from shared_quota_router.internal_call import (
    child_request_id,
    report_internal_outcome,
    select_internal_deployment,
)
from shared_quota_router.metrics import inc
from shared_quota_router.models import ApiProtocol, Feature, LogicalModelProtocols
from shared_quota_router.pipeline import EnhanceEnvelope
from shared_quota_router.protocol_errors import (
    ProtocolAwareRoutingError,
    ProtocolRoutingReason,
)
from shared_quota_router.vision_agents.types import ImageRef
from shared_quota_router.vision_cache import SCHEMA_VER, get_cached, put_cached
from shared_quota_router.vision_ir import allowed_ir_tag_list, validate_visual_evidence

logger = logging.getLogger(__name__)

Translator = Callable[[bytes, str], Awaitable[str]]

_IMAGE_TYPES = frozenset({"image", "image_url"})


_PREFIX = (
    "[gateway visual translation — not repository source; do not write this as a new file]\n"
)

TRANSLATE_SYSTEM = """You translate coding-agent screenshots into structured working memory for a text-only model.
The user message includes the screenshot plus that model's current task and recent conversation.
Use the task and context to decide what to transcribe faithfully and what to keep brief.
Output ONLY one XML fragment. Root element must be <visual-evidence>.
Do not output <html>, <script>, markdown fences, or a solution to the user's problem.
Do not answer the task. Extract visual evidence the text-only model will need.
Choose a carrier:
- terminal / traceback / logs → <pre> with exact visible text
- editor / code → <pre><code> and data-file if a path is visible
- IDE / browser / settings UI → a sketch using ONLY the tags below (div/p/span/ul)
- table → <table>
Allowed tags ONLY: visual-evidence, {allowed_tags}.
Map chrome (status bar, tab bar, address bar, buttons) to <div>, <p>, or <span>.
Never invent tag names such as status-bar, header, nav, or button.
If the image is a landscape, portrait, handwriting, or too blurry to read, output:
<visual-evidence data-reject="out-of-scope"></visual-evidence>
Unreadable glyphs: wrap in <span data-uncertain="1">...</span>. Do not guess.
""".format(allowed_tags=allowed_ir_tag_list())

MAX_IMAGES = 6
MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_TOTAL_IMAGE_BYTES = 12 * 1024 * 1024
MAX_IR_TOKENS = 4000
MAX_TASK_CHARS = 1500
MAX_CONTEXT_CHARS = 2000
CIRCUIT_FAILURES = 3
CIRCUIT_WINDOW_S = 60.0
PARENT_LEASE_TTL = 510

_circuits: dict[tuple[str, str], list[float]] = {}
_circuit_lock = threading.Lock()


def reset_circuit_for_tests() -> None:
    with _circuit_lock:
        _circuits.clear()


def _bucket(translate_model: str, quota_group_id: str) -> tuple[str, str]:
    return (translate_model or "injected", quota_group_id or "")


def _circuit_open(translate_model: str, quota_group_id: str) -> bool:
    key = _bucket(translate_model, quota_group_id)
    with _circuit_lock:
        until = float((_circuits.get(key) or [0.0, 0.0])[1])
        return time.monotonic() < until


def _note_success(translate_model: str, quota_group_id: str) -> None:
    key = _bucket(translate_model, quota_group_id)
    with _circuit_lock:
        _circuits[key] = [0.0, 0.0]


def _note_failure(translate_model: str, quota_group_id: str) -> None:
    key = _bucket(translate_model, quota_group_id)
    with _circuit_lock:
        streak, _until = _circuits.get(key) or [0.0, 0.0]
        streak = float(streak) + 1.0
        until = 0.0
        if streak >= CIRCUIT_FAILURES:
            until = time.monotonic() + CIRCUIT_WINDOW_S
        _circuits[key] = [streak, until]


def _raise_if_circuit_open(translate_model: str, quota_group_id: str) -> None:
    if not _circuit_open(translate_model, quota_group_id):
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
    if details.get("vision") in {"no_translator", "circuit_open", "rejected_scope"}:
        return False
    return True


def _content_plain_texts(content: Any) -> list[str]:
    if isinstance(content, str) and content.strip():
        return [content.strip()]
    if not isinstance(content, list):
        return []
    out: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = str(block.get("type") or "")
        if btype in _IMAGE_TYPES:
            continue
        if btype in {"text", ""}:
            text = str(block.get("text") or "").strip()
            if text:
                out.append(text)
            continue
        if btype == "tool_result":
            out.extend(_content_plain_texts(block.get("content")))
    return out


def _human_plain_texts(content: Any) -> list[str]:
    """User-visible text that is not a tool_result dump."""
    if isinstance(content, str) and content.strip():
        return [content.strip()]
    if not isinstance(content, list):
        return []
    out: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = str(block.get("type") or "")
        if btype in _IMAGE_TYPES:
            continue
        if btype in {"text", ""}:
            text = str(block.get("text") or "").strip()
            if text:
                out.append(text)
    return out


def guide_text_from_messages(messages: Any) -> str:
    """User words + prior turns for MiniMax. Never includes image bytes."""
    if not isinstance(messages, list):
        return ""
    turns: list[tuple[str, str]] = []
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        role = str(msg.get("role") or "user").strip() or "user"
        text = " ".join(_content_plain_texts(msg.get("content"))).strip()
        if text:
            turns.append((role, text))
    if not turns:
        return ""
    from shared_quota_router.memory_extract import redact

    last_user = max((i for i, (role, _) in enumerate(turns) if role == "user"), default=None)
    if last_user is None:
        task = ""
        context_turns = turns
    else:
        task = turns[last_user][1]
        context_turns = turns[:last_user]
    task = redact(task)[:MAX_TASK_CHARS]
    ctx = redact("\n".join(f"{role}: {text}" for role, text in context_turns))
    if len(ctx) > MAX_CONTEXT_CHARS:
        ctx = ctx[-MAX_CONTEXT_CHARS:]
    parts: list[str] = []
    if task:
        parts.append(f"task:\n{task}")
    if ctx:
        parts.append(f"context:\n{ctx}")
    return "\n\n".join(parts)


def _list_at(messages: list[Any], ref: ImageRef) -> list[Any]:
    msg = messages[ref.message_index]
    if not isinstance(msg, dict):
        return []
    node = msg.get("content")
    if not isinstance(node, list):
        return []
    for index in ref.path[:-1]:
        if index < 0 or index >= len(node) or not isinstance(node[index], dict):
            return []
        inner = node[index].get("content")
        if not isinstance(inner, list):
            return []
        node = inner
    return node


def _replace_block(messages: list[Any], ref: ImageRef, new_block: dict[str, Any]) -> None:
    node = _list_at(messages, ref)
    if not node or ref.path[-1] < 0 or ref.path[-1] >= len(node):
        raise _unsupported(
            "composed model image block path is invalid",
            vision="missing_bytes",
        )
    node[ref.path[-1]] = new_block


def collect_image_refs(messages: Any) -> list[tuple[ImageRef, dict[str, Any]]]:
    jobs: list[tuple[ImageRef, dict[str, Any]]] = []
    if not isinstance(messages, list):
        return jobs

    def walk(blocks: list[Any], message_index: int, path: tuple[int, ...]) -> None:
        for i, block in enumerate(blocks):
            if not isinstance(block, dict):
                continue
            btype = str(block.get("type") or "")
            here = path + (i,)
            if btype in _IMAGE_TYPES:
                jobs.append(
                    (
                        ImageRef(
                            ordinal=len(jobs),
                            message_index=message_index,
                            path=here,
                        ),
                        block,
                    )
                )
                continue
            if btype == "tool_result":
                inner = block.get("content")
                if isinstance(inner, list):
                    walk(inner, message_index, here)

    for mi, msg in enumerate(messages):
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if isinstance(content, list):
            walk(content, mi, ())
    return jobs


def guide_for_image(messages: Any, image_ref: ImageRef) -> str:
    """Per-image task/context from an unmodified snapshot. Never includes image bytes."""
    from shared_quota_router.vision_agents.generic import GenericPreset

    return GenericPreset().extract_guide(messages, image_ref)


def vision_cache_digest(
    png: bytes,
    guide: str = "",
    *,
    agent_id: str = "generic",
    prompt_rev: int = 1,
    translate_model: str = "",
    compose_rev: str = "",
) -> str:
    digest = hashlib.sha256()
    digest.update(png)
    digest.update(b"\0agent\0")
    digest.update((agent_id or "generic").encode("utf-8"))
    digest.update(b"\0rev\0")
    digest.update(str(int(prompt_rev)).encode("ascii"))
    blob = (guide or "").strip().encode("utf-8")
    if blob:
        digest.update(b"\0guide\0")
        digest.update(blob)
    digest.update(b"\0translator\0")
    digest.update((translate_model or "").encode("utf-8"))
    digest.update(b"\0compose\0")
    digest.update((compose_rev or "").encode("utf-8"))
    return digest.hexdigest()


def _translate_user_text(guide: str) -> str:
    body = (guide or "").strip()
    if not body:
        return (
            "Translate this screenshot into one <visual-evidence> fragment "
            "for a text-only coding model. Do not answer any task."
        )
    return (
        "Task and conversation for the text-only coding model "
        "(extract visual evidence it will need; do not answer the task):\n"
        f"{body}\n\n"
        "Translate the screenshot into one <visual-evidence> fragment. "
        "Prefer exact visible errors, commands, paths, and UI labels the task needs."
    )


def _circuit_key_for(translator: Translator | None) -> tuple[str, str]:
    if translator is not None and getattr(translator, "translate_model", None):
        return (
            str(getattr(translator, "translate_model")),
            str(getattr(translator, "last_qg", "") or ""),
        )
    return ("injected", "")


async def _translate_one(
    png: bytes,
    translator: Translator | None,
    *,
    guide: str = "",
    agent_id: str = "generic",
    prompt_rev: int = 1,
    translate_model: str = "",
    compose_rev: str = "",
) -> str:
    digest = vision_cache_digest(
        png,
        guide,
        agent_id=agent_id,
        prompt_rev=prompt_rev,
        translate_model=translate_model,
        compose_rev=compose_rev,
    )
    hit = get_cached(digest, schema_ver=SCHEMA_VER)
    if hit is not None:
        inc("enhance_vision_cache_hit")
        return hit
    http_translator = bool(translator is not None and getattr(translator, "translate_model", None))
    if not http_translator:
        _raise_if_circuit_open("injected", "")
    if translator is None:
        raise _unsupported(
            "composed model still has image blocks; vision translator is not mounted",
            composed_peel="disabled",
            vision="no_translator",
        )
    try:
        ir = await translator(png, guide)
        fragment = validate_visual_evidence(ir if isinstance(ir, str) else "")
        if max(1, len(fragment) // 4) > MAX_IR_TOKENS:
            raise _unsupported(
                "vision IR exceeds token budget",
                vision_limit="tokens",
            )
        put_cached(digest, fragment, schema_ver=SCHEMA_VER)
        model, qg = _circuit_key_for(translator)
        _note_success(model, qg)
        inc("enhance_vision_ok")
        return fragment
    except ProtocolAwareRoutingError as exc:
        if _counts_toward_circuit(exc):
            model, qg = _circuit_key_for(translator)
            _note_failure(model, qg)
            inc("enhance_vision_fail")
        raise
    except Exception as exc:
        model, qg = _circuit_key_for(translator)
        _note_failure(model, qg)
        inc("enhance_vision_fail")
        raise _unsupported(
            "vision translator failed",
            vision="upstream",
        ) from exc


async def _rewrite_images_in_messages(
    messages: Any,
    translator: Translator | None,
    *,
    agent_id: str = "generic",
    prompt_rev: int = 1,
    extract_guide: Callable[[Any, ImageRef], str] | None = None,
    translate_model: str = "",
    compose_rev: str = "",
    after_image: Callable[[], None] | None = None,
) -> list[str]:
    evidence: list[str] = []
    if not isinstance(messages, list):
        return evidence
    extract = extract_guide or guide_for_image
    planned: list[tuple[ImageRef, bytes, str]] = []
    for ref, block in collect_image_refs(messages):
        planned.append((ref, _image_bytes(block), extract(messages, ref)))
    for ref, png, guide in planned:
        ir = await _translate_one(
            png,
            translator,
            guide=guide,
            agent_id=agent_id,
            prompt_rev=prompt_rev,
            translate_model=translate_model,
            compose_rev=compose_rev,
        )
        evidence.append(ir)
        _replace_block(messages, ref, _text_block(ir))
        if callable(after_image):
            try:
                after_image()
            except Exception as exc:  # noqa: BLE001 — renew must not fail the parent
                logger.warning("parent lease renew failed: %s", exc)
    return evidence


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


class _MiniMaxTranslator:
    def __init__(
        self,
        env: EnhanceEnvelope,
        translate_model: str,
        *,
        system: str,
    ) -> None:
        self.env = env
        self.translate_model = translate_model
        self.system = system
        self.last_qg = ""

    def _report(
        self,
        kwargs: dict[str, Any],
        *,
        success: bool,
        status_code: int | None = None,
        exception: BaseException | None = None,
    ) -> None:
        fn = self.env.report_outcome
        if callable(fn):
            fn(
                kwargs,
                success=success,
                status_code=status_code,
                exception=exception,
            )
            return
        report_internal_outcome(
            kwargs,
            success=success,
            status_code=status_code,
            exception=exception,
        )

    async def __call__(self, png: bytes, guide: str = "") -> str:
        digest = hashlib.sha256(png).hexdigest()
        child_id = child_request_id(self.env.parent_request_id, "vision", digest)
        child_qg = ""
        try:
            entry = select_internal_deployment(
                self.translate_model,
                select=self.env.select_deployment,
                protocol=ApiProtocol.ANTHROPIC_MESSAGES,
                required_features=frozenset({Feature.TEXT, Feature.IMAGE}),
                parent_request_id=self.env.parent_request_id,
                parent_quota_group_id=self.env.parent_quota_group_id,
                child_id=child_id,
            )
        except ProtocolAwareRoutingError:
            raise
        except Exception as exc:
            raise _unsupported(
                "vision translate model has no available deployment",
                vision="upstream",
            ) from exc
        info = entry.get("model_info") if isinstance(entry.get("model_info"), dict) else {}
        child_qg = str(info.get("quota_group_id") or "")
        self.last_qg = child_qg
        _raise_if_circuit_open(self.translate_model, child_qg)
        params = entry.get("litellm_params") if isinstance(entry.get("litellm_params"), dict) else {}
        report_kwargs = {
            "litellm_call_id": child_id,
            "model_info": info,
            "litellm_params": {**params, "model_info": info},
            "_sq_nested_child": True,
        }
        try:
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
                "system": self.system,
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
                            {"type": "text", "text": _translate_user_text(guide)},
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
                self._report(report_kwargs, success=False, exception=exc)
                raise _unsupported(
                    "vision translate upstream request failed",
                    vision="timeout" if "timeout" in str(exc).lower() else "upstream",
                ) from exc
            status = int(getattr(resp, "status_code", 200) or 200)
            if status >= 400:
                self._report(report_kwargs, success=False, status_code=status)
                raise _unsupported(
                    "vision translate upstream returned an error",
                    vision="upstream",
                )
            raise_for_status = getattr(resp, "raise_for_status", None)
            if callable(raise_for_status):
                try:
                    raise_for_status()
                except Exception as exc:
                    self._report(report_kwargs, success=False, status_code=status)
                    raise _unsupported(
                        "vision translate upstream returned an error",
                        vision="upstream",
                    ) from exc
            payload = resp.json() if hasattr(resp, "json") else {}
            if callable(payload):
                payload = payload()
            text = extract_text_from_messages_response(payload)
            if not text.strip():
                self._report(report_kwargs, success=True, status_code=status)
                raise _unsupported(
                    "vision translator returned empty IR",
                    vision="empty_ir",
                )
            self._report(report_kwargs, success=True, status_code=status)
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


def _compose_rev(logical: LogicalModelProtocols | None, translate_model: str) -> str:
    if logical is not None and logical.compose is not None:
        recipe = logical.compose
        tmpl = (recipe.template or "vision").strip() or "vision"
        return f"{tmpl}:{recipe.execute_model}:{recipe.translate_model}"
    return f"vision::{translate_model}"


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
        from shared_quota_router.vision_agents.capture import maybe_write_capture
        from shared_quota_router.vision_agents.detect import clamp_addendum, resolve_preset
        from shared_quota_router.vision_agents.generic import GenericPreset

        maybe_write_capture(env.headers, env.messages)

        preset, match = resolve_preset(env.headers, env.messages)
        preset, match, addendum = clamp_addendum(preset, match)
        system = TRANSLATE_SYSTEM + (f"\n{addendum}" if addendum else "")

        def _extract(messages: Any, ref: ImageRef) -> str:
            return preset.extract_guide(messages, ref)

        try:
            extract_fn: Callable[[Any, ImageRef], str] = _extract
            # Probe extract once per image up front so a crash falls back before HTTP.
            for ref, _block in collect_image_refs(env.messages):
                extract_fn(env.messages, ref)
        except Exception as exc:  # noqa: BLE001 — extractor bugs must not fail-close
            logger.warning(
                "enhance_vision extract_error agent=%s type=%s",
                preset.id,
                type(exc).__name__,
            )
            preset = GenericPreset()
            match = "extract_error"
            addendum = ""
            system = TRANSLATE_SYSTEM
            extract_fn = preset.extract_guide

        inc("enhance_vision_agent", agent_id=preset.id, match=match)
        tmodel = _translate_model_name(env, logical)
        crev = _compose_rev(logical, tmodel)
        translator: Translator | None = env.translator
        if translator is None:
            if env.select_deployment is None:
                translator = None
            else:
                translator = _MiniMaxTranslator(
                    env,
                    tmodel,
                    system=system,
                )

        def _after_image() -> None:
            renew = env.renew_lease
            if not callable(renew) or not env.parent_quota_group_id or not env.parent_request_id:
                return
            renew(env.parent_quota_group_id, env.parent_request_id, PARENT_LEASE_TTL)

        evidence = await _rewrite_images_in_messages(
            env.messages,
            translator,
            agent_id=preset.id,
            prompt_rev=preset.prompt_rev,
            extract_guide=extract_fn,
            translate_model=tmodel,
            compose_rev=crev,
            after_image=_after_image,
        )
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
            "enhance_vision model=%s agent=%s match=%s outbound_has_image=false evidence=%s",
            env.model_group,
            preset.id,
            match,
            len(env.visual_evidence),
        )

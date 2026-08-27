"""Whitelist validator for ``<visual-evidence>`` fragments."""

from __future__ import annotations

import html
import re
from html.parser import HTMLParser

from shared_quota_router.models import ApiProtocol
from shared_quota_router.protocol_errors import (
    ProtocolAwareRoutingError,
    ProtocolRoutingReason,
)

_ALLOWED_TAGS = frozenset(
    {
        "visual-evidence",
        "pre",
        "code",
        "table",
        "thead",
        "tbody",
        "tr",
        "th",
        "td",
        "p",
        "ul",
        "ol",
        "li",
        "span",
        "div",
        "strong",
        "em",
        "br",
    }
)
_ALLOWED_ATTRS = frozenset({"data-uncertain", "data-file", "data-reject"})
_DANGEROUS_TAGS = frozenset(
    {
        "html",
        "head",
        "body",
        "script",
        "iframe",
        "object",
        "embed",
        "link",
        "style",
        "svg",
        "math",
        "form",
        "meta",
        "base",
        "applet",
        "frame",
        "frameset",
        "template",
        "noscript",
    }
)
_FRAGMENT_RE = re.compile(
    r"<visual-evidence\b[^>]*>.*</visual-evidence>",
    re.IGNORECASE | re.DOTALL,
)
_UNCERTAIN_THRESHOLD = 0.8


def allowed_ir_tag_list() -> str:
    """Comma-separated child tags for the MiniMax system prompt."""
    return ", ".join(sorted(tag for tag in _ALLOWED_TAGS if tag != "visual-evidence"))


class _IRParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tags: list[str] = []
        self.bad_tag: str | None = None
        self.bad_attr: str | None = None
        self.text_chars = 0
        self.uncertain_chars = 0
        self._uncertain_depth = 0
        self.root_reject = False
        self._depth = 0
        self.saw_html = False
        self.saw_script = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in {"html", "head", "body"}:
            self.saw_html = True
        if tag == "script":
            self.saw_script = True
        if tag not in _ALLOWED_TAGS:
            self.bad_tag = tag
        names = {k.lower() for k, _v in attrs}
        extra = names - _ALLOWED_ATTRS
        if extra:
            self.bad_attr = sorted(extra)[0]
        attr_map = {k.lower(): (v or "") for k, v in attrs}
        if tag == "visual-evidence" and self._depth == 0:
            if attr_map.get("data-reject"):
                self.root_reject = True
        if attr_map.get("data-uncertain"):
            self._uncertain_depth += 1
        href = attr_map.get("href", "")
        if href.lower().startswith("javascript:"):
            self.bad_attr = "href"
        self._depth += 1
        self.tags.append(tag)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if self._depth:
            self._depth -= 1
        # naive: decrement uncertain if this endtag might close one
        if tag == "span" and self._uncertain_depth:
            self._uncertain_depth = max(0, self._uncertain_depth - 1)

    def handle_data(self, data: str) -> None:
        visible = data
        n = len(visible)
        if n == 0:
            return
        self.text_chars += n
        if self._uncertain_depth:
            self.uncertain_chars += n


class _CoerceParser(HTMLParser):
    """Rewrite unknown safe tags to ``div`` and drop unknown attributes."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.dangerous: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._emit_start(tag, attrs, self_closing=False)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._emit_start(tag, attrs, self_closing=True)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in _DANGEROUS_TAGS:
            self.dangerous = tag
            return
        mapped = tag if tag in _ALLOWED_TAGS else "div"
        self.parts.append(f"</{mapped}>")

    def handle_data(self, data: str) -> None:
        self.parts.append(html.escape(data, quote=False))

    def _emit_start(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
        *,
        self_closing: bool,
    ) -> None:
        tag = tag.lower()
        if tag in _DANGEROUS_TAGS:
            self.dangerous = tag
            return
        mapped = tag if tag in _ALLOWED_TAGS else "div"
        kept: list[str] = []
        for key, value in attrs:
            lower = key.lower()
            raw_val = value or ""
            if lower.startswith("on") or raw_val.lower().startswith("javascript:"):
                self.dangerous = lower
                return
            if lower not in _ALLOWED_ATTRS:
                continue
            if value is None:
                kept.append(lower)
            else:
                kept.append(f'{lower}="{html.escape(value, quote=True)}"')
        attr_s = (" " + " ".join(kept)) if kept else ""
        if self_closing and mapped == "br":
            self.parts.append(f"<{mapped}{attr_s}>")
            return
        self.parts.append(f"<{mapped}{attr_s}>")
        if self_closing:
            self.parts.append(f"</{mapped}>")


def _feed_parser(fragment: str) -> _IRParser:
    parser = _IRParser()
    parser.feed(fragment)
    parser.close()
    return parser


def _coerce_unknown_tags(fragment: str) -> str:
    parser = _CoerceParser()
    parser.feed(fragment)
    parser.close()
    if parser.dangerous:
        raise ProtocolAwareRoutingError(
            "vision IR contains forbidden tags",
            reason=ProtocolRoutingReason.FEATURE_UNSUPPORTED,
            protocol=ApiProtocol.ANTHROPIC_MESSAGES,
            details={"vision": "forbidden_tag", "tag": parser.dangerous},
        )
    return "".join(parser.parts)


def validate_visual_evidence(raw: str) -> str:
    """Return the ``<visual-evidence>`` fragment or raise FEATURE_UNSUPPORTED."""
    if not isinstance(raw, str) or not raw.strip():
        raise ProtocolAwareRoutingError(
            "vision translator returned empty IR",
            reason=ProtocolRoutingReason.FEATURE_UNSUPPORTED,
            protocol=ApiProtocol.ANTHROPIC_MESSAGES,
            details={"vision": "empty_ir"},
        )
    lowered_raw = raw.lower()
    if "<html" in lowered_raw or "<script" in lowered_raw or "javascript:" in lowered_raw:
        raise ProtocolAwareRoutingError(
            "vision IR contains forbidden html/script",
            reason=ProtocolRoutingReason.FEATURE_UNSUPPORTED,
            protocol=ApiProtocol.ANTHROPIC_MESSAGES,
            details={"vision": "html_or_script"},
        )
    match = _FRAGMENT_RE.search(raw)
    if match is None:
        raise ProtocolAwareRoutingError(
            "vision translator returned no visual-evidence root",
            reason=ProtocolRoutingReason.FEATURE_UNSUPPORTED,
            protocol=ApiProtocol.ANTHROPIC_MESSAGES,
            details={"vision": "no_root"},
        )
    fragment = match.group(0)
    lowered = fragment.lower()
    if "<script" in lowered or "javascript:" in lowered:
        raise ProtocolAwareRoutingError(
            "vision IR contains forbidden script",
            reason=ProtocolRoutingReason.FEATURE_UNSUPPORTED,
            protocol=ApiProtocol.ANTHROPIC_MESSAGES,
            details={"vision": "script"},
        )
    if "<html" in lowered:
        raise ProtocolAwareRoutingError(
            "vision IR must not be a full HTML document",
            reason=ProtocolRoutingReason.FEATURE_UNSUPPORTED,
            protocol=ApiProtocol.ANTHROPIC_MESSAGES,
            details={"vision": "html_doc"},
        )
    try:
        parser = _feed_parser(fragment)
    except ProtocolAwareRoutingError:
        raise
    except Exception as exc:
        raise ProtocolAwareRoutingError(
            "vision IR could not be parsed",
            reason=ProtocolRoutingReason.FEATURE_UNSUPPORTED,
            protocol=ApiProtocol.ANTHROPIC_MESSAGES,
            details={"vision": "parse"},
        ) from exc
    if parser.saw_html or parser.saw_script or parser.bad_tag in _DANGEROUS_TAGS:
        raise ProtocolAwareRoutingError(
            "vision IR contains forbidden tags",
            reason=ProtocolRoutingReason.FEATURE_UNSUPPORTED,
            protocol=ApiProtocol.ANTHROPIC_MESSAGES,
            details={"vision": "forbidden_tag"},
        )
    if parser.bad_tag or parser.bad_attr:
        try:
            fragment = _coerce_unknown_tags(fragment)
            parser = _feed_parser(fragment)
        except ProtocolAwareRoutingError:
            raise
        except Exception as exc:
            raise ProtocolAwareRoutingError(
                "vision IR could not be parsed",
                reason=ProtocolRoutingReason.FEATURE_UNSUPPORTED,
                protocol=ApiProtocol.ANTHROPIC_MESSAGES,
                details={"vision": "parse"},
            ) from exc
    if parser.saw_html or parser.saw_script or parser.bad_tag in _DANGEROUS_TAGS:
        raise ProtocolAwareRoutingError(
            "vision IR contains forbidden tags",
            reason=ProtocolRoutingReason.FEATURE_UNSUPPORTED,
            protocol=ApiProtocol.ANTHROPIC_MESSAGES,
            details={"vision": "forbidden_tag"},
        )
    if parser.bad_tag:
        raise ProtocolAwareRoutingError(
            f"vision IR contains tag {parser.bad_tag!r} not on the whitelist",
            reason=ProtocolRoutingReason.FEATURE_UNSUPPORTED,
            protocol=ApiProtocol.ANTHROPIC_MESSAGES,
            details={"vision": "bad_tag", "tag": parser.bad_tag},
        )
    if parser.bad_attr:
        raise ProtocolAwareRoutingError(
            "vision IR contains a forbidden attribute",
            reason=ProtocolRoutingReason.FEATURE_UNSUPPORTED,
            protocol=ApiProtocol.ANTHROPIC_MESSAGES,
            details={"vision": "bad_attr", "attr": parser.bad_attr},
        )
    if parser.root_reject:
        raise ProtocolAwareRoutingError(
            "vision translator rejected this image as out of V1 scope "
            "(glm-5.2-vision only transcribes coding screenshots: IDE, "
            "terminal, docs, GitHub UI). Landscapes, portraits, handwriting, "
            "and unreadable images are rejected. Use MiniMax-M3 directly "
            "for general scene description.",
            reason=ProtocolRoutingReason.FEATURE_UNSUPPORTED,
            protocol=ApiProtocol.ANTHROPIC_MESSAGES,
            details={"vision": "rejected_scope"},
        )
    if parser.text_chars == 0:
        raise ProtocolAwareRoutingError(
            "vision IR is empty",
            reason=ProtocolRoutingReason.FEATURE_UNSUPPORTED,
            protocol=ApiProtocol.ANTHROPIC_MESSAGES,
            details={"vision": "empty_or_reject"},
        )
    if parser.text_chars and parser.uncertain_chars / parser.text_chars >= _UNCERTAIN_THRESHOLD:
        raise ProtocolAwareRoutingError(
            "vision IR is too uncertain to use",
            reason=ProtocolRoutingReason.FEATURE_UNSUPPORTED,
            protocol=ApiProtocol.ANTHROPIC_MESSAGES,
            details={"vision": "uncertain"},
        )
    return fragment

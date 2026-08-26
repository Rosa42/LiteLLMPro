"""Whitelist validator for ``<visual-evidence>`` fragments."""

from __future__ import annotations

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
_FRAGMENT_RE = re.compile(
    r"<visual-evidence\b[^>]*>.*</visual-evidence>",
    re.IGNORECASE | re.DOTALL,
)
_UNCERTAIN_THRESHOLD = 0.8


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
    parser = _IRParser()
    try:
        parser.feed(fragment)
        parser.close()
    except Exception as exc:
        raise ProtocolAwareRoutingError(
            "vision IR could not be parsed",
            reason=ProtocolRoutingReason.FEATURE_UNSUPPORTED,
            protocol=ApiProtocol.ANTHROPIC_MESSAGES,
            details={"vision": "parse"},
        ) from exc
    if parser.saw_html or parser.saw_script:
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
    if parser.root_reject or parser.text_chars == 0:
        raise ProtocolAwareRoutingError(
            "vision IR is empty or out of V1 scope",
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

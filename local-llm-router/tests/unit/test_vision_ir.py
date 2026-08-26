"""IR quality gate for visual-evidence fragments."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from shared_quota_router.protocol_errors import ProtocolAwareRoutingError
from shared_quota_router.vision_ir import validate_visual_evidence

_FIXTURE_DIR = (
    Path(__file__).resolve().parents[3] / "docs" / "framework-upgrade" / "fixtures" / "vision-eval"
)


def test_accepts_pre_carrier() -> None:
    ir = "<visual-evidence><pre>Traceback (most recent call last):</pre></visual-evidence>"
    assert validate_visual_evidence(ir) == ir


def test_rejects_script() -> None:
    ir = "<visual-evidence><script>alert(1)</script></visual-evidence>"
    with pytest.raises(ProtocolAwareRoutingError):
        validate_visual_evidence(ir)


def test_rejects_html_document() -> None:
    ir = "<html><visual-evidence><p>x</p></visual-evidence></html>"
    with pytest.raises(ProtocolAwareRoutingError):
        validate_visual_evidence(ir)


def test_rejects_empty_shell() -> None:
    ir = "<visual-evidence></visual-evidence>"
    with pytest.raises(ProtocolAwareRoutingError):
        validate_visual_evidence(ir)


def test_rejects_out_of_scope() -> None:
    ir = '<visual-evidence data-reject="out-of-scope"></visual-evidence>'
    with pytest.raises(ProtocolAwareRoutingError):
        validate_visual_evidence(ir)


def test_rejects_mostly_uncertain() -> None:
    ir = (
        '<visual-evidence><span data-uncertain="1">????????</span>'
        "<pre>ok</pre></visual-evidence>"
    )
    with pytest.raises(ProtocolAwareRoutingError):
        validate_visual_evidence(ir)


def test_ci_manifest_samples_match_expect_carrier() -> None:
    manifest = json.loads((_FIXTURE_DIR / "manifest.json").read_text(encoding="utf-8"))
    samples = {row["id"]: row for row in manifest}
    pre = (_FIXTURE_DIR / "samples" / "term-001.xml").read_text(encoding="utf-8")
    table = (_FIXTURE_DIR / "samples" / "table-001.xml").read_text(encoding="utf-8")
    reject = (_FIXTURE_DIR / "samples" / "reject-001.xml").read_text(encoding="utf-8")
    assert samples["term-001"]["expect_carrier"] == "pre"
    assert samples["table-001"]["expect_carrier"] == "table"
    assert samples["reject-001"]["expect_carrier"] == "reject"
    assert "<pre>" in validate_visual_evidence(pre)
    assert "<table>" in validate_visual_evidence(table)
    with pytest.raises(ProtocolAwareRoutingError):
        validate_visual_evidence(reject)

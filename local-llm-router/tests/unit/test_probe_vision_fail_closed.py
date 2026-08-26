"""Vision probe must not treat the facade model name as a vision-stage error."""

from __future__ import annotations

import importlib.util
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "probe_vision_memory_live.py"
_SPEC = importlib.util.spec_from_file_location("probe_vision_memory_live", _SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
_MOD = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MOD)


def test_no_candidates_not_classified_as_vision_fail_closed() -> None:
    raw = (
        '{"type":"error","error":{"message":'
        '"no candidates for model_group=glm-5.2-vision request_id=abc"}}'
    )
    assert _MOD.vision_fail_closed("", raw) is False
    assert _MOD.vision_fail_closed("invalid_request_error", raw) is False


def test_translate_stage_error_is_vision_fail_closed() -> None:
    assert (
        _MOD.vision_fail_closed(
            "feature_unsupported:translate_failed",
            "vision translate failed for glm-5.2-vision",
        )
        is True
    )

"""Redaction helpers for OpenCode vision captures."""

from __future__ import annotations

from shared_quota_router.vision_agents.capture import (
    TINY_PNG_B64,
    redact_capture,
)


def test_redact_capture_strips_secrets_pixels_and_home_paths() -> None:
    payload = {
        "headers": {
            "Authorization": "Bearer sk-live-secret-aaaaaaaa",
            "User-Agent": "opencode/1.2.3",
            "X-Workspace-Root": r"C:\Users\someone\project",
        },
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": "see /Users/someone/shot.png Bearer sk-abcdefghijklmnopqrst",
                    },
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/png",
                            "data": "ORIGINALPIXELSNOTTINY",
                        },
                    },
                ],
            }
        ],
    }
    out = redact_capture(payload)
    assert "authorization" not in out["headers"]
    assert out["headers"]["user-agent"] == "opencode/1.2.3"
    assert "[path]" in out["headers"]["x-workspace-root"]
    assert "someone" not in out["headers"]["x-workspace-root"]
    text = out["messages"][0]["content"][0]["text"]
    assert "sk-abcdefghijklmnopqrst" not in text
    assert "[redacted]" in text
    assert "[path]" in text
    assert out["messages"][0]["content"][1]["source"]["data"] == TINY_PNG_B64


def test_redact_json_escaped_windows_path_and_session() -> None:
    text = (
        'Called the Read tool with the following input: '
        '{"filePath":"C:\\\\Users\\\\someone\\\\shot.png"} ses_abc123456789'
    )
    out = redact_capture(
        {
            "headers": {"x-session-id": "ses_abc123456789"},
            "messages": [{"role": "user", "content": [{"type": "text", "text": text}]}],
        }
    )
    dumped = out["messages"][0]["content"][0]["text"]
    assert "someone" not in dumped
    assert "[path]" in dumped
    assert "ses_abc123456789" not in dumped
    assert out["headers"]["x-session-id"] == "ses_redacted"


def test_maybe_write_capture_is_opt_in(tmp_path, monkeypatch) -> None:
    from shared_quota_router.vision_agents.capture import maybe_write_capture

    dest = tmp_path / "live.json"
    monkeypatch.setenv("GATEWAY_VISION_AGENT_CAPTURE", str(dest))
    maybe_write_capture(
        {"user-agent": "opencode/9", "authorization": "Bearer secret"},
        [{"role": "user", "content": "hi"}],
    )
    raw = dest.read_text(encoding="utf-8")
    assert "opencode/9" in raw
    assert "authorization" not in raw.lower() or '"authorization"' not in raw.lower()
    assert "Bearer secret" not in raw
    assert '"live_gateway": true' in raw

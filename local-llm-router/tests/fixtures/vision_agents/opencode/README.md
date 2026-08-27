# OpenCode vision fixtures

| File | Provenance | What it proves |
| --- | --- | --- |
| `live.json` | 2026-08-27 OpenCode 1.18.5 → this gateway (`/v1/messages`) | Title pre-request: Read-tool wrapper + top-level image |
| `live-2.json` | same session, second POST | Agent request: same inlined image shape, no title prefix |
| `tool_result_image.json` | OpenCode protocol golden (2026-05-22) | Nested `tool_result.content[]` image (test tool `read_screenshot`) |
| `user_media_image.json` | Derived from `lowerImage` | Bare top-level user image (not enough to fingerprint) |
| `source_headers.json` | OpenCode source, non-opencode provider branch | Intended UA / session headers |

Live hop (tap in front of `127.0.0.1:4000`):

- `User-Agent: opencode/1.18.5 ai-sdk/provider-utils/… runtime/bun/…`
- `x-session-id` / `x-session-affinity` present
- No `X-Agent-Client`
- Production Read screenshots are **siblings** on a user content list: wrapper text, `Image read successfully`, then `image`. Not `tool_result`.

Fingerprint (`VISION_AGENT_FINGERPRINTS`, default on) requires all three on the **same** user list. Nested `tool_result` images and chat text “I use OpenCode” do not match.

Re-capture:

```text
python scripts/opencode_gateway_tap.py -o tests/fixtures/vision_agents/opencode/live.json
python scripts/redact_vision_agent_capture.py dump.json -o tests/fixtures/vision_agents/opencode/live.json
```

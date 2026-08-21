# P0 Probe A — MiniMax-M3 direct vision

**Date:** 2026-08-21  
**Goal:** Evidence only — does official MiniMax Anthropic Messages accept an image on `MiniMax-M3`?  
**Path:** Direct MiniMax URL (bypass local gateway IMAGE gate).

## Command

```text
cd E:\LiteLLMPro\local-llm-router
F:\anaconda\envs\py312\python.exe scripts\p0_probe_a_minimax_vision.py
```

Working directory: `E:\LiteLLMPro\local-llm-router`. Python: `F:\anaconda\envs\py312\python.exe` (no `.venv`).

## Result

| Field | Value |
| --- | --- |
| HTTP status | **200** |
| SUMMARY | **SUMMARY PROBE_A=PASS** |
| Exit code | 0 |
| Endpoint | `POST https://api.minimaxi.com/anthropic/v1/messages` |
| Model | `MiniMax-M3` |
| Image shape | Anthropic native `type=image` / `source.type=base64` / `media_type=image/png` |
| Shape retry | **not used** (primary shape succeeded) |
| `config/plans.yaml` changed | **no** (`MiniMax-M3` still `supported_features: [text, streaming, tools, reasoning]`, no `image`) |

Assistant text in the 200 body was exactly `VISION_OK` (`stop_reason=end_turn`). Request did not go through `127.0.0.1:4000`. That exact assistant text means the historical PASS still stands; later runs classify from parsed `content` text blocks only, not a raw-body substring of `VISION_OK`.

## Redacted body snippet (≤500 chars, key/base64/prompt stripped)

```text
{"id":"06d660e8c3c8d1f3ccde21d3181ac466","type":"message","role":"assistant","model":"MiniMax-M3","content":[{"text":"VISION_OK","type":"text"}],"usage":{"input_tokens":66,"output_tokens":3,"cache_creation_input_tokens":0,"cache_read_input_tokens":128,"service_tier":"standard"},"stop_reason":"end_turn","base_resp":{"status_code":0,"status_msg":""}}
```

No API key, full prompt, or image base64 in this snippet.

## Notes

1. Payload: 1×1 PNG test pixel as Anthropic image block plus the VISION_OK / VISION_MISSING instruction. Headers: `Content-Type: application/json`, `x-api-key` from env, `anthropic-version: 2023-06-01`, browser-like User-Agent (same string as `scripts/probe_anthropic_support.py`).
2. MiniMax docs (`type=image` via URL or base64) match the primary shape. The script can retry once with `source.type=url` + data URI if a 400/422 looks like a field-shape error rather than "vision unsupported". That retry was not needed.
3. Transport: `urllib.request.urlopen` against this host raised `URLError`/`SSLEOFError` (`UNEXPECTED_EOF_WHILE_READING`) on this Windows + Python 3.12 combo. A bare `http.client.HTTPSConnection` with HTTP/1.1 ALPN completed TLS and returned HTTP. The probe therefore uses `http.client`, not urllib. A dummy POST without a real key returned 401 `authentication_error` (path exists) before the live keyed run.
4. This is not gateway evidence. `plans.yaml` was not updated; adding `image` is Task 3 and only follows A=PASS.
5. Do not treat this PASS as implementation of vision compose, memory, or `glm-5.2-vision`.

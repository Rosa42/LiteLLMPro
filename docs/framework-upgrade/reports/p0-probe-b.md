# P0 Probe B — pre-call `messages` mutation to live Messages upstream

**Date:** 2026-08-21  
**Goal:** Evidence only — does `async_pre_call_hook` mutation of `data["messages"]` reach the live Anthropic Messages upstream?  
**Path:** Local Docker proxy `POST http://127.0.0.1:4000/v1/messages` (not `.venv` / `llm-router.ps1`).

## Command

Host tests (no Docker):

```text
cd E:\LiteLLMPro\local-llm-router
$env:PYTHONPATH="plugins"
F:\anaconda\envs\py312\python.exe -m pytest tests/unit/test_p0_probe_b_marker.py tests/unit/test_m3_endpoint_gates.py tests/contract/test_p0_direct_protocol_paths.py -q --tb=short
```

Live probe (plugin already in image; recreate for env):

```text
docker compose --env-file .env -f deploy/docker-compose.yaml --profile core up -d litellm
F:\anaconda\envs\py312\python.exe scripts\p0_probe_b_precall_mutate.py
```

Working directory: `E:\LiteLLMPro\local-llm-router`. Python: `F:\anaconda\envs\py312\python.exe`. Proxy: container `local-llm-router-litellm-1` (plugin COPY'd into image). `use_chat_completions_url_for_anthropic_messages` was **not** changed.

## Overall result

| Field | Value |
| --- | --- |
| Overall SUMMARY | **SUMMARY PROBE_B=FAIL** (MiniMax-M3 produced assistant text `pong` without marker) |
| pytest | **28 passed** in 16.84s |
| Marker prefix | `P0B_` (full token not recorded) |
| `.env` after cleanup | no `P0_PROBE_B_MARKER`, no `P0_PROBE_B_MODEL` |
| Container after cleanup | **MARKER EMPTY** |

## Attempt 1 — glm-5.2 (OpenCode) — INCONCLUSIVE

Not a valid probe of pre-call mutation: this path is Console Go / OpenCode and returns empty upstream `messages` even without the marker.

| Field | Value |
| --- | --- |
| HTTP status | **400** |
| SUMMARY | **INCONCLUSIVE** |
| Exit code | 3 |
| Endpoint | `POST http://127.0.0.1:4000/v1/messages` |
| Model | `glm-5.2` |
| Snippet type | **error** (`invalid_request_error`) |
| Proxy healthy | **yes** (200) |
| Docker | rebuild (`up -d --build litellm`) |
| Container marker | **MARKER SET** |
| Client JSON contained marker | **false** |

Redacted error snippet:

```text
{"type":"error","error":{"type":"invalid_request_error","message":"{\"error\":{\"type\":\"invalid_request_error\",\"code\":\"invalid_request_error\",\"message\":\"Error from provider (Console Go): Upstream request failed: [invalid_request_error] 'messages' must contain at least one message\"}}. Received Model Group=glm-5.2\nAvailable Model Group Fallbacks=None"}}
```

Vanilla control after marker removal: same HTTP 400 / Console Go empty `messages`. Inject did not cause that 400. Likely `use_chat_completions_url_for_anthropic_messages: true` plus `anthropic/glm-5.2` on OpenCode Anthropic `api_base`. Flag not changed.

## Attempt 2 — MiniMax-M3 (gateway) — FAIL

Gateway path (not Probe A direct). Recreate only (inject already in image). `P0_PROBE_B_MODEL=MiniMax-M3`.

| Field | Value |
| --- | --- |
| HTTP status | **200** |
| SUMMARY | **SUMMARY PROBE_B=FAIL** |
| Exit code | 1 |
| Endpoint | `POST http://127.0.0.1:4000/v1/messages` |
| Model | `MiniMax-M3` (`anthropic/MiniMax-M3`, `MINIMAX_ANTHROPIC_BASE_URL`) |
| Snippet type | **assistant** (`content[0].text`) |
| Proxy healthy | **yes** (200) |
| Docker | recreate without rebuild (`up -d litellm`) |
| Container marker | **MARKER SET** |
| Client JSON contained marker | **false** |

Assistant text was exactly `pong`. Classification: FAIL = `pong` present (case-insensitive) and marker absent. Client prompt was `Reply with exactly: pong` and did not include the marker, so this is strong evidence the pre-call suffix did **not** reach the live MiniMax Messages upstream.

Redacted assistant snippet (master key and marker stripped; marker was not in this body):

```text
{"id":"06d6ca410ac7faf5d8b17b18456cca53","type":"message","role":"assistant","model":"MiniMax-M3","content":[{"text":"pong","type":"text"}],"usage":{"input_tokens":64,"output_tokens":2,"cache_creation_input_tokens":0,"cache_read_input_tokens":128,"service_tier":"standard"},"stop_reason":"end_turn","base_resp":{"status_code":0,"status_msg":""}}
```

MiniMax did **not** 400 under the global Chat-URL-for-Anthropic-Messages flag (unlike glm-5.2). The request completed; mutation still did not appear in the model reply. Flag was not changed.

B-lab (mock-as-proxy-upstream) was **not** run. Production MiniMax `api_base` was not retargeted.

## Cleanup

1. `P0_PROBE_B_MARKER` and `P0_PROBE_B_MODEL` removed from `.env` (`dotenv_has_probe_keys=False`).
2. Container recreated: `docker compose --env-file .env -f deploy/docker-compose.yaml --profile core up -d litellm`.
3. Container env: **MARKER EMPTY**. Proxy liveliness **200**.
4. Inject code stays, env-gated, default off. Not added to `flag_snapshot()`.

## Notes

1. Host unit tests cover: empty env no-op, string content suffix, list content last text block, mock `probe_marker_hit` true/false without storing Authorization or prompt text.
2. glm-5.2 400 is an OpenCode path issue, not Probe B evidence.
3. MiniMax-M3 FAIL means the live gateway Messages call saw the original client prompt, not the env-gated pre-call suffix.
4. This is not vision, memory, Chat/Responses, or `plans.yaml` `image` work.

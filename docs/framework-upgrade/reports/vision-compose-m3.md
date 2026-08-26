# Vision compose M3 — MiniMax wire + glm-5.2-vision

**Date:** 2026-08-26  
**Goal:** Production facade `glm-5.2-vision` (MiniMax-M3 translate → `glm-5.2` execute). No pixels to GLM. Stub peel ignored when vision flags are on.

## Code

- `vision_compose.py`: SHA-256 file cache, IR quality gate, MiniMax nested select + `httpx` POST `{api_base}/v1/messages`, child id `{parent}#vision:{hash8}`, quota exclusive vs parent Volc group, lease release after HTTP, circuit (3 failures / 60s)
- `anthropic_direct.py`: URL join, env-ref resolve, HTTP/1.1 ALPN TLS context, response text extract (keys not logged)
- `protocol_errors.py`: `message` is settable (LiteLLM assigns `e.message`); public `shared_quota.details` includes `vision`
- `generator.py`: facade `model_name: glm-5.2-vision`; upstream `litellm_params.model: anthropic/glm-5.2` (not `anthropic/glm-5.2-vision`)
- `strategy.py`: after async select, bind `select_deployment` / `release_lease` on the enhance envelope
- `data_paths.py`: Docker `/app/shared_quota_router` → `/app/data/...` (not `/data/...`)
- Operator `config/plans.yaml`: Volc plan model `glm-5.2-vision` (no `image` on deployment features) + `logical_models` compose MiniMax-M3
- Compose volume `../data:/app/data` plus `GATEWAY_VISION_CACHE_DIR` / `GATEWAY_MEMORY_DIR`

## Tests

`pytest tests/unit tests/contract`: **362 passed, 1 skipped**. Flags default **false** in snapshot tests; `async_get_available_deployment` does not run the pipeline when enhance is off.

## Live Docker (2026-08-26)

Proxy `127.0.0.1:4000` healthy (`curl.exe` `/health/liveliness`). Plugin is COPY’d; `config/litellm.yaml` is volume-mounted.

**Network on this Windows + Docker Desktop host:** Linux containers cannot resolve `ark.cn-beijing.volces.com` via Cloudflare DoH (NXDOMAIN). Aliyun / DNSPod DoH return China A records. Overlay `deploy/docker-compose.minimax-host-bridge.yaml` pins that hostname via `extra_hosts` and keeps MiniMax on `scripts/minimax_host_bridge.py` (`127.0.0.1:18443`). `S5_STUB_PEEL` unset. Volc `api_base` stays `https://ark.cn-beijing.volces.com/api/coding` (not mock). Fallback script if Docker TLS SSLEOFs again: `scripts/volc_host_bridge.py`.

`.env`: enhance / vision / memory-retrieve / extract **on**.

| Check | Result |
| --- | --- |
| `glm-5.2` text | **PASS** HTTP 200 from real Volc (after extra_hosts). Earlier `no candidates` was LiteLLM retry after Docker TLS/DNS failure, not an empty registry. |
| `glm-5.2` + PNG | **PASS** HTTP 400, required features `image` on `anthropic_messages` |
| `glm-5.2-vision` + terminal PNG | **PASS** HTTP 200 `live-execute-no-mock`. Model thinking cites cached `<visual-evidence>` (traceback IR), not pixels. Stub peel absent. |
| `glm-5.2-vision` text-only | **PASS** HTTP 200 |
| Discovery `GET /v1/router/model-capabilities` | `glm-5.2-vision` features include `image`; pure `glm-5.2` does not |

Probe `fail-closed-after-translate` no longer matches merely because the model name contains `vision`.

## Secrets

This report has no API keys, full prompts, or image base64.

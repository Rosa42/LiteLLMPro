# Protocol Gateway Phase 0 Compatibility Report

**Task:** P0-01 Record verified endpoint and strategy contracts  
**LiteLLM pin:** `v1.90.5` (`0430743f2fd4005898506e00bc62dd47bcff6fc9`)  
**Date:** 2026-07-26  
**Scope:** Source-backed facts only. No production implementation.

## Verification posture

| Check | Result |
|-------|--------|
| Pin | `config/versions.env` → `LITELLM_VERSION=v1.90.5`, SHA `0430743f…` |
| Submodule HEAD | `0430743f2fd4005898506e00bc62dd47bcff6fc9` (tag `v1.90.5`) |
| Business edits under `upstream/litellm` | None for this task |
| Pre-existing dirty marker | Submodule shows `-dirty` due to CRLF on one tokenizer blob (`litellm/litellm_core_utils/tokenizers/9b5ad71b…`); not a business change and not introduced by this task |

Legend for claims below:

- **Verified:** file + line evidence against pinned source.
- **Runtime contract:** requires P0-02 / P0-03 harness; source alone is insufficient.

---

## 1. Public endpoint → proxy-internal `route_type`

### 1.1 Chat Completions

| Item | Value | Evidence |
|------|-------|----------|
| Public paths | `/v1/chat/completions`, `/chat/completions`, `/engines/{model}/chat/completions`, `/openai/deployments/{model}/chat/completions` | `proxy/proxy_server.py` L8835–8855 |
| Handler | `chat_completion` | same file L8857+ |
| `route_type` passed into processor | **`"acompletion"`** | `proxy_server.py` L8924–8928 |
| Route map label | `acompletion` → `/chat/completions` | `proxy/route_llm_request.py` L74–75 |

### 1.2 Responses

| Item | Value | Evidence |
|------|-------|----------|
| Public paths | `/v1/responses`, `/responses`, `/openai/v1/responses` | `proxy/response_api_endpoints/endpoints.py` L26–40 |
| Handler | `responses_api` | same file L41+ |
| `route_type` (normal + polling pre-call) | **`"aresponses"`** | same file L140, L202 |
| WebSocket | `/v1/responses`, `/responses` → `_aresponses_websocket` family | same file L1072–1074; types in `common_request_processing.py` L933–935 |
| Route map label | `aresponses` → `/responses` | `proxy/route_llm_request.py` L83–84 |

### 1.3 Anthropic Messages

| Item | Value | Evidence |
|------|-------|----------|
| Public path | `/v1/messages` | `proxy/anthropic_endpoints/endpoints.py` L61–65 |
| Handler | `anthropic_response` | same file L66+ |
| `route_type` | **`"anthropic_messages"`** | same file L92–97 |
| Included in processor `route_type` Literal | yes | `proxy/common_request_processing.py` L996 |

### 1.4 Shared processing

All three endpoints funnel through `ProxyBaseLLMRequestProcessing.base_process_llm_request(..., route_type=...)` (`common_request_processing.py` L1200+ / L1381–1388), which calls `route_request(data, route_type, llm_router, ...)`.

---

## 2. `route_type` is proxy-internal — not part of the custom strategy contract

### Verified

1. `route_type` is a parameter of the **proxy** request processor (`common_request_processing.py` L930–1020, L1205+), used as:
   - `function_setup(original_function=route_type, ...)` (L1131–1136)
   - `pre_call_hook(..., call_type=route_type)` (L1140–1144)
   - `route_request(..., route_type=route_type)` (L1383–1388)

2. Custom strategy contract is only:

```text
async_get_available_deployment(
    model,
    messages=None,
    input=None,
    specific_deployment=False,
    request_kwargs=None,
)
get_available_deployment(...same...)
```

Evidence: `litellm/types/router.py` L656–703 (`CustomRoutingStrategyBase`).

3. Mounting replaces Router methods only:

```python
setattr(self, "get_available_deployment", CustomRoutingStrategy.get_available_deployment)
setattr(self, "async_get_available_deployment", CustomRoutingStrategy.async_get_available_deployment)
```

Evidence: `litellm/router.py` L12060–12081.

4. Call sites that invoke deployment selection pass **model / messages / input / request_kwargs** — not `route_type`:
   - Chat path `_acompletion`: `router.py` L2913–2918 (`messages=messages`, `request_kwargs=kwargs`)
   - Generic path (Messages + most non-chat): `router.py` L4745–4750 (`messages=kwargs.get("messages")`, **no explicit `input=`**, `request_kwargs=kwargs`)
   - Responses uses `_aresponses_with_streaming_fallbacks` → `_ageneric_api_call_with_fallbacks` (`router.py` L6112–6116, L4831–4875)

**Conclusion (verified):** Strategy code cannot rely on a `route_type` argument. Any public-protocol signal must be carried in serializable request metadata (or a project-owned boundary outside the strategy signature).

---

## 3. Strategy inputs and project strategy implementation

### 3.1 Upstream contract (`CustomRoutingStrategyBase`)

| Parameter | Role | Notes |
|-----------|------|-------|
| `model` | Logical model / model group name | Required |
| `messages` | Chat-style message list | Optional |
| `input` | Embedding / Responses-style input | Optional; **not always forwarded by generic path** (see §4) |
| `specific_deployment` | Pin to a deployment | Optional |
| `request_kwargs` | Full call kwargs after proxy setup | Optional dict; primary carrier for metadata |

Return: one element from `router.model_list` (a deployment dict).

### 3.2 Project strategy (`plugins/shared_quota_router/strategy.py`)

| Item | Evidence |
|------|----------|
| Duck-typed `SharedQuotaRoutingStrategy` matching base signature | L343–393, L395–401 |
| Uses `request_kwargs` for request id / session / Redis reqctx | `resolve_request_id` L226–239; `session_key_from_request` L188–223; `context_from_request_kwargs` L241–295 |
| Reads **`metadata` only** (not `litellm_metadata`) for session keys and request id | L197–198, L232–236 |
| Does **not** put `RequestRoutingContext` on kwargs (serialization safety) | L28–30, L255–256 |
| Lease after candidate filter; Redis fail-closed | `SharedQuotaSelector.select` L131–185; strategy L441–443 |
| First-byte hard gate before reselection | L411–420 |

---

## 4. Where request fields are populated (and which bucket holds metadata)

### 4.1 Request body and call id

| Field | Where populated | Evidence |
|-------|-----------------|----------|
| Body (`messages` / `input` / `model` / client `metadata`) | Endpoint `_read_request_body` → `ProxyBaseLLMRequestProcessing(data=...)` | chat L8887; responses L93; messages L89 |
| `litellm_call_id` | `common_processing_pre_call_logic`: header `x-litellm-call-id` or new UUID | `common_request_processing.py` L1103–1105 |
| `proxy_server_request` | `add_litellm_data_to_request` | `litellm_pre_call_utils.py` L1430+ |
| Pre-call hooks | `proxy_logging_obj.pre_call_hook(..., call_type=route_type)` | `common_request_processing.py` L1140–1144 |

### 4.2 Metadata bucket name differs by endpoint (**critical**)

Proxy chooses the metadata dict key via `_get_metadata_variable_name(request)`:

```text
LITELLM_METADATA_ROUTES = ("batches", "/v1/messages", "responses", "files")
→ "litellm_metadata" when path matches
→ else "metadata"
```

Evidence: `litellm_pre_call_utils.py` L109–114, L332–350.

| Public endpoint | Metadata key after pre-call setup | Evidence |
|-----------------|-----------------------------------|----------|
| `/v1/chat/completions` | **`metadata`** | path not in `LITELLM_METADATA_ROUTES` |
| `/v1/responses` | **`litellm_metadata`** | `"responses"` in routes; router also forces `litellm_metadata` for generic path (`router.py` L4681–4683) |
| `/v1/messages` | **`litellm_metadata`** | `"/v1/messages"` in routes; same generic path |

After deployment selection, `_update_kwargs_with_deployment` writes `deployment`, `model_info`, `api_base`, `deployment_model_name` into the **router** metadata variable (`router.py` L3203–3240), which for generic API is `litellm_metadata` (`router_utils/batch_utils.py` L168–190: `_ageneric_api_call_with_fallbacks` ∈ `ROUTER_METHODS_USING_LITELLM_METADATA`).

**Implication for `metadata.protocol` (design candidate):**

- Chat: putting `protocol` under request body `metadata` is source-aligned.
- Responses / Messages: the proxy-internal bucket is `litellm_metadata`. Client body `metadata` may **not** be the same dict the router strategy/callbacks see.
- Project strategy today only inspects `request_kwargs["metadata"]` → **Responses/Messages protocol may be invisible to the current strategy unless also present under `litellm_metadata` or duplicated**.

**Classification:** source-verified split; whether a client-injected `metadata.protocol` survives into strategy kwargs for each path is **Runtime contract (P0-02)**.

### 4.3 `messages` vs `input` as validation evidence only

| Path | Top-level body fields | Strategy `messages` arg | Strategy `input` arg |
|------|----------------------|-------------------------|----------------------|
| Chat | `messages` | Passed explicitly from `_acompletion` | Typically unused |
| Responses | `input` | Generic helper uses `kwargs.get("messages")` only → often **None** | **Not passed as named arg** by generic helper (`router.py` L4745–4750); may still live inside `request_kwargs["input"]` |
| Messages | Anthropic `messages` | From kwargs if present | N/A |

Design rule confirmed by tasks.md / plan: **never** treat presence of `messages` vs `input` as the authoritative protocol signal. Use as P0-02 validation evidence only.

### 4.4 Selected deployment metadata available to callbacks

After selection, kwargs receive:

- `kwargs[metadata_key]["model_info"]` (includes deployment `id` and project-owned `model_info` fields such as `quota_group_id` when configured)
- `kwargs[metadata_key]["deployment"]`, `api_base`, `deployment_model_name`
- `kwargs["model_info"]` top-level copy (`router.py` L3263)

Project callbacks read context via `context_from_request_kwargs` and lease release paths (`plugins/shared_quota_router/callbacks.py`). Whether `quota_group_id` / `deployment_id` remain present after each protocol path is **Runtime contract (P0-03)**.

---

## 5. Streaming and first-visible-byte hooks

| Item | Evidence | Classification |
|------|----------|----------------|
| CustomLogger stream hook | `async_log_stream_event(kwargs, response_obj, start_time, end_time)` — `integrations/custom_logger.py` L142–143 | Verified API |
| Project marks first byte | `SharedQuotaCallback.async_log_stream_event` → `mark_first_byte` — `callbacks.py` L96–100 | Verified project code |
| Strategy refuses reselection after first byte | `strategy.py` L411–420 | Verified |
| Responses streaming short-circuit | Streaming `/v1/responses` may return before non-stream ownership tail (`common_request_processing.py` L1597–1611, L1798+) | Verified source behavior |
| Whether first stream event fires **before** any visible client byte for Chat / Messages / Responses | Requires harness | **Runtime contract (P0-02/P0-03)** |

---

## 6. Router dispatch matrix (MVP endpoints)

| Public protocol (intent) | `route_type` | Router entry | Selection helper | Metadata key (source expectation) |
|--------------------------|--------------|--------------|------------------|-------------------------------------|
| `openai_chat` | `acompletion` | `llm_router.acompletion` path | `_acompletion` → `async_get_available_deployment` | `metadata` |
| `openai_responses` | `aresponses` | `factory_function(..., call_type="aresponses")` | `_aresponses_with_streaming_fallbacks` → `_ageneric_api_call_with_fallbacks_helper` | `litellm_metadata` |
| `anthropic_messages` | `anthropic_messages` | `factory_function(..., call_type="anthropic_messages")` | `_ageneric_api_call_with_fallbacks_helper` | `litellm_metadata` |

Evidence for factory branches: `router.py` L6112–6148.

---

## 7. Verified vs requires runtime contract tests

### Verified (this report)

1. Exact `route_type` values: `acompletion`, `aresponses`, `anthropic_messages`.
2. `route_type` is **not** a custom strategy parameter.
3. Exact strategy signature and mount via `set_custom_routing_strategy`.
4. Chat uses `metadata`; Messages/Responses prefer `litellm_metadata` in proxy + generic router path.
5. Generic selection path does not pass `input=` as a named strategy argument.
6. Deployment selection metadata is written into the router metadata bucket after pick.
7. Stream callback surface exists; project first-byte gate exists.
8. No business logic was added under `upstream/litellm` for this task.

### Requires runtime contract tests (P0-02)

1. Injecting `protocol` at the earliest project-owned boundary: does it appear in `request_kwargs` at strategy time for **all three** endpoints?
2. Same injection: does it appear in success / failure / stream callback kwargs?
3. For Responses: is protocol only under `litellm_metadata`, only under `metadata`, both, or neither after pre-call setup?
4. For Messages: same as Responses.
5. Are non-serializable objects ever required to carry protocol (must remain serializable strings only)?

### Requires provider / entry-point tests (P0-03)

1. Direct Chat reaches mock `/v1/chat/completions`.
2. Direct Messages reaches mock `/v1/messages` (local mock first).
3. Direct Responses reaches mock `/v1/responses` (local mock unless real provider configured).
4. Selected `deployment_id` / `quota_group_id` available to callbacks on each path.
5. First visible stream event marks the retry boundary per protocol.
6. NewAPI capability inventory (operator-run probe only; no secrets in tree).

---

## 8. Risks feeding G0

| Risk | Why it matters | Mitigation path |
|------|----------------|-----------------|
| Dual metadata buckets (`metadata` vs `litellm_metadata`) | Design assumed single `metadata.protocol` | P0-02 must test both keys; G0 may require writing protocol into both or a project-owned front boundary |
| Generic path omits named `input=` | Responses body shape ≠ strategy arg | Never use `input` for protocol; still capture presence as evidence only |
| Stream callback timing may differ per route | First-byte invariant | P0-03 stream fixtures per endpoint |
| Strategy only reads `metadata` today | Messages/Responses protocol invisible if only in `litellm_metadata` | P0-02 documents failure; M2-01 must fix reader if G0-B is chosen |

---

## 9. Deliverable checklist (P0-01)

- [x] Exact `route_type` for Chat / Responses / Messages recorded with file:line evidence  
- [x] Confirmed `route_type` is not part of custom strategy contract  
- [x] Recorded `get_available_deployment` / `async_get_available_deployment` signature and inputs  
- [x] Recorded where `messages`, `input`, metadata, request id, selected deployment metadata, and stream hooks are populated  
- [x] Distinguished verified fields vs runtime contract tests  
- [x] No upstream business modifications for this task  

### P0-01 Completion

Files changed:

- `docs/phase-reports/protocol-gateway-phase-0-compatibility.md` (this file)

Implementation:

- Source audit only; no plugin or config behavior change.

Commands executed:

- Source inspection under pinned `upstream/litellm` @ `0430743f…` / `v1.90.5`
- `git rev-parse` / submodule status → pin confirmed; dirty flag is tokenizer CRLF only

Test results:

- N/A (report task); tests begin in P0-02

Security checks:

- Secrets/logging review: PASS (no secrets, no prompt bodies recorded)

Unresolved risks:

- Dual metadata bucket for protocol carrier (see §8)
- Responses `input` not forwarded as named strategy arg

Next task:

- **P0-02** Add protocol metadata propagation contract harness under `tests/contract/`

---

## 10. P0-02 Completion — protocol metadata propagation

**Date:** 2026-07-26  
**Harness:** `tests/contract/test_p0_protocol_metadata_propagation.py`

### Commands

```text
pip install 'litellm==1.90.5' pytest pytest-asyncio
set PYTHONPATH=plugins
pytest tests/contract/test_p0_protocol_metadata_propagation.py -q
→ 6 passed
```

### Results (deterministic)

| Path | Injection key | Strategy sees protocol | Success callback sees protocol | Notes |
|------|---------------|------------------------|--------------------------------|-------|
| Chat (`acompletion`) | `metadata.protocol` | **YES** | **YES** (`litellm_params.metadata.protocol`) | `call_type=acompletion` |
| Responses (`aresponses`) | `litellm_metadata.protocol` | **YES** | **YES** (both `litellm_params.metadata` and `.litellm_metadata`) | `input` in kwargs; named `input=` arg is None |
| Messages (`aanthropic_messages`) | `litellm_metadata.protocol` | **YES** | **YES** (same dual nesting) | `call_type=anthropic_messages` |
| Responses with only `metadata.protocol` | `metadata` only | **YES** via combined reader; **not** auto-copied into `litellm_metadata` | N/A (strategy-only case) | Readers must check both buckets |
| Chat stream | `metadata.protocol` | **YES** | Success after stream when logging drains; per-chunk `async_log_stream_event` may be silent under `mock_response` | First-byte stream hook still project-owned |
| Failure callback shape | nested `litellm_params.metadata.protocol` | N/A | **YES** (direct hook contract) | |

Deployment ids on success callbacks:

- `litellm_params.model_info.deployment_id` / `quota_group_id` present for all three paths (`probe-dep-1` / `probe-qg-1` in fixtures).

### Security

- Fake keys only (`fake-key-not-a-secret`)
- Prompt content is single character `x`
- Capture fixtures assert no `Authorization` / `Bearer` strings

### G0 implications (from P0-02)

1. **G0-B (metadata integration) is viable** for all three MVP endpoint paths **if** protocol is written into the correct bucket (`metadata` for Chat, `litellm_metadata` for Responses/Messages) **or** strategy/callbacks always read **both**.
2. Project strategy today only reads `metadata` (`strategy.py` session/request-id helpers) — must be extended before relying on Messages/Responses protocol.
3. Callbacks must also look under `litellm_params.{metadata,litellm_metadata}`, not only top-level kwargs.
4. Full HTTP proxy injection (via `_get_metadata_variable_name`) is source-aligned with the buckets above; Router harness is sufficient evidence for G0 without spinning the proxy process.

### Unresolved for P0-03

- Direct provider path contracts (mock Chat / Messages / Responses HTTP)
- Real first-visible stream byte boundary under non-mock streaming
- NewAPI operator probe (credentials never in source)

### P0-02 Completion template

Files changed:

- `tests/contract/test_p0_protocol_metadata_propagation.py`
- `docs/phase-reports/protocol-gateway-phase-0-compatibility.md` (this section)

Implementation:

- Capturing strategy + CustomLogger callback harness against LiteLLM Router paths for Chat / Responses / Messages
- Sync strategy-only cases + single-loop async matrix for success callbacks

Commands executed:

- `pytest tests/contract/test_p0_protocol_metadata_propagation.py -q` → **6 passed**

Security checks:

- Secrets/logging review: **PASS**

Unresolved risks:

- Per-chunk stream logging may not fire under `mock_response` (success still carries protocol)
- Full FastAPI proxy not exercised (Router path is the verified selection boundary)

Next task:

- **P0-03** Verify direct protocol entry points and provider contracts

---

## 11. P0-03 Completion — direct protocol paths and provider contracts

**Date:** 2026-07-26  
**Harness:** `tests/contract/test_p0_direct_protocol_paths.py`  
**Mock:** `plugins/shared_quota_router/mock_provider.py` (Chat + Messages + Responses)

### Commands

```text
set PYTHONPATH=plugins
pytest tests/contract/test_p0_direct_protocol_paths.py \
       tests/contract/test_p0_protocol_metadata_propagation.py \
       tests/e2e/test_mock_provider_http.py -q
→ 17 passed
```

### Upstream path evidence (local mock)

| Public intent | LiteLLM model prefix | Upstream path observed | Auth style | Result |
|---------------|----------------------|------------------------|------------|--------|
| Chat | `openai/<model>` | `/chat/completions` | Bearer | **PASS** (text, usage, stream, 401 auth shape) |
| Messages | `anthropic/<model>` | `/v1/messages` | `x-api-key` | **PASS** (text, usage) |
| Messages | `openai/<model>` | **`/responses`** (misroute) | Bearer | **Documented failure mode** — not Messages |
| Responses | `openai/<model>` | `/responses` | Bearer | **PASS** (non-stream text/usage) |

### Deployment / quota metadata

Strategy selection returns `model_info.deployment_id` and `quota_group_id` for Chat, Messages, and Responses fixtures before the upstream call.

### First-byte retry boundary

After the first stream chunk on Chat, setting `RequestRoutingContext.first_byte_sent=True` causes subsequent `get_available_deployment` to raise `NoAvailableDeploymentError` (hard gate preserved).

### Capability inventory

| Provider / path | Status | Evidence | Unknowns |
|-----------------|--------|----------|----------|
| OpenCode Go (Chat) | **Verified by design + local Chat path** | Existing production use; mock Chat path contract | Real tools/stream edge cases operator-owned |
| Volc (Chat) | **Verified by design + local Chat path** | Same as OpenCode | Same |
| Anthropic Messages (generic) | **Local mock verified** when deployment is `anthropic/` | P0-03 path + response shape | Real provider tools/stream |
| OpenAI Responses (generic) | **Local mock verified** | P0-03 `/responses` path | Real Responses provider not configured |
| **NewAPI** | **Unverified** | No credentials in tree; no operator probe run in this session | Chat vs Messages vs both |

### NewAPI operator probe (controlled; never store secrets)

```text
# Credentials only in environment — never commit
# PLAN_NEWAPI_A_BASE_URL / PLAN_NEWAPI_A_API_KEY

# Probe Chat
curl -sS "$PLAN_NEWAPI_A_BASE_URL/v1/chat/completions" \
  -H "Authorization: Bearer $PLAN_NEWAPI_A_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"<logical>","messages":[{"role":"user","content":"ping"}],"max_tokens":8}'

# Probe Messages (Anthropic)
curl -sS "$PLAN_NEWAPI_A_BASE_URL/v1/messages" \
  -H "x-api-key: $PLAN_NEWAPI_A_API_KEY" \
  -H "anthropic-version: 2023-06-01" \
  -H "Content-Type: application/json" \
  -d '{"model":"<logical>","max_tokens":8,"messages":[{"role":"user","content":"ping"}]}'

# Record only: status code, path, auth style accepted, whether tools/stream work
# Do NOT paste API keys or full prompts into git or the phase report body
```

Until a probe result is filed, NewAPI remains **protocol-unset / not Messages-capable** for MVP.

### Critical product implication

Current generator emits `openai/<model>` for all plans, including NewAPI Claude names. Against LiteLLM v1.90.5, **Messages traffic to an `openai/` deployment is sent to `/responses`**, which matches the historical misroute. MVP must either:

1. Keep Messages disabled until a deployment is explicitly `anthropic_messages` with a working Messages upstream, or  
2. Assign an anthropic-native LiteLLM model prefix only after P0 provider evidence.

### Security

- Fake keys only in tests  
- Mock `last_requests` stores path/auth_style/body_keys — never Authorization values or prompt text  
- NewAPI probe instructions forbid committing secrets  

### P0-03 Completion template

Files changed:

- `plugins/shared_quota_router/mock_provider.py`
- `tests/contract/test_p0_direct_protocol_paths.py`
- `docs/phase-reports/protocol-gateway-phase-0-compatibility.md` (this section)

Implementation:

- Multi-protocol local mock (Chat / Messages / Responses)
- Router path contracts + misroute documentation for openai-prefix Messages
- First-byte hard-gate regression on Chat stream

Commands executed:

- `pytest tests/contract/test_p0_direct_protocol_paths.py tests/contract/test_p0_protocol_metadata_propagation.py tests/e2e/test_mock_provider_http.py -q` → **17 passed**

Security checks:

- Secrets/logging review: **PASS**

Unresolved risks:

- NewAPI real capability unknown  
- Responses streaming not exercised against mock in this task  
- Full FastAPI proxy process not spun (Router is the selection boundary)

Next task:

- **G0** Choose integration boundary → ADR

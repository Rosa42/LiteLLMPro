# Remaining Development Plan: Unified Multi-Protocol Gateway

**Date:** 2026-07-26
**Source:** Code-first audit against `docs/phase-reports/unified-api-vs-multi-protocol-progress.md`, `docs/tasks.md`, and residual-risk records.
**Goal:** Production-ready unified external API where heterogeneous upstream providers (Chat + Anthropic) jointly serve traffic, with same-protocol direct passthrough and explicit cross-protocol conversion.
**LiteLLM pin:** `v1.90.5`
**Test baseline (verified this session):** `197 passed, 1 skipped`

## 1. Verified Current State (code is source of truth)

### 1.1 Done and production-usable

- Direct Chat routing MVP: `strategy.py:100-385`, `protocol_gates.py:71-149`, `protocol_context.py:135-218`.
- Protocol/feature domain model: `models.py:43-308`.
- Config schema + validation: `config_schema.py:153-519`.
- Generator with protocol metadata: `generator.py:50-217` (`drop_params: false`, atomic backups).
- Pre-lease capability filter (gateway on): `strategy.py:117-149`.
- Direct-before-convert ranking: `strategy.py:142-147`.
- Affinity after capability filter: `strategy.py:332-342`.
- Lease/first-byte/tried-set invariants: `strategy.py:367-381`, `callbacks.py:240-263`.
- Protocol-aware no-route errors: `protocol_errors.py`.
- Route/reject observability: `protocol_observability.py`.
- Dual feature flags (gateway AND conversion): `feature_flags.py:39-45`.
- C1 conversion contracts + fidelity matrix: `conversion/contracts.py:18-104`.
- C2 messages->chat adapter (text, non-stream): `conversion/adapters/messages_to_chat.py:65-181`.
- C2 request rewriting: `strategy.py:705-804`.
- C2 success response rewriting: `callbacks.py:161-214`.
- C3 route-scoped cooldown isolation: `state_store.py:217-282`, `callbacks.py:499-526`.
- C4 streaming conversion: No-Go (correctly blocked).
- C5 Responses direct: No-Go (no verified Responses deployment).

### 1.2 Production posture (verified)

- `config/plans.yaml`: OpenCode Go + Volc = `openai_chat` enabled; NewAPI = `enabled: false`, protocol unset.
- `config/litellm.yaml`: all enabled deployments use `openai/<model>`; `drop_params: false`.
- `.env.example`: `PROTOCOL_AWARE_GATEWAY_ENABLED=true`, `PROTOCOL_CONVERSION_ENABLED=false`.
- No conversion route configured in production. No Anthropic Messages provider verified.
- LiteLLM submodule pinned `0430743f2fd4005898506e00bc62dd47bcff6fc9`. One uncommitted local change in `upstream/litellm/.../tokenizers/9b5ad...` must not be committed.

### 1.3 Worktree state

The entire protocol/conversion implementation is **uncommitted** (~13 modified + ~20 untracked files). Baseline-stability risk: must establish reviewable, bisectable commits before adding new code.

## 2. Gap Analysis (code-verified, supersedes report claims)

### 2.1 Report-acknowledged gaps (R1-R4)

**R1 (Fatal):** Conversion rewrites request body but not `route_type` or model prefix. `openai/<model>` deployment + Messages request may hit `/responses` not `/chat/completions`. Evidence: `strategy.py:785-804`, `generator.py:50-60`, `tests/contract/test_p0_direct_protocol_paths.py:332-363`.

**R2 (High):** Conversion-only Messages rejected pre-call. `assert_endpoint_allowed()` requires direct `anthropic_messages` upstream. Evidence: `protocol_gates.py:81-113` vs `config_schema.py:423-519`.

**R3 (High success / Fatal failure):** Success: LiteLLM v1.90.5 propagates `async_post_call_success_hook` return to client (verified `proxy/utils.py:2337-2435`). Failure: `async_post_call_failure_hook` returns `None`; `convert_upstream_error()` never called; LiteLLM only honors `HTTPException` returns. Evidence: `callbacks.py:216-229`, `conversion/dispatch.py:118-124`, `proxy/utils.py:2056-2189`.

**R4 (Medium):** Affinity stores `deployment_id` only, no `route_mode`. Evidence: `state_store.py:340-381`.

### 2.2 Hidden gaps NOT in reports

**H1 (Fatal):** `SharedQuotaRoutingStrategy._selector_for()` constructs `SharedQuotaSelector` WITHOUT `logical_models` map. `resolve_route()` receives `logical=None` and rejects ALL conversion candidates. **Production conversion is dead code even with both flags on.** C2 tests pass only via monkeypatch. Evidence: `strategy.py:571-573`, `conversion/registry.py:45-49`, `test_c2_messages_to_chat_pilot.py:268-285`.

**H2 (High):** Generator does not emit `logical_models` policy (`allow_conversion`, `allowed_conversions`). Runtime registry built from `model_list` cannot recover logical policy. Evidence: `generator.py:194-205`.

**H3 (High):** `on_failure()` returns immediately for `ProtocolAwareRoutingError` before releasing lease. Combined with H4, post-selection adapter failures leak leases. Evidence: `callbacks.py:320-342`.

**H4 (High):** Content-block features (image/thinking/tool_use/tool_result) NOT extracted pre-lease. Adapter rejects post-selection, after lease acquired. No `try/finally` around `_apply_convert_to_request_kwargs`. Evidence: `protocol_context.py:106-132`, `conversion/adapters/messages_to_chat.py:149-181`, `strategy.py:705-721`.

**H5 (Medium):** `convert_upstream_error()` is dead code. `async_post_call_failure_hook` never calls it. Evidence: `callbacks.py:216-229` vs `conversion/dispatch.py:118-124`.

**H6 (Medium):** `ProtocolAwareRoutingError.to_public_error()` exists but never called in proxy path. Gate errors fall through to generic `ProxyException`, not native protocol shape. Evidence: `protocol_errors.py:45-95`, no caller.

**H7 (Medium):** Adapter silently drops optional params (temperature/top_p/stop) with `dropped_fields=[]`. Violates design §6.6. Evidence: `conversion/adapters/messages_to_chat.py:70-99`.

**H8 (Low):** Stale docs: `docs/architecture.md:28-36`, `README.md:56-57`, `docs/tasks.md:451` heading, `e2e-verification-m1.md:44-50`. Test counts conflict: 113/160/197.

### 2.3 Direction mismatch (product-critical)

Only registered adapter: `anthropic_messages (public) -> openai_chat (upstream)`. This supports unified Messages external API over Chat upstreams, NOT unified Chat over Anthropic upstreams. Progress report §3.2 P1 row is directionally inverted. Serving Anthropic upstream behind Chat public API needs reverse adapter (`openai_chat -> anthropic_messages`), declared in C1 matrix but unimplemented.

## 3. LiteLLM v1.90.5 Boundary (verified this session)

- `metadata.protocol` reaches strategy for Chat: PASS.
- `litellm_metadata.protocol` reaches strategy for Messages/Responses: PASS.
- `route_type` is proxy-internal, not in strategy contract: verified.
- `async_post_call_success_hook` return replaces client response: PASS.
- `async_post_call_failure_hook` return transforms error only if `HTTPException`: verified. **None = no transform.**
- Messages + `openai/` prefix misroutes to `/responses`: PASS (negative proof).
- `async_post_call_streaming_hook` must not be overridden (corrupts SSE): verified.

**Key new finding:** G0-B is viable for success reshaping. It is NOT viable for failure reshaping without raising `HTTPException` from the failure hook. This was not in the C2 spike report.

## 4. Decision Gate: External Protocol Strategy

Blocks all Phase 2+ work. Three options:

- **Option A:** Unified Chat only. Needs `chat -> messages` adapter (unimplemented) or LiteLLM native spike. Existing adapter is wrong direction.
- **Option B:** Unified Messages only. Needs NewAPI verification + R1-R3 hardening of existing adapter.
- **Option C (design doc target, recommended):** Multi-protocol public + same-protocol direct + cross-protocol conversion. Both adapter directions needed for full coverage. Sequence by actual client need.

Given provider inventory (Chat verified, Anthropic unverified, Responses absent):
1. If NewAPI = Anthropic Messages: build `chat -> messages` adapter so Chat clients reach Anthropic upstream. Existing adapter serves Messages clients reaching Chat upstreams.
2. If NewAPI = Chat-compatible: no conversion needed; direct Chat for both.
3. Responses: No-Go until verified provider exists.

## 5. Phased Development Plan

### Phase 0: Baseline Stabilization (no runtime changes)

**Goal:** Reviewable, bisectable commit history; stale docs fixed.

- **P0-01:** Commit current uncommitted implementation in atomic milestone commits (M1/M2/M3/M4/C1/C2/C3/C4-eval/C5-eval/docs). Do NOT commit `upstream/litellm` tokenizer change.
  - Acceptance: `git status` clean except submodule; `pytest -q` green at each commit.
- **P0-02:** Fix stale docs (`architecture.md`, `README.md`, `tasks.md:451` heading, `e2e-verification-m1.md`). Reconcile test count to one verified number.
- **P0-03:** Isolate or revert `upstream/litellm` tokenizer file; confirm submodule matches pinned SHA.

### Phase 1: Fix Hidden Production-Correctness Gaps (no conversion enablement)

**Goal:** Make the conversion code path correct and safe even while flag stays false. These are bugs independent of R1-R3.

- **P1-01 (H1):** Transport `logical_models` into runtime selector.
  - Fix `_selector_for()` to pass `logical_models` from validated plans or generated config.
  - Add unmocked `get_available_deployment()` test with conversion-configured model list.
  - Acceptance: `resolve_route()` returns convert candidate when policy allows; no monkeypatch needed.
- **P1-02 (H2):** Generator emits `logical_models` policy section.
  - Add `allow_conversion` / `allowed_conversions` to generated `litellm.yaml`.
  - Runtime loader reconstructs `LogicalModelProtocols` from `model_list` + generated policy.
  - Acceptance: round-trip parse -> generate -> parse yields identical policy.
- **P1-03 (H3):** Release lease before early-return in `on_failure()` for `ProtocolAwareRoutingError`.
  - Move lease release before the protocol-error early return.
  - Add test: deterministic conversion error releases lease; no quota mutation.
- **P1-04 (H4):** Extract content-block features pre-lease.
  - Extend `extract_required_features()` to scan `messages[].content[]` for image/thinking/tool_use/tool_result.
  - Reject unsupported content blocks before lease acquisition, not after.
  - Add `try/finally` around `_apply_convert_to_request_kwargs` in strategy.
- **P1-05 (H7):** Adapter must declare dropped optional fields.
  - `MessagesToChatConverter.convert_request()` must populate `dropped_fields` for temperature/top_p/stop/etc.
  - Dispatch rejects if `dropped_fields` contains non-allowlisted fields.
  - Add fixtures with optional params; assert rejection or explicit allowlist.

### Phase 2: Mount Error and Gate-Error Reshaping

**Goal:** Client receives native-protocol error shapes for both gate failures and upstream failures.

- **P2-01 (H6):** Wire `to_public_error()` into the proxy error path.
  - `async_post_call_failure_hook` must convert `ProtocolAwareRoutingError` to `HTTPException` with native status/body and return it (not None).
  - Verify LiteLLM `proxy/utils.py:2056-2189` honors the returned `HTTPException`.
  - Test: gate rejection (UNSUPPORTED_PUBLIC_PROTOCOL) returns Anthropic-shaped error for Messages endpoint, OpenAI-shaped for Chat endpoint.
- **P2-02 (H5):** Mount `convert_upstream_error()` for upstream failures on convert routes.
  - When `route_mode=convert` and upstream returns error, convert to public protocol error shape before returning `HTTPException`.
  - Test: upstream Chat 400 -> Anthropic-shaped `type: error` response to client.

### Phase 3: R2 Public-Reachability Predicate

**Goal:** Conversion-only Messages passes the pre-call gate when a valid conversion route exists.

- **P3-01:** Add `public_reachable` predicate to `protocol_gates.py`.
  - Direct verified upstream OR explicit, active, feature-compatible conversion route.
  - Must check: dual flags, logical `allow_conversion`, exact direction allowlist, registered adapter existence, request feature fidelity, non-streaming, target deployment capability.
  - Must NOT treat conversion YAML alone as verified reachability.
  - Responses remains direct-only (C5 gate preserved).
- **P3-02:** Tests: conversion-only config passes gate when flags on; fails when either flag off; fails for unsupported features; fails for unregistered adapter; fails for Responses conversion.

### Phase 4: R1 + R3 Live Proof (mock first, then real provider)

**Goal:** Prove the actual proxy path hits the correct upstream endpoint and the client receives the correct response shape.

- **P4-01 (R1 mock):** Conversion-enabled Router/proxy contract test.
  - Configure one conversion-only deployment against local mock.
  - Send actual `POST /v1/messages` through proxy.
  - Assert mock receives `/chat/completions` (not `/responses`).
  - If mock receives `/responses` under **default** settings: note G0-B limitation; **next** try LiteLLM native switch (`use_chat_completions_url_for_anthropic_messages`) — see **P4-Native** / G0-Native Spike. Do not patch `upstream/litellm`.
  - Escalate to thin **G0-A** only if native spike fails shared-quota contracts.
- **P4-Native (preferred):** Stock `/v1/messages` + `use_chat_completions_url_for_anthropic_messages=true` → `/chat/completions`; disable project G0-B double rewrite; prove single accounting. Plan: `docs/superpowers/plans/2026-07-26-g0-native-messages-chat-spike.md`.
- **P4-G0A (fallback):** Route-swap contracts — blocked until Native Spike fails; design §4 P0/P1 closed first.
- **P4-02 (R3 success mock):** Assert client receives Anthropic-shaped success.
  - Verify `type: message`, `content[].type: text`, `usage.input_tokens/output_tokens`, `stop_reason`.
- **P4-03 (R3 failure mock):** Assert client receives Anthropic-shaped error after P2-02.
  - Upstream Chat 400 -> Anthropic `type: error` with mapped `error.type`.
- **P4-04 (real provider probe):** Operator-run only, credentials in env.
  - Probe NewAPI to determine actual protocol (Chat vs Messages vs both).
  - If Messages: configure one logical model with direct Messages; verify path + response.
  - If Chat: no conversion needed for that provider.
  - Record `verified_at` + evidence; no key/prompt logging.
- **P4-05:** If NewAPI is Anthropic Messages and Option C is chosen: implement `openai_chat -> anthropic_messages` adapter (reverse direction). New fixtures, new fidelity matrix row, new tests. Separate from C2 pilot.

### Phase 5: Staging Canary (only after Phase 4 all green)

- **P5-01:** Enable both flags on staging for one explicitly configured logical model.
- **P5-02:** Restrict to text, non-streaming. Observe `shared_quota_protocol_route_total{route_mode=convert}`.
- **P5-03:** Rollback: `PROTOCOL_CONVERSION_ENABLED=false`; Redis quota/affinity preserved.
- **P5-04:** Do NOT enable C4 streaming or C5 Responses.

## 6. Risk Register

| Risk | Prob | Impact | Mitigation |
|---|---|---|---|
| R1: `openai/` prefix still misroutes Messages to `/responses` | High | Fatal | P4-01 under default settings; then **P4-Native** switch; G0-A only if native fails |
| H1: Production conversion is dead code | Certain | Fatal | P1-01 fixes; currently masked by flag=false |
| LiteLLM failure hook does not propagate non-HTTPException | Verified | High | P2-01/P2-02 return HTTPException |
| Post-lease adapter failure leaks lease | High | High | P1-03 + P1-04 |
| Direction mismatch blocks Chat-unified goal | High | High | §4 decision gate before Phase 2 |
| Streaming conversion (C4) reopened prematurely | Low | High | Keep No-Go; separate epic |
| Responses conversion smuggled in | Low | High | C5 gate preserved; config validation rejects |
| `upstream/litellm` tokenizer change committed | Medium | Medium | P0-03 isolate |

## 7. Stop / Escalation Conditions

Stop and revise design when:
1. P4-01 under default settings hits `/responses` even with G0-B rewrite → try **G0-Native** (`use_chat_completions_url_for_anthropic_messages`); escalate to thin G0-A only if Native Spike fails shared-quota contracts. Update ADR.
2. `metadata.protocol` stops reaching strategy in a future LiteLLM patch -> re-run P0 contract tests before upgrade.
3. A required protocol path needs business changes in `upstream/litellm`.
4. Capability filtering cannot run before lease acquisition for a required endpoint.
5. Tests require disabling type checking, deleting assertions, or exposing secrets.
6. More than three consecutive implementation attempts fail for the same task.

## 8. Out of Scope (separate epics)

- Streaming conversion (C4 No-Go; needs dedicated stream adapter + 8 invariants).
- Direct Responses enablement (C5 No-Go; needs verified Responses provider).
- Responses <-> Chat/Messages conversion (forbidden in this epic).
- Quota collectors / balance scraping / cookie login.
- Cross-model fallback.

## 9. Acceptance Criteria (overall)

- [x] Phase 0 (partial): stale docs fixed (`architecture.md`, `README.md`); submodule tokenizer dirty file reverted. **Atomic milestone commits deferred** (ask before committing large uncommitted tree).
- [x] Phase 1: H1–H4, H7 fixed; production conversion path is live-code-correct (still flag-off).
- [x] Phase 2: gate errors and upstream convert errors can return `HTTPException` with native shape.
- [x] Phase 3: conversion-only Messages passes pre-call gate via `public_reachable` when flags on.
- [x] Phase 4: mock probe ran — G0-B default still `/responses` → ADR escalate. **Revised:** prefer G0-Native Spike before G0-A; Phase 5 blocked on proven path (native or unblocked G0-A).
- [ ] Phase 5: staging canary — **blocked** on G0-Native (or G0-A fallback) + positive path assertion.
- [x] LiteLLM remains pinned `v1.90.5`; no `upstream/litellm` business edits.
- [x] Redis fail-closed; first-byte hard gate; no cross-model fallback preserved.
- [x] No secrets in logs/config/fixtures.

## 10. Execution notes (2026-07-26)

| Item | Result |
|------|--------|
| H1 logical_models → selector | Fixed (`logical_policy.py` + strategy/bootstrap) |
| H2 generator emits policy | Fixed (`shared_quota_logical_models`) |
| H3 lease on protocol failure | Fixed |
| H4 content-block features + convert try/finally | Fixed |
| H7 dropped optional fields | Fixed (declared → dispatch reject) |
| H5/H6 error reshape | Fixed (failure hook → HTTPException) |
| R2 public_reachable | Fixed |
| R1 path | **Failed under default G0-B** → ADR revised: **G0-Native first**, G0-A fallback |
| Phase 5 | Blocked on Native Spike (or unblocked G0-A) |

**Production posture unchanged:** `PROTOCOL_CONVERSION_ENABLED=false`. Staging enable only after checklist: native switch on + dual flags + P4-Native green.

### 10.1 Follow-up (same day) — native switch falsifies “swap-only” premise

Workspace probe + contract: `use_chat_completions_url_for_anthropic_messages=true` → mock `/chat/completions`.  
Implemented: G0-Native Spike (flags path-ready, disable G0-B rewrite under native, P4-Native tests). G0-A plan remains **BLOCKED**.

### 10.2 G0-Native Spike ops checklist

```text
[ ] litellm_settings.use_chat_completions_url_for_anthropic_messages: true (or env)
[ ] PROTOCOL_AWARE_GATEWAY_ENABLED=true
[ ] PROTOCOL_CONVERSION_ENABLED=true (staging only)
[x] P4-Native contract green (tests/contract/test_p4_native_messages_to_chat_path.py)
[x] direct anthropic/ still /messages with native on
[x] no G0-B rewrite when native on (unit)
[x] conversion-only denied without path ready
[ ] operator: real proxy smoke + spend/lease canary
[ ] rollback drill: CONVERSION=false; optional native off + restart
```

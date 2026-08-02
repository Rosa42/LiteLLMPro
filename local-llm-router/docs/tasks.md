# Protocol-Aware Multi-API Gateway Development Tasks

**Source design:** `docs/protocol-aware-multi-api-gateway-plan.md`  
**Project:** `E:\LiteLLMPro\local-llm-router`  
**LiteLLM pin:** `v1.90.5`  
**Status:** Conversion epic **CLOSED** (C0–C5 evaluated; C4/C5 No-Go); **staging conversion blocked** — see `docs/phase-reports/conversion-residual-risks.md`  
**Scope:** Direct-protocol MVP complete; C2 messages→chat pilot landed (flag default false); C3 circuit isolation landed; dual-flag AND enforced  
**Implementation plan:** `docs/superpowers/plans/2026-07-26-c1-c5-cross-protocol-conversion.md`  
**Last progress update:** 2026-07-26

**C0 kickoff (2026-07-26):** Pilot direction `anthropic_messages → openai_chat`; production `PROTOCOL_CONVERSION_ENABLED` stays false until C2-05; staging flag-on requires C3-01 **and** residual R1–R3 clear.

## 0. Implementation Progress Board

| ID | Task | Status | Evidence / deliverables |
|----|------|--------|-------------------------|
| P0-01 | Endpoint & strategy contracts | **DONE** | `docs/phase-reports/protocol-gateway-phase-0-compatibility.md` |
| P0-02 | Metadata propagation harness | **DONE** | `tests/contract/test_p0_protocol_metadata_propagation.py` |
| P0-03 | Direct protocol path contracts | **DONE** | `tests/contract/test_p0_direct_protocol_paths.py`, multi-protocol mock |
| G0 | Integration boundary ADR | **DONE** | **G0-B** selected — `docs/adr/ADR-protocol-gateway-integration-boundary.md` |
| M1-01 | Protocol / feature enums | **DONE** | `plugins/shared_quota_router/models.py` (`ApiProtocol`, `Feature`) |
| M1-02 | Deployment capability metadata | **DONE** | `Deployment.upstream_protocol` + `registry.py` |
| M1-03 | Config schema & validation | **DONE** | `config_schema.py`, `plans.yaml` / `plans.example.yaml` |
| M1-04 | Config generator | **DONE** | `generator.py`, `cli_config.py`, PS1 `apply` → Python |
| M1-05 | Model discovery public_protocols | **DONE** | `discovery.py`, `GET /v1/router/model-capabilities`, docs |
| M2-01 | Request protocol context | **DONE** | `protocol_context.py`; dual-bucket + `async_pre_call_hook` inject |
| M2-02 | Pre-lease capability filter | **DONE** | `strategy.filter_by_capability` before Redis/lease |
| M2-03 | Affinity after capability filter | **DONE** | incompatible affinity ignored; dual-bucket session key |
| M2-04 | Lease / first-byte invariants | **DONE** | mismatch consumes no lease/tried; callback skips circuit |
| M2-05 | Protocol-aware no-route errors | **DONE** | `ProtocolAwareRoutingError` OpenAI/Anthropic shapes |
| M3-01 | Direct Chat enable + regression | **DONE** | Chat opt-in + `openai/` prefix gate; `test_m3_*` |
| M3-02 | Messages gate / direct | **DONE** | Disabled without verified opt-in; name≠capability |
| M3-03 | Responses controlled disable | **DONE** | Default `protocol_not_enabled`; enable docs |
| M3-04 | drop_params / feature validation | **DONE** | `drop_params: false`; tools/stream rejected pre-drop |
| M4-01 | Protocol observability | **DONE** | `protocol_observability.py`; route/reject counters; label hash |
| M4-02 | Feature-flag rollout / rollback | **DONE** | `PROTOCOL_AWARE_GATEWAY_ENABLED`; ops doc; Redis preserved |
| M4-03 | Full verification + MVP report | **DONE** | `docs/phase-reports/protocol-gateway-mvp.md` (160 pytest green) |
| MVP-GATE | Acceptance checklist | **PASSED** | §8 all required items checked |
| C0 | Conversion kickoff gate | **DONE** | 2026-07-26; pilot `anthropic_messages→openai_chat`; prod conversion off until C2-05 |
| C1-01 | Fidelity / ConversionCapability domain | **DONE** | `models.py` enums + `RouteCandidate` |
| C1-02 | Directional fidelity matrix | **DONE** | `conversion/contracts.py` |
| C1-03 | Config allow_conversion + conversions | **DONE** | `config_schema` / `generator` / `registry` |
| C1-04 | `resolve_route` direct before convert | **DONE** | `conversion/registry.py`; `docs/phase-reports/conversion-c1.md` |
| C2-01 | `PROTOCOL_CONVERSION_ENABLED` + metrics | **DONE** | `record_conversion_result`; `docs/operations-protocol-conversion.md` |
| C2-02 | G0-B request/response conversion spike | **DONE** | Go (conditional); `docs/phase-reports/conversion-c2-spike-g0b.md` |
| C2-03 | Pilot adapter messages→chat (text) | **DONE** | `conversion/adapters/messages_to_chat.py` + fixtures |
| C2-04 | Wire select + dispatch (prod off) | **DONE** | strategy kwargs mutate + post_call response convert |
| C2-05 | C2 acceptance evidence pack | **DONE** | `docs/phase-reports/conversion-c2-pilot.md` |
| C3-01 | Conversion-path circuit isolation | **DONE** | `docs/phase-reports/conversion-c3.md`; route-scoped cooldown |
| C4-01 | Evaluate streaming conversion | **DONE (No-Go)** | `docs/conversion/streaming-evaluation.md` |
| C5-01 | Evaluate direct Responses enablement | **DONE (No-Go)** | `docs/conversion/responses-direct-evaluation.md` |
| C-CLOSE | Conversion epic closure | **DONE** | Board synced; suite green; residual risks: `docs/phase-reports/conversion-residual-risks.md` |

### What works in production path today (MVP)

- Plans declare `upstream_protocol` / `logical_models.public_protocols`.
- Generator emits capability metadata; `drop_params: false`; atomic backups.
- Discovery via `GET /v1/router/model-capabilities`.
- With `PROTOCOL_AWARE_GATEWAY_ENABLED=true`: pre-lease protocol/feature filter + public opt-in gates.
- Messages/Responses controlled disabled until verified deployments + opt-in.
- Protocol route/reject metrics with optional label hashing; conversion metrics dormant.
- Rollback: flag off (legacy Chat) or restore `config/backups/*.bak` — Redis quota kept.

### What does **not** work yet (post-MVP)

- **Production** cross-protocol conversion (C1–C3 code exists; flag default false; staging blocked on residual R1–R3).
- Streaming conversion (C4 **No-Go**).
- Direct `/v1/responses` enablement and any Responses↔* conversion (C5 **No-Go** / out of epic).
- Live NewAPI / Messages / Responses providers in `plans.yaml` (NewAPI disabled, protocol unset).
- Unified public API serving heterogeneous upstreams (progress gap: `docs/phase-reports/unified-api-vs-multi-protocol-progress.md`).
- Repo-wide mypy clean on this venv (numpy stubs / types-PyYAML).

## 1. Delivery Rules

Every task in this document must preserve these repository invariants:

1. Do not add business logic to `upstream/litellm`.
2. Keep LiteLLM pinned to `v1.90.5` unless a separate approved upgrade task exists.
3. Keep `model_group` separate from `quota_group_id`.
4. Do not add default cross-model fallback.
5. Treat Redis errors as fail-closed.
6. Do not retry or switch deployment after visible stream output.
7. Try one quota group at most once per request and at most three quota groups total.
8. Do not retry `BAD_REQUEST` or `CONTENT_POLICY` across accounts.
9. Never infer protocol from model name, provider name, URL shape, or provider prefix.
10. Do not probe protocol endpoints dynamically in production request paths.
11. Do not log API keys, Authorization headers, full prompts, or full responses.
12. Do not commit or push unless explicitly requested.
13. Every task must add or update tests before it is marked complete.

## 2. Scope Boundary

### MVP scope

- Explicit protocol and capability metadata.
- Phase 0 verification of LiteLLM v1.90.5 request metadata propagation.
- Direct `openai_chat -> openai_chat` routing.
- Direct `anthropic_messages -> anthropic_messages` only after a provider passes contract tests.
- `/v1/responses` controlled disabled/no-route behavior until a verified Responses deployment exists.
- Capability filtering before quota lease acquisition.
- Protocol-aware model opt-in, errors, metrics, rollout, and rollback.

### Not in MVP

- Cross-protocol conversion.
- Streaming conversion.
- Runtime protocol discovery.
- Automatic protocol fallback after an upstream error.
- Responses exposure without a verified direct Responses deployment.
- Anthropic Messages exposure based only on a Claude-like model name.

Post-MVP conversion tasks are decomposed in Section 12 (C0–C-CLOSE) from
`docs/superpowers/plans/2026-07-26-c1-c5-cross-protocol-conversion.md` and must
not start runtime work before **C0** kickoff.

## 3. Dependency Graph

```text
[x] P0-01 -> [x] P0-02 -> [x] P0-03 -> [x] G0 (chose G0-B)
                                        |
                              [x] G0-B  (G0-A rejected for now)
                                        |
[x] M1-01 -> [x] M1-02 -> [x] M1-03 -> [x] M1-04 -> [x] M1-05
                                        |              |
                                        +------+-------+
                                               |
                                  [x] M2-01 -> ... -> [x] M2-05
                                               |
                                  [x] M3-01 -> ... -> [x] M3-04
                                               |
                                  [x] M4-01 -> [x] M4-02 -> [x] M4-03 -> [x] MVP-GATE
                                               |
                                  [x] C0 (kickoff)
                                       |
                  +--------------------+--------------------+
                  |                                         |
                  v                                         v
            [x] C1-01 -> C1-02 -> C1-03 -> C1-04      [x] C5-01 (No-Go)
                  |          |                              |  (orthogonal:
                  |          +---> [x] C2-03                |   direct Responses
                  |                 adapter                 |   eval; no conversion)
                  v                                         |
     [x] C2-01 -+                                           |
     [x] C2-02 -+--> [x] C2-04 -> C2-05 -> [x] C3-01        |
       (parallel OK)              |              |          |
                                  |              +--rec--> [x] C4-01 (No-Go)
                                  |                 (hard dep: C2-05)
                                  +-------------------------+
                                                            |
                                                      [x] C-CLOSE
```

**Legend:** solid arrows = hard depends; `rec` = recommended before staging conversion / failure-path eval.  
**MVP-GATE PASSED** (2026-07-26). **Conversion epic CLOSED** (2026-07-26); C4/C5 No-Go. Plan: `docs/superpowers/plans/2026-07-26-c1-c5-cross-protocol-conversion.md`.

## 4. Phase 0: LiteLLM v1.90.5 Compatibility Validation

### P0-01 Record verified endpoint and strategy contracts — **DONE** (2026-07-26)

**Goal:** Replace architectural assumptions with source-backed facts for Chat, Responses, and Messages request paths.

**Depends on:** None  
**Blocks:** P0-02  
**Parallel:** Can run alongside provider inventory preparation only

**Read scope:**

- `upstream/litellm/litellm/proxy/common_request_processing.py`
- `upstream/litellm/litellm/proxy/anthropic_endpoints/endpoints.py`
- Responses endpoint implementation under `upstream/litellm/litellm/proxy/`
- `upstream/litellm/litellm/types/router.py`
- `plugins/shared_quota_router/strategy.py`

**Work:**

- Record the exact `route_type` values for `/v1/chat/completions`, `/v1/responses`, and `/v1/messages`.
- Confirm `route_type` is proxy-internal and not part of the custom strategy contract.
- Record the exact `get_available_deployment()` signature and strategy inputs.
- Record where `messages`, `input`, `metadata`, request ID, selected deployment metadata, and stream callbacks are populated.
- Do not modify `upstream/litellm`.

**Deliverable:**

- `docs/phase-reports/protocol-gateway-phase-0-compatibility.md`

**Verification:**

- Every claim includes file and line references against the pinned source.
- `git diff -- upstream/litellm` is empty.

**Done when:** The report distinguishes verified fields from fields requiring runtime contract tests.

**Completion:** `docs/phase-reports/protocol-gateway-phase-0-compatibility.md` §P0-01.

### P0-02 Add protocol metadata propagation contract harness — **DONE** (2026-07-26)

**Goal:** Determine whether `metadata.protocol` can carry the public protocol into the custom strategy and callbacks.

**Depends on:** P0-01  
**Blocks:** P0-03  
**Files:**

- New contract test under `tests/contract/`
- Test-only strategy/callback fixtures

**Work:**

- Mount a test custom routing strategy against LiteLLM `v1.90.5`.
- Send one minimal request to each endpoint:
  - `/v1/chat/completions`
  - `/v1/responses`
  - `/v1/messages`
- Inject the expected protocol in `metadata.protocol` at the earliest project-owned endpoint boundary available to the test.
- Capture what reaches:
  - `request_kwargs` in strategy selection.
  - Success and failure callbacks.
  - Streaming callback metadata.
- Record `messages` and `input` only as validation evidence; do not use them as protocol inference.
- Use fake keys and local mock providers only.

**Tests:**

- Chat metadata reaches the strategy and callback, or the test records a clear failure.
- Responses metadata reaches the strategy and callback, or the test records a clear failure.
- Messages metadata reaches the strategy and callback, or the test records a clear failure.
- No test logs contain headers or prompt content.

**Done when:** Results are deterministic and documented for all three endpoint paths.

**Completion:** `tests/contract/test_p0_protocol_metadata_propagation.py`; phase-0 report §P0-02. Chat uses `metadata`; Messages/Responses use `litellm_metadata`.

### P0-03 Verify direct protocol entry points and provider contracts — **DONE** (2026-07-26)

**Goal:** Prove which direct paths are implementable before exposing them.

**Depends on:** P0-02  
**Blocks:** G0  
**Files:**

- Contract tests under `tests/contract/`
- Local mock-provider fixtures
- Phase 0 compatibility report

**Work:**

- Verify the LiteLLM entry point and model-selection behavior for direct Chat.
- Verify structured error and streaming callback behavior for direct Chat.
- Verify direct Anthropic Messages against a local mock first.
- Test the configured NewAPI deployment only through a controlled operator-run probe using environment-held credentials; never store its key or response payload in source.
- Determine whether NewAPI is Chat-compatible, Messages-compatible, both, or neither.
- Verify Responses only against a local mock unless a real Responses provider is explicitly configured.
- Record supported authentication header style, path behavior, text, tools, usage, stream, and errors for each verified provider.

**Tests:**

- Direct Chat request reaches `/v1/chat/completions` on the mock provider.
- Direct Messages request reaches `/v1/messages` on the mock provider.
- Direct Responses request reaches `/v1/responses` on the mock provider.
- Selected `deployment_id` and `quota_group_id` remain available to callbacks.
- First visible stream event marks the retry boundary.

**Done when:** The report contains a capability inventory with `verified_at`, evidence, and explicit unknowns.

**Completion:** `tests/contract/test_p0_direct_protocol_paths.py`; multi-protocol `mock_provider.py`. NewAPI remains **unverified**. Messages + `openai/` prefix misroutes to `/responses` (documented).

### G0 Choose the integration boundary — **DONE** (2026-07-26) → **G0-B**

**Goal:** Select one feasible architecture based on P0 evidence.

**Depends on:** P0-03  
**Blocks:** All M1 tasks

#### G0-B: Metadata integration

Choose only if `metadata.protocol` reliably reaches strategy selection and callbacks for every MVP endpoint.

#### G0-A: Project-owned front gateway or isolated protocol lanes

Choose if metadata propagation is missing, inconsistent, or unavailable before quota selection.

**Decision record must include:**

- Evidence from P0 tests.
- Selected boundary and rejected alternative.
- How request ID, protocol, selected deployment, quota lease, callbacks, structured errors, and first-visible-event state cross the boundary.
- Why no undocumented LiteLLM hook is required.
- Rollback boundary.

**Deliverable:**

- `docs/adr/ADR-protocol-gateway-integration-boundary.md`

**Stop condition:** If neither option preserves quota leases and first-byte semantics without modifying upstream business logic, stop implementation and revise the architecture.

**Decision:** **G0-B** accepted. Deliverable: `docs/adr/ADR-protocol-gateway-integration-boundary.md`. G0-A deferred as fallback only.

## 5. Milestone 1: Protocol and Capability Domain Model — **COMPLETE**

### M1-01 Add protocol and feature enums — **DONE** (2026-07-26)

**Depends on:** G0  
**Blocks:** M1-02

**Files:**

- `plugins/shared_quota_router/models.py`
- New unit tests

**Work:**

- Add `ApiProtocol`: `openai_chat`, `openai_responses`, `anthropic_messages`.
- Add the minimal MVP feature set: `text`, `streaming`, `tools`.
- Keep post-MVP features such as reasoning, prompt cache, parallel tools, citations, and conversion fidelity out of runtime code until required.
- Add explicit public-protocol declarations for logical models in a project-owned configuration model.

**Tests:**

- Unknown protocol values fail validation.
- Missing protocol does not imply universal support.
- Enum values serialize as stable strings.

**Done when:** Protocol values are typed and protocol inference is unnecessary.

**Completion:** `ApiProtocol`, `Feature`, `LogicalModelProtocols` in `models.py`; `tests/unit/test_protocol_models.py`.

### M1-02 Extend Deployment with direct capability metadata — **DONE** (2026-07-26)

**Depends on:** M1-01  
**Blocks:** M1-03

**Files:**

- `plugins/shared_quota_router/models.py`
- `plugins/shared_quota_router/registry.py`
- Registry unit tests

**Work:**

- Add `upstream_protocol`.
- Add `supported_features` and `supports_streaming`.
- Keep `model_group`, `quota_group_id`, provider, priority, weight, and enabled semantics unchanged.
- Parse capability metadata from generated `model_info`.
- Treat missing protocol metadata as configuration-invalid for protocol-aware routes.

**Tests:**

- Registry parses explicit protocol and feature metadata.
- Registry preserves model and quota grouping.
- Missing metadata never becomes universal support.
- One model group may contain deployments with different upstream protocols.

**Done when:** The registry provides sufficient data for pre-lease capability filtering.

**Completion:** `Deployment.upstream_protocol` / `supported_features` / `supports_streaming`; registry parse + `filter_by_protocol`.

### M1-03 Define configuration schema and validation — **DONE** (2026-07-26)

**Depends on:** M1-02  
**Blocks:** M1-04

**Files:**

- `config/plans.yaml`
- `config/plans.example.yaml`
- Optional `config/protocols.yaml` if selected by G0
- Project-owned validation code/tests

**Work:**

- Add explicit plan/deployment `upstream_protocol`.
- Add explicit logical-model `public_protocols`.
- Ensure plan-level upstream protocol never grants public exposure automatically.
- Keep NewAPI protocol unset/disabled until P0 evidence identifies it.
- Reject public protocol opt-in without a verified direct route.
- Reject unknown protocols, duplicate IDs, and empty public-protocol declarations.
- Keep credential values in `.env` only.

**Tests:**

- OpenCode Go and Volc validate as `openai_chat`.
- NewAPI remains disabled if protocol is unknown.
- Responses public opt-in fails without a Responses deployment.
- Logical model without `public_protocols` is unavailable everywhere.
- Secret values are not emitted.

**Done when:** Invalid protocol configurations fail before LiteLLM starts.

**Completion:** `config_schema.py`; plans require explicit `logical_models.public_protocols`; NewAPI `enabled: false` without protocol; `tests/unit/test_config_schema.py`.

### M1-04 Update the configuration generator — **DONE** (2026-07-26)

**Depends on:** M1-03  
**Blocks:** M1-05, M2-01

**Files:**

- `scripts/llm-router.ps1`
- Generated `config/litellm.yaml`
- Generator tests

**Work:**

- Stop hard-coding every deployment solely as `openai/<model>` without protocol context.
- Generate protocol-specific LiteLLM parameters based only on verified metadata and G0's selected boundary.
- Generate `model_info.upstream_protocol`, supported features, public protocol metadata, and existing account fields.
- Preserve environment references rather than values.
- Create a timestamped backup of generated `litellm.yaml` before replacement.
- Write atomically: generate temporary file, validate, then replace.
- Keep output ASCII-safe for the Windows execution path.

**Tests:**

- Generated OpenCode Go and Volc entries remain Chat-only.
- No Responses entry is generated without verified capability.
- Unknown NewAPI protocol produces a disabled or rejected deployment.
- Generated output contains no secret values.
- Existing model, account, quota, priority, and timeout fields remain intact.
- Invalid generation leaves the previous file untouched.

**Done when:** `apply` is deterministic, fail-closed, atomic, and reversible.

**Completion:** `generator.py` + `cli_config.py`; `llm-router.ps1 apply` calls Python; timestamped backups under `config/backups/`; `tests/unit/test_generator.py`.

### M1-05 Add protocol metadata to model discovery — **DONE** (2026-07-26)

**Depends on:** M1-04  
**Blocks:** M3-01

**Files:** Selected model-list integration boundary from G0, tests, docs

**Work:**

- Keep one model entry per logical model.
- Add `metadata.public_protocols` only if the selected boundary preserves it safely.
- Document that presence in `/v1/models` does not prove endpoint availability.
- If LiteLLM v1.90.5 cannot preserve custom metadata, expose a project-owned capability endpoint rather than changing model names.

**Tests:**

- Model listing never implies all protocols.
- Chat-only model lists only Chat capability.
- Unknown/disabled protocol is absent.

**Done when:** Clients can discover protocol opt-in without protocol-encoded model names.

**Completion:** Stock `/v1/models` does **not** preserve custom metadata (v1.90.5). Project endpoints:
`GET /v1/router/model-capabilities`, `GET /shared-quota/v1/model-capabilities`.
Docs: `docs/model-capability-discovery.md`. Tests: `tests/unit/test_discovery.py`.
E2E: `docs/phase-reports/e2e-verification-m1.md` (15/15 live checks).

## 6. Milestone 2: Capability-Aware Direct Routing — **NOT STARTED** (next)

### M2-01 Add request protocol and feature context — **DONE** (2026-07-26)

**Depends on:** M1-04  
**Blocks:** M2-02

**Files:** Determined by G0; likely `plugins/shared_quota_router/` and endpoint/front-gateway code

**Work:**

- Establish authoritative public protocol before request normalization.
- Carry only serializable protocol and feature values.
- Extract MVP-required features: text, streaming, tools.
- Never infer protocol from `messages`, `input`, model, provider, URL, or prefix.
- Do not place non-serializable request contexts in LiteLLM kwargs.

**Tests:**

- Protocol context is available before candidate selection.
- Chat, Messages, and disabled Responses requests have distinct protocol values.
- Serialization and exception paths do not fail on metadata.

**Done when:** Candidate selection receives an authoritative protocol and feature set.

**Completion:** `plugins/shared_quota_router/protocol_context.py`; callback `async_pre_call_hook`; `tests/unit/test_m2_protocol_routing.py` (M2-01 cases).

### M2-02 Filter protocol and features before state checks and lease acquisition — **DONE** (2026-07-26)

**Depends on:** M2-01  
**Blocks:** M2-03

**Files:**

- `plugins/shared_quota_router/strategy.py`
- Selector unit and contract tests

**Work:**

- Filter model-group deployments by direct `upstream_protocol` match.
- Filter by streaming and tool support.
- Keep conversion candidates out of MVP.
- Apply provider, quota-group, deployment-cooldown, and retry-context checks after capability filtering.
- Keep Redis fail-closed.
- Ensure capability mismatch does not consume a lease or retry budget.

**Tests:**

- Messages request excludes Chat-only deployments before Redis lease operations.
- Tool request excludes deployments without tools.
- Streaming request excludes deployments without streaming.
- Empty compatible set returns a protocol-aware no-route result.
- Protocol mismatch does not mutate provider, quota, or deployment state.

**Done when:** No incompatible deployment can reach ranking or lease acquisition.

**Completion:** `SharedQuotaSelector.filter_by_capability` + `select(protocol_ctx=...)`; mismatch raises before lease.

### M2-03 Apply affinity after capability filtering — **DONE** (2026-07-26)

**Depends on:** M2-02  
**Blocks:** M2-04

**Files:**

- `plugins/shared_quota_router/strategy.py`
- Affinity tests

**Work:**

- Resolve affinity only against the post-capability-filter candidate set.
- Ignore an incompatible affinity target without error.
- Continue normal priority, in-flight, last-success, and deployment-ID ranking.
- Preserve current session-key behavior and privacy constraints.

**Tests:**

- Incompatible affinity target is ignored.
- Compatible fallback deployment is selected.
- Ignored affinity does not consume a quota attempt.
- Compatible affinity still wins within the eligible set.

**Done when:** Affinity cannot bypass protocol or feature eligibility.

**Completion:** incompatible affinity ignored; `session_key_from_request` dual-bucket.

### M2-04 Preserve lease, retry, and first-byte invariants — **DONE** (2026-07-26)

**Depends on:** M2-03  
**Blocks:** M2-05

**Files:**

- `plugins/shared_quota_router/strategy.py`
- `plugins/shared_quota_router/callbacks.py`
- Integration tests

**Work:**

- Acquire leases only after capability and state filtering.
- Preserve one attempt per quota group and three groups maximum.
- Preserve no switch after `first_byte_sent`.
- Define first visible event at the selected public protocol boundary.
- Release leases on every pre-output success/failure and cancellation path.
- Do not retry deterministic protocol or capability errors.

**Tests:**

- No retry after visible stream output.
- No content from two deployments is concatenated.
- Client cancellation releases lease.
- Protocol mismatch consumes no lease.
- `BAD_REQUEST` and `CONTENT_POLICY` do not cross accounts.

**Done when:** Existing routing invariants pass with protocol-aware filtering enabled.

**Completion:** protocol mismatch → no lease / no tried; callback `should_allow_retry` false; no circuit update on protocol errors.

### M2-05 Add protocol-aware no-route and configuration errors — **DONE** (2026-07-26)

**Depends on:** M2-04  
**Blocks:** M3-01

**Files:**

- Project-owned error definitions/mapping
- Classifier and endpoint tests

**Work:**

- Distinguish unsupported public protocol, no compatible deployment, configuration-invalid deployment, and ordinary upstream failure.
- Return the public endpoint's native structured error shape.
- Do not classify a pre-call capability mismatch as provider outage or quota exhaustion.
- Keep upstream `404` under existing classifier policy; status alone is not capability evidence.
- Reject semantically required unsupported fields before `drop_params` can remove them.

**Tests:**

- Chat no-route returns OpenAI-shaped error.
- Messages no-route returns Anthropic-shaped error when Messages is enabled.
- Disabled Responses returns a controlled not-enabled/no-route response.
- Errors contain no upstream credentials or internal URLs.
- No circuit state changes for pre-call capability errors.

**Done when:** Clients and operators can distinguish configuration/capability failures from runtime provider failures.

**Completion:** `plugins/shared_quota_router/protocol_errors.py`; Responses → `protocol_not_enabled`; unit coverage in `test_m2_protocol_routing.py`.

## 7. Milestone 3: Direct Endpoint Enablement — **COMPLETE**

### M3-01 Enable and regression-test direct Chat — **DONE** (2026-07-26)

**Depends on:** M1-05, M2-05  
**Blocks:** M3-02

**Work:**

- Keep `/v1/chat/completions` enabled for explicitly opted-in Chat models.
- Ensure OpenCode Go and Volc requests use `/v1/chat/completions`, never `/v1/responses`.
- Preserve non-streaming, streaming, tools, usage, and errors only where verified.

**Tests:**

- OpenCode Go Chat smoke test.
- Volc Chat smoke test.
- `glm-5.2` failover across quota groups without cross-model fallback.
- Upstream logs show Chat path only.

**Done when:** Current working OpenCode behavior remains intact and the observed Responses misroute cannot recur.

**Completion:** `protocol_gates.assert_endpoint_allowed` + Chat `openai/` prefix check; `tests/unit/test_m3_endpoint_gates.py`.

### M3-02 Gate direct Messages on verified provider capability — **DONE** (2026-07-26)

**Depends on:** M3-01, P0-03  
**Blocks:** M3-03

**Work:**

- Keep `/v1/messages` disabled if no provider passed the Messages contract.
- If NewAPI or another provider passed, assign `anthropic_messages` explicitly and opt in only verified logical models.
- Ensure Chat-only deployments are excluded before leases.
- Do not expose Messages because a model name contains `claude`.

**Tests:**

- No verified provider: controlled disabled/no-route response.
- Verified provider: direct `/v1/messages` reaches only that provider.
- Regression: OpenCode Go receives no `/v1/messages` or `/v1/responses` call.
- Text, stream, tools, usage, errors, and auth style match verified fixtures.

**Done when:** Messages is either safely direct or safely disabled.

**Completion:** Messages disabled without opt-in + verified upstream; claude-name alone insufficient; enablement doc.

### M3-03 Keep Responses disabled until verified — **DONE** (2026-07-26)

**Depends on:** M3-02  
**Blocks:** M3-04

**Work:**

- Return a controlled disabled/no-route response for `/v1/responses`.
- Do not bridge Responses to Chat in MVP.
- Add a configuration gate that requires at least one verified Responses deployment before exposure.
- Document the operator process for adding a future Responses provider.

**Tests:**

- `/v1/responses` never reaches OpenCode Go or Volc.
- Enabling Responses without a capable deployment fails startup validation.
- Local mock Responses provider can pass the future enablement contract without changing MVP defaults.

**Done when:** Responses cannot be accidentally routed to a Chat-only upstream.

**Completion:** default `protocol_not_enabled`; `docs/enabling-messages-responses.md`.

### M3-04 Harden `drop_params` and feature validation — **DONE** (2026-07-26)

**Depends on:** M3-03  
**Blocks:** M4-01

**Work:**

- Define which fields are semantically required for each enabled protocol.
- Reject unsupported tools or streaming requirements before LiteLLM drops fields.
- Evaluate `drop_params: false` for strict protocol-aware deployments.
- Document any retained safe-to-drop optional fields.

**Tests:**

- Required unsupported field returns `400`.
- No tool definition is silently removed.
- Strict-mode fixtures expose unsupported params deterministically.
- Existing valid Chat requests remain compatible.

**Done when:** Protocol capability filtering cannot be bypassed by silent parameter dropping.

**Completion:** generator `drop_params: false`; pre-call tools/stream rejection; unit coverage.

## 8. Milestone 4: Operations, Rollout, and Acceptance — **COMPLETE**

### M4-01 Add protocol observability — **DONE** (2026-07-26)

**Depends on:** M3-04  
**Blocks:** M4-02

**Files:**

- Metrics/logging code under `plugins/shared_quota_router/`
- Operations documentation

**Work:**

- Record public protocol, upstream protocol, route mode, result, and failure kind.
- Keep conversion metrics dormant in MVP.
- Do not log prompts, responses, credentials, or Authorization headers.
- For shared/multi-tenant operation, hash or suppress model/deployment labels unless a metrics salt is configured.

**Tests:**

- Secret scanning on logs and fixtures.
- Protocol route selection and rejection counters increment correctly.
- Local-only mode may retain raw operational labels.

**Done when:** Operators can identify protocol routing decisions without sensitive data.

**Completion:** `protocol_observability.py`; `shared_quota_protocol_route_total` / `_reject_total`; salt hashing; `test_m4_ops.py`.

### M4-02 Implement feature-gated rollout and rollback — **DONE** (2026-07-26)

**Depends on:** M4-01  
**Blocks:** M4-03

**Work:**

- Add `PROTOCOL_AWARE_GATEWAY_ENABLED` with default `false` during rollout.
- Make the feature flag the primary rollback path.
- Keep timestamped generated-YAML backup as catastrophic fallback.
- Preserve Redis quota state across rollback.
- Ensure disabled new endpoints return controlled responses.

**Tests:**

- Flag off preserves legacy Chat behavior.
- Flag on enables capability filtering.
- Rollback does not clear Redis or alter quota-group state.
- Invalid generated config leaves previous config active.

**Done when:** Rollout and rollback are deterministic and documented.

**Completion:** `feature_flags.py`; strategy/gates honor flag; `docs/operations-protocol-gateway.md`; `.env.example`.

### M4-03 Run full verification and publish phase report — **DONE** (2026-07-26)

**Depends on:** M4-02  
**Blocks:** MVP-GATE

**Commands:** Use repository-supported equivalents discovered during implementation.

- Full `pytest` suite.
- Contract tests against LiteLLM `v1.90.5`.
- Type checking configured by the repository.
- Linting configured by the repository.
- Generator validation.
- Chat and conditional Messages smoke tests.
- Secret scan on changed files and logs.

**Deliverable:**

- `docs/phase-reports/protocol-gateway-mvp.md`

The report must include:

- Changed files.
- Implementation summary.
- Commands and exact results.
- Provider capabilities actually verified.
- Disabled endpoints and reasons.
- Unresolved risks.
- Rollback instructions.
- Post-MVP recommendation.

**Done when:** All acceptance evidence is recorded and no required test is skipped.

**Completion:** `docs/phase-reports/protocol-gateway-mvp.md` — pytest 160 passed; plans validate; secret scan OK.

### MVP-GATE Acceptance checklist — **PASSED** (2026-07-26)

- [x] G0 integration boundary is decided with command-backed evidence. *(G0-B, 2026-07-26)*
- [x] LiteLLM remains pinned to `v1.90.5`.
- [x] No business changes exist under `upstream/litellm`. *(business logic only in plugins)*
- [x] Every enabled deployment declares an explicit upstream protocol. *(OpenCode/Volc; NewAPI disabled/unset)*
- [x] Every public protocol is explicitly opted in per logical model. *(via `logical_models`)*
- [x] Protocol and feature filtering occurs before lease acquisition. **← M2**
- [x] Affinity cannot override capability eligibility. **← M2**
- [x] Protocol mismatch consumes no lease or retry attempt. **← M2**
- [x] Redis remains fail-closed. *(existing shared-quota invariant)*
- [x] No default cross-model fallback exists. *(existing config)*
- [x] No retry occurs after visible stream output. *(existing first-byte gate; M2 must preserve)*
- [x] Chat traffic reaches only Chat endpoints. **← M3-01 hard guarantee**
- [x] Messages is direct and verified, or controlled disabled. **← M3-02** *(disabled until verified)*
- [x] Responses is controlled disabled until a verified provider exists. **← M3-03**
- [x] Required unsupported fields are not silently dropped. **← M3-04**
- [x] Errors use the requested endpoint's native shape. **← M2-05**
- [x] No secrets or full prompt/response bodies appear in logs. *(policy + generator secret scan; keep auditing)*
- [x] Feature-flag rollback preserves quota state. **← M4-02**
- [x] Full tests and phase report are complete. **← M4-03** *(`protocol-gateway-mvp.md`; 160 pytest)*

## 9. Parallel Execution Plan

### Wave 0: Validation — **COMPLETE**

- [x] P0-01, provider inventory preparation.
- [x] P0-02 after P0-01.
- [x] P0-03 after P0-02.
- [x] G0 after P0-03 → **G0-B**.

### Wave 1: Domain and configuration — **COMPLETE**

- [x] M1-01.
- [x] M1-02 after M1-01.
- [x] M1-03 after M1-02.
- [x] M1-04 after M1-03.
- [x] M1-05 after M1-04.

### Wave 2: Routing — **COMPLETE**

- [x] M2-01 after M1-04 (also unblocked by M1-05 for discovery).
- [x] M2-02 through M2-05 sequentially because they touch the same selection and lease path.

### Wave 3: Endpoint enablement — **COMPLETE**

- [x] M3-01.
- [x] M3-02 after direct Chat regression is green.
- [x] M3-03 and M3-04 after Messages gating is stable.

### Wave 4: Operations — **COMPLETE**

- [x] M4-01.
- [x] M4-02.
- [x] M4-03 → MVP-GATE.

### Wave 5: Cross-protocol conversion — **CLOSED** (2026-07-26)

Depends on C0 kickoff. Detailed work in §12. Plan: `docs/superpowers/plans/2026-07-26-c1-c5-cross-protocol-conversion.md`.

- [x] C0 kickoff gate (required before any `plugins/` conversion domain/runtime work).
- [x] C1-01 → C1-04 (contracts / schema / resolve_route).
- [x] C2-01 and C2-02 may run in parallel; C2-03 after C1-02 (parallel with flag/spike); then C2-04 → C2-05. Prod conversion stays off until C2-05 evidence.
- [x] C3-01 before any **staging/prod** `PROTOCOL_CONVERSION_ENABLED=true` traffic (hard for staging enablement).
- [x] C4-01 after C2-05 (hard); C3-01 recommended before failure-path stream eval. **No-Go**.
- [x] C5-01 after C0 only (orthogonal to C2/C3; may parallel C4); evaluate may No-Go. **No-Go**.
- [x] C-CLOSE after C1–C3 done and C4/C5 evaluated (Go or No-Go recorded).

## 10. Suggested Atomic Change Sets

These are suggested review units, not instructions to commit automatically.

1. ~~Compatibility report and contract harness.~~ **done**
2. ~~Integration-boundary ADR.~~ **done**
3. ~~Protocol and feature domain model.~~ **done**
4. ~~Configuration schema and validation.~~ **done**
5. ~~Generator metadata and atomic backup behavior.~~ **done** (+ M1-05 discovery)
6. ~~Pre-lease protocol filtering.~~ **done** (M2)
7. ~~Callback / context / lease invariant updates.~~ **done** (M2)
8. ~~Protocol-aware errors.~~ **done** (M2)
9. ~~Chat enablement and regression.~~ **done** (M3-01)
10. ~~Conditional Messages enablement.~~ **done** (M3-02 — disabled until verified)
11. ~~Responses disablement guard.~~ **done** (M3-03)
12. ~~`drop_params` and feature validation.~~ **done** (M3-04)
13. ~~Observability.~~ **done** (M4-01)
14. ~~Feature flag and rollback.~~ **done** (M4-02)
15. ~~Full verification report and MVP gate.~~ **done** (M4-03 / MVP-GATE)
16. ~~C1 domain + fidelity matrix + config validators (C1-01..C1-03).~~ **done**
17. ~~C1 `resolve_route` / ranking without live conversion (C1-04).~~ **done**
18. ~~C2 flag + metrics + G0-B spike report (C2-01..C2-02).~~ **done**
19. ~~C2 pilot adapter + fixtures (C2-03).~~ **done**
20. ~~C2 wiring + acceptance pack; prod flag remains false (C2-04..C2-05).~~ **done**
21. ~~C3 conversion circuit isolation (C3-01).~~ **done**
22. ~~C4 streaming evaluation report (C4-01).~~ **done (No-Go)**
23. ~~C5 Responses direct evaluation report (C5-01).~~ **done (No-Go)**
24. ~~Conversion epic closure + board sync (C-CLOSE).~~ **done**

Each change set must leave the full relevant test subset green and must not include unrelated refactoring.

## 11. Stop/Escalation Conditions

Stop implementation and revise the design when any condition occurs:

1. `metadata.protocol` cannot reach selection and the chosen alternative cannot preserve quota and stream invariants.
2. A required protocol path needs business changes in `upstream/litellm`.
3. A direct provider does not preserve tools, usage, errors, or streaming as declared.
4. Capability filtering cannot run before lease acquisition.
5. Protocol mismatch mutates quota/provider circuit state.
6. First-visible-event state cannot be shared with retry logic.
7. Tests require disabling type checking, deleting assertions, or exposing secrets.
8. More than three consecutive implementation attempts fail for the same task; stop, restore the last working state, and consult architecture/debugging review.
9. **(Conversion)** C2-02 G0-B spike cannot convert request **and** reshape response/error without violating first-byte or lease invariants — escalate to thin G0-A or revise plan; do not ship half-wired convert.
10. **(Conversion)** Conversion failure would open the same circuit key as direct traffic on the same deployment — stop until C3-01 lands. **Resolved by C3-01.**
11. **(Conversion)** Any path would enable Responses conversion before a verified direct Responses deployment (C5 gate). **C5 No-Go keeps gate closed.**

## 12. Post-MVP Backlog: Explicit Conversion Only — **CLOSED** (2026-07-26)

**Plan (source of task decomposition):** `docs/superpowers/plans/2026-07-26-c1-c5-cross-protocol-conversion.md`  
**Design:** `docs/protocol-aware-multi-api-gateway-plan.md` §6.4–6.6, §8.4–8.6, §9.4–10.5, §11.5, §12.3–12.4, §14–15  

**Hard rules for this epic:**

- Do not start runtime conversion until **C0** kickoff.
- Prefer direct same-protocol over conversion (`route_mode_rank`: direct=0, convert=1).
- Production default: `PROTOCOL_CONVERSION_ENABLED=false` until C2-05 evidence.
- No Responses↔* conversion in this epic; C5 is **direct** Responses evaluation only.
- Tools / reasoning / images / structured output / streaming remain rejected on the C2 pilot path.
- Do not treat LiteLLM’s internal translators as verified project conversion.

**Recommended pilot direction (C0 may override):** `anthropic_messages` (public) → `openai_chat` (upstream).

---

### C0 Conversion kickoff gate — **DONE** (2026-07-26)

**Depends on:** MVP-GATE  
**Blocks:** All C1+ work that creates/modifies `plugins/shared_quota_router/` conversion domain or runtime (including C1-01 models). After C0 is **DONE**, docs/contract scaffolding and C1 contracts may proceed before C2 mount wiring.

**Work:**

- Confirm staging/prod Chat is stable with `PROTOCOL_AWARE_GATEWAY_ENABLED=true`.
- Select pilot direction and logical model (default messages→chat).
- Record that production conversion stays off until C2-05; staging `flag=true` requires C3-01 (or written risk acceptance).
- Flip §0 board: C0 → **DONE**; when coding starts set C1-01 → **IN PROGRESS** (do not mark the whole epic IN PROGRESS).

**Done when:** Kickoff note dated in §0 evidence column (direction + model + owners).

**Plan ref:** Task 0

---

### C1-01 Fidelity + ConversionCapability domain model — **DONE** (2026-07-26)

**Depends on:** C0  
**Blocks:** C1-02

**Files:**

- `plugins/shared_quota_router/models.py`
- `tests/unit/test_c1_conversion_contracts.py`

**Work:**

- Add `FidelityClass`: `equivalent`, `lossy_safe`, `lossy_unsafe`, `unsupported`.
- Add `RouteMode`: `direct`, `convert`.
- Add `ConversionCapability` (`source`=public, `target`=upstream), `RouteCandidate`.
- Extend `Feature` for post-MVP gates: `reasoning`, `prompt_cache`, `structured_output`, `image`, `parallel_tool_calls`, `citations` (parseable; not auto-inferred unless present).
- Extend `Deployment.conversions` and `LogicalModelProtocols.allow_conversion` / `allowed_conversions`.

**Tests:**

- Unknown fidelity strings (e.g. design draft `safe_for_text_tools_non_streaming`) raise.
- Direction asymmetry preserved; `RouteMode` wire values stable.

**Done when:** Unit tests green; no routing behavior change yet.

**Plan ref:** Task 1

---

### C1-02 Directional fidelity matrix contracts — **DONE** (2026-07-26)

**Depends on:** C1-01  
**Blocks:** C1-03, C2-03

**Files:**

- `plugins/shared_quota_router/conversion/__init__.py`
- `plugins/shared_quota_router/conversion/contracts.py`
- `tests/unit/test_c1_conversion_contracts.py`

**Work:**

- Register pilot directions: messages→chat and chat→messages (C2 implements one).
- Per-feature fidelity: reasoning=`lossy_unsafe`; prompt_cache=`unsupported`; C2 pilot tools/streaming=`unsupported`; text=`equivalent`.
- `validate_request_against_fidelity` rejects `lossy_unsafe` / `unsupported` before lease.
- Define `ConvertedRequest` / `ConvertedResponse` with `warnings` + `dropped_fields`.

**Tests:**

- Text-only non-stream accepted; tools/stream/reasoning/cache rejected with `ProtocolAwareRoutingError`.

**Done when:** Matrix tests green; no adapter implementation required.

**Plan ref:** Task 2

---

### C1-03 Config schema: allow_conversion + conversions — **DONE** (2026-07-26)

**Depends on:** C1-02  
**Blocks:** C1-04

**Files:**

- `plugins/shared_quota_router/config_schema.py`
- `plugins/shared_quota_router/generator.py`
- `plugins/shared_quota_router/registry.py`
- `tests/unit/test_config_schema.py` / `test_generator.py` / `test_registry.py`

**Work:**

- Parse logical-model `allow_conversion` + `conversion_policy.allowed[]`.
- Emit/parse deployment `model_info.protocol.conversions[]`.
- Fail-closed validators (design §8.6): duplicate directions; invalid fidelity; `streaming: true` without proven adapter (C1 requires `streaming: false`); conversion declared while `allow_conversion: false`; public protocol only via conversion without matching allowlist + deployment conversion + target upstream; conversion target with no capable deployment.
- Default existing plans: no conversions; `allow_conversion` defaults false.

**Tests:**

- Each §8.6 rejection case; happy path with explicit allowlist.

**Done when:** Generator + load validate green; MVP plans unchanged in behavior.

**Plan ref:** Task 3

---

### C1-04 `resolve_route` — direct preferred over convert — **DONE** (2026-07-26)

**Depends on:** C1-03  
**Blocks:** C2-04

**Files:**

- `plugins/shared_quota_router/conversion/registry.py` and/or `registry.py`
- `plugins/shared_quota_router/strategy.py`
- `tests/unit/test_c1_conversion_contracts.py`
- Regression: `tests/unit/test_m2_protocol_routing.py`, `test_m3_endpoint_gates.py`

**Work:**

- `resolve_route(...)` → `RouteCandidate | None` (direct if `upstream_protocol == public`; else convert if flag+policy+capability+fidelity).
- Pre-lease filter includes convert candidates only when `PROTOCOL_CONVERSION_ENABLED` (wire flag stub OK if C2-01 not landed — default treat as false).
- Ranking key: `(route_mode_rank, affinity, priority, inflight, last_success, deployment_id)`.
- Affinity ignored if not in post-capability set; convert mismatch consumes no lease/tried.

**Tests:**

- Direct wins when both exist; convert only when no direct + policy; flag off ⇒ no convert.

**Done when:** C1 board rows DONE; short note `docs/phase-reports/conversion-c1.md`.

**Plan ref:** Task 4–5

---

### C2-01 Feature flag + conversion observability hooks — **DONE** (2026-07-26)

**Depends on:** C1-04  
**Blocks:** C2-04

**Files:**

- `plugins/shared_quota_router/feature_flags.py`
- `plugins/shared_quota_router/protocol_observability.py`
- `.env.example`
- `docs/operations-protocol-conversion.md`
- `tests/unit/test_c2_messages_to_chat_pilot.py`

**Work:**

- Add `PROTOCOL_CONVERSION_ENABLED` default **false**.
- `record_conversion_result` increments reserved `shared_quota_protocol_conversion_*` counters with safe labels.
- Document dual-flag matrix with `PROTOCOL_AWARE_GATEWAY_ENABLED` and rollback (flag off; Redis preserved).

**Tests:**

- Default false; explicit record increments counters; no secrets in log helpers.

**Done when:** Ops doc + `.env.example` updated.

**Plan ref:** Task 6

---

### C2-02 Spike: can G0-B host request/response conversion? — **DONE** (2026-07-26)

**Depends on:** C0 (may run in parallel with C2-01 / C1 after C0; **must** finish before C2-04)  
**Blocks:** C2-04 (mount point decision)

**Files:**

- `docs/phase-reports/conversion-c2-spike-g0b.md`
- Optional harness under `tests/contract/`

**Work:**

- Prove or refute on LiteLLM v1.90.5:
  1. Pre-call can rewrite Messages body to Chat upstream fields consistent with selected deployment.
  2. Response/error can be reshaped to Anthropic Messages for the client without breaking streaming/first-byte accounting.
- **Go:** proceed C2-04 on G0-B hooks. **No-Go:** thin G0-A front adapter ADR; still no `upstream/litellm` edits.

**Done when:** Dated spike report with go/no-go and chosen mount points.

**Plan ref:** Task 7

---

### C2-03 Pilot adapter `anthropic_messages → openai_chat` (text-only, non-stream) — **DONE** (2026-07-26)

**Depends on:** C1-02  
**Blocks:** C2-04

**Files:**

- `plugins/shared_quota_router/conversion/adapters/base.py`
- `plugins/shared_quota_router/conversion/adapters/messages_to_chat.py`
- `plugins/shared_quota_router/conversion/dispatch.py`
- `tests/fixtures/conversion/messages_to_chat/`
- `tests/unit/test_c2_messages_to_chat_pilot.py`

**Work:**

- Implement `ProtocolConverter`: `convert_request`, `convert_response`, `convert_error`.
- Map system/messages text, max_tokens, usage, finish/stop reason, error envelope.
- Reject tools/images/reasoning/streaming; non-empty `dropped_fields` ⇒ error (C2 allowlist empty).
- Fixtures: basic + multiturn request; response; usage; finish-reason map; error.

**Tests:**

- Fixture round-trips; tools request raises `FEATURE_UNSUPPORTED`.

**Done when:** Adapter unit tests green; not yet wired to live select unless C2-04.

**Plan ref:** Task 8

---

### C2-04 Wire selection + dispatch (production still disabled by default) — **DONE** (2026-07-26)

**Depends on:** C2-01, C2-02 (Go), C2-03, C1-04  
**Blocks:** C2-05, C3-01

**Files:**

- `plugins/shared_quota_router/strategy.py`
- `plugins/shared_quota_router/callbacks.py`
- `plugins/shared_quota_router/protocol_gates.py`
- Unit + contract tests as needed

**Work:**

- On convert select, write dual-bucket metadata: `shared_quota_route_mode`, `shared_quota_conversion`.
- Pre-call / success/failure hooks per C2-02 mount decision; call dispatch converter.
- Flag off ⇒ never select convert even if configured.
- Deterministic conversion errors: no retry; no circuit mutation (until C3 specializes keys).
- Record `route_mode=convert` + conversion metrics; must not bypass M3 public opt-in.

**Tests:**

- Flag on + policy → convert candidate; flag off → Messages gate/no-route unchanged; M2/M3 regression green.

**Done when:** Suite subset green; prod default flag still false.

**Plan ref:** Task 9

---

### C2-05 C2 acceptance evidence pack — **DONE** (2026-07-26)

**Depends on:** C2-04  
**Blocks:** Operator decision to enable conversion. **Staging/prod `PROTOCOL_CONVERSION_ENABLED=true` additionally requires C3-01** (or dated risk-acceptance note in the pilot report).

**Files:**

- `docs/phase-reports/conversion-c2-pilot.md`
- Optional `config/plans.conversion-pilot.example.yaml` (no secrets)

**Work:**

- Document direction, model, fixtures, commands, uncovered features, rollback.
- Explicit: production `PROTOCOL_CONVERSION_ENABLED=false` until operators accept evidence.
- Go/No-Go for staging flag=true.

**Done when:** Report published; §0 C2 rows marked DONE with evidence links.

**Plan ref:** Task 10

---

### C3-01 Conversion-path circuit isolation — **DONE** (2026-07-26)

**Depends on:** C2-04  
**Blocks:** Staging/prod conversion traffic; recommended before C4/C5 eval that exercises failure paths

**Files:**

- `plugins/shared_quota_router/callbacks.py`
- `plugins/shared_quota_router/state_store.py`
- `plugins/shared_quota_router/strategy.py`
- `tests/unit/test_c3_conversion_circuit_isolation.py`
- `docs/phase-reports/conversion-c3.md`

**Work:**

- Isolate health by `deployment_id` + `upstream_protocol` + adapter direction (or `route_mode`).
- Suggested keys: `cooldown:dep:{id}:direct` vs `cooldown:dep:{id}:convert:{source}>{target}`.
- Deterministic conversion failures: no quota/provider circuit; no cross-deployment retry.
- Upstream 5xx/quota still classified normally but must not cool down sibling **direct** path on same deployment.

**Tests:**

- Convert cooldown does not block direct; mapping errors not retried; quota not marked exhausted.

**Done when:** Tests green; §0 C3-01 DONE.

**Plan ref:** Task 11

---

### C4-01 Evaluate streaming conversion (go/no-go) — **DONE (No-Go)** (2026-07-26)

**Depends on:** C2-05 (contracts exist); C3-01 recommended  
**Blocks:** Any `streaming: true` on conversion capabilities

**Files:**

- `docs/conversion/streaming-evaluation.md`
- `tests/unit/test_c4_streaming_conversion_eval.py` (document required invariants; may skip/impl reject)

**Work (evaluate, do not enable by default):**

- Define first **converted** visible event as first-byte boundary (§15.3).
- Lease held across adapter buffering; configurable max buffer latency from tests.
- Prove or refute: event order, backpressure, cancellation, usage, mid-stream failure; never splice second upstream after visible output.
- Default expectation: **No-Go**; keep matrix `STREAMING=unsupported`.

**Done when:** Dated go/no-go report linked from §0; runtime streaming convert remains off unless Go.

**Plan ref:** Task 12

---

### C5-01 Evaluate direct Responses enablement (no conversion) — **DONE (No-Go)** (2026-07-26)

**Depends on:** C0 (and MVP-GATE). **Orthogonal to C2/C3** — does not require conversion pilot or circuit isolation. May run parallel to C4.  
**Blocks:** Public `/v1/responses` opt-in; **blocks any future Responses conversion epic**

**Files:**

- `docs/conversion/responses-direct-evaluation.md`
- `tests/unit/test_c5_responses_direct_eval.py`
- `docs/enabling-messages-responses.md` (C5 No-Go note)

**Work:**

- Inventory: is there a verified `upstream_protocol: openai_responses` deployment?
- Contract: path, reasoning/tools/usage/stream/errors as declared — **not** Chat bridge.
- Require explicit `public_protocols: [openai_responses]`.
- **Forbidden in this task:** `openai_chat ↔ openai_responses` conversion.

**Done when:** Go → document enable steps + gate relaxation per model; No-Go → keep controlled disable. §0 updated.

**Plan ref:** Task 13

---

### C-CLOSE Conversion epic closure — **DONE** (2026-07-26)

**Depends on:** C1-04, C2-05, C3-01; C4-01 and C5-01 evaluated (Go or No-Go recorded)

**Work:**

- Sync §0 statuses and evidence.
- Confirm conversion counters stay 0 on default production path (flag false).
- Full `pytest tests/ -q`.
- If C2-02 chose G0-A: add `docs/adr/ADR-conversion-adapter-boundary.md`.

**Done when:** Epic board consistent; no open “TODO” without explicit deferral note.

**Plan ref:** Task 14

**Closure notes:** G0-B retained for lab/unit only (no G0-A ADR yet). C4/C5 recorded **No-Go**. Production `PROTOCOL_CONVERSION_ENABLED` remains **false**. Staging enablement requires C3-01 **plus** residual R1–R3 clear (`docs/phase-reports/conversion-residual-risks.md`). Dual-flag AND enforced via `is_conversion_routing_active()`.
---

## 13. Task Completion Template

Use this template when closing each task:

```markdown
### <TASK-ID> Completion

Files changed:
- ...

Implementation:
- ...

Commands executed:
- `<command>` -> PASS/FAIL

Test results:
- ...

Security checks:
- Secrets/logging review: PASS/FAIL

Unresolved risks:
- ...

Next task:
- ...
```

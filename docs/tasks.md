# Protocol-Aware Multi-API Gateway Development Tasks

**Source design:** `docs/protocol-aware-multi-api-gateway-plan.md`  
**Project:** `E:\LiteLLMPro\local-llm-router`  
**LiteLLM pin:** `v1.90.5`  
**Status:** In progress — Wave 0 + Milestone 1 complete; M2+ pending  
**Scope:** Phase 0 validation and direct-protocol MVP only  
**Last progress update:** 2026-07-26

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
| M2-01 | Request protocol context | **TODO** | Not started |
| M2-02 | Pre-lease capability filter | **TODO** | Strategy still ignores protocol |
| M2-03 | Affinity after capability filter | **TODO** | — |
| M2-04 | Lease / first-byte invariants | **TODO** | Base lease/first-byte exist; not protocol-aware |
| M2-05 | Protocol-aware no-route errors | **TODO** | — |
| M3-01 | Direct Chat enable + regression | **TODO** | Chat smoke works; no protocol hard gate yet |
| M3-02 | Messages gate / direct | **TODO** | NewAPI unverified; no Messages gate |
| M3-03 | Responses controlled disable | **TODO** | Path still open in LiteLLM (observed 400) |
| M3-04 | drop_params / feature validation | **TODO** | Still `drop_params: true` |
| M4-01 | Protocol observability | **TODO** | — |
| M4-02 | Feature-flag rollout / rollback | **TODO** | No `PROTOCOL_AWARE_GATEWAY_ENABLED` yet |
| M4-03 | Full verification + MVP report | **TODO** | Partial E2E: `docs/phase-reports/e2e-verification-m1.md` |
| MVP-GATE | Acceptance checklist | **OPEN** | See §8 (partial checkboxes only) |
| C1–C5 | Post-MVP conversion | **BLOCKED** | Must not start before MVP-GATE |

### What works in production path today (after M1)

- Plans declare `upstream_protocol` / `logical_models.public_protocols`.
- Generator emits capability metadata into `config/litellm.yaml` (atomic + backup).
- Clients can discover opt-in via `GET /v1/router/model-capabilities` (not stock `/v1/models`).
- OpenCode Go / Volc Chat completions smoke against live proxy (E2E 2026-07-26).

### What does **not** work yet

- Request-path protocol injection and dual-bucket read in strategy (`metadata` vs `litellm_metadata`).
- Filtering deployments by protocol **before** quota lease.
- Controlled disable for Messages / Responses at the public endpoint boundary.
- Feature flag rollback and protocol metrics.

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

Post-MVP conversion tasks are listed separately in Section 12 and must not start before MVP acceptance.

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
                                  [ ] M2-01 -> ... -> [ ] M2-05   ← NEXT
                                               |
                                  [ ] M3-01 -> ... -> [ ] M3-04
                                               |
                                  [ ] M4-01 -> ... -> [ ] MVP-GATE
```

`G0` is resolved (**G0-B: metadata integration**). Production protocol filtering must not start without G0; M2 may now proceed.

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

### M2-01 Add request protocol and feature context — **TODO**

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

### M2-02 Filter protocol and features before state checks and lease acquisition — **TODO**

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

### M2-03 Apply affinity after capability filtering — **TODO**

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

### M2-04 Preserve lease, retry, and first-byte invariants — **TODO** (base invariants exist; need protocol integration)

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

### M2-05 Add protocol-aware no-route and configuration errors — **TODO**

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

## 7. Milestone 3: Direct Endpoint Enablement — **NOT STARTED**

### M3-01 Enable and regression-test direct Chat — **TODO**

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

### M3-02 Gate direct Messages on verified provider capability — **TODO**

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

### M3-03 Keep Responses disabled until verified — **TODO**

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

### M3-04 Harden `drop_params` and feature validation — **TODO**

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

## 8. Milestone 4: Operations, Rollout, and Acceptance — **NOT STARTED**

### M4-01 Add protocol observability — **TODO**

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

### M4-02 Implement feature-gated rollout and rollback — **TODO**

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

### M4-03 Run full verification and publish phase report — **TODO** (partial: M1 E2E report only)

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

### MVP-GATE Acceptance checklist

- [x] G0 integration boundary is decided with command-backed evidence. *(G0-B, 2026-07-26)*
- [x] LiteLLM remains pinned to `v1.90.5`.
- [x] No business changes exist under `upstream/litellm`. *(business logic only in plugins)*
- [x] Every enabled deployment declares an explicit upstream protocol. *(OpenCode/Volc; NewAPI disabled/unset)*
- [x] Every public protocol is explicitly opted in per logical model. *(via `logical_models`)*
- [ ] Protocol and feature filtering occurs before lease acquisition. **← M2**
- [ ] Affinity cannot override capability eligibility. **← M2**
- [ ] Protocol mismatch consumes no lease or retry attempt. **← M2**
- [x] Redis remains fail-closed. *(existing shared-quota invariant)*
- [x] No default cross-model fallback exists. *(existing config)*
- [x] No retry occurs after visible stream output. *(existing first-byte gate; M2 must preserve)*
- [ ] Chat traffic reaches only Chat endpoints. **← M3-01 hard guarantee**
- [ ] Messages is direct and verified, or controlled disabled. **← M3-02**
- [ ] Responses is controlled disabled until a verified provider exists. **← M3-03**
- [ ] Required unsupported fields are not silently dropped. **← M3-04**
- [ ] Errors use the requested endpoint's native shape. **← M2-05**
- [x] No secrets or full prompt/response bodies appear in logs. *(policy + generator secret scan; keep auditing)*
- [ ] Feature-flag rollback preserves quota state. **← M4-02**
- [ ] Full tests and phase report are complete. **← M4-03** *(M1 E2E only so far)*

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

### Wave 2: Routing — **NEXT**

- [ ] M2-01 after M1-04 (also unblocked by M1-05 for discovery).
- [ ] M2-02 through M2-05 sequentially because they touch the same selection and lease path.

### Wave 3: Endpoint enablement — **PENDING**

- [ ] M3-01.
- [ ] M3-02 after direct Chat regression is green.
- [ ] M3-03 and M3-04 after Messages gating is stable.

### Wave 4: Operations — **PENDING**

- [ ] M4-01.
- [ ] M4-02.
- [ ] M4-03.

## 10. Suggested Atomic Change Sets

These are suggested review units, not instructions to commit automatically.

1. ~~Compatibility report and contract harness.~~ **done**
2. ~~Integration-boundary ADR.~~ **done**
3. ~~Protocol and feature domain model.~~ **done**
4. ~~Configuration schema and validation.~~ **done**
5. ~~Generator metadata and atomic backup behavior.~~ **done** (+ M1-05 discovery)
6. Pre-lease protocol filtering. **← next**
7. Affinity ordering and lease invariants.
8. Protocol-aware error mapping.
9. Direct Chat regression hardening.
10. Conditional Messages enablement.
11. Responses disablement guard.
12. Observability and feature-gated rollout.
13. Documentation and phase report.

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

## 12. Post-MVP Backlog: Explicit Conversion Only — **NOT STARTED / BLOCKED**

These tasks are intentionally excluded from MVP. Do not start until MVP-GATE.

### C1 Define directional conversion contracts

- Define source and target protocol.
- Define feature-level fidelity: `equivalent`, `lossy_safe`, `lossy_unsafe`, `unsupported`.
- Treat reasoning as `lossy_unsafe` until verified.
- Treat prompt caching as `unsupported` across conversion initially.
- Require explicit logical-model allowlisting.

### C2 Pilot one non-streaming text-only direction

- Select one direction based on a real client need.
- Reject tools, reasoning, images, structured output, and streaming initially.
- Add request, response, usage, finish-reason, and error fixtures.
- Keep production conversion disabled until acceptance evidence exists.

### C3 Add conversion-path circuit isolation

- Isolate health by deployment, upstream protocol, and adapter direction.
- Conversion failure must not poison direct traffic.
- Do not retry deterministic conversion failures.

### C4 Evaluate streaming conversion

- Define first visible converted event.
- Hold the quota lease through adapter buffering.
- Use a configurable, test-derived buffering limit.
- Test event ordering, backpressure, cancellation, tool deltas, usage, and mid-stream failure.
- Never splice a second upstream after visible output.

### C5 Evaluate Responses enablement

- Require a verified direct Responses provider first.
- Add direct Responses before any Responses conversion.
- Validate reasoning, tools, usage, streaming events, and errors.
- Keep Responses disabled if the provider contract is incomplete.

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

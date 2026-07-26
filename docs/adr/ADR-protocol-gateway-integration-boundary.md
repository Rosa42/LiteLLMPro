# ADR: Protocol Gateway Integration Boundary

**Status:** Accepted — **implemented as project boundary for M1+**  
**Date:** 2026-07-26  
**Decision ID:** G0  
**Depends on:** P0-01, P0-02, P0-03 (`docs/phase-reports/protocol-gateway-phase-0-compatibility.md`)  
**LiteLLM pin:** v1.90.5 (`0430743f2fd4005898506e00bc62dd47bcff6fc9`)  
**Progress:** Task board `docs/tasks.md` §0 — Wave 0 + M1 complete; M2 (runtime filter) still TODO

## Context

The protocol-aware multi-API gateway must:

1. Establish an authoritative public protocol before quota lease acquisition.
2. Prefer direct same-protocol passthrough (Chat→Chat, Messages→Messages).
3. Keep business logic out of `upstream/litellm`.
4. Preserve Redis fail-closed, first-byte hard gate, one attempt per quota group / three groups max.

Phase 0 asked whether `metadata.protocol` can carry the public protocol into the custom routing strategy and callbacks for every MVP endpoint path.

## Options

### G0-B — Metadata integration

Inject a serializable protocol string at the project-owned request boundary; strategy and callbacks read it from LiteLLM kwargs.

### G0-A — Project-owned front gateway / isolated protocol lanes

Terminate public endpoints in project code, normalize to an internal envelope, then call LiteLLM with a fully controlled kwargs shape (or separate internal lanes per protocol).

## Evidence (command-backed)

| Claim | Result | Source |
|-------|--------|--------|
| Chat strategy sees `metadata.protocol` | **PASS** | `pytest tests/contract/test_p0_protocol_metadata_propagation.py` |
| Responses strategy sees `litellm_metadata.protocol` | **PASS** | same |
| Messages strategy sees `litellm_metadata.protocol` | **PASS** | same |
| Success callbacks carry protocol under `litellm_params.{metadata,litellm_metadata}` | **PASS** | same (single-loop matrix) |
| Dual bucket: Chat=`metadata`, Messages/Responses=`litellm_metadata` | **Verified source + harness** | P0-01 + P0-02 |
| Chat upstream path `/chat/completions` | **PASS** | `test_p0_direct_protocol_paths.py` |
| Messages upstream path `/v1/messages` with `anthropic/` prefix | **PASS** | same |
| Messages with `openai/` prefix misroutes to `/responses` | **PASS (negative)** | same |
| Responses upstream path `/responses` | **PASS** | same |
| First-byte blocks reselection | **PASS** | same |
| Full suite subset | **17 passed** | P0-02 + P0-03 + mock e2e |

## Decision

**Choose G0-B: Metadata integration.**

### Why G0-B

1. Protocol reaches strategy selection for all three MVP Router paths when injected into the correct bucket.
2. Protocol reaches success callbacks when read from `litellm_params` (nested), so lease/release accounting can stay protocol-aware without a front gateway.
3. Existing registration (`set_custom_routing_strategy` + CustomLogger callbacks + startup hook) is sufficient — **no upstream business patch**.
4. A front gateway (G0-A) would duplicate LiteLLM auth, streaming, and error shaping without solving the dual-bucket issue any better than a small dual-key reader.

### Why not G0-A (now)

G0-A remains a **fallback** if later MVP work proves that:

- Proxy HTTP injection (not just Router SDK kwargs) drops protocol before selection, or  
- First-visible-event state cannot be shared for a required endpoint without a project-owned stream adapter.

Neither condition is true on P0 evidence.

## Selected boundary design

```text
Public client
    │
    ├─ POST /v1/chat/completions     → protocol = openai_chat
    ├─ POST /v1/messages             → protocol = anthropic_messages  (opt-in later)
    └─ POST /v1/responses            → protocol = openai_responses    (disabled until verified)
    │
    ▼
LiteLLM proxy pre-call
    │  writes into:
    │    Chat      → data["metadata"]["protocol"]
    │    Messages  → data["litellm_metadata"]["protocol"]
    │    Responses → data["litellm_metadata"]["protocol"]
    │
    ▼
SharedQuotaRoutingStrategy.get_available_deployment
    │  reads protocol via dual-key helper:
    │    request_kwargs["metadata"] or request_kwargs["litellm_metadata"]
    │  filters by upstream_protocol BEFORE Redis lease
    │
    ▼
SharedQuotaCallback (CustomLogger)
    │  reads protocol via:
    │    kwargs["litellm_params"]["metadata"|"litellm_metadata"]
    │  marks first_byte on stream; releases leases
```

### Cross-boundary state

| Concern | Carrier |
|---------|---------|
| Request id | `litellm_call_id` (+ Redis reqctx keyed by it) |
| Public protocol | serializable string in metadata / litellm_metadata |
| Selected deployment | returned model_list entry; `model_info.deployment_id` |
| Quota lease | Redis via `LeaseManager`; tried set in `RequestRoutingContext` |
| First visible event | `RequestRoutingContext.first_byte_sent` (process cache + Redis) |
| Structured errors | public endpoint native shape (M2-05 / M3) — not in G0 scope |

### Project-owned injection points (implementation guidance for M1+)

Prefer, in order:

1. **CustomLogger / pre-call hook** owned by `shared_quota_router` that sets protocol from the HTTP path / `call_type` (never from model name).
2. If proxy path mapping is insufficient, a **minimal startup registration** that wraps only the project plugin surface — still no business logic in `upstream/litellm`.

Authoritative protocol source for MVP endpoints:

| Endpoint path | Protocol value |
|---------------|----------------|
| `/v1/chat/completions` (and aliases) | `openai_chat` |
| `/v1/messages` | `anthropic_messages` |
| `/v1/responses` | `openai_responses` |

Never infer protocol from model name, provider id, URL host, or LiteLLM model prefix. Prefix (`openai/` vs `anthropic/`) is a **deployment capability / generator** concern, not a public-protocol inference rule.

## Rejected alternative details

| Alternative | Rejection reason |
|-------------|------------------|
| G0-A front gateway now | Higher ops surface; P0 shows metadata path works |
| Infer protocol from `messages` vs `input` | Explicitly forbidden; validation evidence only |
| Infer from `openai/` prefix | Messages with `openai/` misroutes to `/responses` |
| Modify `upstream/litellm` | Violates repository invariant |

## Rollback boundary

1. **Primary:** `PROTOCOL_AWARE_GATEWAY_ENABLED=false` (M4-02) → legacy Chat-only selection.
2. **Config:** restore timestamped `config/litellm.yaml` backup from generator.
3. **Code:** revert plugin changes under `plugins/shared_quota_router/`; no submodule rollback required if pin stays v1.90.5.
4. **Redis:** leave quota keys intact across rollback (do not flush).

## Stop condition check

Does G0-B preserve quota leases and first-byte semantics without upstream business logic?

**Yes** on P0 evidence:

- Leases remain in project strategy after capability filter (M2).  
- First-byte gate already in strategy + callback.  
- No `upstream/litellm` business edits required.

## Consequences

### Immediate (M1+)

1. Add dual-key protocol reader in strategy/callbacks.
2. Domain model: `ApiProtocol`, deployment `upstream_protocol`, logical-model `public_protocols`.
3. Generator must not label NewAPI as Messages until operator probe; keep Responses disabled.
4. Deployments that serve Messages must use an anthropic-native LiteLLM model configuration — not bare `openai/<claude-name>`.

### Deferred

- G0-A remains available if proxy-level injection fails in M2/M3.
- Cross-protocol conversion stays post-MVP.

## References

- `docs/tasks.md` §4 G0  
- `docs/protocol-aware-multi-api-gateway-plan.md`  
- `docs/phase-reports/protocol-gateway-phase-0-compatibility.md`  
- `tests/contract/test_p0_protocol_metadata_propagation.py`  
- `tests/contract/test_p0_direct_protocol_paths.py`

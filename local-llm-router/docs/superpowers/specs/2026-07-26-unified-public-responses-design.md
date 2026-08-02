# Design: Unified Public Responses API (Chat + Messages upstreams)

**Date:** 2026-07-26 (revised; eng confirmation same day)  
**Status:** **Draft approved for M0–M1 planning only** — **no coding** until M0–M1 plan is reviewed  
**Verdict:** §3 architecture + §4 milestones **accepted**. Prod launch = **Policy A**.  
**LiteLLM pin:** v1.90.5 — no `upstream/litellm` business edits

### Locked decisions (2026-07-26)

| Item | Decision |
|------|----------|
| Architecture §3 | Accepted (native bridge, `transform_owner`, per-direction readiness, sole accounting owner) |
| Milestones §4 | Accepted (M1→M2→M3; no project dual adapters / custom SSE) |
| Prod launch | **Policy A:** M1–M3 contracts green ⇒ may launch; direct Responses provider **optional** |
| Policy A guardrails | Only directions that are M3-green + fail-closed gates done + **production profile explicit approve** |
| Policy B | Not global; may attach later per-model for compliance/commercial reasons |
| Next | Write/review **M0–M1 plan**; still **no implementation** until that plan is approved |

---

## 0. Review disposition (blocking)

External review (2026-07-26) + inline probes under pin:

| Finding | Disposition |
|---------|-------------|
| LiteLLM already bridges Responses → `acompletion` (non-stream + stream SSE) | **Adopt native bridge as default transform owner** — do **not** build project Responses→Chat / Responses→Messages request/response/SSE adapters unless native fails contracts |
| Chat: set `use_chat_completions_api: true` → `/chat/completions` | M1 canary |
| Anthropic: completion bridge → `/v1/messages` | M2 after NewAPI probe — **separate logical model**, not merged with GLM/Kimi |
| Inventory has no shared `model_group` across Chat + Messages | Acceptance rewritten: **many logical models, one public protocol (Responses)** — not one model with mixed Chat/Messages deps |
| First-byte gate not E2E proven; Router mid-stream fallback conflicts | M3 = integration safety (lease/cancel/disable mid-fallback), not “existing” claim |
| Callback forbids streaming chunk rewrite | Project stream adapter has **no mount point** on stock path → native or G0-A iterator only |
| Global `conversion_routing_active` OR-ready | Wrong for multi-direction; need **per-(source,target,owner) readiness** |
| `has_verified_upstream` = config declaration only | Rename / policy: “declared direct” ≠ probed verified |
| Prod “must have direct Responses” | **Policy A locked** — optional direct; launch on M3 + gates + profile approve |
| Feature reject matrix incomplete for Responses fields | Gate work item before claiming reject vision/json_schema/reasoning |
| `async_pre_call_hook` fail-open on unknown errors | Must fail-closed before Responses public gates |

**Inline probe (reviewer):** Responses→OpenAI Chat → `/chat/completions`; Responses→Anthropic → `/v1/messages`; both non-stream `ResponsesAPIResponse`; both stream full Responses SSE. Suite at review time: 214 passed, 1 skipped.

---

## 1. Product goal (unchanged intent, corrected shape)

**Intent:** Clients use **OpenAI Responses** as the unified public API style.

**Correct inventory mapping:**

```text
Public protocol: openai_responses
  │
  ├─ logical model glm-5.2 / kimi-k3 / …  → Chat deployments (OpenCode/Volc)
  │     transform_owner = litellm_native (Responses→completion bridge)
  │     deployment: use_chat_completions_api: true  (Chat canary)
  │
  └─ logical model claude-opus-… (separate) → Messages deployment (NewAPI, after probe)
        transform_owner = litellm_native (Anthropic completion bridge)
```

**Not in scope as a routing unit:** one logical model with both Chat-only and Messages-only candidates (would be forbidden cross-model fallback if faked).

---

## 2. Goals / Non-goals

### 2.1 Goals

1. Opt-in logical models expose **`public_protocols: [openai_responses]`** (product can deprecate Chat public over time via config/proxy allowlist — see §8).  
2. Prefer **LiteLLM native** Responses→Completion bridge for Chat and Anthropic upstreams.  
3. Project owns: protocol gates, quota select/lease, C3 cooldown, affinity, accounting owner, **mid-stream fallback disable/intercept**, staging/prod launch policy.  
4. Staging canary on **existing Chat models** first; Messages only after NewAPI protocol probe on a **dedicated** Claude logical model.  
5. No `upstream/litellm` business edits.

### 2.2 Non-goals (revised)

- Project-owned Responses→Chat / Responses→Messages adapters (request/response/SSE) — **deferred until native fails**.  
- Merging GLM/Kimi and Claude into one `model_group`.  
- Global third conversion boolean.  
- Claiming first-byte hard gate as already E2E proven.  
- Hard-wiring “prod requires direct Responses provider” into conversion architecture (policy §7).

---

## 3. Architecture (revised)

```text
Client POST /v1/responses (± stream)
        │
        ▼
Stock LiteLLM Responses entry + ProxyBaseLLMRequestProcessing
        │
        ▼
SharedQuotaRoutingStrategy
  • protocol_ctx = openai_responses
  • candidates = direct Responses deps (if any) OR chat/messages deps
    allowed for this logical model via native bridge
  • RouteCandidate.transform_owner ∈ {direct, litellm_native, project_adapter}
  • lease once; metadata: route_mode, conversion dir, transform_owner
        │
        ├─ direct          → upstream /responses
        ├─ litellm_native  → LiteLLM CompletionTransformationHandler
        │                    → acompletion → Chat /messages as appropriate
        │                    → native Responses body / SSE
        └─ project_adapter → ONLY if native No-Go (future); needs G0-A stream iterator
        │
        ▼
SharedQuotaCallback accounting (sole owner for quota)
  • MUST NOT skip reshape based on unrelated Messages native flag
  • MUST NOT rewrite stream chunks (forbidden today)
```

### 3.1 `RouteCandidate` / readiness model

```text
transform_owner: direct | litellm_native | project_adapter

readiness(source, target, owner) → bool
  # NOT a single global OR of path flags

route_candidate_enabled(route) =
  GATEWAY ∧ CONVERSION
  ∧ readiness(route.public, route.upstream, route.transform_owner)
  ∧ env_profile allows that owner/path
  # Named intentionally — must NOT collapse back into a global boolean
  # like is_conversion_routing_active() for all directions.
```

**Fix required vs current code:**

- `is_native_messages_chat_path_active()` must **not** cause `_maybe_convert_success_response` to skip **all** convert directions.  
- Messages G0-Native switch is scoped to Messages public / that owner only.  
- Do not fold `responses_convert_path_ready` into a blunt OR on `is_conversion_routing_active()`.

### 3.2 Native bridge references (pin v1.90.5)

- `litellm/responses/main.py` — `use_chat_completions_api` / provider config → completion bridge  
- `litellm/responses/litellm_completion_transformation/handler.py` — `acompletion` + response/stream transform  

Chat deployment params should include `use_chat_completions_api: true` where Responses public must hit `/chat/completions`.

---

## 4. Milestones (streamlined)

| ID | Work | Exit |
|----|------|------|
| **M0** | ADR + locks; `transform_owner` + `route_candidate_enabled(route)`; reshape-skip scoping; **Policy A**; pick definite mid-stream mechanism (§6.1) | Spec/ADR/plan approved |
| **M1** | **One** Chat logical-model canary (`glm-5.2` **or** one kimi-*); `use_chat_completions_api: true`; mock `/chat/completions`; native Responses body/SSE; **rollback drill** before any second model | Path + shape green; rollback OK; no project adapter |
| **M2** | Probe NewAPI; if Messages: **new** Claude logical model → `/messages` via native; no merge with Chat groups | Probe evidence + path contract |
| **M3** | Single lease/accounting; cancel releases; Responses errors; **enforce** §6.1 mechanism | After first **client-visible** Responses event → **no further deployment select** |
| **Prod** | Policy **A** + guardrails; direct Responses provider optional | Ops explicit approve per direction |

**Removed from near-term plan:** writing project Responses adapters; custom SSE mappers; M2 adapter coding before NewAPI probe.

---

## 5. C5 / reachability (policy vs mechanism)

### 5.1 Staging

- Allow Responses public on Chat (and later Messages) logical models via **litellm_native** when flags + per-direction readiness OK.  
- Escape hatch name: env profile e.g. `SHARED_QUOTA_ENV_PROFILE=staging` — not a third product conversion flag.

### 5.2 Production launch — **Policy A** (locked)

**Policy A:** After M1–M3 contracts pass for a direction, that direction **may** launch without a direct Responses provider.

Guardrails (ADR must record):

- Only `(source, target, owner)` directions that are **M3-green**
- Fail-closed gates complete (feature extract + pre_call no fail-open)
- **Production profile explicit approve** for that direction
- Direct Responses provider remains **optional** (not a global architecture gate)
- Policy B may later attach **per-model** for compliance/commercial reasons — not global

### 5.3 Prior absolute C5 ban

Superseded for **staging/prod native-bridge** paths under Policy A + guardrails. Absolute “never via conversion” remains for **project_adapter** until that owner is approved.

---

## 6. Streaming & first-byte (honest status)

| Claim | Reality |
|-------|---------|
| First-byte hard gate “existing” | Partially implemented; contract often **manually** sets `first_byte_sent` — not E2E auto from stream callback |
| LiteLLM Router | Supports Responses mid-request fallback — **conflicts** with no-switch-after-first-byte |
| Project stream reshape | `callbacks` forbid chunk rewrite — no stock mount for project SSE adapter |

**M3 work:** enforce §6.1; lease/cancel; rely on **native** SSE transform.

### 6.1 Mid-stream no-reselect — mechanism must be chosen in M0–M1 plan

**Acceptance (normative):** After the first **client-visible** Responses stream event is emitted, the system **must not** select another deployment for that request (no Router mid-stream fallback, no second `get_available_deployment`).

M0–M1 plan **must pick exactly one** primary mechanism (not “disable or intercept”):

| Option | Idea | Notes |
|--------|------|--------|
| **S1** | Strategy/context: set `first_byte_sent` from real stream success path; `get_available_deployment` hard-refuses if set; **and** force Router retries/fallback off for Responses stream (`num_retries=0` / disable fallback for that call) via project-controlled kwargs or router settings scoped to canary | Prefer if achievable without upstream business edit |
| **S2** | Thin Responses stream wrapper (project) that consumes native iterator, marks first event, suppresses further router retry by owning the stream end-to-end | Closer to G0-A; only if S1 insufficient |
| **S3** | Document LiteLLM fallback as incompatible → staging No-Go for streaming until S1/S2 lands; ship non-stream M1 only | Escape hatch, not preferred end state |

Plan tasks must include a contract that **fails** if a second deployment select occurs after first visible event.

---

## 7. Gates & fail-closed (P1 backlog)

Before Responses public staging:

1. **Feature extract** for Responses: `input_image`, `parallel_tool_calls`, `reasoning`, `text.format`, etc. — reject unsupported until matrix says otherwise.  
2. **`async_pre_call_hook`:** unknown exceptions must **not** fail-open.  
3. **“Only Responses” surface:** clarify product meaning:
   - **Model-level:** new models opt into Responses only; Chat/Messages routes still exist for legacy models; **or**
   - **Edge-level:** reverse-proxy allowlist only `POST /v1/responses` (LiteLLM also mounts GET/DELETE/cancel/input-items/WS — out of protocol_context today).

---

## 8. Unnecessary work (do not schedule)

- Project Responses→Chat / →Messages adapters  
- Project Responses SSE event conversion  
- M2 adapter implementation before NewAPI probe  
- Global third conversion flag  
- Scheduler complexity for mixed Chat+Messages candidates on one `model_group`

---

## 9. Acceptance (revised)

1. **M1:** Exactly **one** Chat logical model canary (glm-5.2 **or** one kimi-*) with Responses public → mock `/chat/completions` via native bridge; Responses-shaped non-stream (+ stream sample); **rollback drill** before enabling any second model.  
2. **M2:** Separate Claude logical model after Messages probe → mock `/messages`; Responses-shaped.  
3. **M3:** One lease / one accounting / cancel releases; after first client-visible Responses stream event, **no second deployment select** (mechanism S1/S2 locked in plan).  
4. **Prod:** Policy **A** + guardrails; optional direct provider.  
5. No project adapter code unless native spike fails.  
6. `upstream/litellm` business clean.

---

## 10. Open decisions (for M0–M1 plan)

1. ~~Prod launch A vs B~~ → **A locked**.  
2. Edge allowlist vs model-level “Responses-only” (product).  
3. **Must choose S1 vs S2** (or temporary S3) for mid-stream no-reselect — in M0–M1 plan, not deferred to M3 prose.  
4. How generator emits `use_chat_completions_api: true` for Responses-public Chat deps.  
5. Which single canary model: `glm-5.2` vs specific kimi id.

---

## 11. Approval

- [x] Architecture §3 + milestones §4 accepted (eng 2026-07-26)  
- [x] Prod policy **A** chosen  
- [x] Three wording fixes (`route_candidate_enabled`, single canary, §6.1 mechanism)  
- [ ] M0–M1 implementation plan reviewed: `docs/superpowers/plans/2026-07-26-unified-public-responses-m0-m1.md`  
- [ ] Then coding may start on M0/M1 tasks only

**Do not implement adapters or stream mappers under the pre-revision draft.**

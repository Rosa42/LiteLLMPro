# Unified Public Responses — M0–M1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Land ADR + direction-scoped readiness scaffolding, then prove **one** Chat logical-model canary (`glm-5.2`) serves public Responses via LiteLLM native completion bridge → upstream `/chat/completions`, with rollback drill. **No** project Responses adapters or custom SSE.

**Architecture:** Stock `/v1/responses` + `SharedQuotaRoutingStrategy` + LiteLLM `use_chat_completions_api` bridge (`transform_owner=litellm_native`). Enablement via `route_candidate_enabled(route)` — never a global conversion OR-flag. Mid-stream no-reselect: prefer **S1** (real `first_byte_sent` + Responses stream retries/fallback off).

**Tech Stack:** LiteLLM v1.90.5, `plugins/shared_quota_router`, pytest + MockHandler.

**Design:** `docs/superpowers/specs/2026-07-26-unified-public-responses-design.md`  
**Prod policy:** **A** (M3-green + fail-closed gates + production profile approve; direct Responses provider optional)

## Global Constraints

- **No coding until this plan is reviewed/approved.** This file is planning only until then.
- No `upstream/litellm` business edits.
- No project Responses→Chat / →Messages adapters or SSE mappers in M0–M1.
- Do **not** open a second logical model until M1 canary + rollback green.
- Do **not** merge Claude into Chat `model_group`s (M2 later).
- Prefer Chinese comments in new project code.
- Commits only when user asks.

## Locked plan choices

| Topic | Choice |
|-------|--------|
| Canary model | **`glm-5.2`** (OpenCode + Volc already share this Chat group) |
| Transform owner (M1) | `litellm_native` |
| Mid-stream mechanism | **S1 primary**; if S1 cannot turn off Router `_aresponses_streaming_iterator` mid-fallback without upstream edit → document spike failure and escalate **S2** in a plan amend (do not silently ship stream canary) |
| M1 stream scope | Non-stream contract **required**; stream sample **required** only if S1 proven in Task M1-S; else non-stream only + S3 temporary for stream |
| Surface “only Responses” | **Model-level** for canary (`public_protocols: [openai_responses]` on glm-5.2); edge allowlist out of M0–M1 |

## File map

| File | Responsibility |
|------|----------------|
| `docs/adr/ADR-unified-public-responses.md` | Policy A, transform_owner, route_candidate_enabled, S1, C5 supersede |
| `docs/conversion/responses-direct-evaluation.md` | Conditional / Policy A note |
| `docs/superpowers/specs/2026-07-26-unified-public-responses-design.md` | Already revised; sync any plan deltas |
| `plugins/shared_quota_router/models.py` | `TransformOwner` enum; `RouteCandidate.transform_owner` |
| `plugins/shared_quota_router/route_readiness.py` (new) | `readiness(source,target,owner)`, `route_candidate_enabled(route)` |
| `plugins/shared_quota_router/feature_flags.py` | Env profile helper; **stop** using Messages native flag as global reshape skip |
| `plugins/shared_quota_router/callbacks.py` | Scope `_maybe_convert_success_response` skip to Messages G0-B owner only |
| `plugins/shared_quota_router/protocol_gates.py` | Responses public via native Chat bridge under staging/profile; remove absolute C5 early-return for Policy A paths |
| `plugins/shared_quota_router/generator.py` | Emit `use_chat_completions_api: true` for Chat deps when logical model Responses-public |
| `plugins/shared_quota_router/strategy.py` | Set transform_owner; call `route_candidate_enabled`; first_byte refuse |
| `config/plans.yaml` / logical models | Canary: glm-5.2 `public_protocols: [openai_responses]` (staging only until ops) |
| `tests/contract/test_m1_responses_chat_native_bridge.py` | Path + shape + rollback docs |
| `tests/unit/test_route_candidate_enabled.py` | Direction readiness not global |

**Out of M0–M1:** NewAPI probe, Claude model, project adapters, edge allowlist, full Responses feature extract (schedule as M1-gate prerequisite Task if needed for canary safety — minimal reject for tools/vision on Responses if extract missing).

---

### Task M0-1: ADR — Policy A + boundaries

**Files:**
- Create: `docs/adr/ADR-unified-public-responses.md`
- Modify: `docs/conversion/responses-direct-evaluation.md`
- Modify: `docs/adr/ADR-conversion-adapter-boundary.md` (link; Responses public ≠ Messages G0-Native)

- [ ] **Step 1: Write ADR** recording:
  - Public Responses product goal; multi logical models
  - Default `transform_owner=litellm_native` for Responses→Chat/Messages
  - `route_candidate_enabled(route)` (not global bool)
  - **Policy A** + guardrails (M3-green ∧ fail-closed ∧ production profile approve)
  - Policy B optional per-model later
  - S1 mid-stream no-reselect acceptance text
  - Supersede absolute C5 No-Go for native-bridge under Policy A

- [ ] **Step 2: Update responses-direct-evaluation.md** to Conditional / Policy A

- [ ] **Step 3: Human review ADR** (no code)

---

### Task M0-2: Choose & document S1 spike checklist (still docs + test skeleton)

**Files:**
- Modify: this plan §Locked (already S1)
- Create: `docs/phase-reports/responses-m1-s1-spike-notes.md` (filled during implementation)

S1 checklist to prove before stream canary:

1. `async_log_stream_event` / equivalent sets `first_byte_sent` on **first client-visible** Responses event (not manual test poke only).  
2. `get_available_deployment` raises when `first_byte_sent`.  
3. For Responses stream canary calls: `num_retries=0` and/or fallbacks empty / max_fallbacks=0 so `_aresponses_streaming_iterator` cannot start a second deployment.  
4. Contract: spy/select counter ≥2 after first event ⇒ **FAIL**.

If 3 impossible without upstream edit → **stop stream canary**; amend plan to S2 or S3; keep non-stream M1.

- [ ] **Step 1: Document spike checklist in phase-report stub**
- [ ] **Step 2: Plan amend gate** — stream canary blocked until checklist green

---

### Task M0-3: `TransformOwner` + `route_candidate_enabled` (TDD)

**Files:**
- Modify: `plugins/shared_quota_router/models.py`
- Create: `plugins/shared_quota_router/route_readiness.py`
- Test: `tests/unit/test_route_candidate_enabled.py`

**Interfaces:**

```python
class TransformOwner(str, Enum):
    DIRECT = "direct"
    LITELLM_NATIVE = "litellm_native"
    PROJECT_ADAPTER = "project_adapter"

def readiness(
    source: ApiProtocol,
    target: ApiProtocol,
    owner: TransformOwner,
) -> bool: ...

def route_candidate_enabled(route: RouteCandidate, *, profile: str | None = None) -> bool: ...
```

- [ ] **Step 1: Failing tests**
  - Messages native flag ON must **not** alone enable Responses→Chat project_adapter
  - Responses→Chat + `LITELLM_NATIVE` ready when gateway∧conversion∧profile allows
  - Global `is_conversion_routing_active()` remaining for legacy Messages→Chat G0-B must be documented; new Responses paths use `route_candidate_enabled` only

- [ ] **Step 2: Implement minimal readiness table** for M1:
  - `(openai_responses, openai_chat, litellm_native)` → True when flags + profile
  - `project_adapter` → False until future epic

- [ ] **Step 3: Tests PASS**

- [ ] **Step 4: Commit** (only if user asked)

---

### Task M0-4: Fix reshape-skip scoping + pre_call fail-closed (prerequisite)

**Files:**
- Modify: `plugins/shared_quota_router/callbacks.py`
- Test: extend `tests/unit/test_g0_native_disables_g0b_rewrite.py` + new fail-closed test

- [ ] **Step 1: Failing test** — native Messages flag ON + `route_mode=convert` for a **non-Messages-G0-B** metadata shape must **not** skip project reshape solely due to Messages flag (Responses native owner never relied on project reshape anyway; assert Messages skip is gated on public protocol / owner meta)

Recommended rule:

```python
# only skip G0-B project reshape when owner is litellm_native for Messages→Chat
# or metadata says transform_owner=litellm_native
```

- [ ] **Step 2: `async_pre_call_hook`** — unknown exceptions must not continue (fail-closed); add unit test

- [ ] **Step 3: PASS + commit** (if asked)

---

### Task M1-1: Generator / plans — canary glm-5.2 Responses-public + `use_chat_completions_api`

**Files:**
- Modify: `plugins/shared_quota_router/generator.py`
- Modify: `config/plans.yaml` / logical_models (or document staging overlay)
- Test: `tests/unit/test_generator.py` assert emitted param

- [ ] **Step 1: Failing test** — when logical model has `openai_responses` public and deployment is Chat, litellm_params include `use_chat_completions_api: true`

- [ ] **Step 2: Implement generator emit**

- [ ] **Step 3: Configure canary only `glm-5.2`** with `public_protocols: [openai_responses]` in staging overlay / plans — **no other models**

- [ ] **Step 4: PASS**

---

### Task M1-2: Gates — Responses public via native Chat for canary

**Files:**
- Modify: `plugins/shared_quota_router/protocol_gates.py`
- Modify: `plugins/shared_quota_router/config_schema.py` if needed (staging profile allows Responses without direct Responses upstream)
- Test: unit gate tests

- [ ] **Step 1: Failing test** — glm-5.2 Chat-only deps + Responses public + staging profile + conversion flags → `public_reachable` True via litellm_native path

- [ ] **Step 2: Failing test** — production profile without explicit direction approve → False even if staging would pass

- [ ] **Step 3: Implement** (replace absolute C5 early-return with Policy A rules)

- [ ] **Step 4: Minimal feature reject** — if Responses body has tools/vision/etc. and extract missing, reject unsupported **or** document canary text-only client constraint in ops checklist

- [ ] **Step 5: PASS**

---

### Task M1-3: Contract — Responses → `/chat/completions` (native)

**Files:**
- Create: `tests/contract/test_m1_responses_chat_native_bridge.py`

- [ ] **Step 1: Non-stream** — Router/proxy `aresponses` with glm-5.2, Chat dep `api_base=mock`, `use_chat_completions_api=true` → MockHandler path contains `/chat/completions`; response is Responses-shaped (`output` / status — assert fields native returns)

- [ ] **Step 2: Stream sample** — only if S1 checklist green; else skip with reason S3

- [ ] **Step 3: Select counter / lease** — non-stream single select + lease release path smoke

- [ ] **Step 4: PASS**

---

### Task M1-4: Rollback drill + ops notes

**Files:**
- Modify: `docs/operations-protocol-conversion.md` or new `docs/operations-protocol-responses.md`
- Modify: `docs/phase-reports/remaining-dev-plan.md` / unified progress (short note)

Rollback drill (document + optional script checklist):

```text
[ ] Remove glm-5.2 Responses public opt-in (or CONVERSION/profile off)
[ ] Restart
[ ] Responses canary → controlled reject / no-route
[ ] Chat public models (if any) unaffected
[ ] Redis quota/affinity untouched
[ ] Do not enable second model until drill recorded
```

- [ ] **Step 1: Write ops checklist**
- [ ] **Step 2: Mark M1 exit** — canary + rollback recorded
- [ ] **Step 3: Commit** (if asked)

---

## Stop / escalation

1. Native bridge does not hit `/chat/completions` with `use_chat_completions_api=true` → stop; re-check generator/router params; **do not** write project adapter in M1.  
2. S1 cannot disable mid-stream fallback → no stream canary; amend to S2/S3.  
3. Temptation to enable kimi + glm together → **forbidden** until M1 exit.  
4. Reshape skip regresses Messages G0-Native → fix before merge.  
5. Upstream business edit requested → refuse; escalate design.

## Self-review

| Spec item | Task |
|-----------|------|
| Policy A ADR | M0-1 |
| `route_candidate_enabled` | M0-3 |
| S1 chosen + acceptance | M0-2, M1-3 |
| One glm-5.2 canary | M1-1..4 |
| No project adapters | Global |
| Reshape scope fix | M0-4 |
| Rollback before scale-out | M1-4 |

## Review intake

| Input | Disposition |
|-------|-------------|
| Eng confirm §3/§4 + Policy A + three wording fixes | Folded into design + this plan |

---

**After plan approval:** implement M0-1 → M1-4 in order. **M2/M3 not in this plan.**

## Implementation status (2026-07-26)

- [x] M0–M1 code/docs landed in tree; suite `208 passed, 1 skipped` at checkpoint.
- Stream canary still gated on S1 checklist (`docs/phase-reports/responses-m1-s1-spike-notes.md`).
- Staging: set `SHARED_QUOTA_ENV_PROFILE=staging` + dual flags before live canary.

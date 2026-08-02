# G0-Native Spike — Messages→Chat via LiteLLM switch

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prove LiteLLM v1.90.5 `use_chat_completions_url_for_anthropic_messages` + project strategy/gates deliver public `POST /v1/messages` → upstream `/chat/completions` with Anthropic client shape, **without** FastAPI route swap.

**Architecture:** Stock `/v1/messages` → `ProxyBaseLLMRequestProcessing` → `SharedQuotaRoutingStrategy` (select/lease/gates) → LiteLLM native Messages→Chat adapter when switch on. Project C2 body rewrite / post_call reshape **disabled** under native readiness. Callback remains **sole** accounting owner unless spike proves otherwise.

**Tech Stack:** LiteLLM v1.90.5, `plugins/shared_quota_router`, pytest + MockHandler, real proxy app where required.

**Design:** `docs/superpowers/specs/2026-07-26-thin-g0a-front-adapter-design.md` (§3 G0-Native)  
**Blocked sibling:** `docs/superpowers/plans/2026-07-26-thin-g0a-front-adapter.md` (G0-A fallback — do not implement until this spike fails)

## Global Constraints

- No `upstream/litellm` business edits.  
- Dual-flag AND for conversion traffic.  
- Stream convert remains No-Go (C4) unless spike explicitly reopens.  
- Responses must not become reachable via conversion.  
- Exactly one transform owner; exactly one accounting owner.  
- Production default: `PROTOCOL_CONVERSION_ENABLED=false` until spike green + ops checklist.  
- Prefer Chinese comments in new project code.

## File map

| File | Responsibility |
|------|----------------|
| `config/litellm.yaml` (or generator emit) | `litellm_settings.use_chat_completions_url_for_anthropic_messages: true` when conversion path intended |
| `plugins/shared_quota_router/feature_flags.py` | `native_messages_chat_ready` / fold into `is_conversion_routing_active` readiness |
| `plugins/shared_quota_router/strategy.py` | Disable `_apply_convert_to_request_kwargs` when native ready |
| `plugins/shared_quota_router/callbacks.py` | Skip G0-B `_maybe_convert_success_response` when native ready; accounting once |
| `tests/contract/test_p4_native_messages_to_chat_path.py` | **P4-Native** positive path |
| `tests/contract/test_p4_conversion_messages_to_chat_path.py` | Keep G0-B negative **or** gate behind native-off; do not delete evidence without ADR note |
| `docs/adr/ADR-conversion-adapter-boundary.md` | Document native switch; G0-A = fallback |
| `docs/operations-protocol-conversion.md` | Native switch ops + rollback |

---

### Task N1: Enable + document native switch

**Files:**
- Modify: config / generator as appropriate
- Modify: ADR + ops docs
- Test: small unit asserting flag readable from env/settings

- [x] **Step 1: Confirm pin behavior**
- [x] **Step 2: Wire setting into project config path** (generator + `config/litellm.yaml`, default false)
- [x] **Step 3: ADR note** — already in ADR-conversion-adapter-boundary.md
- [ ] **Step 4: Commit** (only if user asked)

---

### Task N2: Disable project G0-B convert mutate when native ready

**Files:**
- Modify: `strategy.py`, `callbacks.py`, `feature_flags.py`
- Test: `tests/unit/test_g0_native_disables_g0b_rewrite.py`

**Interfaces:**
- `is_native_messages_chat_path_active() -> bool` (reads litellm flag + optional cache)
- When true: **no** `_apply_convert_to_request_kwargs`; **no** `_maybe_convert_success_response`
- Strategy still sets metadata: `shared_quota_route_mode`, conversion id, deployment ids for cooldown/metrics

- [x] **Step 1: Failing tests** — `tests/unit/test_g0_native_disables_g0b_rewrite.py`
- [x] **Step 2: Implement skip gates**
- [x] **Step 3: Tests PASS**
- [ ] **Step 4: Commit** (only if user asked)

---

### Task N3: P4-Native contract (Router or proxy mock path)

**Files:**
- Create: `tests/contract/test_p4_native_messages_to_chat_path.py`

- [x] **Step 1: Positive path** — `test_p4_native_messages_openai_hits_chat_completions`
- [x] **Step 2: Direct anthropic/ still `/messages`** with native switch **on**
- [ ] **Step 3: Chat 400 → Anthropic error** (native adapter) — deferred to staging/proxy smoke if mock coverage thin
- [ ] **Step 4: Accounting once** — deferred; callback remains sole owner (no explicit gateway double-call)
- [ ] **Step 5: Commit** (only if user asked)

---

### Task N4: Conversion gate readiness

**Files:**
- Modify: `feature_flags.py`, `protocol_gates.py` / `public_reachable` consumers
- Test: unit + contract

- [x] **Step 1:** `is_conversion_routing_active()` requires native ∨ g0a_mount
- [x] **Step 2:** conversion-only without path → deny (`test_p3_conversion_only_denied_without_path_ready`)
- [ ] **Step 3: Commit** (only if user asked)

---

### Task N5: Real proxy smoke (preferred) + ops checklist

- [ ] **Step 1:** Prefer in-process or docker LiteLLM proxy — **operator** (Router contract covered by P4-Native)
- [x] **Step 2:** Ops checklist recorded in `remaining-dev-plan.md` §10.2 + `operations-protocol-conversion.md`
- [x] **Step 3:** residual-risks / remaining-dev-plan updated — R1 mitigated by native; G0-A deferred
- [ ] **Step 4: Commit** (only if user asked)

---

## Stop / escalation

1. Native switch on but mock still `/responses` under real proxy → re-check settings load order; do **not** jump to G0-A until config proven.  
2. Native hits `/chat/completions` but double accounting / double reshape → fix owner contract before enabling conversion flag.  
3. Native cannot preserve shared-quota lease / C3 / deployment binding → open **G0-A fallback** plan with P0-2/P0-3/P1-1..6 closed first.  
4. Stream convert unexpectedly enabled by native → keep project reject or document C4 reopen.  
5. Never patch `upstream/litellm` to force the switch.

## Self-review

| Spec item | Task |
|-----------|------|
| Native switch wired | N1 |
| No dual transform | N2 |
| P4-Native `/chat/completions` | N3 |
| Gate requires path readiness | N4 |
| Ops + residual risks | N5 |
| G0-A route swap | Out of scope until spike fails |

## Review intake

| Input | Disposition |
|-------|-------------|
| User review 2026-07-26 (P0 native falsification + TOCTOU/proxy/accounting) | Design rewritten; this spike created; G0-A blocked |
| Prior G0-A architecture/code reviews | Retained as **fallback constraints** only |

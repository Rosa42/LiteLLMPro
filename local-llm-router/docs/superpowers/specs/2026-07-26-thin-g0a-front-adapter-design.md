# Design: Messages→Chat convert — G0-Native first, G0-A fallback

**Date:** 2026-07-26  
**Status:** **FAIL for immediate G0-A coding** — major revision required; **G0-Native Spike first**  
**Primary path:** LiteLLM native switch `use_chat_completions_url_for_anthropic_messages` + project strategy/gates  
**Fallback path:** Thin G0-A route swap (only if native fails shared-quota / pinning / cooldown contracts)  
**Supersedes for convert path:** G0-B body-only path assumption that “only route swap can hit `/chat/completions`”  
**Keeps:** G0-B metadata dual-bucket for **direct** protocol routing; dual-flag AND; H1–H7; C3 isolation

---

## 0. Verdict (2026-07-26 review)

| Item | Verdict |
|------|---------|
| Implement current thin G0-A plan as written | **No** — needs major change, not polish |
| Why | (1) P4 path premise falsified by LiteLLM v1.90.5 native switch; (2) route-swap design still has TOCTOU, proxy-chain bypass, dual accounting, secret resolution gaps |
| Next | **G0-Native Spike** (real proxy contracts) → only then consider G0-A fallback |

---

## 1. Problem (revised)

### 1.1 What we thought

Public `POST /v1/messages` + `openai/` Chat deployment hits upstream `/responses` (P0-03, P4-01).  
G0-B body rewrite cannot change that path → escalate to replace FastAPI `/v1/messages` (thin G0-A).

### 1.2 What pinned LiteLLM v1.90.5 actually provides

```text
litellm.use_chat_completions_url_for_anthropic_messages
# env: LITELLM_USE_CHAT_COMPLETIONS_URL_FOR_ANTHROPIC_MESSAGES=true
# yaml: litellm_settings.use_chat_completions_url_for_anthropic_messages: true
```

Source: `upstream/litellm/litellm/llms/anthropic/experimental_pass_through/messages/handler.py`  
(`_should_route_to_responses_api` returns False when the flag is on → Messages→Chat adapter, not Responses.)

**Workspace probe:** with only that env set, the former P4-01 mock path became `POST /chat/completions` instead of `/responses`.

### 1.3 Remaining real problems (native does not auto-solve)

Native switch fixes **upstream URL for openai/ Messages**, but project still owns:

- public protocol / conversion-only gates (`public_reachable`)
- shared quota select + single lease
- route-scoped convert cooldown (C3)
- affinity / priority / failover
- **single ownership** of request rewrite + response reshape (must not double-apply G0-B C2 adapter)
- accounting owner (lease release / metrics once)

---

## 2. Goal

When conversion is selected for `anthropic_messages → openai_chat`:

1. Upstream receives **`/chat/completions`** (not `/responses`).  
2. Client still sees **Anthropic Messages** success/error JSON.  
3. Prefer **stock proxy chain** (auth → processor → strategy → native Messages→Chat adapter).  
4. No `upstream/litellm` business edits.  
5. Direct `anthropic/` deployments still hit `/messages`.  
6. Quota lease once; C3 convert cooldown still correct.

Non-goals: streaming convert productization (C4 No-Go until reopened); Responses convert; Chat→Messages reverse adapter; editing LiteLLM core.

---

## 3. Primary architecture — G0-Native

```text
Client POST /v1/messages
        │
        ▼
Stock LiteLLM anthropic_response / ProxyBaseLLMRequestProcessing
        │
        ▼
Project SharedQuotaRoutingStrategy (unique select + lease)
        │  route_mode=direct | convert (metadata only; no body rewrite when native on)
        ▼
LiteLLM Messages handler
  if use_chat_completions_url_for_anthropic_messages:
      LiteLLMMessagesToCompletionTransformationHandler → /chat/completions
  else:
      Responses path (legacy; must stay off when conversion-only Chat deps exist)
        │
        ▼
Native Chat→Anthropic response (+ project callback accounting once)
```

### 3.1 Ownership split

| Concern | Owner when native path active |
|---------|-------------------------------|
| Auth, spend, guardrails, budget, proxy logging | Stock `ProxyBaseLLMRequestProcessing` |
| Protocol gate / `public_reachable` / conversion-only | Project pre-call / strategy filters |
| Quota select, lease, affinity, C3 cooldown | Project strategy + **callback as sole accounting owner** |
| Request/response Messages↔Chat transform | **LiteLLM native adapter only** |
| Project C2 `_apply_convert_to_request_kwargs` / post_call reshape | **Disabled** when native switch readiness is true |
| Upstream path `/chat/completions` vs `/responses` | Native switch |

### 3.2 Readiness gate (must be one boolean)

```text
native_messages_chat_ready =
    litellm.use_chat_completions_url_for_anthropic_messages is True
    AND project probes confirm /v1/messages + openai/ → /chat/completions

conversion_routing_active =
    PROTOCOL_AWARE_GATEWAY_ENABLED
    AND PROTOCOL_CONVERSION_ENABLED
    AND (native_messages_chat_ready OR g0a_mount_ready)   # never conversion without a proven path
```

Mount-ready applies **only** if G0-A fallback is enabled. Native-only mode: `g0a_mount_ready` unused / false.

### 3.3 G0-Native Spike acceptance (blocking)

Real LiteLLM proxy app (not Router-only unit):

1. **P4-Native:** `POST /v1/messages` + Chat deployment → mock `/chat/completions`; Anthropic success body.  
2. Chat upstream 400 → Anthropic `type=error`.  
3. Direct `anthropic/` → `/messages` (native switch must not break direct).  
4. Conversion-only + flags on → gate allows; flags off → reject.  
5. Quota failover / single lease / C3 convert cooldown still correct.  
6. **No double reshape:** project G0-B convert apply + native adapter do not both mutate.  
7. **No double accounting:** callback success/failure fires once per request.  
8. Optional params / tools: document native coverage vs C2 text-only pilot (prefer native).  
9. Stream convert: remain rejected or explicitly re-scoped (C4).

If all pass → **do not implement G0-A route swap**; enable conversion on native + project gates only.

---

## 4. Fallback architecture — thin G0-A (only if native fails)

Enter G0-A only when native cannot satisfy one of:

- project deployment exact binding under shared-quota semantics  
- single-lease / route-scoped cooldown  
- project-specific `public_reachable` without breaking stock  
- proven inability to disable double G0-B rewrite while keeping gates

### 4.1 Known P0 gaps that must be closed **before** coding G0-A

#### P0-2 TOCTOU — observe-then-stock reselect

`select(acquire_lease=False)` then delegate stock → stock strategy selects again → `route_mode` / deployment can diverge.

**Required contract (pick one):**

- **A.** Wrapper does **no** pre-select; single atomic select+execute entry; or  
- **B.** Direct and convert both use project pinned executors (no free stock reselect); or  
- **C.** Stay on native (preferred) so strategy select is the only decision.

#### P0-3 Low-level `litellm.acompletion` bypasses proxy chain

Not “MVP spend gap”. Affects key/team model access, guardrails, budget, proxy logging, headers, spend lifecycle.

**Required:** either stay inside `ProxyBaseLLMRequestProcessing` / Router path, or explicitly document which controls are forfeited and get product sign-off — not “fix later”.

### 4.2 P1 gaps (G0-A)

| ID | Issue | Required decision |
|----|--------|-------------------|
| P1-1 | Explicit `on_success`/`on_failure` + global callback → double lease/metrics/cooldown | **Accounting owner:** callback-only **or** gateway-only with `shared_quota_accounting_owner=g0a` skip |
| P1-2 | `api_key: os.environ/NAME` from generator — low-level call must resolve secrets | Reuse project secret resolver; never log raw keys |
| P1-3 | Project `deployment_id` ≠ LiteLLM `model_info.id` | `find_model_entry` uniqueness + no drift tests |
| P1-4 | Native adapter richer than C2 text-only | Prefer native; avoid dual transform semantics |
| P1-5 | Lazy `_force_load` + route swap fragile | Real proxy startup route identity tests |
| P1-6 | Mount fail + `CONVERSION=true` | `conversion_active` must AND `g0a_mount_ready` |

### 4.3 G0-A sketch (reference only — not approved to implement yet)

Lazy-aware warm → swap `POST /v1/messages` → atomic decision/execute → accounting owner contract → secret resolution → real proxy mount tests.  
See plan status: **blocked pending G0-Native Spike**.

Historical notes (lazy warm, single lease, pinned call, metadata buckets) remain in plan appendix as **fallback constraints**, not current build order.

---

## 5. Invariants (both paths)

- Direct ≻ convert ranking unchanged.  
- Redis fail-closed; stream convert rejected until C4 reopened.  
- C3 route-scoped cooldown on convert failures.  
- Responses never unlocked via conversion.  
- No secrets in logs; `dropped_fields` path-only.  
- Exactly one transform owner; exactly one accounting owner.  
- No `upstream/litellm` business diffs.

---

## 6. Acceptance matrix

| ID | Path | Criterion |
|----|------|-----------|
| P4-Native | Stock `/v1/messages` + native switch | Mock `/chat/completions`; Anthropic body |
| P4-G0A | Only if fallback approved | Mounted wrapper + contracts in §4 |
| Direct | Both | `anthropic/` → `/messages` |
| Gate | Both | conversion-only requires proven path readiness |
| Accounting | Both | lease ±1 once; cooldown once |
| ADR | Docs | Native switch documented; G0-A marked fallback |

---

## 7. Risks

| Risk | Mitigation |
|------|------------|
| Treat route swap as only path | Document native switch in ADR; run P4-Native first |
| Double G0-B + native transform | Disable project convert apply when native ready |
| Double callback accounting | Single owner contract (native spike must prove) |
| Native switch ≠ quota policy | Strategy still selects; switch only fixes URL adapter |
| LiteLLM upgrade drift | Re-run path probes on pin bumps |
| G0-A L0 rollback | Unmount/redeploy — flag alone does not restore routes |

---

## 8. Rollback

| Level | Native-primary | G0-A fallback (if ever mounted) |
|-------|----------------|----------------------------------|
| L0 | N/A (no route swap) | Unmount / redeploy without g0a |
| L1 | `PROTOCOL_CONVERSION_ENABLED=false` | Same (+ wrapper may remain) |
| L1b | Keep `GATEWAY=true` | Same |
| L2 | Strip conversion-only allowlists | Same |
| L3 | `GATEWAY=false` | Same |
| Native off | Clear `use_chat_completions_url_for_anthropic_messages` + restart (openai/ Messages may return to `/responses`) | — |

---

## 9. Open follow-ups

- Reverse adapter Chat public → Anthropic upstream.  
- Streaming convert productization.  
- Whether to retire project C2 text adapter once native proven.  
- G0-A only if Spike fails shared-quota contracts.

---

## 10. Links

- Plan (Native Spike): `docs/superpowers/plans/2026-07-26-g0-native-messages-chat-spike.md`  
- Plan (G0-A fallback, blocked): `docs/superpowers/plans/2026-07-26-thin-g0a-front-adapter.md`  
- ADR: `docs/adr/ADR-conversion-adapter-boundary.md`  
- LiteLLM: `handler.py` `_should_route_to_responses_api`, `__init__.py` env default

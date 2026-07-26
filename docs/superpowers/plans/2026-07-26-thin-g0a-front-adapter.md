# Thin G0-A Front Adapter Implementation Plan

> **STATUS: BLOCKED — do not implement until G0-Native Spike fails.**  
> Primary path: `docs/superpowers/plans/2026-07-26-g0-native-messages-chat-spike.md`  
> Design verdict: **FAIL for immediate coding** (`docs/superpowers/specs/2026-07-26-thin-g0a-front-adapter-design.md`)  
> Reason: LiteLLM v1.90.5 native `use_chat_completions_url_for_anthropic_messages` falsifies “must swap `/v1/messages`” premise; G0-A still has unresolved P0 TOCTOU / proxy-chain bypass / dual accounting.

> **For agentic workers:** Do **not** start Task 1–6 below unless Spike stop-condition escalates to G0-A **and** P0-2/P0-3/P1-1..6 in the design are closed. Prefer G0-Native plan.

**Goal (fallback only):** When Messages→Chat conversion is selected and native path cannot satisfy shared-quota contracts, dispatch via project-owned `/v1/messages` wrapper…

**Architecture (fallback):** …lazy-warm + route swap… (details below retained as constraints, not current build order).

**Tech Stack:** LiteLLM v1.90.5 proxy + Router, FastAPI, `plugins/shared_quota_router`, pytest + MockHandler.

**Design spec:** `docs/superpowers/specs/2026-07-26-thin-g0a-front-adapter-design.md` (§4 Fallback)  
**Prerequisite gate:** G0-Native Spike failed shared-quota / pinning / cooldown **or** product explicitly rejects native; then close:

1. **P0-2** Atomic decision/execute (no observe-then-stock TOCTOU)  
2. **P0-3** Stay in proxy processor **or** signed-off bypass list (not “spend gap later”)  
3. **P1-1** Accounting owner contract (callback XOR gateway)  
4. **P1-2** Secret resolution for `os.environ/NAME`  
5. **P1-3** `find_model_entry` uniqueness vs LiteLLM `model_info.id`  
6. **P1-5** Real proxy lazy startup mount tests  
7. **P1-6** `conversion_active` AND `g0a_mount_ready`

**Historical gate (superseded):** R-G0A-1..6 + CODE REV — still required **if** G0-A proceeds, but insufficient alone after 2026-07-26 native review.

---

## Appendix — original task breakdown (frozen until unblocked)

The following Tasks 1–6 are **not** the current development order. They remain as a draft for fallback implementation after the prerequisite gate above.

**Original Goal:** When Messages→Chat conversion is selected, dispatch through LiteLLM Chat from a project-owned `/v1/messages` wrapper, while direct Anthropic upstreams keep the stock Messages path.

**Original Architecture:** At proxy startup, **lazy-warm** Anthropic Messages (`anthropic_passthrough`), then swap FastAPI `POST /v1/messages` for a thin wrapper (auth + gates). Decision select uses `acquire_lease=False` (no `mark_tried`). `route_mode=direct` delegates stock `anthropic_response` (lease once inside stock). `route_mode=convert` acquires lease once, then **pinned** `litellm.acompletion` (api_base/api_key/model from `find_model_entry`) — **not** `router.acompletion`. Returns Anthropic-shaped JSON. No `upstream/litellm` business edits.

**Original Gate:** Architecture + code-landing reviews = Conditional Go with R-G0A-1..6 — **superseded** by native-first FAIL verdict.

## Global Constraints

- LiteLLM pin: `v1.90.5` (`0430743f2fd4005898506e00bc62dd47bcff6fc9`); **no** business edits under `upstream/litellm/`.
- Convert only when `is_conversion_routing_active()` (gateway ∧ conversion ∧ path readiness).
- Streaming convert remains **rejected** (C4 No-Go).
- Responses must not become reachable via conversion (C5).
- Redis fail-closed; do not flush quota/affinity on flag toggles.
- No secrets/prompts in logs, metrics reasons, or fixtures.
- Prefer Chinese comments in new project code where comments are needed.
- Production default: `PROTOCOL_CONVERSION_ENABLED=false` until Native Spike (or unblocked G0-A Task 6) green.

## File map

| File | Responsibility |
|------|----------------|
| `plugins/shared_quota_router/g0a_messages_gateway.py` | Decide direct vs convert; convert lane orchestration + lease/callback/cooldown accounting |
| `plugins/shared_quota_router/g0a_route_mount.py` | Swap/restore FastAPI `/v1/messages`; hold stock endpoint ref |
| `plugins/shared_quota_router/bootstrap.py` | Call mount after router ready |
| `plugins/shared_quota_router/strategy.py` | `select_route_candidate(...)` → `RouteCandidate` for gateway |
| `tests/unit/test_g0a_messages_gateway.py` | Unit: direct vs convert branching, stream reject, flag off, callback/lease |
| `tests/contract/test_p4_conversion_messages_to_chat_path.py` | **Replace** G0-B negative test with G0-A positive `/chat/completions` (+ mounted proxy POST) |
| `tests/contract/test_g0a_messages_direct_and_errors.py` | Direct `anthropic/` still `/messages`; errors; R2 conversion-only gate pass |
| `docs/adr/ADR-conversion-adapter-boundary.md` | Mark G0-A implemented / acceptance |
| `docs/operations-protocol-conversion.md` | G0-A mount + rollback notes |
| `docs/phase-reports/conversion-residual-risks.md` | Close R1/R3 after G0-A green (Task 6) |

**Out of file map (do not add):** `run_messages_to_chat_completion` helper — logic stays only in `g0a_messages_gateway.py`.

---

### Review amendments (spec-coverage + ops + architecture + code-landing 2026-07-26)

Must land in Tasks below (already folded into Steps where marked **[REV]** / **[R-G0A]**):

1. Convert lane must call lease release + `SharedQuotaCallback` success/failure (Design §4).  
2. Task 4 must include mounted-app `POST /v1/messages` contract (not only unit orchestrator).  
3. Task 5: R2 positive — flags ON + conversion-only **passes** M3 gate.  
4. Convert upstream failure → route-scoped cooldown (C3) asserted or explicitly invoked.  
5. Task 6 updates `conversion-residual-risks.md`; cites remaining-dev-plan §7.1 / §9 unblock.  
6. Task 3 gate order: `resolve_request_protocol_context` → `public_reachable` / `assert_endpoint_allowed` → `select_route_candidate`.  
7. Convert lane owns response shape — **no** second rewrite via G0-B `async_post_call_success_hook`.  
8. Direct path: prefer stock endpoint delegate; `ProxyBaseLLMRequestProcessing` rebuild only as documented fallback.  
9. **[OPS]** Mount success is a hard enable prerequisite; mount fail + `CONVERSION=true` is fatal (R1 resurfaces). Expose `g0a_mount_ok` + metric.  
10. **[OPS]** Deployment pinning for convert upstream is **required** (no second strategy select / double lease).  
11. **[OPS]** Pass `user_api_key_dict` into convert upstream call; document spend gap + optional untracked counter.  
12. **[OPS]** Ops doc L0 unmount rollback; fix stale “must have direct Messages” line conflicting with `public_reachable`.  
13. **[OPS]** Staging enable checklist: mount=true ∧ P4-01 positive ∧ direct `/messages` ∧ spend probe (accept or fix gap).  
14. **[R-G0A-1]** Lazy-aware mount: warm `anthropic_passthrough` before swap; import `anthropic_response` by module; remove only POST `/v1/messages`; test must cover lazy middleware (no bare-FastAPI false positive).  
15. **[R-G0A-2]** Single-lease model: decision `select_route_candidate(acquire_lease=False)` — **no** `mark_tried` before direct stock; convert acquires once then pinned dispatch; forbid `select(True)+router.acompletion`.  
16. **[R-G0A-3]** Chat path uses `metadata.protocol=openai_chat` (not only `litellm_metadata`); keep convert markers dual-bucket; pass `litellm_call_id=request_id` for lease release.  
17. **[R-G0A-4]** HTTP-level mounted `POST /v1/messages` acceptance (Task 4 Step 2).  
18. **[R-G0A-5]** Parent ADR addendum: G0-B remains for Chat/direct Messages; Messages→Chat convert supersedes to G0-A on proxy.  
19. **[R-G0A-6]** Warm/swap fails 3× → middleware-before-lazy or Option B lane; still no upstream business edits.  
20. **[CODE]** Convert upstream = **`litellm.acompletion`** pinned (`api_base`/`api_key`/`model` from `find_model_entry`). **Forbid** `router.acompletion` and bare `specific_deployment=True` (custom strategy ignores pin → double select / `tried_quota_groups` filter-out).  
21. **[CODE]** Wrapper signature matches stock: `(fastapi_response, request, user_api_key_dict)`.  
22. **[CODE]** Pinning must be correct in Task 2 (not deferred to “fix until Task 4 PASS”).

---

### Task 1: Select API returns `RouteCandidate` for the gateway

**Files:**
- Modify: `plugins/shared_quota_router/strategy.py`
- Test: `tests/unit/test_g0a_select_route_candidate.py`

**Interfaces:**
- Consumes: existing `SharedQuotaSelector.filter_route_candidates`, `select`, lease manager
- Produces: `SharedQuotaSelector.select_route_candidate(model_group, context, *, protocol_ctx, acquire_lease: bool = False, ...) -> RouteCandidate`  
  - Gateway **decision** / peek: `acquire_lease=False` → **must not** `mark_tried` / acquire (direct stock will select once).  
  - Convert **execute**: `acquire_lease=True` only inside convert lane after decision.  
  - Raises same errors as `select` when no candidate.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_g0a_select_route_candidate.py
def test_select_route_candidate_returns_convert(monkeypatch):
    monkeypatch.setenv("PROTOCOL_AWARE_GATEWAY_ENABLED", "true")
    monkeypatch.setenv("PROTOCOL_CONVERSION_ENABLED", "true")
    clear_flag_cache()
    # registry: chat-only deployment + conversions + logical allowlist
    # ... build SharedQuotaSelector like test_remaining_dev_p1_p3 ...
    cand = sel.select_route_candidate(
        "pilot",
        RequestRoutingContext(request_id="g0a-1"),
        protocol_ctx=RequestProtocolContext(
            protocol=ApiProtocol.ANTHROPIC_MESSAGES,
            required_features=frozenset({Feature.TEXT}),
            source="test",
        ),
        acquire_lease=False,  # [R-G0A-2] decision peek
    )
    assert cand.route_mode is RouteMode.CONVERT
    assert cand.deployment.deployment_id == "chat-convert"

def test_select_route_candidate_peek_does_not_mark_tried(monkeypatch):
    # acquire_lease=False → context.tried_quota_groups unchanged / Redis tried empty
    ...
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_g0a_select_route_candidate.py -q`  
Expected: FAIL (`select_route_candidate` missing)

- [ ] **Step 3: Implement `select_route_candidate`**

Reuse `filter_route_candidates` → `filter_candidates` state filters → rank; if `acquire_lease`: lease on chosen deployment + mark tried; else return ranked `RouteCandidate` without side effects. Do **not** call `_apply_convert_to_request_kwargs` here (gateway owns convert).

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/unit/test_g0a_select_route_candidate.py -q`  
Expected: PASS

- [ ] **Step 5: Commit** (only if user asked)

```bash
git add plugins/shared_quota_router/strategy.py tests/unit/test_g0a_select_route_candidate.py
git commit -m "feat(g0a): expose select_route_candidate for convert gateway"
```

---

### Task 2: Convert-lane orchestrator (no FastAPI yet)

**Files:**
- Create: `plugins/shared_quota_router/g0a_messages_gateway.py`
- Test: `tests/unit/test_g0a_messages_gateway.py`

**Interfaces:**
- Consumes: `select_route_candidate`, `convert_public_request`, `convert_upstream_response`, `convert_upstream_error`, `MessagesToChatConverter` direction, `is_conversion_routing_active`, `find_model_entry`, `litellm.acompletion`
- Produces:
  - `async def handle_messages_convert_lane(*, public_body: dict, route: RouteCandidate, model_list: list[dict], request_id: str, user_api_key_dict=None) -> dict`
  - `class MessagesGatewayDecision(Enum): DIRECT | CONVERT | REJECT`
  - `def decide_messages_path(route: RouteCandidate | None, *, stream: bool) -> MessagesGatewayDecision`

- [ ] **Step 1: Write failing unit tests**

```python
def test_decide_convert_when_route_convert():
    assert decide_messages_path(convert_candidate, stream=False) is MessagesGatewayDecision.CONVERT

def test_decide_reject_stream_convert():
    assert decide_messages_path(convert_candidate, stream=True) is MessagesGatewayDecision.REJECT

@pytest.mark.asyncio
async def test_convert_lane_calls_litellm_acompletion_pinned(monkeypatch):
    calls = []
    async def fake_acompletion(**kwargs):
        calls.append(kwargs)
        return {
            "id": "chatcmpl-1",
            "choices": [{"message": {"role": "assistant", "content": "hi"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        }
    monkeypatch.setattr("litellm.acompletion", fake_acompletion)
    # assert NOT using router.acompletion
    out = await handle_messages_convert_lane(
        public_body={"model": "pilot", "messages": [{"role": "user", "content": "x"}], "max_tokens": 8},
        route=convert_route_candidate,
        model_list=[{...}],  # openai/ model + api_base
        request_id="r1",
    )
    assert out["type"] == "message"
    assert calls and calls[0].get("api_base")
    assert calls[0]["metadata"]["protocol"] == "openai_chat"
    assert calls[0]["metadata"]["shared_quota_route_mode"] == "convert"
```

Also assert: same `request_id` → inflight +1 only (mock lease); convert failure → `on_failure` / convert cooldown.

- [ ] **Step 2: Run tests — expect FAIL**

Run: `pytest tests/unit/test_g0a_messages_gateway.py -q`

- [ ] **Step 3: Implement orchestrator**

```python
# g0a_messages_gateway.py (sketch) — [R-G0A-2][R-G0A-3][CODE]
async def handle_messages_convert_lane(*, public_body, route, model_list, request_id, user_api_key_dict=None):
    if public_body.get("stream") is True:
        raise ProtocolAwareRoutingError(..., reason=FEATURE_UNSUPPORTED, protocol=ANTHROPIC_MESSAGES)
    # Lease already acquired by caller with acquire_lease=True OR acquire here exactly once.
    converted = convert_public_request(public_body, direction=(ANTHROPIC_MESSAGES, OPENAI_CHAT))
    chat_body = converted.payload
    entry = find_model_entry(model_list, route.deployment)
    params = entry["litellm_params"]
    meta = {
        "protocol": "openai_chat",  # Chat bucket — NOT litellm_metadata-only
        "shared_quota_route_mode": "convert",
        "shared_quota_conversion": "anthropic_messages>openai_chat",
        "deployment_id": route.deployment.deployment_id,
        "quota_group_id": route.deployment.quota_group_id,
        "provider_id": route.deployment.provider_id,
    }
    try:
        upstream = await litellm.acompletion(  # NOT router.acompletion
            model=params["model"],  # openai/...
            api_base=params.get("api_base"),
            api_key=params.get("api_key"),
            messages=chat_body["messages"],
            max_tokens=chat_body.get("max_tokens"),
            stream=False,
            litellm_call_id=request_id,
            metadata=meta,
            # dual-bucket: also mirror convert markers into litellm_metadata if hooks need them
            litellm_metadata={**meta},
            user_api_key_dict=user_api_key_dict,
        )
    except Exception as exc:
        await get_callback().on_failure(...)  # lease + C3 convert cooldown
        # map via convert_upstream_error / to_public_error
        raise
    payload = upstream if isinstance(upstream, dict) else upstream.model_dump()
    out = convert_upstream_response(payload, direction=(ANTHROPIC_MESSAGES, OPENAI_CHAT)).payload
    await get_callback().on_success(...)
    record_route_selection(...); record_conversion_result(...)
    return out
```

**Forbidden:** `router.acompletion`, `specific_deployment=True` alone, G0-B post_call reshape.

**[REV] Auth / spend:** Pass `user_api_key_dict`; document spend gap + optional `shared_quota_convert_spend_untracked_total`.

- [ ] **Step 3b: Unit test — convert failure writes convert cooldown only; success releases lease; inflight +1**

- [ ] **Step 4: Run tests — expect PASS**

- [ ] **Step 5: Commit** (only if user asked)

```bash
git add plugins/shared_quota_router/g0a_messages_gateway.py tests/unit/test_g0a_messages_gateway.py
git commit -m "feat(g0a): messages convert lane via pinned litellm.acompletion"
```

---

### Task 3: FastAPI `/v1/messages` lazy-aware swap mount

**Files:**
- Create: `plugins/shared_quota_router/g0a_route_mount.py`
- Modify: `plugins/shared_quota_router/bootstrap.py`
- Test: `tests/unit/test_g0a_route_mount.py` (+ lazy-feature simulation)

**Interfaces:**
- Consumes: FastAPI `app`, `_force_load` / lazy warm, `anthropic_response` import, `g0a_messages_gateway`
- Produces: `mount_g0a_messages_gateway(app=None) -> bool`, `is_g0a_messages_mounted() -> bool`, `get_stock_messages_endpoint() -> Callable | None`

- [ ] **Step 1: Write failing tests for lazy-aware mount + mount gate**

```python
def test_mount_after_lazy_warm_replaces_v1_messages():
    # Prefer: attach_lazy_features(app) OR simulate first-request warm, then mount
    # Assert single POST /v1/messages handler identity == g0a wrapper
    # Assert get_stock_messages_endpoint() is anthropic_response (module import OK)
    ...

def test_cold_start_scan_alone_is_insufficient():
    # Document: without warm, mount returns False or warms first — never silent no-op leave conversion-ready
    ...

def test_convert_branch_requires_mount_ok(monkeypatch):
    # flags on but is_g0a_messages_mounted() False → never litellm.acompletion
    ...
```

- [ ] **Step 2: Run — expect FAIL**

- [ ] **Step 3: Implement mount [R-G0A-1]**

Algorithm:

1. If already mounted → return True.  
2. **Warm** lazy feature `anthropic_passthrough` (`await _force_load(app, feat)` or warm endpoint) while event loop running.  
3. `stock_ref = anthropic_response` via **module import** (fallback if route scan fails).  
4. Remove from `app.routes` only routes with path `/v1/messages` and POST (keep `/v1/messages/count_tokens`).  
5. `include_router` project wrapper.  
6. On failure: return False; inc `shared_quota_g0a_mount_failure_total`; leave stock; `g0a_mount_ok=False`.  
7. Convert branch requires `is_conversion_routing_active() and is_g0a_messages_mounted()`.

```python
@router.post("/v1/messages", dependencies=[Depends(user_api_key_auth)])
async def g0a_anthropic_messages(
    fastapi_response: Response,
    request: Request,
    user_api_key_dict: UserAPIKeyAuth = Depends(user_api_key_auth),
):
    body = await _read_request_body(request)
    # Gate order: protocol ctx → public_reachable / assert_endpoint_allowed → params → select
    route = select_route_candidate(..., acquire_lease=False)  # decision only
    decision = decide_messages_path(route, stream=bool(body.get("stream")))
    if decision is DIRECT:
        return await stock_ref(fastapi_response, request, user_api_key_dict)
    if decision is CONVERT:
        # acquire_lease=True once, then handle_messages_convert_lane(..., model_list=...)
        ...
    # REJECT → HTTPException Anthropic-shaped
```

**Direct:** no gateway lease / no mark_tried before stock.  
**Convert:** lease once in convert branch; pinned `litellm.acompletion`.

- [ ] **Step 4: Wire `bootstrap._wait_and_register` — after `register(router)`, await lazy warm + `mount_g0a_messages_gateway()`; log mount bool**

- [ ] **Step 5: Tests PASS + commit** (only if user asked)

```bash
git add plugins/shared_quota_router/g0a_route_mount.py plugins/shared_quota_router/bootstrap.py tests/unit/test_g0a_route_mount.py
git commit -m "feat(g0a): lazy-aware swap of proxy /v1/messages"
```

---

### Task 4: Contract — convert hits `/chat/completions` (flip P4-01)

**Files:**
- Modify: `tests/contract/test_p4_conversion_messages_to_chat_path.py` (**delete/replace** `test_p4_01_g0b_still_misroutes_*`; do not leave conflicting negative+positive tests)
- Test helper: mount FastAPI app via `mount_g0a_messages_gateway` + `httpx.AsyncClient` for full-path POST

**Interfaces:**
- Consumes: mounted gateway + `handle_messages_convert_lane` + `MockHandler`
- Produces: positive P4-01 satisfying remaining-dev-plan §7.1 / Design §6#1

- [ ] **Step 1: Fast path — orchestrator unit/contract via `handle_messages_convert_lane` → `/chat/completions`**

```python
@pytest.mark.asyncio
async def test_p4_01_g0a_convert_lane_hits_chat_completions(mock_base, monkeypatch):
    # ... flags on, convert-only model_list, MockHandler.clear ...
    out = await handle_messages_convert_lane(...)
    assert out["type"] == "message"
    assert out["content"][0]["type"] == "text"
    path = MockHandler.last_requests[-1]["path"]
    assert "/chat/completions" in path
    assert "/responses" not in path
```

- [ ] **Step 2: [REV] Mounted proxy contract — real `POST /v1/messages`**

```python
@pytest.mark.asyncio
async def test_p4_01_g0a_mounted_post_v1_messages_hits_chat_completions(mock_base, monkeypatch):
    # Build FastAPI app, include a stub stock /v1/messages, mount_g0a_messages_gateway(app)
    # Wire llm_router mock/real Router with convert-only deployment api_base=mock_base
    # httpx.AsyncClient(app=app, base_url="http://test") POST /v1/messages with Anthropic body
    # Assert MockHandler last path contains /chat/completions
    # Assert response JSON type == message
```

If full LiteLLM auth cannot run in-process, use test dependency override for `user_api_key_auth` — document in test docstring. **This step is required**, not optional.

- [ ] **Step 3: Confirm pinning already correct from Task 2** — if `/responses` still seen, stop (do not iterate blindly)

- [ ] **Step 4: Commit** (only if user asked)

```bash
git add tests/contract/test_p4_conversion_messages_to_chat_path.py
git commit -m "test(g0a): P4-01 prove convert upstream is /chat/completions via proxy"
```

---

### Task 5: Contract — direct Messages path, errors, R2 gate, C3 cooldown

**Files:**
- Create: `tests/contract/test_g0a_messages_direct_and_errors.py`
- Modify: `plugins/shared_quota_router/g0a_messages_gateway.py` (if gaps)

- [ ] **Step 1: Direct path test**

Use `anthropic/pilot` deployment + Messages public; after mount (or stock delegate), mock must see `/messages` (not `/chat/completions`).

- [ ] **Step 2: Upstream Chat 400 → Anthropic error**

FakeRouter `acompletion` raises exception with `status_code=400` and OpenAI error body; gateway returns Anthropic `type=error`.

- [ ] **Step 3: Flag off → convert-only model rejected at gate (no acompletion)**

- [ ] **Step 3b: [REV] R2 positive — flags ON + conversion-only Messages passes `assert_endpoint_allowed` / `public_reachable`**

- [ ] **Step 3c: [REV] Convert upstream 503 → `is_route_in_cooldown(dep, convert:anthropic_messages>openai_chat)`; direct cooldown clear**

- [ ] **Step 4: Run**

`pytest tests/contract/test_g0a_messages_direct_and_errors.py tests/contract/test_p4_conversion_messages_to_chat_path.py -q`  
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add tests/contract/test_g0a_messages_direct_and_errors.py plugins/shared_quota_router/g0a_messages_gateway.py
git commit -m "test(g0a): direct path, R2 gate pass, convert errors and C3 cooldown"
```

---

### Task 6: Docs, ADR, ops; full suite; staging gate

**Files:**
- Modify: `docs/adr/ADR-conversion-adapter-boundary.md`
- Modify: `docs/operations-protocol-conversion.md`
- Modify: `docs/phase-reports/remaining-dev-plan.md` (Phase 4 unblock / §7.1 satisfied)
- Modify: `docs/phase-reports/conversion-residual-risks.md` (**[REV]** close R1/R3 for G0-A path)
- Optional: `docs/tasks.md` §0 G0-A note

- [ ] **Step 1: Update ADR-conversion status to Implemented (G0-A thin messages gateway)** with link to positive P4-01 tests

- [ ] **Step 1b: [R-G0A-5] Addendum on `ADR-protocol-gateway-integration-boundary.md`:** G0-B remains for Chat / direct Messages; Messages→Chat convert on proxy supersedes to G0-A

- [ ] **Step 2: Ops — mount bool, dual flag, L0–L4 rollback, “mount fail ⇒ never CONVERSION=true”, spend gap**

Include Flag×Mount matrix from ops review (GATEWAY × CONVERSION × mount → path/risk).

- [ ] **Step 2b: [REV] residual-risks — R1/R3 closed by G0-A + mounted P4-01; note mount×flag fatal combo mitigated by enable gate**

- [ ] **Step 2c: Fix ops doc stale line** — conversion-only Messages is allowed when G0-A mount ok + flags on + `public_reachable` (remove “must have direct Messages until R2”)

- [ ] **Step 3: Full suite**

Run: `pytest tests/ -q`  
Expected: all green (record exact passed/skipped counts)

- [ ] **Step 4: Phase 5 canary checklist (operator; do not auto-enable prod)**

```text
[ ] is_g0a_messages_mounted() / startup mount=true
[ ] PROTOCOL_AWARE_GATEWAY_ENABLED=true
[ ] PROTOCOL_CONVERSION_ENABLED=true (staging only)
[ ] one logical model allow_conversion + conversions
[ ] P4-01: POST /v1/messages → upstream /chat/completions (not /responses)
[ ] direct anthropic/ still → /messages
[ ] spend probe: accept MVP gap OR verify spend row
[ ] rollback drill: L1 then L0; Redis untouched; single POST /v1/messages handler
```

- [ ] **Step 5: Commit**

```bash
git add docs/adr/ADR-conversion-adapter-boundary.md docs/operations-protocol-conversion.md docs/phase-reports/remaining-dev-plan.md docs/phase-reports/conversion-residual-risks.md
git commit -m "docs(g0a): mark thin front adapter accepted and operable"
```

---

## Stop / escalation (during implementation)

1. Cannot remove/replace FastAPI `/v1/messages` without upstream edit → fall back to **Option B** internal lane and update design ADR (do not patch LiteLLM).  
2. Lazy warm fails 3× → escalate to middleware-before-`LazyFeatureMiddleware` or Option B (**[R-G0A-6]**); still no upstream business edits.  
3. Convert still hits `/responses` despite pinned `openai/...` + `litellm.acompletion` → stop and re-diagnose entrypoint.  
4. Direct Messages regression / `tried_quota_groups` empties candidates → abort convert mount; restore stock (L0); verify decision select used `acquire_lease=False`.  
5. More than three failed Task 3 mount attempts → stop and redesign mount (middleware vs swap).  
6. **Mount fail while intending CONVERSION=true** → hard stop; do not enable conversion (R1 resurfaces via `public_reachable`).  
7. Double lease / inflight +2 observed → stop until pinned `litellm.acompletion` path is exclusive.  
8. `router.acompletion` still used on convert → treat as plan violation; rewrite to `litellm.acompletion`.

## Self-review

| Spec item | Task |
|-----------|------|
| Convert uses Chat entry (`litellm.acompletion` pinned) | Task 2, 4 |
| Direct keeps Messages (no pre-mark_tried) | Task 1, 3, 5 |
| Lazy-aware mount | Task 3 |
| Single lease | Task 1–3 |
| No upstream business edit | Global + Task 3 mount |
| Stream convert rejected | Task 2 |
| P4-01 positive `/chat/completions` (lane + mounted POST) | Task 4 |
| Error Anthropic shape + C3 cooldown | Task 5 |
| R2 conversion-only gate pass | Task 5 |
| Lease/callback on convert lane | Task 2 |
| Parent ADR addendum | Task 6 |
| residual-risks R1/R3 close | Task 6 |
| Flags / ops / ADR | Task 6 |
| Reverse adapter / streaming | Out of scope |

**Naming:** Design prose may say `convert_request`/`convert_response`; code/plan use `convert_public_request` / `convert_upstream_response` / `convert_upstream_error`.

No TBD placeholders in task steps. Types: `RouteCandidate`, `MessagesGatewayDecision`, `handle_messages_convert_lane` consistent across tasks.

---

## Review intake

| Review | ID | Disposition |
|--------|-----|-------------|
| 规格覆盖 | [G0-A规格覆盖审阅](7bd5b250-681a-4650-8dc6-e37206768869) | Amendments folded (REV) 2026-07-26 |
| 运维安全 | [G0-A运维安全审阅](ac0f0213-2ee3-4671-9855-759ee95b2c05) | Mount×flag fatal, L0, spend, pinning → OPS REV 2026-07-26 |
| 架构可行性 | [G0-A架构可行性审阅](5db40d70-6a74-4256-a993-ca31093c5eb3) | R-G0A-1..6 folded — then **superseded** by native-first FAIL |
| 代码落地 | [G0-A代码落地审阅](74d1f958-efed-4ce2-85c3-38bae7c1466d) | CODE REV folded — then **superseded** by native-first FAIL |
| 用户复核 2026-07-26 | — | **FAIL G0-A coding**; G0-Native Spike first; G0-A = fallback only |

# C2-02 Spike: G0-B request/response conversion on LiteLLM v1.90.5

**Date:** 2026-07-26  
**Verdict:** **GO (conditional)** — unit wiring OK for G0-B non-streaming pilot; **staging flag-on blocked** until live S1/S3 probes pass (see residual risks).

## Evidence

| Step | Finding | Source |
|------|---------|--------|
| Selection sees `request_kwargs` | `SharedQuotaRoutingStrategy.get_available_deployment(..., request_kwargs=)` can mutate kwargs after select | `strategy.py`, extension-points doc |
| Pre-call runs before select | Protocol inject/gates in `async_pre_call_hook`; convert **cannot** know route_mode yet | `callbacks.py` |
| Request convert mount | **After select**, mutate `request_kwargs` (messages/max_tokens; drop `system`) when `route_mode=convert` | C2-04 wiring |
| Response convert mount | `async_post_call_success_hook` **returns** `response`; can replace with Anthropic-shaped dict | CustomLogger contract + callback |
| Streaming | Out of C2 scope; stream hook must not rewrite chunks (existing SSE hazard note) | callbacks NOTE |
| Upstream pin | No `upstream/litellm` edits | ADR G0-B |
| S1 Chat path (live) | **Not proven** — P0 shows Messages + `openai/` may hit `/responses` | Residual R1 |
| S3 client shape (live) | **Not proven** — hook return may be discarded by proxy | Residual R3 |

## Decision

1. **Conditional Go** for lab/unit G0-B text non-streaming `anthropic_messages → openai_chat`.
2. Mount points: strategy kwargs mutation (request) + `async_post_call_success_hook` return (response).
3. **Hard stop for staging/prod `PROTOCOL_CONVERSION_ENABLED=true`** until live probe proves: upstream `/chat/completions` (not `/responses`) **and** client receives Anthropic JSON. Fail → thin **G0-A** + ADR addendum (`docs/phase-reports/conversion-residual-risks.md`).

## Non-goals

- Streaming conversion
- Tools / reasoning / images
- Responses conversion
- Conversion-only Messages public without M3 predicate revision (R2)

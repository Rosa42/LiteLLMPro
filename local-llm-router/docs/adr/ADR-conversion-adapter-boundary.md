# ADR Addendum: Conversion adapter boundary — Native first, G0-A fallback

**Date:** 2026-07-26 (revised)  
**Status:** Accepted (revised)  
**Parent ADR:** `docs/adr/ADR-protocol-gateway-integration-boundary.md` (G0-B)  
**Trigger:** remaining-dev-plan Phase 4 / stop condition §7.1; native-switch falsification of G0-A premise

## Context

C2 assumed G0-B (mutate `request_kwargs` after select + reshape success response) could serve public `anthropic_messages` against an `openai/` Chat deployment.

P0 / P4-01 showed that **with default LiteLLM settings**, Messages + `openai/` hits `/responses` (not `/chat/completions`). Body rewrite alone does not change that path.

**Revision (same day):** LiteLLM v1.90.5 already exposes:

```text
litellm.use_chat_completions_url_for_anthropic_messages
# env: LITELLM_USE_CHAT_COMPLETIONS_URL_FOR_ANTHROPIC_MESSAGES
# yaml: litellm_settings.use_chat_completions_url_for_anthropic_messages
```

When true, `_should_route_to_responses_api` returns False and the stock Messages path uses the native Messages→Chat adapter (`/chat/completions`). Workspace probe confirmed mock path flips from `/responses` to `/chat/completions` with only that switch.

Therefore: **replacing FastAPI `/v1/messages` is not the only way** to fix the convert upstream URL. Thin G0-A route swap remains a **fallback** if native cannot meet shared-quota / pinning / cooldown / gate contracts.

## Decision

1. **Do not enable** staging/prod `PROTOCOL_CONVERSION_ENABLED` for Messages→Chat on G0-B body rewrite alone (still true).  
2. **Prefer G0-Native:** enable LiteLLM `use_chat_completions_url_for_anthropic_messages`, keep project strategy/gates/quota/callback, and **disable** project C2 request rewrite + post_call reshape when native path is active (single transform owner).  
3. **Conversion readiness** must require a proven path: `native_messages_chat_ready` **OR** `g0a_mount_ready` — never conversion-only traffic on stock Responses misroute.  
4. **Escalate to thin G0-A** (route swap / pinned low-level call) **only** if the G0-Native Spike fails shared-quota contracts; before coding G0-A, close P0 TOCTOU, proxy-chain bypass policy, accounting owner, secret resolution, and real lazy-mount tests (see design §4).  
5. **Do not** patch `upstream/litellm` business logic.  
6. Keep dual-flag AND, H1–H7, and C3 isolation.

## Consequences

- Next implementation work is **G0-Native Spike**, not G0-A Tasks 1–6.  
- P4 acceptance splits: **P4-Native** (stock + switch) vs **P4-G0A** (only if fallback).  
- Remaining-dev-plan Phase 5 remains blocked until a proven path (native or unblocked G0-A) + positive path assertion.  
- On LiteLLM pin upgrades, re-run path probes — native switch semantics may drift.  
- Reverse adapter (`openai_chat → anthropic_messages`) remains a separate workstream.

## Non-goals of the native switch

- Not a substitute for project quota / lease / cooldown policy.  
- Not deployment pinning by itself.  
- Not permission to leave G0-B convert mutate enabled alongside native adapter (dual transform risk).

## Links

- Design: `docs/superpowers/specs/2026-07-26-thin-g0a-front-adapter-design.md`  
- Spike plan: `docs/superpowers/plans/2026-07-26-g0-native-messages-chat-spike.md`  
- G0-A plan (blocked): `docs/superpowers/plans/2026-07-26-thin-g0a-front-adapter.md`  
- LiteLLM: `llms/anthropic/experimental_pass_through/messages/handler.py`  
- Evidence (G0-B default path): `tests/contract/test_p4_conversion_messages_to_chat_path.py`

# Conversion epic residual risks (post C-CLOSE review follow-up)

**Date:** 2026-07-26  
**Context:** Four plan reviews completed after C0–C5 implementation. This document records what was fixed immediately vs what remains a hard stop before staging conversion traffic.

**Review agents:**
- [规格覆盖审阅](56641f75-d5b2-43d2-b829-ba073d52c191)
- [运维安全审阅](527dd016-42b1-4716-a9ee-a89ddbbb96d2)
- [架构可行性审阅](034c392c-e8b3-4448-b6c8-15f9b2033178)
- [代码落地可行性审阅](2cee1056-2cb4-4b3e-b921-654daa74da07)

## Fixed in this follow-up

| Finding | Action |
|---------|--------|
| Dual-flag AND not enforced in code | `is_conversion_routing_active()` = gateway ∧ conversion; used by strategy + dispatch |
| Ops matrix doc vs runtime drift | `docs/operations-protocol-conversion.md` updated; matrix test added |
| H1–H7 / R2 (remaining-dev-plan Phase 1–3) | logical_models wiring, lease release, feature extract, dropped fields, error HTTPException, `public_reachable` |
| R1 path probe | Confirmed fail under **default** settings → ADR revised: **G0-Native first**, G0-A fallback |
| Dual-flag + path readiness | `is_conversion_routing_active()` = gateway ∧ conversion ∧ (native ∨ g0a_mount) |
| G0-B rewrite under native | Skipped when `use_chat_completions_url_for_anthropic_messages` |

## Still open — staging/prod conversion hard stops

| ID | Severity | Finding | Required before `PROTOCOL_CONVERSION_ENABLED=true` on staging |
|----|----------|---------|----------------------------------------------------------------|
| R1 | **Fatal → mitigated (spike)** | Default G0-B: Messages + `openai/` → `/responses` (P4-01). **P4-Native** with LiteLLM switch → `/chat/completions` (contract green). | Staging: set `use_chat_completions_url_for_anthropic_messages: true` + dual flags; keep G0-A deferred. Re-probe on LiteLLM pin bumps. |
| R2 | **High** | M3 `assert_endpoint_allowed` still requires `has_verified_upstream(anthropic_messages)`. Conversion-only (Chat upstream only) cannot pass pre-call. | **Fixed in code:** `public_reachable` (path readiness now required). |
| R3 | **High** | Response convert: under G0-Native, LiteLLM native adapter owns Anthropic shape (project G0-B reshape disabled). Live proxy Anthropic body still needs staging canary. | Staging canary / real proxy smoke. |
| R4 | **Med** | Affinity stores `deployment_id` only (no `route_mode`). | Before dual-mode same deployment in prod. |
| R5 | **Med** | Spec §8.6 gaps / `JSON_SCHEMA` vs `STRUCTURED_OUTPUT` / product “real client need” gate for pilot direction. | Plan addendum or next epic kickoff; not required for flag-off production. |
| R6 | **Low** | C4/C5 No-Go criteria soft on buffer latency / Responses checklist sign-off. | Already No-Go; harden when re-opened. |

## Intentionally fail-closed (do not “fix” by loosening)

- Messages/Responses without verified **direct** upstream remain rejected at M3 gate (R2). This blocks conversion-only pilots until a reviewed predicate lands.
- Responses must not become reachable via conversion (C5 No-Go).
- Streaming conversion remains unsupported (C4 No-Go).
- Convert-path infra failures use route-scoped cooldown only (C3); do not write convert state into legacy `is_in_cooldown` alone.

## Rollback reminder (conversion-only configs)

If a logical model ever opts into Messages **only** via conversion:

1. L1: `PROTOCOL_CONVERSION_ENABLED=false` → restart  
2. L2: remove conversion-only protocol from `public_protocols`, set `allow_conversion: false`, re-apply (or restore `litellm.yaml` bak)  
3. Verify Messages → controlled no-route; Chat direct still OK; Redis quota/affinity **not** flushed  

Until R2 lands, L2 is precautionary — gate already blocks conversion-only Messages.

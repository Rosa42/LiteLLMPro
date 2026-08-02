# ADR: Unified Public Responses API

**Date:** 2026-07-26  
**Status:** Accepted (M0)  
**Related:** `docs/superpowers/specs/2026-07-26-unified-public-responses-design.md`  
**Supersedes (partial):** absolute C5 “Responses never via conversion” for **LiteLLM native bridge** paths under Policy A

## Context

Product wants clients to use OpenAI Responses as the unified public style while internal fleets remain Chat (OpenCode/Volc) and, later, Messages (NewAPI). LiteLLM v1.90.5 already bridges Responses → `acompletion` (non-stream + SSE) when `use_chat_completions_api` / Anthropic completion bridge applies. Inventory has **no** shared `model_group` across Chat and Claude — acceptance is many logical models, one public protocol.

## Decision

1. **Default transform owner** for Responses→Chat / Responses→Messages is `litellm_native`. Do not build project request/response/SSE adapters unless native fails contracts.  
2. Enablement uses **`route_candidate_enabled(route)`** per `(source, target, transform_owner)` — never a global OR of path flags.  
3. **Launch Policy A:** a direction may go production when M3-green ∧ fail-closed gates done ∧ **production profile explicit approve**. Direct Responses provider is **optional**. Policy B may attach later **per-model** for compliance only.  
4. Mid-stream: after the first **client-visible** Responses stream event, **no further deployment selection** (mechanism **S1** preferred: real `first_byte_sent` + disable Responses stream retries/fallback for canary).  
5. M1 canary: **one** logical model (`glm-5.2`) only; rollback before any second model.  
6. Messages G0-Native reshape skip must be scoped to Messages public / that owner — not all convert directions.

## Consequences

- Update `public_reachable` / config validation to allow Responses public via Chat (native) under staging/internal/test profiles.  
- Generator emits `use_chat_completions_api: true` for Chat deps serving Responses-public models.  
- C5 evaluation doc becomes Conditional under Policy A.  
- Project adapter owner remains blocked until a future ADR.

## Links

- Plan: `docs/superpowers/plans/2026-07-26-unified-public-responses-m0-m1.md`  
- Design: `docs/superpowers/specs/2026-07-26-unified-public-responses-design.md`  
- LiteLLM: `responses/main.py`, `responses/litellm_completion_transformation/handler.py`

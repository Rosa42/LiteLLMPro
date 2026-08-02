# C5 / Responses Public Enablement Evaluation

**Date:** 2026-07-26 (revised)  
**Verdict:** **Conditional** — absolute No-Go superseded for **LiteLLM native bridge** under **Policy A** (`docs/adr/ADR-unified-public-responses.md`).

## Inventory

| Check | Result |
|-------|--------|
| Enabled plan deployment with `upstream_protocol: openai_responses` | **None** (OpenCode/Volc are `openai_chat`; NewAPI disabled) |
| Native Responses→Chat bridge (pin v1.90.5) | **Available** (`use_chat_completions_api` / completion transformation) |
| Live verified Responses-native provider | **Missing** (optional under Policy A) |
| Shared Chat+Messages `model_group` | **None** — use separate logical models |

## Policy

| Profile | Responses public via Chat/Messages native bridge |
|---------|--------------------------------------------------|
| staging / internal / test | Allowed when flags + `route_candidate_enabled` + logical allowlist |
| production | Policy **A**: only M3-green directions with explicit production approve; direct Responses provider optional |

## Still forbidden (near term)

1. Project-owned Responses↔Chat/Messages adapters / custom SSE (until native fails).  
2. Faking one `model_group` that mixes GLM/Kimi Chat with Claude Messages.  
3. Global “conversion on” without per-direction readiness.

## Next

1. M1: `glm-5.2` canary Responses public → `/chat/completions` via native.  
2. M2: NewAPI probe → separate Claude logical model.  
3. M3: lease/accounting/cancel + mid-stream no-reselect (S1).

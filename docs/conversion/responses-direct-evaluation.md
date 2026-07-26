# C5 Direct Responses Enablement Evaluation

**Date:** 2026-07-26  
**Verdict:** **No-Go** — keep `/v1/responses` controlled disabled for public traffic.

## Inventory

| Check | Result |
|-------|--------|
| Enabled plan deployment with `upstream_protocol: openai_responses` | **None** in current `config/plans.yaml` set (OpenCode/Volc are `openai_chat`; NewAPI disabled/unset) |
| Contract path `/responses` (mock) | Covered in P0 (`test_p0_direct_protocol_paths.py`) as harness only |
| Live verified Responses provider | **Missing** |
| `public_protocols: [openai_responses]` opt-in | Would fail config validation without a Responses upstream |

## Hard gates (unchanged)

1. Require verified direct Responses deployment **before** any public opt-in.
2. **Forbidden in this epic:** `openai_chat ↔ openai_responses` conversion.
3. Validate reasoning / tools / usage / streaming / errors against that provider before enablement.

## Recommendation

Keep M3-03 controlled disable. When a Responses-capable account is available:

1. Add plan `upstream_protocol: openai_responses` + contract probe.
2. Opt-in logical model `public_protocols`.
3. Update `docs/enabling-messages-responses.md` with Go evidence.
4. Only then consider a separate Responses conversion epic.

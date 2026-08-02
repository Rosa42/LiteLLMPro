# Responses M1 — S1 mid-stream spike checklist

**Status:** Stub for implementation  
**Mechanism:** S1 (preferred) — real `first_byte_sent` + disable Responses stream retry/fallback for canary

## Checklist (must be green before stream canary)

1. [ ] First **client-visible** Responses stream event sets `first_byte_sent` via real callback/stream path (not only manual test poke).  
2. [ ] `get_available_deployment` hard-refuses when `first_byte_sent`.  
3. [ ] Canary Responses stream calls: `num_retries=0` and/or empty fallbacks / `max_fallbacks=0` so Router `_aresponses_streaming_iterator` cannot start a second deployment.  
4. [ ] Contract: deployment-select counter ≥ 2 after first visible event ⇒ **FAIL**.

## If S1 blocked

- Amend plan to **S2** (thin stream wrapper) or temporary **S3** (non-stream M1 only).  
- Do **not** ship stream canary silently.

## Notes

Fill evidence paths / PR links here during M1-S work.

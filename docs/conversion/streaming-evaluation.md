# C4 Streaming Conversion Evaluation

**Date:** 2026-07-26  
**Verdict:** **No-Go** — keep `Feature.STREAMING = unsupported` / `streaming: false` on all conversion capabilities.

## Checklist

| # | Invariant | Result |
|---|-----------|--------|
| 1 | First **converted** visible event = first-byte boundary | Not proven (no stream adapter) |
| 2 | Lease held across adapter buffering | Not proven |
| 3 | Configurable max buffer latency | Not defined |
| 4 | Event order preserved | N/A |
| 5 | Backpressure / cancellation | N/A |
| 6 | Tool deltas | Out of pilot scope |
| 7 | Usage + mid-stream failure shaping | N/A |
| 8 | Never splice second upstream after visible output | Existing direct invariant; not re-proven for convert |

## Evidence

- Matrix reject: `tests/unit/test_c4_streaming_conversion_eval.py`
- SSE hazard note remains in `callbacks.py` (do not rewrite stream chunks casually)
- C2 spike explicitly excluded streaming

## Next

Re-open only with a dedicated streaming adapter spike (likely thin G0-A stream adapter) and red→green contract tests for all eight invariants.

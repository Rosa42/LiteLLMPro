# P0 Probe B remount S1 — post-select `request_kwargs`

**Date:** 2026-08-24  
**Goal:** After Probe B FAIL (pre-call `data["messages"]`), inject the same env marker in `get_available_deployment` after account select, on `request_kwargs["messages"]` and the named `messages` list.

## Code

- `feature_flags.py`: `inject_p0_probe_b_marker` / `inject_p0_probe_b_marker_on_select` (env-gated, default off)
- `strategy.py`: call after convert block, before `_write_route_meta`
- Pre-call inject remains (already proven not to reach MiniMax on this path). S1 PASS means the **strategy** copy is what LiteLLM sends.

## Unit tests

**23 passed** (`test_p0_probe_b_marker.py` + `test_c2_messages_to_chat_pilot.py`).

## Live `POST http://127.0.0.1:4000/v1/messages`

Docker Desktop started. Image rebuilt (`COPY plugins/shared_quota_router`). Container had `HAS_S1 True` and `MARKER SET` during the probe.

| Field | Value |
| --- | --- |
| SUMMARY | **SUMMARY PROBE_B=PASS** (S1 remount) |
| HTTP status | **200** |
| Model | `MiniMax-M3` |
| Client JSON contained marker | **false** |
| Marker prefix | `P0B_` |

Assistant text quoted the injected token (redacted in logs as `***`) and also said it would reply `pong`. Original Probe B was exactly `pong` with **no** token in the body. Client never sent the token, so MiniMax could only quote it if post-select `request_kwargs` mutation reached the official Messages HTTP.

S2 (mock as proxy upstream) later run as a second evidence layer: [`p0-probe-b-s2.md`](./p0-probe-b-s2.md).

## Cleanup

- `P0_PROBE_B_MARKER` / `P0_PROBE_B_MODEL` removed from `.env`
- Container recreated; **MARKER EMPTY**
- Proxy `/health/liveliness` **200**
- Inject code stays, default off

## Next (not done)

S5: IMAGE gate exemption / delay for composed models, only after this hang-point is accepted for vision peel. Do not treat S1 PASS as vision compose implemented.

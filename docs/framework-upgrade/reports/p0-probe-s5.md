# P0 remount S5 — composed IMAGE defer + post-select peel

**Date:** 2026-08-24  
**Goal:** After S1/S2 proved `request_kwargs` mutation reaches outbound HTTP, let a composed facade past the pre-select IMAGE gate, peel images after account select, and keep plain `glm-5.2` closed.

This is **not** MiniMax translation / `visual-evidence` IR. Stub peel only (env `S5_STUB_PEEL`).

## Code

- `composed_vision.py`: `S5_COMPOSED_MODELS` (default empty), IMAGE stripped from capability checks, peel after select
- `protocol_gates.py` / `strategy.py`: use capability features for composed models
- `protocol_context.py`: recurse `tool_result` for nested images
- `mock_provider.py`: `has_image` boolean on `/probe/last` (no bytes stored)
- Production `plans.yaml` / `litellm.yaml`: **no** `glm-5.2-vision`

## Unit tests

**61 passed** (`test_s5_composed_image_gate.py` + Probe B marker + M3 gates + mock e2e + M2 routing).

## Live Docker `POST http://127.0.0.1:4000/v1/messages`

Plugin rebuilt into the image. Probe window only: `glm-5.2-vision` cloned from `glm-5.2` with `api_base=http://mock-s2:18080`. Restored afterward (`yaml_has_vision=False`).

| Leg | Model | Client has image | Client has marker | Result |
| --- | --- | --- | --- | --- |
| Control | `glm-5.2` | true | false | HTTP **400**, required features include `image`. **SUMMARY PROBE_S5=PASS** (`--expect reject`) |
| Treatment | `glm-5.2-vision` | true | false | HTTP **200**, mock path `/v1/messages`, **`has_image=false`**, **`probe_marker_hit=true`**. **SUMMARY PROBE_S5=PASS** (`--expect peeled`) |

Control was re-run on the restored proxy (no mock, no marker). Treatment used in-network mock; assistant text was stock `hello from mock messages` (boolean hit, not model echo).

## Cleanup

- `P0_PROBE_B_MARKER` / `P0_PROBE_B_MODEL` removed
- `S5_COMPOSED_MODELS` / `S5_STUB_PEEL` not left on the running container (`AFTER_S5 EMPTY`)
- `mock-s2` removed; `:18080` down
- Proxy `/health/liveliness` **200**

## Next (not done)

Real vision compose: MiniMax translation to typed IR, SHA-256 cache, advertise `glm-5.2-vision` in plans. Hang-point + IMAGE timing are closed; do not treat stub peel as the recipe.

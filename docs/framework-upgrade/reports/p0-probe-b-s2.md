# P0 Probe B remount S2 — mock as proxy upstream

**Date:** 2026-08-24  
**Goal:** Complete the evidence chain after S1 live echo. Do not trust model paraphrasing. Prove the **outbound HTTP body** LiteLLM sends on `POST /v1/messages` does (treatment) or does not (control) contain the env marker.

Hard evidence is mock `GET /probe/last` → `probe_marker_hit` (boolean only; no prompt, no key, no marker string).

## Setup

- Live proxy: Docker `local-llm-router-litellm-1` (`POST http://127.0.0.1:4000/v1/messages`)
- Overlay: `deploy/docker-compose.s2-probe.yaml` (sidecar `mock-s2` on `127.0.0.1:18080`, LiteLLM `MINIMAX_ANTHROPIC_BASE_URL=http://mock-s2:18080`)
- Control overlay: `deploy/docker-compose.s2-control.yaml` (`P0_PROBE_B_MARKER=""` on LiteLLM only)
- Script: `scripts/p0_probe_b_s2_mock_upstream.py`
- Model: `MiniMax-M3`
- Client JSON: `Reply with exactly: pong` — **never** contained the marker
- Marker prefix: `P0B_` (full token not recorded)
- Production MiniMax `api_base` was **not** permanently changed. Window-only retarget; restored after.

## Host tests (before live)

```text
cd E:\LiteLLMPro\local-llm-router
$env:PYTHONPATH="plugins"
F:\anaconda\envs\py312\python.exe -m pytest tests/e2e/test_mock_provider_http.py tests/unit/test_p0_probe_b_marker.py -q --tb=short
```

**14 passed** (includes new `GET /probe/last` + `/probe/reset`).

## Evidence chain

| Leg | LiteLLM inject | MiniMax `api_base` | Client body has marker | Mock path | `probe_marker_hit` | SUMMARY |
| --- | --- | --- | --- | --- | --- | --- |
| Control | **EMPTY** | `mock-s2` | false | `/v1/messages` | **false** | **SUMMARY PROBE_B_S2=PASS** (`--expect miss`) |
| Treatment | **SET** (S1 post-select) | `mock-s2` | false | `/v1/messages` | **true** | **SUMMARY PROBE_B_S2=PASS** (`--expect hit`) |

Both legs: proxy HTTP **200**; mock assistant text was the stock `hello from mock messages` (mock does not echo the token). Classification is the boolean hit, not assistant text.

Control proves: client did not send the token, and mock does not false-positive.  
Treatment proves: with S1 inject on, the token appears in the **outbound** Anthropic Messages JSON. Combined with S1 live MiniMax echo, the hang-point is `request_kwargs` after select.

## Cleanup

- `P0_PROBE_B_MARKER` / `P0_PROBE_B_MODEL` removed from `.env` (`dotenv_has_probe_keys=False`)
- Overlay dropped; LiteLLM recreated; `AFTER_LITELLM_MARKER EMPTY`; `AFTER_MINIMAX_IS_MOCK False`
- `mock-s2` removed; host `:18080` down
- Proxy `/health/liveliness` **200**

## Next (not done)

S5: IMAGE gate exemption / delay for composed models. Do not treat S2 PASS as vision compose implemented.

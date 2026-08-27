# Vision Agent Presets Implementation Plan

> **For agentic workers:** A/B are implemented. C fixtures exist from OpenCode protocol/source. Do not enable production fingerprints until a live capture through this gateway. Do not commit unless the user asks.

**Goal:** Close vision invariants (no pixels on the execute model; cache key and MiniMax POST share one guide), then add generic + header/UA OpenCode presets.

**Architecture:** G0-B hang-point unchanged. Sync select uses `contextvars` so async production is not fail-closed. Vision stage snapshots images, extracts per-image guides, calls `translator(png, guide)`, caches `sha256(png‖agent_id‖prompt_rev‖guide)`. Presets live under `vision_agents/`. Fingerprints stay off until C.

**Tech Stack:** Python plugin `shared_quota_router`, pytest, LiteLLM v1.90.5 (untouched).

**Spec:** `docs/framework-upgrade/vision-agent-prompt-presets.md` v2.

**Python:** `F:\anaconda\envs\py312\python.exe` from `local-llm-router` with `PYTHONPATH=plugins`.

---

## Files

| Path | Phase | Role |
| --- | --- | --- |
| `plugins/shared_quota_router/vision_async_flag.py` | A | `ContextVar` `sq_vision_async_select` |
| `plugins/shared_quota_router/composed_vision.py` | A | peel: async defer vs public sync `vision=sync_path` |
| `plugins/shared_quota_router/strategy.py` | A | async entry set/reset flag |
| `plugins/shared_quota_router/vision_cache.py` | A | `SCHEMA_VER = 3` |
| `plugins/shared_quota_router/vision_compose.py` | A | `ImageRef`, snapshot, `translator(png, guide)`, digest |
| `plugins/shared_quota_router/vision_agents/*` | B | types, generic, opencode, detect, registry |
| `tests/unit/test_s5_composed_image_gate.py` | A | sync fail-closed + async defer |
| `tests/unit/test_vision_compose.py` | A/B | translator arity, per-image guide, cache isolation |
| `tests/unit/test_vision_agents.py` | B | detection + fallback |

C: `tests/fixtures/vision_agents/opencode/` — **do not create fake production fingerprints**.

---

## Phase A — invariants (implement now)

### A1. Public sync fail-closed; async defers peel

- [x] Failing tests then implementation.
- [x] `vision_async_flag.py` + strategy set/reset + peel branch.

### A2. Snapshot + `translator(png, guide)` (no POST re-extract)

- [x] Per-image snapshot; MiniMax uses passed guide.

### A3. Cache digest v3

- [x] `agent_id` + `prompt_rev`; `SCHEMA_VER = 3`.

### A4. Regression

- [x] unit + contract green.

## Phase B — presets (after A)

### B1–B5

- [x] `vision_agents/` generic + OpenCode header/UA; addendum cap; extract fallback; headers through strategy; tests.


---

## Phase C — live capture, fingerprints on

- [x] Redacted live dumps (`live.json`, `live-2.json`) through this gateway.
- [x] UA on this hop is `opencode/<ver> …`.
- [x] `match_messages` requires Read-tool wrapper + success line + image.
- [x] Extract skips wrapper chrome; `prompt_rev = 2`.
- [x] `VISION_AGENT_FINGERPRINTS` default on (kill-switch `false`).

---

## Anti-patterns (do not)

- Unconditional “has image → 400” inside `get_available_deployment` (kills async).
- `S5_STUB_PEEL` as translation.
- Re-extract guide from mutated `env.messages`.
- Fingerprint from “I use OpenCode”.
- Edit `upstream/litellm`.
- Commit/push unless the user asks.

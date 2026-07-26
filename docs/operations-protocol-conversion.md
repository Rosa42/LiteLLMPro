# Protocol conversion operations (C2+)

## Feature flags (dual matrix — code-enforced AND)

Runtime selection / dispatch uses `is_conversion_routing_active()` =

`PROTOCOL_AWARE_GATEWAY_ENABLED ∧ PROTOCOL_CONVERSION_ENABLED`

Raw env bits remain visible in `flag_snapshot()`; only the AND unlocks convert.

| `PROTOCOL_AWARE_GATEWAY_ENABLED` | `PROTOCOL_CONVERSION_ENABLED` | Behavior |
|----------------------------------|-------------------------------|----------|
| false | false | Legacy Chat selection; Messages/Responses gated; **no convert** |
| true | false | MVP protocol-aware direct routes only; **no convert** (default) |
| false | true | **Misconfig:** convert still **off** (AND fails); Messages gates unchanged |
| true | true | Direct preferred; explicit convert candidates allowed when configured |

Defaults: gateway may be `true` in MVP ops; **conversion always defaults `false`**.

**Staging/prod `CONVERSION=true` is blocked** until a **proven path** clears residual R1–R3:

1. **Preferred — G0-Native:** `litellm_settings.use_chat_completions_url_for_anthropic_messages: true` (or env `LITELLM_USE_CHAT_COMPLETIONS_URL_FOR_ANTHROPIC_MESSAGES=true`) + project gates; disable project G0-B double rewrite; P4-Native green.  
2. **Fallback — G0-A:** only if Native Spike fails; mount-ready AND conversion flag; see design §4.

Path readiness must be part of conversion activation (native ready **or** `g0a_mount_ready`) — flag alone is unsafe under default Responses routing.

```powershell
# Keep conversion off (safe)
$env:PROTOCOL_CONVERSION_ENABLED = "false"

# Staging only after residual-risk clear (R1–R3) + proven path
$env:PROTOCOL_AWARE_GATEWAY_ENABLED = "true"
$env:PROTOCOL_CONVERSION_ENABLED = "true"
$env:LITELLM_USE_CHAT_COMPLETIONS_URL_FOR_ANTHROPIC_MESSAGES = "true"  # G0-Native
# restart proxy/worker
```

Redis quota / affinity / lease keys are **never** flushed when toggling these flags.

## Rollback

| Level | Steps |
|-------|--------|
| **L0** | If G0-A was mounted: restore stock `POST /v1/messages` (unmount / redeploy). Flag-off alone does **not** undo route swap. Native-only deploys: L0 N/A. |
| **Native off** | Clear `use_chat_completions_url_for_anthropic_messages` / env + restart (openai/ Messages may return to `/responses`). |
| L1 | `PROTOCOL_CONVERSION_ENABLED=false` → restart. Direct traffic unchanged; wrapper may remain if G0-A mounted. |
| L1b | Keep `PROTOCOL_AWARE_GATEWAY_ENABLED=true` unless whole gateway must roll back. |
| L2 | If conversion-only public was applied: remove that protocol from `public_protocols`, set `allow_conversion: false`, strip deployment `conversions`, re-apply (or restore `config/backups/litellm.yaml.*.bak`). |
| L3 | `PROTOCOL_AWARE_GATEWAY_ENABLED=false` → legacy Chat; Messages still gated. |
| L4 | Roll back plugin build + L0 if applicable. |

**After L0/L1/L2 verify:** convert candidates = 0; conversion counters stop; Chat direct OK; **single** POST `/v1/messages` handler; Redis `sq:quota:*` untouched.

## Metrics

| Counter | When |
|---------|------|
| `shared_quota_protocol_conversion_total` | Convert attempt result (`result=success\|failure`) |
| `shared_quota_protocol_conversion_failure_total` | Convert failures only |
| `shared_quota_protocol_route_total` | Includes `route_mode=convert` when selected |

Labels: `direction` (registry keys only), `result`, `reason` (enum / routing reason codes) — **no** prompts, bodies, field values, or secrets.
`dropped_fields` / warnings must stay path-or-code only.

## Pilot direction

Default candidate: public `anthropic_messages` → upstream `openai_chat` (text, non-streaming only).

**Conversion-only Messages** (no direct Anthropic upstream) is allowed only when **all** hold:

1. G0-A `/v1/messages` mount succeeded (`is_g0a_messages_mounted()`),
2. `PROTOCOL_AWARE_GATEWAY_ENABLED` ∧ `PROTOCOL_CONVERSION_ENABLED`,
3. logical `allow_conversion` + matching `conversions` + registered adapter (`public_reachable`).

Without (1), do **not** set `PROTOCOL_CONVERSION_ENABLED=true` — gate may allow traffic that still hits stock G0-B `/responses` misroute.

See thin G0-A design/plan: `docs/superpowers/specs/2026-07-26-thin-g0a-front-adapter-design.md`.

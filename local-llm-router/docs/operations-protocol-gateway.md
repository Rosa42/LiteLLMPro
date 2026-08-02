# Protocol-aware gateway operations (M4)

## Feature flag (primary rollback)

| Env | Default | Effect |
|-----|---------|--------|
| `PROTOCOL_AWARE_GATEWAY_ENABLED` | `false` | `true`: M2/M3 capability + public opt-in gates. `false`: legacy Chat selection; `/v1/messages` and `/v1/responses` remain controlled disabled unless verified + opted-in. |

Redis quota / affinity / lease keys are **never** flushed when toggling this flag.

```powershell
# Enable MVP protocol-aware path
$env:PROTOCOL_AWARE_GATEWAY_ENABLED = "true"

# Rollback to legacy Chat selection (instant; no Redis wipe)
$env:PROTOCOL_AWARE_GATEWAY_ENABLED = "false"
# then restart proxy / worker so processes re-read env
```

## Catastrophic config fallback

Generator writes timestamped backups under `config/backups/litellm.yaml.<UTC>.bak`
before replacing `config/litellm.yaml`. Invalid generation leaves the previous file
untouched.

```powershell
Copy-Item config\backups\litellm.yaml.<stamp>.bak config\litellm.yaml -Force
.\scripts\llm-router.ps1 restart
```

## Metrics (safe labels)

Counters (process-local unless exported elsewhere):

- `shared_quota_protocol_route_total` — selection result
- `shared_quota_protocol_reject_total` — pre-call / strategy protocol rejection
- `shared_quota_protocol_no_route_total` — legacy alias for rejects

Labels may include `public_protocol`, `upstream_protocol`, `route_mode`, `result`,
`reason`, and optionally hashed `model_group` / `deployment_id` / `quota_group_id`.

| Env | Purpose |
|-----|---------|
| `SHARED_QUOTA_METRICS_LABEL_SALT` | Hash operational labels |
| `SHARED_QUOTA_METRICS_RAW_LABELS=true` | Local-only raw labels (no salt) |

Never log API keys, Authorization, prompts, or full responses.

Conversion metrics names are reserved but **dormant** in MVP (always zero).

## Enable Messages / Responses later

See `docs/enabling-messages-responses.md`.

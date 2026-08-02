# Protocol Responses operations (Policy A / M1 canary)

## Launch Policy A (locked)

A direction may go production when:

1. M3 contracts green for that `(source, target, owner)`
2. Fail-closed gates complete
3. `SHARED_QUOTA_ENV_PROFILE=production` **and** direction listed in  
   `SHARED_QUOTA_PRODUCTION_APPROVED_DIRECTIONS`  
   (example: `openai_responses>openai_chat:litellm_native`)

Direct Responses provider is **optional**.

## Staging canary (glm-5.2 only)

```powershell
$env:SHARED_QUOTA_ENV_PROFILE = "staging"
$env:PROTOCOL_AWARE_GATEWAY_ENABLED = "true"
$env:PROTOCOL_CONVERSION_ENABLED = "true"
# regenerate litellm.yaml so Chat deps get use_chat_completions_api: true
# restart proxy
```

Canary model: **`glm-5.2` only** until rollback drill recorded. Do not enable a second model.

## M1 rollback drill

```text
[ ] Remove openai_responses from glm-5.2 public_protocols (and/or allow_conversion false)
[ ] Or set PROTOCOL_CONVERSION_ENABLED=false / profile production without approve
[ ] Restart
[ ] POST /v1/responses model=glm-5.2 → controlled reject / no-route
[ ] Chat public for other models unaffected
[ ] Redis sq:quota:* untouched
[ ] Record drill date before enabling any second Responses-public model
```

## Mid-stream (S1)

See `docs/phase-reports/responses-m1-s1-spike-notes.md`. Stream canary blocked until checklist green.

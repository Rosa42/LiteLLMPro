# Model Capability Discovery (M1-05)

**Status:** **DONE** (2026-07-26)  
**Task:** `docs/tasks.md` → M1-05  
**E2E:** `docs/phase-reports/e2e-verification-m1.md`

## Why a project-owned endpoint?

LiteLLM **v1.90.5** `GET /v1/models` builds OpenAI-compatible objects via
`create_model_info_response` and **does not** include custom
`model_info.public_protocols` (only `id` / `object` / `owned_by`, plus optional
fallback metadata). Relying on stock `/v1/models` alone cannot satisfy protocol
opt-in discovery without modifying upstream.

Per G0-B and tasks M1-05, this project exposes a **project-owned** catalog
instead of encoding protocols into model names.

## Endpoints

| Method | Path | Auth |
|--------|------|------|
| GET | `/v1/router/model-capabilities` | Proxy API key (same as other routes) |
| GET | `/shared-quota/v1/model-capabilities` | Alias |

Registered at proxy startup via
`shared_quota_router.bootstrap:register_proxy_startup` →
`discovery_routes.mount_discovery_routes()`.

### Query parameters

| Param | Values | Meaning |
|-------|--------|---------|
| `style` | `openai` (default) | `data[].metadata.public_protocols` |
| `style` | `capability` | `data[].public_protocols` top-level |

### Example

```bash
curl -sS http://127.0.0.1:4000/v1/router/model-capabilities \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY"
```

```json
{
  "object": "list",
  "source": "shared_quota_router.discovery",
  "disclaimer": "Presence in this listing indicates public protocol opt-in only. ...",
  "data": [
    {
      "id": "kimi-k3",
      "object": "model",
      "metadata": {
        "public_protocols": ["openai_chat"]
      }
    }
  ]
}
```

## Rules

1. **One entry per logical model** (`model_name`), even if multiple deployments exist.
2. **Only explicit opt-in** from generated `model_info.public_protocols` (from `plans.yaml` → `logical_models`).
3. Models with **no** `public_protocols` are **omitted** (unavailable on every public endpoint).
4. Unknown protocol strings are dropped; they never appear as supported.
5. Listing **never** implies all of `openai_chat` / `openai_responses` / `anthropic_messages`.
6. **Presence ≠ routability**: exhausted quotas, cooldowns, or missing deployments can still fail at request time.

## Relationship to `/v1/models`

| Surface | What clients see |
|---------|------------------|
| `GET /v1/models` | Standard OpenAI id list (LiteLLM stock). **No** protocol metadata in v1.90.5. |
| `GET /v1/router/model-capabilities` | Protocol opt-in discovery for this gateway. |
| Generated `config/litellm.yaml` | Per-deployment `model_info.public_protocols` for internal routing (M1-04). |

Do **not** encode protocol into model names (e.g. `kimi-k3-messages`).

## Implementation

| Module | Role |
|--------|------|
| `plugins/shared_quota_router/discovery.py` | Pure aggregation / response builders |
| `plugins/shared_quota_router/discovery_routes.py` | FastAPI routes + mount |
| `plugins/shared_quota_router/bootstrap.py` | Mount on proxy startup |

## Tests

```bash
set PYTHONPATH=plugins
pytest tests/unit/test_discovery.py -q
```

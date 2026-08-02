# E2E Verification Report — M1 (Protocol Config + Discovery)

**Date:** 2026-07-26  
**Proxy:** `http://127.0.0.1:4000` (restarted to load M1-05 routes)  
**LiteLLM pin:** v1.90.5  

## Commands

```text
# Offline tests
set PYTHONPATH=plugins
pytest tests/ -q
→ 113 passed

# Config
python -m shared_quota_router.cli_config validate --plans config/plans.yaml
python -m shared_quota_router.cli_config apply --plans config/plans.yaml --output config/litellm.yaml
→ OK: 3 plans, 30 deployments, 27 logical models with public_protocols

# Live (after .\scripts\llm-router.ps1 restart)
python scripts/e2e_verify_m1.py
→ PASS=15 FAIL=0
```

## Live matrix

| Check | Result | Notes |
|-------|--------|-------|
| `GET /health/liveliness` | **PASS** | `"I'm alive!"` |
| `GET /v1/models` | **PASS** | 29 models; includes `kimi-k3` |
| Stock `/v1/models` has `public_protocols` | **PASS (absent)** | Confirms v1.90.5 limitation → project endpoint required |
| `GET /v1/router/model-capabilities` | **PASS** | 27 models; `source=shared_quota_router.discovery` |
| Chat-only protocols | **PASS** | All `["openai_chat"]`; 0 Claude rows |
| Capabilities alias | **PASS** | `/shared-quota/v1/model-capabilities` 200 |
| Disclaimer | **PASS** | Present on capabilities payload |
| `POST /v1/chat/completions` kimi-k3 | **PASS** | 200, tokens>0, output present |
| `POST /v1/chat/completions` glm-5.2 | **PASS** | 200, tokens>0, output present |
| Generated `litellm.yaml` protocol fields | **PASS** | `upstream_protocol` + `public_protocols` |
| No secrets in generated yaml | **PASS** | env refs only |
| `POST /v1/responses` kimi-k3 | **Observed 400** | M3 Responses gate not yet implemented; path still open in LiteLLM |

## Scope note

This verifies **completed M1 work** (config schema, generator, discovery) and **Chat smoke** against real upstreams.

**Not verified (not implemented yet):**

- M2 protocol filtering before lease  
- M2 dual-bucket protocol injection on every request  
- M3 Messages/Responses controlled disable/enable  
- M4 feature flag / protocol metrics  

## How to re-run

```powershell
cd E:\LiteLLMPro\local-llm-router
.\scripts\llm-router.ps1 restart   # if discovery routes 404
$env:PYTHONPATH="plugins"
python -m pytest tests/ -q
python scripts/e2e_verify_m1.py
```

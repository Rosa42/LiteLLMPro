# Protocol Gateway MVP Report

**Date:** 2026-07-26  
**Project:** `E:\LiteLLMPro\local-llm-router`  
**LiteLLM pin:** `v1.90.5`  
**Scope:** Direct-protocol MVP (P0 → MVP-GATE). Cross-protocol conversion (C1–C5) is out of scope.

## 1. Implementation summary

| Wave | Result |
|------|--------|
| P0 + G0 | Compatibility contracts + **G0-B** metadata integration ADR |
| M1 | Protocol/feature domain, plans schema, generator, discovery API |
| M2 | Dual-bucket protocol context; pre-lease capability filter; affinity; protocol errors |
| M3 | Chat opt-in + `openai/` prefix; Messages/Responses controlled disable; `drop_params: false` |
| M4 | Protocol metrics/observability; `PROTOCOL_AWARE_GATEWAY_ENABLED`; ops docs; this report |

**Default runtime:** set `PROTOCOL_AWARE_GATEWAY_ENABLED=true` for the MVP path (env default remains `false` for safe rollback).

## 2. Changed / added files (M4 focus + protocol gateway chain)

### New
- `plugins/shared_quota_router/feature_flags.py`
- `plugins/shared_quota_router/protocol_observability.py`
- `plugins/shared_quota_router/protocol_context.py`
- `plugins/shared_quota_router/protocol_errors.py`
- `plugins/shared_quota_router/protocol_gates.py`
- `tests/unit/test_m2_protocol_routing.py`
- `tests/unit/test_m3_endpoint_gates.py`
- `tests/unit/test_m4_ops.py`
- `docs/operations-protocol-gateway.md`
- `docs/enabling-messages-responses.md`
- `docs/phase-reports/protocol-gateway-mvp.md` (this file)

### Updated (representative)
- `strategy.py`, `callbacks.py`, `bootstrap.py`, `registry.py`, `models.py`, `metrics.py`, `generator.py`
- `config/litellm.yaml` (`drop_params: false`)
- `.env.example` (gateway flag + metrics salt)
- `docs/tasks.md` §0 board + MVP-GATE checklist

## 3. Commands and results

```text
set PYTHONPATH=plugins

pytest tests/ -q
→ 160 passed, 4 warnings (LiteLLM logging_worker RuntimeWarning; pre-existing)

python -m shared_quota_router.cli_config validate --plans config/plans.yaml
→ OK: 3 plan(s), 27 logical model(s) with public_protocols
  - opencode-a / volc-c: openai_chat enabled
  - newapi-a: protocol unset, enabled=False

ruff check (M4-touched modules after --fix)
→ clean on feature_flags / protocol_observability / protocol_gates / test_m3 / test_m4

mypy plugins/shared_quota_router (full package)
→ blocked by environment: numpy stub syntax (Py3.11) + missing types-PyYAML
  (not introduced by M4; tracked as tooling risk)

Secret scan (plugin sources via log_text_has_secrets heuristic)
→ secretish_files 0 / OK
```

Live Chat smoke against real upstreams was previously recorded in
`docs/phase-reports/e2e-verification-m1.md` (kimi-k3 / glm-5.2 **PASS**).
Re-run when the proxy is up:

```powershell
$env:PROTOCOL_AWARE_GATEWAY_ENABLED = "true"
.\scripts\llm-router.ps1 restart
python scripts/e2e_verify_m1.py
```

Conditional Messages smoke: **N/A** — no verified Messages provider; endpoint remains controlled disabled (unit-covered).

## 4. Provider capabilities actually verified

| Provider | Protocol | Status |
|----------|----------|--------|
| OpenCode Go | `openai_chat` | Verified (config + prior live Chat E2E) |
| Volc Coding Plan | `openai_chat` | Verified (config + prior live Chat E2E) |
| NewAPI | unset | Disabled / unverified |
| Any Anthropic Messages upstream | — | Not verified → `/v1/messages` gated |
| Any OpenAI Responses upstream | — | Not verified → `/v1/responses` gated |

## 5. Disabled endpoints and reasons

| Endpoint | Behavior | Reason |
|----------|----------|--------|
| `/v1/chat/completions` | Enabled for opted-in Chat models | MVP direct path |
| `/v1/messages` | Controlled `protocol_not_enabled` / unsupported | No verified `anthropic_messages` deployment + opt-in |
| `/v1/responses` | Controlled `protocol_not_enabled` | No verified Responses deployment; no Chat bridge |

## 6. Rollback instructions

1. **Primary:** `PROTOCOL_AWARE_GATEWAY_ENABLED=false` → restart proxy/worker. Redis quota state preserved. Messages/Responses stay gated.
2. **Config:** restore `config/backups/litellm.yaml.<UTC>.bak` over `config/litellm.yaml`, then restart.
3. See `docs/operations-protocol-gateway.md`.

## 7. Unresolved risks

- Full-package `mypy` fails on this Windows venv (numpy stubs / missing `types-PyYAML`).
- Repo-wide `ruff` still reports historical issues outside M4-touched files.
- Live proxy gate for Messages/Responses depends on CustomLogger `async_pre_call_hook` registration; unit tests cover the gate logic.
- Feature flag default is `false`; operators must set `true` for MVP protocol-aware behavior (documented in `.env.example`).

## 8. Post-MVP recommendation

Proceed to **C1–C5** only after operators confirm flag-on production Chat behavior. First conversion pilot should be a single non-streaming text direction with isolated circuit metrics (currently dormant).

## 9. MVP-GATE status

All checklist items in `docs/tasks.md` §8 are marked complete with evidence above and prior phase reports.

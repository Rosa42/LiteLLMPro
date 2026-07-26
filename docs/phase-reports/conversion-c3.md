# C3 Conversion Circuit Isolation

**Date:** 2026-07-26  
**Status:** DONE

## Behavior

- Route-scoped Redis keys: `sq:cooldown:dep:{deployment_id}:{route_key}` where `route_key` is `direct` or `convert:{source}>{target}`.
- `filter_candidates` checks the route key for the selected `RouteMode`; legacy deployment cooldown applies to **direct only**.
- Convert-path infra failures (`PROVIDER_OUTAGE`, 5xx, short rate, network, …) write **convert route cooldown only** — they do **not** open provider or deployment circuits.
- Shared account/quota scopes (`SHARED_QUOTA_EXHAUSTED`, `AUTH_INVALID`, `ACCOUNT_DISABLED`) still apply on convert (shared fate).
- Deterministic `ProtocolAwareRoutingError` (mapping / feature unsupported) skips circuit updates and is not retried.

## Evidence

- `tests/unit/test_c3_conversion_circuit_isolation.py`
- Implementation: `state_store.py` (`put_route_cooldown` / `is_route_in_cooldown`), `strategy.filter_candidates`, `callbacks._apply_classification`

## Staging note

`PROTOCOL_CONVERSION_ENABLED=true` on staging/prod is unblocked from a circuit-isolation perspective after this task; keep flag default **false** until operators intentionally enable.

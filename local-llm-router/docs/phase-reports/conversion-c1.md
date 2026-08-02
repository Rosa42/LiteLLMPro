# Conversion C1 Completion

**Date:** 2026-07-26  
**Tasks:** C1-01 … C1-04  
**Plan:** `docs/superpowers/plans/2026-07-26-c1-c5-cross-protocol-conversion.md`

## Summary

Landed directional conversion **contracts and selection** without enabling production conversion:

- Domain: `FidelityClass`, `RouteMode`, `ConversionCapability`, `RouteCandidate`; extended `Feature` and `Deployment.conversions`.
- Fidelity matrix: `conversion/contracts.py` (messages↔chat; reasoning=`lossy_unsafe`; prompt_cache/tools/streaming=`unsupported` for pilot).
- Config: `allow_conversion` + `conversion_policy.allowed`; plan/model `conversions`; §8.6 fail-closed validators; generator emits conversions; registry parses them.
- Routing: `resolve_route` + `filter_route_candidates`; direct ranked before convert; `PROTOCOL_CONVERSION_ENABLED` default **false**.

## Commands

```text
pytest tests/unit/test_c1_conversion_contracts.py tests/unit/test_m2_protocol_routing.py tests/unit/test_strategy.py tests/unit/test_config_schema.py -q
→ 60 passed
```

## Next

C2-01 observability hooks + C2-02 G0-B spike before wiring live convert dispatch.

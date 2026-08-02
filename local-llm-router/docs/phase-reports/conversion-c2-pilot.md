# Conversion C2 Pilot Acceptance

**Date:** 2026-07-26  
**Direction:** `anthropic_messages` (public) → `openai_chat` (upstream)  
**Status:** Implementation complete; **production `PROTOCOL_CONVERSION_ENABLED` remains false**

## Deliverables

| Item | Path |
|------|------|
| Spike (G0-B GO) | `docs/phase-reports/conversion-c2-spike-g0b.md` |
| Ops | `docs/operations-protocol-conversion.md` |
| Adapter | `plugins/shared_quota_router/conversion/adapters/messages_to_chat.py` |
| Dispatch | `plugins/shared_quota_router/conversion/dispatch.py` |
| Fixtures | `tests/fixtures/conversion/messages_to_chat/` |
| Tests | `tests/unit/test_c2_messages_to_chat_pilot.py` |

## Mount points (G0-B)

1. **Request:** after deployment select, `get_available_deployment` mutates `request_kwargs` when `route_mode=convert`.
2. **Response:** `async_post_call_success_hook` returns Anthropic-shaped dict when metadata says convert.
3. Metadata keys: `shared_quota_route_mode`, `shared_quota_conversion`.

## Coverage

- Text + system + multiturn content blocks (text only)
- Usage + finish_reason → stop_reason
- Error envelope Anthropic shape
- Reject tools / streaming

## Not covered (still unsupported)

tools, tool_choice, images, reasoning/thinking, streaming, prompt cache, Responses, reverse chat→messages adapter

## Commands

```text
pytest tests/unit tests/contract -q
```

## Enablement gate

| Environment | Requirement |
|-------------|-------------|
| Production | Keep `PROTOCOL_CONVERSION_ENABLED=false` |
| Staging flag=true | C2 evidence (this doc) **and** C3-01 circuit isolation (or dated risk acceptance) |
| Rollback | Set conversion flag false; restart; Redis untouched |

## Go / No-Go

**Go for code merge with flag off.**  
**No-Go for staging traffic** until C3-01 lands.

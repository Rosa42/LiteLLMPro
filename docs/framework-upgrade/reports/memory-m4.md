# Memory M4/M5 — workspace JSONL retrieve + extract queue

**Date:** 2026-08-26  
**Goal:** Workspace-scoped gateway memory. Unknown workspace skips. Fail-open. Never write Redis `sq:*`.

## Code

- F4 retrieve: `memory_workspace.py` / `memory_store.py` / `memory_retrieve.py` — `X-Workspace-Root` or metadata, weak infer only with ≥2 absolute paths, user `<gateway_memory>` inject (not system), 300ms budget
- Workspace header plumbing: LiteLLM Messages kwargs have no top-level `headers`; `collect_request_headers` reads `proxy_server_request.headers` (Windows `E:\...` normalized inside Linux without `Path.resolve`)
- F5 enqueue: `callbacks.on_success` only `enqueue_from_kwargs` (no HTTP in the callback)
- F5 worker: `process_next_job` — “记住 / please remember / remember that” writes a redacted note; optional cheap-model extract via `GATEWAY_MEMORY_EXTRACT_MODEL` (default in `.env`: MiniMax-M2.5) with quota exclusive vs parent
- Redact: `sk-…`, `Bearer`, `ark-…`, `api_key=`
- Queue depth 32; process exit drops jobs
- `GATEWAY_MEMORY_EXTRACT_ENABLED` currently **true** in operator `.env` (retrieve still fail-open)


## Tests

Unit: retrieve inject / unknown skip / flag off; `proxy_server_request` header collect; extract queue full drop; remember-rule writes only after `process_next_job`; extract HTTP failure does not write. Suite with contract: **362 passed, 1 skipped**.

## Live (2026-08-26)

JSONL under `data/gateway-memory/{sha256[:32]}.jsonl` (workspace hash prefix `34b1e82a`). Overlay execute → mock-s2 recorded inject on `/probe/last`. Production Volc (extra_hosts, not mock) re-checked the same workspace:

| Check | Result |
| --- | --- |
| Known workspace + keyword overlap (overlay mock) | **PASS** HTTP 200, mock `has_gateway_memory=true` |
| Known workspace (production Volc) | **PASS** HTTP 200; model thinking cites `<gateway_memory>` including the handwritten LiteLLM pin |
| No `X-Workspace-Root` (overlay + production) | **PASS** HTTP 200; no gateway-memory inject |
| `please remember` extract (overlay + production) | **PASS** HTTP 200, token appeared in JSONL |

LiteLLM may fire success logging twice, so extract can enqueue duplicate notes for one user turn. Ugly capture (`that ` prefix) remains. Not blocking.

Volc `api_base` is production HTTPS, not mock. Memory files are not Redis `sq:*`.

## Secrets

This report has no API keys or workspace paths from the operator machine.

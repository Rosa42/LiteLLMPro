"""Operator notes: enabling Anthropic Messages / OpenAI Responses (post-MVP gate).

MVP default (M3):
- Public Chat Completions is enabled for logical models with
  ``logical_models.<name>.public_protocols: [openai_chat]`` and Chat deployments.
- ``/v1/messages`` and ``/v1/responses`` return controlled
  ``protocol_not_enabled`` / unsupported errors unless **both**:
  1. An enabled deployment declares matching ``upstream_protocol``
  2. The logical model opts in via ``public_protocols``

Never enable Messages because a model name contains ``claude``.
Never bridge Responses payloads to Chat in MVP.

## Enable Anthropic Messages (future)

1. Complete Phase 0 / contract verification for the provider (auth, path,
   text, tools, stream, usage, errors). Record evidence under
   ``docs/phase-reports/``.
2. Set ``plans[].upstream_protocol: anthropic_messages`` (or per-model override)
   and ``enabled: true`` only for verified plans.
3. Opt in logical models:
   ``logical_models.<name>.public_protocols: [anthropic_messages]``
   (may also keep ``openai_chat`` if dual public exposure is intended).
4. Run ``scripts/llm-router.ps1 apply`` (or Python generator CLI). Startup
   validation rejects Messages opt-in without a matching upstream deployment.
5. Smoke: ``POST /v1/messages`` must hit only the verified provider path
   (``anthropic/`` prefix). OpenCode Go / Volc must receive **no** Messages calls.
6. Confirm discovery: ``GET /v1/router/model-capabilities`` lists
   ``anthropic_messages`` only for opted-in models.

## Enable OpenAI Responses (future)

**C5 evaluation (2026-07-26): No-Go** — see `docs/conversion/responses-direct-evaluation.md`.
Current `config/plans.yaml` has no `upstream_protocol: openai_responses` deployment;
keep `/v1/responses` controlled-disabled. Do **not** enable via Chat conversion.

When a verified Responses upstream exists:

1. Verify a direct Responses provider (not Chat). Do **not** treat ``openai/``
   prefix as Responses capability.
2. Set ``upstream_protocol: openai_responses`` on that deployment.
3. Opt in ``public_protocols: [openai_responses]`` on the logical model.
4. Apply config. Validation fails if Responses is opted in without a capable
   deployment.
5. Smoke ``POST /v1/responses`` against local mock first, then the real provider.
6. Keep Chat traffic on ``/v1/chat/completions`` only — no Responses→Chat bridge.

## Rollback

- Remove the protocol from ``public_protocols`` and re-apply, **or**
- Disable the plan (``enabled: false``) and re-apply.
- Feature-flag rollback lands in M4 (``PROTOCOL_AWARE_GATEWAY_ENABLED``).
"""

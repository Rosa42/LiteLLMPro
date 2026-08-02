# Patches

## Policy

- Prefer official LiteLLM Custom Routing Strategy registration (no patch).
- If Proxy cannot load a custom strategy without code change, a **minimal** patch may exist:

```text
0001-register-shared-quota-router.patch
```

### Hard rules

1. The patch may **only** register the custom strategy object with the Router.
2. **No** quota / routing / classifier business logic inside the patch.
3. Apply at **image build time** (see `deploy/Dockerfile.litellm`).
4. Review checklist before merging:
   - [ ] Diff touches only registration wiring
   - [ ] No Redis / quota_group / FailureKind code
   - [ ] Rebase-tested against pinned `LITELLM_VERSION`

M0 has **no** patch file by design.

# Upstream LiteLLM management

## Submodule

```bash
# From local-llm-router root (after git init)
git submodule add https://github.com/BerriAI/litellm.git upstream/litellm
cd upstream/litellm
git fetch --tags
git checkout v1.90.5
cd ../..
git add upstream/litellm
```

**Read-only:** never commit business logic into `upstream/litellm`.

## Shallow clone fallback (network issues)

```bash
git submodule add --depth 1 https://github.com/BerriAI/litellm.git upstream/litellm
cd upstream/litellm
git fetch --depth 1 origin tag v1.90.5
git checkout v1.90.5
```

If `submodule add` fails, clone manually:

```bash
git clone --depth 1 --branch v1.90.5 https://github.com/BerriAI/litellm.git upstream/litellm
```

Then register as submodule or document the pin in `config/versions.env`.

## Version pin

See `config/versions.env`:

```text
LITELLM_VERSION=v1.90.5
```

Reject production defaults of: `latest`, `main`, `nightly`, `rc`, `dev`.

## Contract tests (phase 7+)

Either:

1. Install `litellm` matching the pin in a venv, or
2. Set `PYTHONPATH` to include `upstream/litellm` (package layout permitting).

Record the exact command that makes `tests/contract/` green in the phase 7 report.

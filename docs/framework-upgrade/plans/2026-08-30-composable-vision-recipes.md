# Configurable vision recipes — Implementation Plan

> **Status (2026-08-30): complete.** Schema, CLI, internal select, outcome report, cache SCHEMA_VER 4, discovery two-flag omit, and §11 tests are in tree. Operator live switch to `glm-5.3` is documented in [`../maintenance.md`](../maintenance.md), not a remaining coding task.
>
> **For agentic workers (historical):** Execute task-by-task. Use TDD (failing test first). Do **not** git commit unless the user asks. Python: `F:\anaconda\envs\py312\python.exe`, cwd `local-llm-router`, `PYTHONPATH=plugins`.

**Goal:** Operators can create and switch vision facades on the host: `execute_model` (e.g. `glm-5.2` → `glm-5.3`) and `translate_model` (e.g. keep `MiniMax-M3` or another Messages+IMAGE model), without rethink, fallback, or in-proxy YAML edits.

**Architecture:** Same G0-B hang-point. `plans.yaml` remains SoT (`plans[]` + `logical_models`). Generator only emits `plan.models` rows and rewrites the facade upstream name to `execute_model`. Runtime uses `is_vision_compose` + trusted `select_internal_deployment` (ContextVar). Host CLI dual-writes YAML then `apply --enable-messages-chat-native`.

**Tech Stack:** LiteLLM v1.90.5 plugin `shared_quota_router`, pytest, PowerShell `llm-router.ps1`.

**Spec:** `docs/framework-upgrade/specs/2026-08-28-composable-recipes-design.md`

**Product note:** Live inventory has `glm-5.3` (Volc Messages, no IMAGE), not a separate id `glm-5.3-flash`. Execute-slot switches use `glm-5.3`. Translator stays `MiniMax-M3` unless another deployment has `anthropic_messages` + `image`. If Volc later adds a flash id, the same CLI `--execute` / `--vision` flags apply.

**Out of this plan:** rethink, automatic fallback, Docker volume changes, in-container admin API.

---

## File map

| File | Responsibility |
|------|----------------|
| `plugins/shared_quota_router/models.py` | `ComposeRecipe.template` |
| `plugins/shared_quota_router/composed_vision.py` | `is_vision_compose`; `defers_image_gate` uses it |
| `plugins/shared_quota_router/config_schema.py` | Parse/validate: keys, aliases, eligible routes, IMAGE, lineage, nest/cycle, `facade_role`, advertised formula |
| `plugins/shared_quota_router/generator.py` | Emit `template` + `facade_role`; facade timeout ≥ 480; reject silent id rewrite (`ascii_safe(id) != id` → error) |
| `plugins/shared_quota_router/internal_call.py` | ContextVar + `select_internal_deployment` + `report_internal_outcome` |
| `plugins/shared_quota_router/strategy.py` | Skip public opt-in **only** when ContextVar set |
| `plugins/shared_quota_router/pipeline.py` | `is_internal_call` trusts ContextVar only; memory-extract must set the Var |
| `plugins/shared_quota_router/vision_compose.py` | Slots from recipe; `{text,image}` select; outcome report; per-(model,qg) circuit; digest+SCHEMA_VER; lease renew |
| `plugins/shared_quota_router/discovery.py` | Omit vision facades unless **both** enhance flags |
| `plugins/shared_quota_router/compose_mutator.py` | **New.** In-memory add/update/remove + file lock + dual backup |
| `plugins/shared_quota_router/cli_config.py` | `compose-vision-*` subcommands |
| `scripts/llm-router.ps1` | Thin wrap; always pass `--enable-messages-chat-native` |
| `tests/unit/test_compose_recipe_config.py` | Schema counterexamples |
| `tests/unit/test_internal_select.py` | **New.** Opt-in bypass + IMAGE features |
| `tests/unit/test_compose_mutator.py` | **New.** Transaction + remove guards + glm-5.3 execute |

---

## Phases (do in order)

1. **Schema (this first coding slice)** — template, eligible IMAGE, lineage, no nesting, facade_role. Existing `glm-5.2-vision` tests stay green.
2. **Internal select + pipeline trust** — P0-1; migrate memory-extract to ContextVar in the same change.
3. **Vision HTTP lifecycle** — P0-5, cache SCHEMA_VER 4, timeout/lease.
4. **Host mutator CLI** — P0-6; `compose-vision-update --id glm-5.2-vision --execute glm-5.3 --vision MiniMax-M3`.
5. **Discovery two-flag omit** + generator timeout/id check.
6. **Full `tests/unit` + `tests/contract`**.

Do not wire “any Messages model as a slot” into production HTTP before phases 1–3.

---

### Task 1: ComposeRecipe.template + is_vision_compose

**Files:** `models.py`, `composed_vision.py`, `tests/unit/test_compose_recipe_config.py`

- [ ] Failing tests: missing `template` still parses as vision; `template: rethink` raises; `is_vision_compose` false when compose is None; `defers_image_gate` false if we ever had non-vision compose (V1 cannot load it).
- [ ] Implement `ComposeRecipe.template: str = "vision"` and `is_vision_compose(logical)`.
- [ ] `defers_image_gate`: `S5_COMPOSED_MODELS` OR `is_vision_compose`.

```python
def is_vision_compose(logical: LogicalModelProtocols | None) -> bool:
    if logical is None or logical.compose is None:
        return False
    tmpl = (logical.compose.template or "vision").strip() or "vision"
    return tmpl == "vision"
```

Run: `F:\anaconda\envs\py312\python.exe -m pytest tests/unit/test_compose_recipe_config.py -v`

---

### Task 2: Parser — canonical keys, reject unknown / fallback, aliases

**Files:** `config_schema.py` `_parse_compose_recipe`

Allowed keys: `template`, `execute_model`, `translate_model`, `reasoning`, `vision`.

- Alias `reasoning` → execute, `vision` → translate; conflict if both present and differ.
- Default template `vision`; any other template → `ConfigValidationError`.
- `fallback` or unknown key → error.

Existing tests without `template` must still pass.

---

### Task 3: Eligible routes + IMAGE + lineage + no-nest (P0-2/3/4)

**Files:** `config_schema.py` `validate_plans_document`, `PlanModelEntry.facade_role`

Helpers:

```python
LOGICAL_MODEL_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9._-]{1,63}$")

def eligible_routes(doc, model, protocol, features) -> set[tuple[str, str]]:
    # (plan.id, quota_group_id) for enabled rows matching protocol and features ⊆ resolved_features
```

- Translate: `anthropic_messages` + `{text, image}`.
- Execute: `anthropic_messages` + `{text}`.
- Quota groups = those routes only (not all protocols).
- `F_plans == E_plans`; extra facade row on MiniMax plan → error (`lineage` / plan id in message).
- Slot cannot be self, any `logical.compose`, or `facade_role==vision` model.
- Cycle DFS on compose edges.
- `facade_role: vision` required on facade plan rows except grandfather id `glm-5.2-vision`.
- Facade `public_protocols` exactly `{anthropic_messages}`.
- `advertised_features` must equal `(execute_base ∪ {image})` where `execute_base` is execute logical advertised or intersection of execute Messages `supported_features`.

Counterexample tests (must fail closed):

| Test | Match |
|------|--------|
| translate MiniMax-M2.7 without IMAGE | `image` |
| facade row on MiniMax plan | `lineage` or MiniMax plan id |
| execute_model is glm-5.2-vision | `compose` / nest |
| compose on glm-5.2 (existing non-facade rows) | `facade_role` |
| execute glm-5.3 + translate MiniMax-M3, facade only on volc | **pass** (operator story) |

---

### Task 4: Generator emit template; id rewrite guard

- Write `template: vision` under compose.
- If `ascii_safe(model_name) != model_name` for any model_list name → `ConfigValidationError` (ids already validated; this is belt-and-suspenders).
- Facade `timeout: 480` when `is_vision_compose`.

---

### Task 5: Internal select (P0-1)

**New** `internal_call.py` APIs:

- `trusted_internal_token()` context manager sets `sq_trusted_internal`.
- `select_internal_deployment(...)` wraps existing select with token + `required_features`.
- Strategy: skip `model_opts_into_public` iff token set.
- `pipeline.is_internal_call`: token only, **not** metadata.
- `memory_extract.py` must enter the token around its select (regression).

Tests: private translator (empty public_protocols) selectable internally; public request with metadata `internal_call=true` still requires opt-in and still runs pipeline.

Child select **must** pass `required_features={text, image}` — fixture with two MiniMax deployments, only one has IMAGE.

---

### Task 6: Vision outcome + circuit + cache + lease (P0-5)

- `report_internal_outcome` → same classifier as `SharedQuotaCallback.on_failure` / `on_success`.
- Circuit key `(translate_model, quota_group_id)`.
- `vision_cache_digest` includes `translate_model`; `SCHEMA_VER` 3→4.
- Renew parent lease after each image; generated timeout 480.

---

### Task 7: Mutator CLI (P0-6) — operator surface

```
python -m shared_quota_router.cli_config compose-vision-slots --plans config/plans.yaml
python -m shared_quota_router.cli_config compose-vision-add --id my-vision --execute glm-5.3 --vision MiniMax-M3
python -m shared_quota_router.cli_config compose-vision-update --id glm-5.2-vision --execute glm-5.3 --vision MiniMax-M3 --force
python -m shared_quota_router.cli_config compose-vision-remove --id my-vision
```

Transaction: lock → mutate memory → validate → render with `--enable-messages-chat-native` → backup both files → atomic plans then litellm; litellm fail rolls plans back.

`remove --id glm-5.2` refuses (not a facade).

Inject facade rows onto every eligible Messages plan of `--execute`. Do not inject onto MiniMax plans.

`llm-router.ps1` wrappers call the same argv including `--enable-messages-chat-native`.

---

### Task 8: Discovery + docs

- Omit vision facade unless `GATEWAY_ENHANCE_ENABLED` and `VISION_COMPOSE_ENABLED`.
- Update `test_discovery_lists_compose_features_when_vision_flag_on` to set **both** flags.
- Sync `vision-compose.md` / `pipeline.md` if any sentence still says metadata `internal_call`.

---

## Verification

```
cd local-llm-router
$env:PYTHONPATH="plugins"
F:\anaconda\envs\py312\python.exe -m pytest tests/unit tests/contract -q
```

Expect: previous vision tests green; new §11 counterexamples green; no rethink/fallback code.

After CLI exists, operator proof (host, then recreate litellm):

```
compose-vision-update --id glm-5.2-vision --execute glm-5.3 --vision MiniMax-M3 --force
```

Generated `litellm.yaml` facade block: `execute_model: glm-5.3`, `translate_model: MiniMax-M3`, `model: anthropic/glm-5.3`, Volc env refs (not MiniMax).

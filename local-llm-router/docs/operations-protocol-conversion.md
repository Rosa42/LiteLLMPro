# Protocol conversion operations (C2+)

## Feature flags (dual matrix — code-enforced AND)

Runtime selection / dispatch uses `is_conversion_routing_active()` =

`PROTOCOL_AWARE_GATEWAY_ENABLED ∧ PROTOCOL_CONVERSION_ENABLED ∧ messages_chat_path_ready`

其中本期 **Messages→Chat path ready = native-only**（`is_native_messages_chat_path_active()`）；
**G0-A mount 不计入** readiness（P0-G0A）。

Raw env bits remain visible in `flag_snapshot()`; only the AND unlocks convert.

| `PROTOCOL_AWARE_GATEWAY_ENABLED` | `PROTOCOL_CONVERSION_ENABLED` | Behavior |
|----------------------------------|-------------------------------|----------|
| false | false | Legacy Chat selection; Messages/Responses gated; **no convert** |
| true | false | MVP protocol-aware direct routes only; **no convert** (default) |
| false | true | **Misconfig:** convert still **off** (AND fails); Messages gates unchanged |
| true | true | Direct preferred; explicit convert candidates allowed when **native path ready** |

Defaults: gateway may be `true` in MVP ops; **conversion always defaults `false`**.

**A6 vs A7（P1-SCOPE）**：A6=discovery 静态 `public_protocols`；A7=运行时门控（conversion/native flags）。列出 ≠ 可达；关 conversion 时 kimi 运行时不可达但 discovery 仍可能列出。

### Native SoT（P1-SOT）— 本期 Messages→Chat **native-only**

**唯一批准输入：** CLI apply 带 `--enable-messages-chat-native`（写入 YAML）。
禁止口头批准；禁止依赖裸 env 作为生产 SoT。

```text
yaml.use_chat_completions_url_for_anthropic_messages =
  (--enable-messages-chat-native)
  ∧ (∃ logical: allow_conversion ∧ anthropic_messages→openai_chat)
```

无 flag / 无 convert policy → 必须生成 **`false`**。

运行时：若 litellm 属性已加载且为 `False`，**禁止**再 OR/回退遗留 env；
仅属性缺失时才严格解析 env（仅 `1`/`true`/`yes`/`on` 为真）。

```powershell
# 批准并生成 YAML true（须 plans 含 Messages→Chat convert policy）
python -m shared_quota_router.cli_config apply `
  --plans config/plans.yaml `
  --output config/litellm.yaml `
  --enable-messages-chat-native

# Staging：gateway + conversion + 已写入的 native YAML
$env:PROTOCOL_AWARE_GATEWAY_ENABLED = "true"
$env:PROTOCOL_CONVERSION_ENABLED = "true"
# 不要用 LITELLM_USE_CHAT_COMPLETIONS_URL_FOR_ANTHROPIC_MESSAGES 作为生产开关
# restart proxy/worker
```

**Staging/prod `CONVERSION=true` 仍须** proven path（本期 = G0-Native YAML true）+ project gates；P4-Native green。
G0-A 本期 out of scope（`g0a_mount_ready` 不激活 Messages→Chat）。

Redis quota / affinity / lease keys are **never** flushed when toggling these flags.

## Rollback

| Level | Steps |
|-------|--------|
| **L0** | 本期 G0-A **N/A**（未 mount）。若将来启用 G0-A：须独立开关 + unmount，另开 ADR。 |
| **L1** | `PROTOCOL_CONVERSION_ENABLED=false` → restart。Direct traffic unchanged。 |
| **L2（native off）** | ① `apply` **无** `--enable-messages-chat-native` → YAML `use_chat_completions_url_for_anthropic_messages: false`；② **删除** native 相关 env（如 `LITELLM_USE_CHAT_COMPLETIONS_URL_FOR_ANTHROPIC_MESSAGES`）；③ **重启**；④ 确认 `g0a_mount_ready==False`（本期应恒假）；⑤ convert 候选 = 0。**禁止**仅用 env=`false` 回滚。 |
| L1b | Keep `PROTOCOL_AWARE_GATEWAY_ENABLED=true` unless whole gateway must roll back. |
| L3 | restore `config/backups/litellm.yaml.*.bak` 或 `PROTOCOL_AWARE_GATEWAY_ENABLED=false` → legacy Chat。 |
| L4 | 去掉 convert-only public / `allow_conversion`；或回滚插件构建。 |

**After L0/L1/L2 verify:** convert candidates = 0；conversion counters stop；Chat direct OK；Redis `sq:quota:*` untouched。

## Metrics

| Counter | When |
|---------|------|
| `shared_quota_protocol_conversion_total` | Convert attempt result (`result=success\|failure`) |
| `shared_quota_protocol_conversion_failure_total` | Convert failures only |
| `shared_quota_protocol_route_total` | Includes `route_mode=convert` when selected |

Labels: `direction` (registry keys only), `result`, `reason` (enum / routing reason codes) — **no** prompts, bodies, field values, or secrets.
`dropped_fields` / warnings must stay path-or-code only.

## Pilot direction

Default candidate: public `anthropic_messages` → upstream `openai_chat` (text, non-streaming only).

**Conversion-only Messages**（无 direct Anthropic upstream）本期仅经 **G0-Native**：

1. YAML `use_chat_completions_url_for_anthropic_messages: true`（CLI `--enable-messages-chat-native` + convert policy）,
2. `PROTOCOL_AWARE_GATEWAY_ENABLED` ∧ `PROTOCOL_CONVERSION_ENABLED`,
3. logical `allow_conversion` + matching `conversions` + registered adapter (`public_reachable`).

G0-A mount **本期不作为** path ready；勿指望 unmount 以外的 flag-off 来关掉误 mount 的 G0-A。

See thin G0-A design/plan（历史备选，本期禁用）: `docs/superpowers/specs/2026-07-26-thin-g0a-front-adapter-design.md`.

# 设计方案：模型分治 — Anthropic 公网统一入口下的 Direct / Convert

**日期：** 2026-08-01  
**修订：** 2026-08-01 **v0.3**（第二轮专家审查；仍仅批准 S0）  
**状态：** Proposed → **S0 门禁中**（**不批准 S1a/canary**，直至本文 P0 关闭且 pytest 全绿）  
**项目：** `local-llm-router`（LiteLLM pin `v1.90.5`）  
**读者：** 产品 / 运维 / 实现 AGENT  
**前置决策：** 对外统一暴露 Anthropic Messages（`POST /v1/messages`）；对内按模型能力分治。

---

## 0. 一句话

客户端只打 Anthropic；`glm-5.2` / `claude-opus-4-8` **直连**；`kimi-k3`（后 `deepseek-v4-flash`）**仅经 LiteLLM G0-Native Messages→Chat**。  
本期 **native-only**（G0-A 不参与 path ready）；同 key 同 `quota_group_id`；convert **禁流（C4）**；分类器用**现有** `FailureKind` 钉死契约；S0 未绿不得 canary。

---

## 0.1 审查裁决（v0.2 → v0.3）

| 裁决 | 内容 |
|------|------|
| 方向 | 同 key 同 qg、native C4、删 owner、先 S0 再单模型 — **保留** |
| S1a | **仍不批准**；须先关本节全部 **P0** |
| 工作树 | 与既有 Responses Policy A / Anthropic 改动并存；本方案 **不覆盖、不回滚 Policy A**，但 Messages convert canary **不得**扩大 Responses 面 |

### P0（阻断 S1a）

| ID | 问题 | v0.3 规范 |
|----|------|-----------|
| **P0-G0A** | `native ∨ g0a` 使 L2（仅关 native）无法停 convert | 本期 **Messages→Chat path ready = native only**；`g0a_mount` **不计入**；L2 验收见 §9.4 |
| **P0-CLF** | RegionError/400 枚举含糊；403→整 qg DISABLED | **钉死**现有 `FailureKind` 映射表；OpenCode 分发；断言 **route/deployment cooldown key** |
| **P0-C4** | （继承 v0.2）native stream 可达 | 统一 `stream \|= STREAMING∈required`；native **不**依赖 `get_converter()` |

### P1（S0 应收口；未收口不得宣称 S0 完成）

| ID | 问题 | v0.3 规范 |
|----|------|-----------|
| **P1-SOT** | 「运维批准」无输入；env 可盖过 YAML false | CLI/env **单一批准源**；YAML 加载后 **false 优先于遗留 env** |
| **P1-A5** | 只测 helper，非 `/v1/messages` wire | **固定 HTTP 400** + 真实 Anthropic endpoint 顶层 body |
| **P1-CAP** | `DEFAULT_CHAT_FEATURES` 含 stream/tools | Anthropic 分治配置：**禁止省略** features；缺省改为 `[text]` 或强制显式 |
| **P1-QG-ID** | qg 缺格式/别名/冲突规则 | §6.2 完整约束；非法 ID **拒绝**，禁止 `ascii_safe` 静默改写 |
| **P1-C4-BYPASS** | `stream=False` 但 features 含 STREAMING | 边界归一化；gate 对 native 不强制 project converter |
| **P1-SCOPE** | C5 No-Go vs 现网 Policy A / discovery | §12：本方案 **不撤销** Policy A；A6=静态声明；A7=运行时可达 |

### 验证基线（审查时）

`216 passed, 1 skipped, 2 failed` — Chat-only 旧测未修。**pytest 未全绿 ⇒ S0 未完成。**

---

## 1. 背景与动机

### 1.1 产品约束

| 约束 | 选择 |
|------|------|
| 公网协议（本方案交付面） | **仅**保证四模型经 `anthropic_messages` 可用；**不**在本 epic 关闭既有 Responses Policy A |
| 首批逻辑模型 | `glm-5.2`、`claude-opus-4-8`、`kimi-k3`、（后）`deepseek-v4-flash` |
| 转换实现 | **仅** `TransformOwner.LITELLM_NATIVE`（Messages→Chat） |
| G0-A | **本期 out of scope**（代码可保留，但 readiness 不含） |
| 流式转换 | **硬 No-Go（C4）** |
| 共享额度 | 同 key 同 qg；故障用 deployment/route cooldown |

### 1.2 上游实测（2026-08-01，仅非流式文本）

| 模型 | Messages | Chat | 路径 |
|------|----------|------|------|
| `glm-5.2` | ✅ | ✅ | direct Anthropic |
| `claude-opus-4-8` | ✅ | — | direct Anthropic |
| `kimi-k3` | ❌ | ✅ | convert |
| `deepseek-v4-flash` | ❌ | ✅（中国区 opt-in 后） | convert（第二阶段） |

未探针的 streaming/tools：**不得**写入 `supported_features`。

---

## 2. 目标架构

```text
 Client ── POST /v1/messages ──► LiteLLM Proxy
                                    │
                    protocol_gates（C4 归一化 stream）
                    SharedQuotaStrategy
                                    │
              ┌─────────────────────┴─────────────────────┐
              │ direct                                      │ convert (NATIVE only)
              ▼                                             ▼
        anthropic/<model>                             openai/<model>
        OpenCode/Volc/NewAPI Messages                 OpenCode Chat
              │                                             │
              └──────── quota_group_id=opencode-a ──────────┘
                        （同一 OPENCODE_GO_KEY_A）
```

### 2.1 变换所有权

| 路径 | Owner | 说明 |
|------|-------|------|
| glm / claude | `DIRECT` | 无改写 |
| kimi / deepseek | `LITELLM_NATIVE` | 禁止项目 G0-B 双改写 |
| G0-A / PROJECT_ADAPTER | **本期禁用** | 见 §4.5 |

**禁止**配置字段 `owner:`（会被静默忽略）。

---

## 3. 路由矩阵

| model | public | upstream | mode | 上线阶段 |
|-------|--------|----------|------|----------|
| `glm-5.2` | anthropic_messages | OpenCode+Volc anthropic | direct | S1a 可伴随（direct） |
| `claude-opus-4-8` | anthropic_messages | NewAPI anthropic | direct | 同上 |
| `kimi-k3` | anthropic_messages | OpenCode chat | convert | **S1a canary（P0 后）** |
| `deepseek-v4-flash` | anthropic_messages | OpenCode chat | convert | **S1b** |

硬规则：direct ≻ convert；kimi/deepseek 无 Anthropic deployment；convert∧stream → 租约前拒绝。

---

## 4. 配置设计

### 4.1 Base URL

| 用途 | env | 值 |
|------|-----|-----|
| OpenCode Anthropic | `OPENCODE_GO_ANTHROPIC_BASE_URL` | `https://opencode.ai/zen/go` |
| OpenCode Chat | `OPENCODE_GO_BASE_URL` | `https://opencode.ai/zen/go/v1` |
| Volc Anthropic | `VOLC_CODING_ANTHROPIC_BASE_URL` | `https://ark.cn-beijing.volces.com/api/coding` |
| NewAPI | `PLAN_NEWAPI_A_BASE_URL` | `https://7646881.cloud` |

### 4.2 `plans.yaml` 示意（S1a）

```yaml
plans:
  - id: opencode-a-msg
    provider_id: opencode-go
    priority: 10
    quota_group_id: opencode-a
    base_url_env: OPENCODE_GO_ANTHROPIC_BASE_URL
    api_key_env: OPENCODE_GO_KEY_A
    upstream_protocol: anthropic_messages
    supported_features: [text]    # 必须显式；禁止省略触发 DEFAULT_CHAT_FEATURES
    supports_streaming: false
    models: [glm-5.2]

  - id: volc-c-msg
    provider_id: volcengine
    priority: 20
    quota_group_id: volc-c
    base_url_env: VOLC_CODING_ANTHROPIC_BASE_URL
    api_key_env: VOLC_CODING_KEY_C
    upstream_protocol: anthropic_messages
    supported_features: [text]
    supports_streaming: false
    models: [glm-5.2]

  - id: newapi-a
    provider_id: newapi
    priority: 30
    enabled: true
    quota_group_id: newapi-a
    base_url_env: PLAN_NEWAPI_A_BASE_URL
    api_key_env: PLAN_NEWAPI_A_API_KEY
    upstream_protocol: anthropic_messages
    supported_features: [text]
    supports_streaming: false
    models: [claude-opus-4-8]

  - id: opencode-a-chat
    provider_id: opencode-go
    priority: 10
    quota_group_id: opencode-a      # 同 key 同 qg
    base_url_env: OPENCODE_GO_BASE_URL
    api_key_env: OPENCODE_GO_KEY_A
    upstream_protocol: openai_chat
    supported_features: [text]
    supports_streaming: false
    conversions:
      - from: anthropic_messages
        to: openai_chat
        streaming: false
        fidelity: lossy_safe
    models: [kimi-k3]               # deepseek 留 S1b

logical_models:
  glm-5.2:
    public_protocols: [anthropic_messages]
    allow_conversion: false
  claude-opus-4-8:
    public_protocols: [anthropic_messages]
    allow_conversion: false
  kimi-k3:
    public_protocols: [anthropic_messages]
    allow_conversion: true
    conversion_policy:
      allowed:
        - from: anthropic_messages
          to: openai_chat
```

### 4.3 能力缺省（P1-CAP）

| 规则 | 要求 |
|------|------|
| Anthropic 分治 / convert 相关 plan | **`supported_features` 必填**；省略 → `ConfigValidationError` |
| 推荐全局 | 将 `DEFAULT_CHAT_FEATURES` 收窄为 `{TEXT}`，或仅对 `upstream_protocol: openai_chat` 且显式 `legacy_defaults: true` 才填充旧默认 |
| S0 最低交付 | 至少：**分治 plans 路径强制显式 features**，防止静默打开 stream/tools |

### 4.4 Native SoT 与批准源（P1-SOT）

**唯一批准输入（钉死一种，S0 实现选 A）：**

| 方案 | 机制 | 选用 |
|------|------|------|
| **A（推荐）** | apply CLI：`shared_quota_router.cli_config apply --enable-messages-chat-native` | **本期采用** |
| B | `.env` 仅允许 `SHARED_QUOTA_ENABLE_MESSAGES_CHAT_NATIVE=1`（整数 0/1，禁止字符串 true/false）经 CLI 读入后写入 YAML，然后 **删除**该 env | 备选 |

**生成规则：**

```text
yaml.use_chat_completions_url_for_anthropic_messages =
  (--enable-messages-chat-native)
  ∧ (∃ logical: allow_conversion ∧ anthropic_messages→openai_chat)
```

无 flag / 无 convert policy → 必须生成 **`false`**。

**运行时读取（须改 `is_native_messages_chat_path_active`）：**

```text
1. 若 litellm 模块属性已由 proxy YAML setattr 加载：
     以 bool(litellm.use_chat_completions_url_for_anthropic_messages) 为准
     —— 当值为 False 时，**禁止**再 OR/回退 env
2. 仅当属性缺失（单测/未启动 proxy）时，才读 env；
     env 解析必须用严格解析：仅 "1"/"true"/"yes"/"on"（大小写不敏感）为真；
     "false"/""/缺失为假（不得用 Python bool("false")）
```

**禁止：** 运维口头「批准」无 CLI 记录；禁止依赖裸 env 作为生产 SoT。

### 4.5 Path ready：本期 native-only（P0-G0A）

**修订前（错误用于本期）：**

```text
path_ready = native ∨ g0a_mount   # L2 关 native 后 convert 仍可能活着
```

**本期 Messages→Chat：**

```text
messages_chat_path_ready = is_native_messages_chat_path_active()
                         ∧ NOT is_g0a_messages_mount_ready()   # 防御：若误 mount 则 fail-closed 或告警

is_conversion_routing_active (Messages→Chat 方向)
  = gateway ∧ PROTOCOL_CONVERSION_ENABLED ∧ messages_chat_path_ready
```

实现落点（S0）：

1. `is_conversion_path_ready()` **拆分**或增加 `is_messages_chat_native_path_ready()`，供 Messages→Chat 使用。  
2. `route_readiness.readiness(..., LITELLM_NATIVE, messages→chat)` 仅看 native。  
3. `readiness(..., PROJECT_ADAPTER, messages→chat)` → **恒 False（本期）**，即使 `g0a_mount_ready`。  
4. Registry：native 关闭时 **不得**落入 PROJECT_ADAPTER 候选（或 adapter readiness 已 False）。  
5. 运维文档：本期 L0（G0-A unmount）标为 N/A；若将来启用 G0-A，须独立开关 + L0 纳入回滚，**另开 ADR**。

---

## 5. 请求生命周期与 C4（P0-C4 / P1-C4-BYPASS）

### 5.1 Stream 归一化（所有边界）

在 `public_reachable` / `assert_endpoint_allowed` / `resolve_route` 入口：

```text
effective_stream = stream OR (Feature.STREAMING in (required_features or {}))
```

之后 **只**使用 `effective_stream`；禁止两参数不一致。

### 5.2 Native Messages→Chat 候选

```text
if public==anthropic_messages and upstream==openai_chat and native_ready:
  if effective_stream: return None          # C4
  if extras beyond TEXT: return None
  return RouteCandidate(owner=LITELLM_NATIVE, streaming=False, features={TEXT})
```

**禁止** `streaming=True` capability。

### 5.3 Gate 与 converter（P1-C4-BYPASS）

`public_reachable` 今日对**所有** convert route 调用 `get_converter()`。  
Native 路径 **没有** project converter 注册时会误拒或误依赖。

**规范：**

```text
if route.transform_owner == LITELLM_NATIVE:
  # 不要求 get_converter；以 readiness(native) + capability 为准
elif route.transform_owner == PROJECT_ADAPTER:
  # 本期不应到达；若到达则要求 get_converter
```

### 5.4 Convert 成功路径

与 v0.2 相同：禁止项目 G0-B mutate；LiteLLM native 负责请求/响应形；callback sole accounting。

---

## 6. 额度组（P0-QG + P1-QG-ID）

### 6.1 绑定

| quota_group_id | api_key_env | plans |
|----------------|-------------|-------|
| `opencode-a` | `OPENCODE_GO_KEY_A` | `opencode-a-msg`, `opencode-a-chat` |
| `volc-c` | `VOLC_CODING_KEY_C` | `volc-c-msg` |
| `newapi-a` | `PLAN_NEWAPI_A_API_KEY` | `newapi-a` |

### 6.2 ID 与安全约束（S0 必须实现）

| 规则 | 要求 |
|------|------|
| 格式 | `^[a-z][a-z0-9-]{1,63}$`（小写字母开头；小写/数字/连字符） |
| 非法 | **`ConfigValidationError`**；**禁止** `ascii_safe()` 静默改写 qg / plan id |
| 缺省 | `quota_group_id` 省略 ⇒ `plan.id`（须已满足格式） |
| 同 key | 规范化后相同的 `api_key_env`（trim、大小写敏感按 POSIX env 名）出现在多个 enabled plan ⇒ **必须**相同 `quota_group_id`，否则校验失败 |
| 别名 env | **不支持**「两个 env 名指向同一密钥」的自动合并；文档禁止；若需合并须人工同一 `api_key_env` |
| deployment_id | 仍为 `{plan.id}-{model}`；同 qg 下允许不同 plan.id，故 deployment_id 自然不冲突 |
| 冲突 | 全局 `deployment_id` 重复 → 校验失败 |

---

## 7. 分类器契约（P0-CLF）— 钉死，禁止「或」

### 7.1 仅使用现有枚举

`FailureKind` **不新增** `PROVIDER_POLICY` / `REGION_BLOCKED` / `PROTOCOL_MISMATCH`（除非另开 enum ADR）。本期映射：

| Fixture（须入库） | 识别信号 | kind | scope | confidence | callback 动作 |
|-------------------|----------|------|-------|------------|---------------|
| OpenCode RegionError | status=403 **且** body `error.type==RegionError` 或 message 含 `requires explicit opt in` / `hosted in China` | `CONTENT_POLICY` | `DEPLOYMENT` | ≥0.9 | **deployment cooldown**（或 no-op + 指标）；**禁止**走 AUTH_INVALID 分支；**禁止** qg DISABLED |
| Messages 打 Chat-only 模型 | status=400，`invalid_request_error`，message 含 `Upstream request failed` | `BAD_REQUEST` | `REQUEST` | ≥0.85 | **不**熔断 qg；**不**必 cooldown（与现 BAD_REQUEST 一致） |
| 真鉴权失败 | status=401，或 403 **且非** RegionError 形态 | `AUTH_INVALID` | `QUOTA_GROUP` | ≥0.95 | 可 DISABLED qg |
| 额度耗尽 | 既有高置信文案 | `SHARED_QUOTA_EXHAUSTED` | `QUOTA_GROUP` | 高置信门槛 | mark_exhausted |

> 选用 `CONTENT_POLICY` 承载 RegionError：**仅为复用「不熔断账号」的 callback 分支**（与 CONTENT_POLICY/BAD_REQUEST 同属非 qg 熔断集合）。`normalized_message` 必须为 `region_blocked`，指标可按 message 区分。若实现上更干净，允许改 callback 使 `DEPLOYMENT_ERROR`+scope=deployment 进入 cooldown 路径——但 **kind 必须在 S0 PR 中写死一种并测死**，本文默认：

**最终钉死（实现必须二选一并改本文勾选）：**

- **选定：`DEPLOYMENT_ERROR` + `scope=deployment` + `confidence≥0.9` + `normalized_message=region_blocked`**  
- 并 **扩展** `_apply_classification`：对该组合执行 `set_deployment_cooldown`，**不** DISABLED qg。  

（优于挪用 CONTENT_POLICY，避免语义污染。）

### 7.2 Provider 分发

```text
callback.on_failure:
  classifier = registry.get(provider_id)  # opencode-go → OpenCodeGoClassifier
  默认 fallback GenericOpenAIClassifier
```

`OpenCodeGoClassifier`：**先**匹配 RegionError；再委托 generic 其余。

### 7.3 验收断言（不止「qg 未禁用」）

| 断言 | 要求 |
|------|------|
| A9a | 注入 RegionError 后 `sq:quota:opencode-a` status ≠ DISABLED |
| A9b | 存在 deployment 级 cooldown key（项目真实 key 名，如 `sq:deployment:opencode-a-chat-kimi-k3` 或 route-scoped convert key）被写入，**单测断言 key 与 TTL/字段** |
| A9c | 同 qg 下 `opencode-a-msg` glm deployment 仍可被 `resolve`/`get_available` 选中 |

---

## 8. A5 Wire Contract（P1-A5）

### 8.1 固定响应

| 项 | 值 |
|----|-----|
| HTTP | **400**（唯一；禁止「或」） |
| Content-Type | `application/json` |
| Body | `{"type":"error","error":{"type":"invalid_request_error","message":"<含 stream 与 unsupported/conversion 语义的英文或稳定字符串>"}}` |
| 禁止 | 顶层 `choices`；OpenAI error envelope；丢失 `type:error` |

### 8.2 测试要求

1. Helper 单测可保留，**不足够**。  
2. **必须**经真实（或 ASGI）`POST /v1/messages` 路径断言 status=400 与顶层 JSON。  
3. 若 LiteLLM `anthropic_endpoints` 会剥掉 detail：S0 须证明 wire 仍满足 §8.1，或增加项目层在 endpoint 前短接返回；**以 wire 为准**，helper 绿不算过。

---

## 9. 验收标准

### 9.1 功能

| ID | 期望 |
|----|------|
| A1–A3 | 同 v0.2（Anthropic shape；禁止 `choices`） |
| A4 | S1b |
| A5 | **HTTP 400** + §8.1 body；租约未创建（lease/inflight 无增量） |
| A6 | discovery **静态**列出已配置 `public_protocols`（**不**随 conversion flag 隐藏） |
| A7 | `PROTOCOL_CONVERSION_ENABLED=false` 时 kimi **运行时**不可达；discovery 仍可能列出（文档说明 A6≠运行时） |
| A8 | 仅一个 `sq:quota:opencode-a` |
| A9 | §7.3 |
| A10 | L2 后：native YAML false、env 已删、`resolve_route` convert 候选=0、**无** PROJECT_ADAPTER 候选 |

### 9.2 不变量

- 不改 `upstream/litellm` 业务逻辑（wire 修复优先在插件/配置；若必须碰 upstream 则另开 ADR）  
- 同 key 同 qg；非法 ID 拒绝  
- Messages→Chat：**native-only** path ready  
- C4：effective_stream 归一化  
- pytest 全绿  

### 9.3 回滚（修订 L2）

| Level | 动作 | 验收 |
|-------|------|------|
| L1 | `PROTOCOL_CONVERSION_ENABLED=false` + 重启 | convert 选路关闭 |
| **L2** | ① `apply` **无** `--enable-messages-chat-native` → YAML `false`；② **删除** native 相关 env；③ 重启；④ 确认 `g0a_mount_ready==False`（本期应为恒假）；⑤ 若未来有 G0-A：**L0 unmount** + 去掉 conversions / allow_conversion | A10 |
| L3 | restore `litellm.yaml` backup | — |
| L4 | 去掉 kimi public/convert policy | — |

---

## 10. Responses / Discovery 范围（P1-SCOPE）

| 项 | 本方案立场 |
|----|------------|
| C5 / Policy A | **不撤销**既有 `ADR-unified-public-responses`；glm Responses canary 与本 epic **正交** |
| 本 epic 交付 | 四模型 Anthropic Messages 分治；**不**要求关闭 Policy A |
| 负向测试 | S0 增加：开启 Messages→Chat native 时，**未** opt-in Responses 的模型仍 `protocol_not_enabled`；**不**因 native=true 而扩大 Responses 可达集 |
| A6 | **静态**配置声明 |
| A7 | **运行时**门控 / flag |

---

## 11. 分阶段落地

### Phase S0 — 唯一当前可批准阶段

- [ ] **P0-G0A：** Messages→Chat readiness = native-only；PROJECT_ADAPTER 恒关；L2/A10  
- [ ] **P0-CLF：** fixture + OpenCode 分发 + DEPLOYMENT_ERROR/region_blocked + cooldown key 断言  
- [ ] **P0-C4 + P1-C4-BYPASS：** effective_stream；native streaming=False；gate 不强制 get_converter(native)  
- [ ] **P1-SOT：** CLI `--enable-messages-chat-native`；generator；YAML false 不回退 env  
- [ ] **P1-QG-ID：** 字段 + 格式校验 + 同 key 同 qg  
- [ ] **P1-CAP：** features 必填 / 默认收窄  
- [ ] **P1-A5：** HTTP 400 wire 测经 `/v1/messages`  
- [ ] **P1-SCOPE：** Responses 负向测 + A6/A7 文档  
- [ ] 修复 2 个 Chat-only 失败测；`pytest -q` 全绿  

**S0 出口：** 上表全勾 + pytest 绿。**仍不**对真实流量开 conversion canary，除非另行书面批准 S1a。

### Phase S1a — kimi canary（S0 后另批）

- [ ] plans §4.2；CLI enable native；conversion on；staging  
- [ ] 冒烟 A1–A3、A5–A8、A10 演练  

### Phase S1b — deepseek  

### Phase S2 / S3 — hardening / 能力探针后开通 stream·tools（direct）

---

## 12. 明确非目标

- 本期启用 G0-A 作为 Messages→Chat 回退  
- 配置 `owner` 字段  
- 新增 FailureKind 而不改 callback（禁止模糊「或」）  
- 用拆 qg 隔离协议错误  
- 用 env=`false` 回滚 native  
- 省略 features 依赖旧 DEFAULT（stream/tools）  
- 本 epic 关闭 Responses Policy A  
- S0 未完成即 S1a  

---

## 13. AGENT 实现顺序

1. P0-G0A readiness 拆分  
2. P0-C4 stream 归一化 + registry  
3. P0-CLF classifier + callback cooldown  
4. P1-QG-ID + P1-CAP schema  
5. P1-SOT CLI/generator/feature_flags  
6. P1-A5 wire 测 + 修旧测  
7. 停：等待批准 S1a  

---

## 14. 决策记录

| 决策 | 选择 |
|------|------|
| Messages→Chat path | **Native-only**（G0-A 不计 ready） |
| RegionError | `DEPLOYMENT_ERROR` + deployment cooldown + `region_blocked` |
| Native 批准 | CLI `--enable-messages-chat-native` → YAML |
| YAML vs env | 已加载 YAML **false 压过**遗留 env |
| A5 | HTTP **400** + Anthropic error wire |
| Features | 必填显式 `[text]` |
| qg ID | 严格正则；拒绝静默改写 |
| Policy A | 共存，不在本 epic 撤销 |
| 上线 | **仅批 S0** |

---

## 15. 变更记录

| 版本 | 说明 |
|------|------|
| v0.1 | 初稿；误拆 qg；可直接 S1 |
| v0.2 | 同 key 同 qg；C4；删 owner；S0 后 canary |
| **v0.3** | **native-only path；分类器钉死；CLI SoT；A5 wire 400；features/qg 约束；C4 归一化；Policy A 范围；明确不批 S1a** |

**v0.3 批准含义：** 批准实现 **Phase S0 清单**；**不**批准 S1a/canary，直至 S0 出口条件满足并另行批准。

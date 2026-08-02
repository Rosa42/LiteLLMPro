# 需求说明：双协议公网 + 流式能力开通

| 项 | 内容 |
|----|------|
| 文档类型 | 产品 / 技术需求（开流 **准入门槛** + 开通范围） |
| 日期 | 2026-08-01 |
| 修订 | **v0.2.1**（lease 策略钉死 **R1 续租**） |
| 状态 | **Conditional No-Go — 暂不得进入实现**；须先关闭本文全部 **P0**，pytest 基线转绿，再批准单 deployment canary |
| 项目 | `local-llm-router`（LiteLLM Proxy pin `v1.90.5` + `shared_quota_router`） |
| 关联 | `docs/superpowers/specs/2026-08-01-model-split-anthropic-direct-and-convert.md`（v0.3）；`docs/conversion/streaming-evaluation.md`（C4 No-Go）；现网 S1b + 双协议 |
| 读者 | 协议 / 路由 / 运维 / 实现 AGENT |

---

## 0. 审查结论（v0.1 → v0.2）

| 项 | 裁决 |
|----|------|
| 总体方向 | **保留**：direct-only 开流；convert 禁流（C4）；分阶段 canary；双协议软件 A/B |
| 实现准入 | **Conditional No-Go**：v0.1 将正确性门槛留作「专家问题」或 P2 — **不可接受** |
| tools | **明确延期 S2b**；本需求不探针、不写入 `tools` |
| 基线 | 开流改动前必须 **pytest 全绿**（审查时曾 `3 failed, 265 passed, 1 skipped`，与本需求无直接关系亦须先清） |

### P0 清单（全部关闭前禁止实现开流）

| ID | 标题 | 一句话 |
|----|------|--------|
| **P0-FB** | 首字节后禁止切换 | 真实异步链路须证明：首个公网事件发出后不得 retry / fallback / 拼接其它 deployment |
| **P0-LEASE** | 流式 lease 正确性（**选定 R1 续租**） | 正常结束 / 中途异常 / 断连 / 取消 / 超时 / 重复回调 / 续租失败与绝对上限 均不泄漏、不双减 |
| **P0-SOT** | streaming 字段单一 SoT | `supported_features` 为作者侧 SoT；禁止 OR 语义导致「只关一个字段无法回滚」 |
| **P0-DEP** | 按 deployment 探针与开通 | 矩阵按 deployment 行；未通过的 deployment 不得因逻辑模型开流而被选中 |
| **P0-PROBE** | 双层探针、防假阳性 | 真实上游 probe ≠ ASGI mock；公网 E2E 另测；严格协议终止与 error-in-200 |
| **P0-WIRE** | 流中途错误 wire | Anthropic/OpenAI 流已 200 后的错误形态钉死；现有 `anthropic_wire` 异常处理器不够 |

---

## 1. 背景与现状（已落地，勿推倒）

### 1.1 已完成能力

| 能力 | 状态 | 说明 |
|------|------|------|
| 公网 Anthropic Messages | ✅ | `POST /v1/messages` |
| 公网 OpenAI Chat | ✅ | `POST /v1/chat/completions`（按模型 opt-in） |
| glm-5.2 / claude-opus-4-8 | ✅ | **仅** Anthropic；Messages **直连** |
| deepseek-v4-flash / kimi-k3 | ✅ | OpenAI Chat **直连**；Anthropic 经 **Messages→Chat native convert** |
| 同 key 同 `quota_group_id` | ✅ | OpenCode msg+chat → `opencode-a` |
| Convert 禁流（C4） | ✅ | convert + `stream=true` → 租约前 400 |
| 回滚演练 A7/A10 | ✅ | L1 conversion；L2 native |

当前各 plan：`supported_features: [text]`，`supports_streaming: false`。  
**非流式文本**双协议已可用于软件 A / B。

### 1.2 目标用户场景

| 软件 | 客户端格式 | 入口 | 主力模型 | 次要 |
|------|------------|------|----------|------|
| **软件 A** | OpenAI | `/v1/chat/completions` | `deepseek-v4-flash` | `kimi-k3`（可选） |
| **软件 B** | Anthropic | `/v1/messages` | `claude-opus-4-8`、`glm-5.2` | deepseek/kimi convert（**仅非流**） |

鉴权：允许同一 `LITELLM_MASTER_KEY`，靠路径区分协议。多虚拟 key 为可选增强。

### 1.3 已知实现缺口（审查证据，驱动 P0）

| 缺口 | 证据要点 |
|------|----------|
| 首字节边界未在 async chunk 路径成立 | Router 可对 `MidStreamFallbackError` continuation 拼接另一流；项目仅在 `async_log_stream_event` 置位，而 `CustomStreamWrapper.__anext__` 普通 chunk **不**触发该回调；合同测手工置位 |
| lease 非幂等 / 断连不释放 / TTL 与无限流不匹配 | release Lua 不校验 lease 归属即可 DECR；success/failure 双 hook 可能重复；客户端断连只清 LiteLLM parallel limiter；lease TTL=`request_timeout+30` 而默认可不限制流总时长 |
| streaming OR 语义 | `supports_feature(STREAMING) = supports_streaming OR STREAMING∈features` |
| glm 双 deployment | `opencode-a-msg` 与 `volc-c-msg` 独立；按逻辑模型开流会误伤未探针上游 |
| 流中途 Anthropic wire | `anthropic_wire` 管建立前 JSON；流中通用生成器可出 `data: {"error":...}`，非 Anthropic `event: error` |

---

## 2. 问题陈述

在 **不破坏** 双协议非流式的前提下，为 **已探针通过的 direct deployment** 开通流式，并保证：

1. 首字节后路由不变；  
2. lease / inflight 在所有终止路径正确；  
3. 能力按 deployment 声明与选路；  
4. 探针无假阳性；  
5. 流中途错误协议合法。

**在 P0 关闭前，禁止**：改 `supported_features` 含 `streaming`、宣称流式生产可用、进入实现 PR（除「关闭 P0 / 修基线」本身）。

---

## 3. 目标与非目标

### 3.1 目标

| ID | 目标 | 优先级 |
|----|------|--------|
| G1 | 软件 A：OpenAI Chat **按 deployment** 可承诺流式（探针通过后） | P0 |
| G2 | 软件 B：Anthropic **直连 deployment** 可承诺流式（探针通过后） | P0 |
| G3 | P0-FB / LEASE / SOT / DEP / PROBE / WIRE 全部有可自动化验收 | P0 |
| G4 | 未开通 / convert 路径拒流语义稳定 | P0 |
| G5 | pytest 基线绿 + 非流式双协议回归 | P0 |
| G6 | 软件 A/B 客户端配置说明（含流式限制） | P1 |
| G7 | 非正确性类运维硬化（仪表盘美化、Docker 文档等） | P2（**不含** lease/首字节） |

### 3.2 非目标

- Messages→Chat **convert 流式**（C4 维持 No-Go）。  
- **tools**（**S2b**）。  
- G0-A / PROJECT_ADAPTER 作 Messages→Chat 回退。  
- 撤销 Responses Policy A。  
- glm/claude 的 OpenAI Chat 公网（另立需求）。  
- 多租户计费 / UI。

---

## 4. P0 硬性需求（开流前必须关闭）

### 4.1 P0-FB — 首字节后禁止切换

**规范：**

1. **边界定义：**「首个公网可观察事件」已写入响应体（OpenAI：首个非空/有效 SSE data chunk；Anthropic：首个 SSE `event:`/`data:` 事件）的时刻为 `t_first_public`。  
2. **`t_first_public` 之前：** 允许在门控/选路策略范围内的失败重试（若现网策略已允许），但须可观测。  
3. **`t_first_public` 之后：** **禁止** 任何 retry、fallback、`MidStreamFallbackError` continuation、切换 `deployment_id`、拼接第二上游流。异常只能终止当前流并走 §4.6 wire。  
4. **边界写入：** 须在 **真实 async 消费路径** 上、与「事件即将/已经发给客户端」同步置位（不得依赖当前未触发的 `async_log_stream_event`  alone；不得仅靠测试手工置位）。

**验收（硬）：**

| ID | 要求 |
|----|------|
| A-FB1 | 合同/集成：注入中途故障后，日志或 trace 证明 **仅一个** `deployment_id` 被访问 |
| A-FB2 | 禁止路径：模拟 Router mid-stream fallback / continuation 时，shared-quota 层拒绝或短路，客户端看不到拼接的第二模型输出 |
| A-FB3 | 删除「消费 chunk 后手工 `mark_first_byte`」作为唯一证明方式；异步 `__anext__` 路径必须自动置位 |

### 4.2 P0-LEASE — 流式租约生命周期

**规范：**

1. **release 幂等：** 仅当 lease 存在且归属当前 `request_id` 时才允许 DECR inflight；重复 release / 错误 id **不得**使 inflight 少于真实持有数。  
2. **单一完成语义：** 无论 completion logging hook 与 proxy post-call hook 如何竞态，同一请求至多一次有效 release（或等价幂等）。  
3. **客户端断连 / 取消：** 必须触发 shared-quota release（不得只依赖 LiteLLM parallel limiter 清理）。  
4. **时长策略（已钉死）：方案 R1 — 续租**  
   - 流进行中按固定间隔（或等价心跳）延长 Redis lease / inflight TTL。  
   - **必须**配置**绝对上限**（wall-clock max stream duration）；触及上限 → 主动断流 + release + 合法错误 wire。  
   - 续租失败（Redis 错误 / lease 已不存在）→ 视为致命：断流 + 尝试幂等 release，不得静默继续占额度。  
   - **不采用 R2**（固定 TTL、不续租、仅靠初始超时掐断）。  
5. 默认「无限流 + 固定 TTL、不续也不断」**禁止**作为生产行为。

**验收场景矩阵（均须有测）：**

| 场景 | 期望 |
|------|------|
| 正常结束 | inflight 回到基线；无残留 lease |
| 中途上游异常 | 同上 + 正确 wire |
| 客户端断连 | 同上（有时限） |
| 取消 | 同上 |
| 续租成功使流超过初始 TTL | lease 仍有效直至结束或绝对上限；inflight 不因 TTL 误过期而泄漏或双减 |
| 触及绝对上限 | 断流 + release + 客户端可见终止/错误 |
| 续租失败 | 断流 + 幂等 release；无静默悬挂 |
| 重复回调 | inflight 不双减为负、不泄漏 |

### 4.3 P0-SOT — streaming 字段单一来源

**规范（钉死）：**

| 角色 | 字段 |
|------|------|
| **作者侧 SoT** | `supported_features` 是否包含 `streaming` |
| **生成兼容字段** | `supports_streaming` **仅由 generator 从 features 派生**；plans 手写若与 features 不一致 → **`ConfigValidationError`** |
| **运行时** | `supports_feature(STREAMING)` **只**认 `STREAMING ∈ supported_features`（废除 OR `supports_streaming`） |

**回滚验收：** 去掉 features 中的 `streaming` 并 apply 后：生成 YAML **同时**满足「features 无 streaming」且 `supports_streaming: false`；流式请求被拒；非流仍可用。

### 4.4 P0-DEP — 按 deployment 探针与开通

**规范：**

1. 路径矩阵 **以 `deployment_id` 为行**（不得仅按逻辑模型）。  
2. 仅 **探针通过** 的 deployment 获得 streaming capability。  
3. 同逻辑模型下，未通过的 deployment **仍可服务非流**；流式选路 **不得** 选中未通过者。  
4. deepseek/kimi：允许在同一 chat plan 内 **按 model** 覆盖 streaming（一模型通过不等于另一模型通过）。

**现网 deployment 行（开通前填探针结果）：**

| deployment_id | 公网协议 | 模型 | 模式 | 非流 | 流式 |
|---------------|----------|------|------|------|------|
| `opencode-a-chat-deepseek-v4-flash` | openai_chat | deepseek-v4-flash | 直连 | ✅ | 待 P0 后探针 |
| `opencode-a-chat-kimi-k3` | openai_chat | kimi-k3 | 直连 | ✅ | 待 P0 后探针 |
| `opencode-a-msg-glm-5.2` | anthropic_messages | glm-5.2 | 直连 | ✅ | 待 P0 后探针 |
| `volc-c-msg-glm-5.2` | anthropic_messages | glm-5.2 | 直连 | ✅ | 待 P0 后探针（独立） |
| `newapi-a-claude-opus-4-8` | anthropic_messages | claude-opus-4-8 | 直连 | ✅ | 待 P0 后探针 |
| （convert 路径） | anthropic_messages | deepseek / kimi | convert | ✅ | **禁止（C4）** |

### 4.5 P0-PROBE — 双层探针（防假阳性）

**禁止：** 仅 ASGI mock、或「至少一个 chunk」、或跨路径推断（Chat 能流 ⇒ Messages convert 能流）。

#### Layer 1 — 真实上游 deployment probe

- 直连该 deployment 的上游 base（脱敏记录）。  
- 验证 provider SSE、**增量时序**、合法终止。  
- OpenAI：合法 `chat.completion.chunk`、有效 delta、最终 `[DONE]`。  
- Anthropic：事件顺序合法，含 `message_stop`。  
- HTTP 200 体内 error → **失败**。  
- 记录：TTFE、总时长、事件数、上游模型名（无密钥）。

#### Layer 2 — 公网 Proxy E2E

- 经 LiteLLMPro 公网入口；验证门控、**实际选中的 deployment_id**、协议 wire、lease、断连、指标。  
- 与 Layer 1 分离；Layer 1 通过不等于 Layer 2 通过。

**现有脚本定位：** `_dual_protocol_smoke.py`、`probe_anthropic_support.py` 仅为 **非流式基线**；流式须新脚本/扩展，退出码与报告路径在实现计划中定义。

### 4.6 P0-WIRE — 流中途错误协议

| 公网 | `t_first_public` 前失败 | `t_first_public` 后失败 |
|------|-------------------------|-------------------------|
| Anthropic | 保持现网：HTTP 4xx + `{"type":"error",...}`（异常处理器路径） | **必须** SSE `event: error` + body `{"type":"error",...}`；**禁止**随后伪造 `message_stop`；**禁止**仅输出 OpenAI 形 `data: {"error":...}` 充数 |
| OpenAI | 现网 JSON error envelope | 钉死一种合法错误流形态（实现计划写死）；不得无结束地挂起 |

`anthropic_wire.py` 的 ProxyException 处理器 **不满足** 流中途要求；须另设计（中间件 / 包装 generator / 上游适配），优先插件层。

### 4.7 未开通 streaming 时的客户端行为（钉死）

直连 deployment **未**声明 `streaming ∈ supported_features`，且请求 `stream=true`（或等价 required feature）：

→ **硬拒绝**（HTTP 400 级门控），**禁止**静默降级为非流 200，**禁止**返回空流装成功。

convert + stream：维持 C4（已实现方向不变）。

---

## 5. 开通流程（P0 关闭之后）

```text
pytest 基线绿
  → 关闭 P0-FB/LEASE/SOT/DEP/PROBE/WIRE（代码+测）
  → 按 deployment 执行 Layer1 + Layer2 探针
  → 仅通过行写入 streaming capability
  → 单 deployment canary（建议先软件 A deepseek 或软件 B 单上游）
  → 扩大矩阵
```

tools：**不**进入本流程（S2b）。

---

## 6. 质量属性

| 类别 | 要求 |
|------|------|
| 安全 | 探针/日志禁止打印上游 key / master key |
| 回归 | 任一 deployment 开流后：非流式双协议冒烟仍通过；C4 负向仍通过 |
| 回滚 | 去掉 features.`streaming` + apply → §4.3 回滚验收 |
| 观测 | 最小字段：`protocol`、`stream`、`route_mode`、`deployment_id`、首字节标记 |
| 环境 | 默认 staging；生产另批 |

---

## 7. 验收总表

### 7.1 实现准入（Conditional Go 条件）

- [ ] pytest `-q` 全绿（清现有 3 fail 基线）  
- [ ] P0-FB / LEASE / SOT / DEP / PROBE 框架 / WIRE 契约测试齐全  
- [ ] 本文 §4 无悬空项（**R1 已选定**；绝对上限默认值在实现计划中给出并可配置）  
- [ ] 书面批准：**允许进入单 deployment 流式 canary**（非全矩阵）

### 7.2 Canary 出口（单 deployment）

- [ ] 该行 Layer1 + Layer2 记录通过  
- [ ] A-FB / lease 场景针对该路径抽测通过  
- [ ] 同逻辑模型其它未开通 deployment 不会被流式选中  
- [ ] 非流回归 + C4 负向通过  

### 7.3 明确不验收

- convert 流式成功  
- tools  
- glm/claude OpenAI 公网  

---

## 8. 依赖与约束

1. LiteLLM `v1.90.5`；优先插件/配置；碰 upstream 须 ADR。  
2. Messages→Chat：native-only；G0-A 不计 ready。  
3. C4：convert 禁流。  
4. 同 key 同 qg。  
5. 不得用 OR 语义双字段开流。  

---

## 9. 交付物

| 阶段 | 交付物 |
|------|--------|
| **Tasks 看板** | [`docs/superpowers/plans/2026-08-01-dual-protocol-streaming-tasks.md`](../plans/2026-08-01-dual-protocol-streaming-tasks.md) |
| P0 关闭 | 设计补丁 / 实现计划；**R1 续租 + 绝对上限**参数与合同测 |
| 探针 | Layer1/Layer2 脚本 + 报告模板 |
| Canary | 单 deployment 配置 diff + 冒烟记录 |
| 文档 | 软件 A/B 客户端说明（流式限制） |
| S2b（另文） | tools 探针与开通 |
| P2 | 与正确性无关的运维硬化清单 |

---

## 10. 变更记录

| 版本 | 说明 |
|------|------|
| 2026-08-01 v0.1 | 初稿；多项正确性留作专家问题 / P2 |
| 2026-08-01 v0.2 | **Conditional No-Go**；吸收审查六条 P0；deployment 矩阵；双层探针；流中途 wire；SOT/lease/首字节钉死；tools→S2b；基线转绿门槛 |
| **2026-08-01 v0.2.1** | **lease 时长策略选定 R1（续租 + 绝对上限）**；明确不采用 R2 |

---

## 11. 附录：现网只读参考

- plans：`opencode-a-msg` / `volc-c-msg` / `newapi-a` / `opencode-a-chat`  
- logical：glm/claude → `[anthropic_messages]`；deepseek/kimi → `[openai_chat, anthropic_messages]` + convert  
- 非流基线脚本：`scripts/_dual_protocol_smoke.py`、`scripts/_rollback_drill.py`  
- C4 评估：`docs/conversion/streaming-evaluation.md`  

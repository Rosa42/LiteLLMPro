# 进展总结：统一对外 API × 内部多协议供应商

**日期：** 2026-07-26  
**项目：** `E:\LiteLLMPro\local-llm-router`（LiteLLM pin `v1.90.5`）  
**读者：** 产品 / 运维 / 后续开发  
**目标一句话：** 对外只暴露一种（或少数）统一 API 格式；内部让 OpenAI Chat、Anthropic Messages、（未来）Responses 等不同协议的供应商共同提供服务，并按模型/配额自动路由。

---

## 1. 目标拆解（避免混为一谈）

| 层级 | 含义 | 示例（你的场景） |
|------|------|------------------|
| **A. 同协议多供应商** | 公网协议 = 上游协议，只做选路/配额/熔断 | `glm-5.2` → OpenCode Chat；另一 Chat 套餐回退 |
| **B. 异协议直连共存** | 公网仍按协议分流；Agent 按模型打不同入口 | Chat 打 glm；Messages 打 NewAPI opus |
| **C. 统一公网协议 + 内部转换** | 客户端只打一种格式，中转站转换后再打异协议上游 | 对外 Responses，背后调 NewAPI Anthropic |

**当前完成度粗估（相对目标 C）：约 45–55%。**  
相对目标 A：约 **90%+**（生产可用，缺个别供应商启用）。  
相对目标 B：约 **70%**（机制齐，Messages/Responses 供应商未验证启用）。  
相对目标 C：约 **25–35%**（仅有 Messages→Chat 试点骨架，默认关闭且有硬门禁）。

---

## 2. 已完成进展

### 2.1 直连协议网关 MVP（P0 → MVP-GATE）— **已通过**

| 能力 | 状态 | 说明 |
|------|------|------|
| 部署声明 `upstream_protocol` | ✅ | 配置级区分 Chat / Messages / Responses 能力 |
| 逻辑模型 `public_protocols` 显式 opt-in | ✅ | 不因模型名含 claude 就开放 Messages |
| 租约前按协议/特性过滤 | ✅ | 不会把 Chat 上游误当成 Messages 打 |
| 同模型多 Chat 部署自动路由 | ✅ | 如 OpenCode 优先、Volc 回退（priority/配额/熔断） |
| 协议感知错误与观测 | ✅ | no-route 不污染配额电路；route/reject 指标 |
| 功能开关回滚 | ✅ | `PROTOCOL_AWARE_GATEWAY_ENABLED` |
| 测试基线 | ✅ | 全量约 **197** pytest 绿（含 conversion 单测） |

**现网实际：** OpenCode / Volc 均为 `openai_chat`，逻辑模型只开放 Chat。  
**NewAPI：** `enabled: false`，未声明协议 → **尚未参与服务**。

### 2.2 跨协议转换 epic（C0–C-CLOSE）— **已关闭，但生产默认不用**

| 项 | 状态 | 说明 |
|----|------|------|
| 转换契约 / fidelity 矩阵 | ✅ | `conversion/contracts.py` |
| 配置 `allow_conversion` + `conversions` | ✅ | fail-closed 校验 |
| `resolve_route`：direct ≻ convert | ✅ | |
| 试点适配器 Messages→Chat（非流式文本） | ✅ 代码在 | `PROTOCOL_CONVERSION_ENABLED` 默认 **false** |
| 转换路径熔断隔离（C3） | ✅ | convert cooldown 不毒化同部署 direct |
| 双 flag AND | ✅ | gateway ∧ conversion 才真正选 convert |
| 流式转换（C4） | ❌ **No-Go** | 保持 unsupported |
| 公网 Responses 直连（C5） | ❌ **No-Go** | 无 verified Responses 上游 |
| Responses↔任何协议转换 | ❌ **未做 / 禁止** | 与「对外 Responses、背后 Anthropic」直接相关 |

证据：`docs/tasks.md` §0；`docs/phase-reports/conversion-*.md`；残余风险见下。

---

## 3. 距离「统一对外 + 内部多协议共同服务」还差什么

按你关心的终态（**对外统一格式，内部 Chat + Anthropic 等一起干活**）拆缺口：

### 3.1 缺口地图

```text
                    ┌─────────────────────────────────────┐
  客户端期望        │  统一公网 API（如只暴露 Chat 或        │
                    │  只暴露 Responses）                    │
                    └─────────────────┬───────────────────┘
                                      │
                    ┌─────────────────▼───────────────────┐
  中转站缺口        │  ① 选定并启用「对外统一协议」         │
                    │  ② 转换层（请求/响应/错误/用量）       │  ← 最大缺口
                    │  ③ 按模型选 upstream（Chat vs Anthropic）│
                    │  ④ 流式 / tools / 高保真（按需）        │
                    └─────────────────┬───────────────────┘
                                      │
              ┌───────────────────────┼───────────────────────┐
              ▼                       ▼                       ▼
        OpenCode Chat            NewAPI Messages         （未来其它）
        glm-5.2 ✅ 已通          opus ⚠️ 未启用验证        Responses ❌
```

### 3.2 分档差距（可执行）

| 阶段 | 交付物 | 相对终态 | 预估工作量* | 阻塞 |
|------|--------|----------|-------------|------|
| **P0 立刻可用** | 同 session 换 model：glm 走 Chat、opus 走 Messages（Agent 双协议） | 达目标 **B**，非统一入口 | 小：启用并验证 NewAPI | NewAPI 协议探测 + plans 启用 |
| **P1 准统一** | 对外只暴露 **Chat**；Chat 上游直连；Anthropic 上游靠 **Messages→Chat 转换**（非流式文本） | 部分达目标 **C**（仅 Chat 统一） | 中：清残余风险 R1–R3 + 门控谓词 R2 + 预发探针 | G0-B 是否误打 `/responses`；conversion-only 门控 |
| **P2 真·统一 Responses** | 对外 `/v1/responses`；内部 Chat / Anthropic 均可服务 | 完整目标 **C**（若选 Responses 为对外标准） | **大**：Responses 直连启用 + **新转换 epic**（Responses↔Chat / Responses↔Messages）+ 流式评估 | C5 No-Go；转换未立项；流式 C4 No-Go |
| **P3 生产级转换** | tools / reasoning / streaming / 错误形塑齐全 | 生产可承诺统一 API | 大：按 fidelity 矩阵逐项做 | 当前矩阵多项 unsupported |

\*工作量仅相对本仓库现状的量级判断，非正式排期。

### 3.3 硬阻塞清单（转换上线前必须清）

摘自 `docs/phase-reports/conversion-residual-risks.md`：

| ID | 严重度 | 内容 |
|----|--------|------|
| **R1** | Fatal | Live 未证明：Messages 入口选 `openai/` Chat 部署时上游真是 `/chat/completions`（P0 有误打 `/responses` 证据） |
| **R2** | High | M3 仍要求 Messages **直连** verified；纯 conversion-only 公网进不去 |
| **R3** | High | 响应改写依赖 post_call 返回值，live 客户端是否收到 Anthropic 形未证明 |
| R4+ | Med/Low | affinity 未带 route_mode；规格边角；C4/C5 再开时要硬化验收 |

**在 R1–R3 清除前：禁止 staging/prod 打开 `PROTOCOL_CONVERSION_ENABLED=true`。**

---

## 4. 对照你的业务例子

| 诉求 | 现在能否 | 说明 |
|------|----------|------|
| Session 里用 OpenCode `glm-5.2` | ✅ | Chat 直连已通 |
| Session 里用 NewAPI `opus-4.8`（Anthropic 原生） | ❌ 配置未启用 | 启用后可走 **Messages 直连**（目标 B） |
| 同一 Agent **只打一种** OpenAI 协议，同时用到 glm + opus | ❌ | 需要转换（目标 C）；Responses 路径尤其没有 |
| 对外 Responses，背后调 NewAPI Anthropic | ❌ | 必须 Responses→Messages 转换；**未实现** |

**推荐近路（不依赖转换）：**  
Agent 对 `glm-5.2` 走 Chat，对 opus 走 Messages；中转站两边直连。这能最快实现「一个 session 两种模型」，但**不是**「对外统一单一 API 格式」。

---

## 5. 完成度一览（看板级）

| 波次 | 状态 |
|------|------|
| P0 兼容 / G0 集成边界 | ✅ DONE（G0-B） |
| M1 配置与发现 | ✅ DONE |
| M2 协议路由与租约不变量 | ✅ DONE |
| M3 Chat 启用；Messages/Responses 门控 | ✅ DONE（后两者默认关） |
| M4 观测与开关；MVP-GATE | ✅ PASSED |
| C1–C3 转换契约 / 试点 / 熔断隔离 | ✅ 代码 DONE，**运行默认关** |
| C4 流式转换 | ✅ 评估完 = **No-Go** |
| C5 Responses 直连 | ✅ 评估完 = **No-Go** |
| 统一对外 + 转换共同服务（终态） | ⬜ **未达** |

详细任务板：`docs/tasks.md` §0。  
运维：`docs/operations-protocol-gateway.md`、`docs/operations-protocol-conversion.md`。  
启用 Messages：`docs/enabling-messages-responses.md`。

---

## 6. 建议的下一决策（选一条主路径）

1. **先要混用模型、可接受双协议客户端** → 启用并验证 NewAPI Messages；保持转换关闭。  
2. **坚持对外只暴露 Chat** → 立项清 R1–R3，再小流量开 Messages→Chat 转换；NewAPI 若只有 Anthropic，opus 走转换或要求 NewAPI 提供 Chat。  
3. **坚持对外只暴露 Responses** → 新开 epic（Responses 直连 + 转换），工作量明显大于 2；当前文档应视为 **未开始**。

---

## 7. 一句话结论

**同协议多供应商自动路由已基本落地；异协议供应商「按入口直连共存」只差 NewAPI 等验证启用；「对外统一一种 API、内部靠转换喂多协议供应商」仍处试点+门禁阶段，距生产可用大约还差一整段转换 hardening +（若选 Responses）一整条新 epic。**

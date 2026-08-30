# 同权 IMM 重思考 — 发散稿（非最终版 / 不可施工）

| 项 | 值 |
| --- | --- |
| 文档类型 | 想法延申 / Adjust-4 + Feas-4 |
| 日期 | 2026-08-31 |
| 状态 | **发散稿 v0.5**。未过隔离审查；**不是**规格，**不得**按本文编码 |
| 本轮 | 网关把 **原来的一轮模型调用一拆为三**；对 AGENT **仍是一个模型、一次交互**；summary 的 **返回格式 = 标准 Messages 助手消息** |

维护方案仍禁止按本文施工。

---

## 0. 分层用语（本轮锁定）

| 名称 | 是什么 | 不是什么 |
|------|--------|----------|
| **对外一轮** | 客户端一次 `POST`（如 `/v1/messages`）→ 一次标准助手消息（可含标准 `tool_use`） | AGENT 自己打三路；专有 summary JSON/信封 |
| **对内三路** | 网关在选号成功后、**用户可见 execute 之前**，用当前上下文打 biz/tech/homolog 三次（无 tools） | nested select 换额度组；译图那种异模型子选号 |
| **融合 = 原 execute 槽** | 第四次 HTTP 就是 LiteLLM 本来那次执行：注入三路短结构后出发；流式只来自这次 | 再发明一种客户端协议；把三路原文拼进用户流 |
| **标准格式** | 与今天 MiniMax Messages 助手消息同形（`content` 文本块 / `tool_use` / 后续 `tool_result` 轮） | `to_fetch[]` 出现在 **对线** 上 |
| **`to_fetch`** | 仅融合前的 **内部** 混合态 | 要求 OpenCode 解析私有标签才去 Read |

SE 三模态、子调用禁工具、位点默认 `unverified`：沿用 v0.4。

---

## 1. 目标 / 非目标

**目标**

1. AGENT / 工具运行时的模型面：**一个逻辑模型、一次请求–响应**（后续 `tool_result` 仍是今天那种第二轮 HTTP，不是三模型）。
2. 网关把这一轮的 **思考带宽** 拆成三路定制 `system`，再融进 **同一条** 标准助手消息。
3. 三路只基于当前 `messages`；需要看的文件通过 **标准 tool_use**（若本轮请求带了 tools）交给 AGENT，而不是私有清单。
4. 用户可见流 **只** 来自融合/execute（`pipeline.md` 不变量 7）。

**非目标**

- 轮廓 **R 作主路径否决**：技能侧打三路会破坏「一次交互」。
- 对线专有 summary schema。
- 三路 worker 上带 tools（会变成三次工具交互，AGENT 面就不干净）。
- 静默、不可关地把所有 `MiniMax-M2.7` 流量加厚（与「不把 glm-5.2 偷成门面」同类；**逻辑名 vs flag 未选**）。

---

## 2. 调用时序（对外 1 = 对内 3+1）

```
客户端 POST /v1/messages     ← AGENT 只看见这一次
  选号 MiniMax（Fill First / lease 不变）
  Stage：并行或串行 3× 同部署 POST（无 tools，当前上下文 + 各 system）
  把短结构注入 messages（不对用户流式打出 worker 原文）
  LiteLLM 出发 execute     ← 唯一用户可见上游；即融合
  返回标准助手消息
AGENT 按标准协议执行 tool_use（若有）
再 POST（messages 含 tool_result）← 仍是「同一个模型」；是否再拆三路见 §7
```

「一拆为三」不是把对外三次计费成一次，而是 **思考拆三、出口仍一**。内部是 3 次 worker + 1 次 execute。不变量 7：三路必须在 execute **之前** 结束。

流式：只流 execute。首 token 延迟 ≈ 最慢 worker + 融合起速。OpenCode `stream=true` 仍建议不当第一刀验收，但 **对线形状** 必须能走非流式标准 Messages。

---

## 3. 与前几轮的衔接

| 前轮 | 本轮如何落地 |
|------|----------------|
| 定制 prompt 三方向 | 三次内部 POST 的 `system` |
| 当前上下文、不扫仓 | worker 无工具、无 grep |
| summary 后 AGENT 读文件 | 融合进 execute，**用标准 tool_use** 表达待读；不是私有 `to_fetch` 对线 |
| 延迟新息 | 真值在 **下一轮** `tool_result` 里，不在本轮 worker 里 |

v0.4 的「观测任务书」改为：**内部** 任务书 → **外部** 标准助手消息（文本说明 + 可选 tool_use）。AGENT 不必知道发生过三路。

### 3.2 混合态

`to_fetch[]` 仍内部。融合 prompt 必须：有 tools 且存在 unverified 位点 → **优先发标准 Read/Grep 类 tool_use**，禁止只在散文里写「请自行打开某文件」（那会变成两次交互才读盘，违背本轮）。

无 tools 的纯聊天：只能文本列出疑点，无法「显式获取」——见 §7 旁路，本问不抢。

---

## 4. Feas-4

| # | 新假设 | 风险 | 测法 | 结果 | 证据 |
|---|--------|------|------|------|------|
| E1 | 选号后、execute 前可以打多次上游 HTTP | 中 | 视觉阶段 | ✅ 形状 | `vision_compose.py` 译图 httpx 后剥图，再让 LiteLLM 打 execute |
| E2 | 同 MiniMax 额度组可用 nested `select_internal_deployment` | 致命 | `assert_quota_exclusive` | ❌ | 父子同组失败。三路必须 **复用已选 deployment 的 api_base/key**，新路径，尚无现成函数 |
| E3 | AGENT 编排三路仍算「一次交互」 | 高 | 用户本轮 | ❌ | 主路径否决 R |
| E4 | 对线输出私有 summary 对象 | 高 | 「标准格式」 | ❌ | 必须是 Messages 助手消息 |
| E5 | worker 带上客户端 tools | 高 | 「一次交互」 | ❌ | 三次 tool_use 竞争；D2 仍成立 |
| E6 | ContextVar 子调用会再拆三路 | 中 | pipeline 不变量 4 | ✅ 可挡 | worker 走 `trusted_internal` 则不再进增强阶段，避免 3×3 |

架构可行性（对外 1 / 对内 3+1）：**通过**（E2 是实施缺口不是物理否决）。  
实施就绪：**否**。

否决：R 主路径；对线私有 schema；worker tools；用 nested select 打 `minimax-official`。

---

## 5. 轮廓

| 轮廓 | 本轮 |
|------|------|
| **P** | 网关拆三 + 原 execute 融合。**主路径候选**。逻辑名仍可能是 `MiniMax-M2.7`（须 flag，不可关则否决） |
| **Q** | 同 P，但显式逻辑名（如 `MiniMax-M2.7-imm`）。AGENT 仍一次交互，只是选了另一 id |
| **R** | **否决为主路径**（AGENT 会看见三路或自建编排） |

P vs Q 未选。

---

## 6. 实施切片

无。欠：`tool_result` 后续轮是否还拆三；P 的 flag vs Q 的新 id。

---

## 7. 下一问

对外一轮已经收窄。下一轮 HTTP（messages 里带上 **本轮 tool_result**）若仍一律拆三，费用是每工具轮 4×。

---

## 8. 方案变更

### Change 10: 挂载
- Before: P/Q/R 未选；R 更贴读盘。
- After: **网关主路径**（P 或 Q）；R 否决为主路径。读盘仍是 AGENT 标准工具，发生在 **标准** 助手消息之后。
- Why: 「对 AGENT 相当于一个模型一次交互」。

### Change 11: 出口格式
- Before: summary 可以是观测任务书（内部结构可能对线）。
- After: 对线 = 标准 Messages 助手消息；`to_fetch` 只内部，有 tools 时融进标准 `tool_use`。
- Why: 「summary 返回格式与标准格式一致」。

### Change 12: 3+1 而不是只 3
- Before: 未写融合落在哪次 HTTP。
- After: 三路 worker + **原 execute 槽当融合**；用户流只来自 execute。
- Why: 不变量 7；E1 有视觉先例。

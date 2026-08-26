#  请求增强网关 — 设计提案


| 项       | 值                                                                      |
| ------- | ---------------------------------------------------------------------- |
| 文档类型    | **设计提案**（Design Proposal）                                              |
| 状态      | **方向通过；挂点与 IMAGE 时序已闭合（S1 / S2 / S5）。** pre-call 剥图仍不可用。施工方案已拟定。Q1–Q6 已冻结。未写规格之前不实现 MiniMax 翻译。 |
| 日期      | 2026-08-21（2026-08-24 探针；2026-08-25 挂接施工方案）                          |
| 读者      | 本仓库后续实现与评审（含 AGENT）                                                    |
| 实现落点    | `local-llm-router/plugins/shared_quota_router/`                        |
| LiteLLM | 钉死 v1.90.5；不改 `upstream/litellm` 业务逻辑                                  |


本文件把「可插拔模块 + 多模型组合 + 网关层共享记忆」整理成一份可评审的提案。它不是施工清单。

**施工门槛：** 核心原则与现网关纪律一致。P0：A PASS、B FAIL（pre-call）、S1/S2/S5 PASS。`MiniMax-M3` 已声明 `image`。生产配置**未**新增 `glm-5.2-vision`。施工方案：[`plans/2026-08-25-vision-and-memory.md`](./plans/2026-08-25-vision-and-memory.md)。S5 stub 剥图不是配方；未写规格之前不实现 MiniMax 翻译。

评审处理原则：只吸收与现有额度内核、fail-closed / 禁止静默降级纪律相容的建议；研究型条目记入 §17。

---

## 0. 这份文档为什么叫「设计提案」

按本仓库已有文档分工：


| 名称         | 何时用                      | 本仓库里的例子                                     |
| ---------- | ------------------------ | ------------------------------------------- |
| **设计提案**   | 想法已成形、方案未冻结，用来对齐动机、边界、取舍 | **本文**                                      |
| 规格（spec）   | 提案通过后，把单一子系统写成可验收的接口与行为  | `docs/superpowers/specs/`                   |
| 实现计划（plan） | 规格可施工后，拆任务与顺序            | `docs/superpowers/plans/`、`docs/分阶段开发方案.md` |
| ADR        | 一条架构决策已经采纳，记录「选了什么、为什么」  | `local-llm-router/docs/adr/`                |
| 架构摘要       | 描述**已经实现**的系统            | `local-llm-router/docs/architecture.md`     |


不叫 PRD：这里的核心是网关如何改请求，不是产品功能清单。  
不叫 RFC：可以当 RFC 用（征求意见），但本仓库更常见的是提案 → spec → ADR。  
不叫 ADR：模块落地时再为「G0-B 上挂流水线」「合成模型不是 fallback」等单点决策补 ADR。

---



## 1. 动机

本地中转站今天解决的是：**同一逻辑模型、多个 Coding Plan 账号、按额度组熔断与切换**。客户端（OpenCode、Cursor 以及其它走 OpenAI / Anthropic 兼容接口的本地 AI app）只看见 `glm-5.2`、`MiniMax-M3`、`claude-opus-5` 这类名字。

这一层已经可用，而且必须保持完整：关增强、卸模块之后，额度路由、协议门控、流式首字节纪律应与现在行为一致。

接下来要补的是另一类能力，且必须能**增量引入、按模块关掉**，而不是改写现有架构：

1. **模块可增减。** 记忆、视觉翻译、以后的脱敏或预算裁剪，都应当像插件一样挂上或卸下。卸掉某一个模块，不得要求改额度核心，也不得让其它模块无法运行。现有框架的完整性是硬约束，不是「尽量兼容」。
2. **多模型组合使用。** 单个上游模型很难同时是「最强文本推理」和「能看图」。网关应能按配方把多个模型**智能路由并串联**：例如视觉模型先把图片翻译成文本模型能消费的表示，再交给强文本模型做逻辑推理与执行。组合是垂直的能力编排，与现有「同一模型名、多个账号」的水平额度路由正交。
3. **记忆上升到网关层。** 今天各种记忆都局限在单一软件里。凡是本地把这台网关当作模型入口的 AI app，应共享**同一份**网关记忆，而不是 OpenCode 一份、Cursor 一份。

这三件事共享同一位置：请求在离开网关、到达用户选定的执行模型之前，可以被检查、改写、补上下文。额度路由仍然只负责选哪个账号；增强层负责「这次请求要经过哪些模块、要不要串联其它模型」。

---



## 2. 目标与非目标



### 2.1 目标

1. **增量模块。** 增强能力以独立模块（阶段）存在：可单独开启、关闭、测试、回滚。关闭全部增强模块后，行为回到当前额度 + 协议网关。增加模块**不得改变额度选号语义**（`strategy.py` 的 Fill First / affinity / tried / lease）。挂载必然要接到现有 callback、协议扫描和配置系统，不要求「零改动其它文件」。
2. **多模型组合。** 第一份配方是「视觉翻译 + 文本执行」，对外合成逻辑模型 `glm-5.2-vision`。V1 **仅** `anthropic_messages`（与当前 `glm-5.2` / `MiniMax-M3` 的 `plans.yaml` 一致）。
3. **网关级共享记忆。** 同一工作区下，经本机网关的 AI app 共享一份记忆。
4. **客户端协议。** 合成模型 V1 只服务 Messages。Chat / Responses 不是本配方的 V1 范围（Responses 仍为项目 No-Go）。OpenCode 不必为正确性改本地会话存储。



### 2.2 非目标（含评审后明确拒绝）

- 不把现有 `glm-5.2` 偷偷改成「有时能看图」。无图请求必须保持原延迟与原扣费。
- 不做默认跨模型降级（例如失败后 Kimi→GLM）。组合配方是显式配置的串联，不是额度耗尽后的 fallback。
- 视觉翻译失败时**不**用占位描述 / 空译文糊弄执行模型。那是静默语义降级。熔断只允许更快地返回明确错误。
- 不把网关记忆当成会话压缩器或聊天记录备份。
- 不把完整 prompt、API Key、图片 base64 写入记忆库。
- 不在第一期做视频理解、computer-use 点击、像素级还原、自然风景 / 人像 / 手写。
- 不为增强层另起一套 HTTP / SSE 前置网关（保持现有 G0-B 边界）。
- 不修改 `upstream/litellm` 业务代码。
- 不要求关闭 Cursor / OpenCode 自带记忆；网关记忆是额外的共享层。
- 不在 V1 把合成模型做到 Chat 或 Responses。当前 `glm-5.2` 仅 `public_protocols: [anthropic_messages]`；Responses 为受控 No-Go。Cursor 若只走 OpenAI Chat，V1 不宣称支持。
- V1 不做：DAG 编排、图像语义近似缓存、把图像 embedding 塞进文本模型、HyDE、WASM/eBPF 插件、管道热重载、跨额度组的「预留 15% 配额」、OpenTelemetry 作为必选依赖、metrics histogram。

---



## 3. 核心想法（从直觉到可执行）



### 3.1 原始表述

> 实现类似于记忆机制模块的增量化引入（可不影响原来框架或者架构完整性的情况下增减模块），同时实现多模型组合使用（比如说结合视觉大模型和文本大模型，做智能路由，串联使用）。暂时想到的具体场景是通过视觉大模型对图片做翻译，使得可以即保留强能力文本大模型的逻辑推理能力，又使得模型具备识图能力，能利用上图片的能力。引入记忆模块，使得可以使得记忆机制上升到网关层，所有本地使用该网关模型的 ai app 都共享一份记忆。



### 3.2 三条原则

**模块增减不破坏原架构。** 现有额度路由、协议门控、lease、熔断、探测恢复是稳定内核。增强模块只通过信封与内核对话。关掉记忆，视觉组合仍可用；关掉视觉，记忆仍可用；两个都关，等于今天的网关。禁止为某个模块改选号语义；选号后改 `request_kwargs` 不在此禁令内。

**组合是「翻译再执行」，不是「给文本模型装眼睛」。** 视觉模型把像素翻译成文本模型的母语（结构化标记 / OCR / 语义草图）。文本模型始终看不到像素，但能基于译文做原来擅长的推理与改代码。系统上限由译文质量决定。对外诚实：合成模型广告 vision；对内在交给文本模型前剥图。这是 late fusion（先专模块再文本），与 Visual ChatGPT / HuggingGPT 同类；本网关的执行模型是纯文本，early fusion / 把图像向量注入 GLM 做不到，也不做。

**记忆属于网关，不属于某一个 app。** 只要请求打到这台网关，就走同一套检索 / 注入 / 写入。用工作区隔离内容，不用「来自 OpenCode 还是 Cursor」当主键，否则又会回到软件孤岛。无法判定工作区时**不检索**，不用全局记忆兜底。

另外两条执行纪律：

- **改的是当次上游请求，不是客户端历史。** 每一轮都必须访问历史上每一个 image block 并替换；哈希缓存跳过的是视觉模型调用，不是扫描。
- **中间表示用结构化标记，不用散文 caption。** 视觉模型不得输出可交付网页。

---



## 4. 两层路由，互不替代

必须把两种「路由」分开，否则会和仓库里「禁止默认跨模型降级」打架。


|      | 额度路由（已有）                      | 组合路由（本提案）                |
| ---- | ----------------------------- | ------------------------ |
| 问题   | 同一个逻辑模型，选哪个账号                 | 这一跳能力不够，要不要先经过另一个模型      |
| 方向   | 水平：多账号、同模型名                   | 垂直：多模型、不同职责              |
| 触发   | 额度、熔断、亲和                      | 请求内容（例如有图）+ 配方配置         |
| 失败   | 换账号，不换模型（默认）                  | 翻译失败则明确拒绝；不换执行模型，不输出假译文  |
| 代码位置 | `strategy.py` / store / lease | 流水线阶段；阶段内部可再调一次额度路由去打子模型 |


智能路由在本提案里的含义是：**按内容决定走哪条配方、跳过哪些模块**，而不是「M3 挂了就改用 Kimi」。例如：无图则跳过视觉翻译；记忆模块关闭或工作区未知则跳过检索；请求打到纯 `glm-5.2` 则整条组合配方都不跑。

翻译子调用走现有额度路由：M3 的 429 / 超时 / 熔断沿用 deployment cooldown 与 quota_group 状态机。不要在视觉模块里再造一套与内核冲突的「账号级熔断」。模块级熔断只表示：**本阶段连续失败后快速拒绝合成模型请求**，不再空等视觉超时。

子调用隔离（实现正确性前提，不是细节）：`strategy.py` 的 `_CTX_BY_REQUEST_ID` 按 `litellm_call_id`（及 fallback id）做进程级缓存。视觉 / 记忆抽取若复用父请求的 `litellm_call_id`，`mark_tried` 与 `first_byte_sent` 会写进父请求的 `RequestRoutingContext`。子调用必须合成独立 id，例如 `{parent}#vision:{hash8}`、`{parent}#memory-extract:{hash8}`，并自建独立 context，禁止把父 ctx 传入子路由。

---



## 5. 总体架构

增强层挂在**选号之后、出发 HTTP 之前**：改的是 `request_kwargs["messages"]`（及若不同对象的 named `messages`）。内核选号语义不变。合成模型在 pre-call **推迟 IMAGE 能力检查**，否则带图请求到不了这个挂点。

```text
本地 AI app（OpenCode / Cursor / 其它）
        │  逻辑模型名；messages 里可能带原图
        ▼
LiteLLM Proxy  pre-call hook
        │  注入协议 metadata（G0-B：raise 与 metadata 仍有效）
        │  现有协议 / 特性门控
        │    合成模型：推迟 IMAGE（S5）
        │    纯 glm-5.2 带图：FEATURE_UNSUPPORTED
        ▼
额度路由 select（strategy.get_available_deployment）
        │  为本跳执行模型选账号；不改 Fill First / affinity / tried / lease
        │
        │  请求增强 = 选号后改 request_kwargs；可整段关闭
        ├─ 建信封
        ├─ 解析 workspace scope（未知则记忆跳过）
        ├─ [模块] 视觉翻译 / 剥图   配方命中且有图；可卸；fail-closed
        ├─ [模块] 记忆检索         有 workspace 才检索；可卸；超时 fail-open
        ├─ [模块] 上下文预算       只裁注入段；可卸
        │
        ▼
上游执行模型（用户看见的流只来自这里；出发 body 已无 image）
        │
        ├─ 流式首字节门（内核）
        └─ [模块] 记忆抽取（异步；脱敏后才入库；可卸）
```

V1 流水线是**声明的线性顺序**，不是 DAG。记忆检索依赖视觉译文，必须在翻译之后、出发 HTTP 之前；无图时翻译是空操作，记忆仍可跑。PII 扫描若将来要做，作为记忆写入路径上的步骤，不必与翻译并行抢首字节。并行 DAG 留到真有两个互不依赖且都挡首字节的模块再加。

视觉翻译模块内部会**再走一次**额度路由去调用视觉模型。子调用有自己的 quota group、lease、tried-set。子调用耗尽不得把执行模型账号标成 `SHARED_QUOTA_EXHAUSTED`。二者额度本来就分开，不存在「从 GLM 额度里预留 15% 给识图」这种跨组预留。

### 5.1 挂点证据（不要再把 pre-call 当剥图点）

曾意向：`async_pre_call_hook` 里、`enforce_pre_call_gates` **之前**改 `data["messages"]`，让 IMAGE 门控看到已剥图的请求。

**探针 B（2026-08-21）FAIL。** live `POST /v1/messages` 打 `MiniMax-M3`：客户端 body 不含 marker，助手原文为 `pong`。pre-call 对 `data["messages"]` 的改写**没有**到达 Messages 上游。该挂点不能用来做 V1 剥图。证据：[`reports/p0-probe-b.md`](./reports/p0-probe-b.md)。

已证明的路径：

| 项 | 结论 | 证据 |
| --- | --- | --- |
| pre-call 改 `data["messages"]` | **不到**上游 | 探针 B FAIL |
| 选号后改 `request_kwargs["messages"]` | **到**上游 | S1 live MiniMax 回显；S2 mock `probe_marker_hit` 对照 |
| 合成模型推迟 IMAGE + 选号后剥图 | **出发 HTTP 无图**；纯 `glm-5.2` 带图仍 400 | S5 stub 剥图（非 MiniMax 翻译） |

其它仍成立：

- G0-B：pre-call **就地写入 metadata** 能进 strategy；门控 **raise** 能挡住请求。
- 「raise 生效」≠「mutate `data["messages"]` 会到达上游 HTTP」。
- C2 的 S3「hook return may be discarded」针对 **post_call 返回值**，不要和 pre-call 请求体混为一谈。
- Chat 改写仍属未证明，**不得**据此实现 Chat 合成模型。

S5 stub（`S5_STUB_PEEL`，默认关）只证明剥图挂点。无 stub 时合成模型带图必须 fail-closed，禁止把像素送给执行模型，也禁止空译文糊弄。真正的 MiniMax → `<visual-evidence>` 翻译仍待规格。

观测：复用现有 `metrics.py`（仅 counter / gauge）。V1 阶段耗时记 **计数 + 该进程内 max**（或日志毫秒），**不**为 P95 给 metrics.py 加 histogram，也不引入 OpenTelemetry。日志带父 `request_id` + 子调用独立 id + `stage`。

---



## 6. 增量模块：如何增减而不伤内核



### 6.1 不变量（验收用）

1. 关闭全部增强 flag 后，现有单测与契约测试全绿，协议与额度语义不变。
2. 关闭记忆模块，视觉翻译仍可运行；关闭视觉模块，记忆检索仍可运行；二者无硬依赖。
3. 新增模块不得改变额度选号语义。允许改 callback 挂载点、协议扫描（如递归 `tool_result`）、`plans.yaml` / discovery schema。允许在 `get_available_deployment` **选号成功之后**改 `request_kwargs`（与 C2 convert 同挂点）。禁止为某个模块改 Fill First / affinity / tried / lease。V1 注册表是声明式有序列表 + `Stage` protocol，不做动态插件加载或依赖图。
4. 失败策略写在模块自己身上：视觉 fail-closed；记忆 fail-open。内核 Redis fail-closed 不延伸到记忆。
5. 模块不得在用户可见首字节之后改上游或拼接另一模型输出。
6. Feature flag 关闭后行为等于未部署该模块，无需清库才能回滚（记忆库可留盘，只是不再读写）。



### 6.2 阶段契约

每个模块：名称、默认关闭的 feature flag、超时、失败策略、token 预算、对信封的补丁。模块之间禁止互相 import 实现，只通过信封传数据。顺序由流水线声明。

信封至少包含：逻辑模型、协议、是否流式、规范化 messages、特性集、workspace（可空）、本轮产物（译文、命中的记忆）、各阶段耗时。V1 没有预算裁剪模块，**不为「剩余预算」定义语义**；字段可缺省。

建议超时（本机网关，可配置）：视觉翻译整体（含上游）单独设上限；记忆检索 **≤ 300ms**，超时视为失败并 skip。视觉不得为了赶时间输出空译文。

### 6.3 挂载点：G0-B；剥图在选号之后

不另起 G0-A HTTP 网关。改 messages 已在现有 strategy 挂点被证明能到达 Messages 上游，不应推翻已采纳的边界 ADR。配置用环境变量 / 现有 feature flag，不热替换阶段实现。

正确说法：

- **pre-call 改 `data["messages"]` 不到上游**（探针 B）。不要写「G0-B 已验证」来覆盖这一条。
- **选号后改 `request_kwargs["messages"]` 到达上游**（S1 / S2）。V1 剥图与翻译写在这里。
- P0 还证明：协议 metadata 与门控 raise 有效。

S5 配套：合成模型推迟 IMAGE 能力检查；纯 `glm-5.2` 不变。无翻译器时合成模型带图 fail-closed（探针 stub 默认关）。

---



## 7. 多模型组合：第一份配方是图像翻译



### 7.1 配方 `glm-5.2-vision`（Q6：Messages-only）

对外逻辑模型与内部执行模型必须分开写清，避免 discovery / 回滚 / 选号混成一个名字。


| 项                  | V1 值                                                      |
| ------------------ | --------------------------------------------------------- |
| 对外逻辑模型             | `glm-5.2-vision`                                          |
| `public_protocols` | **仅** `anthropic_messages`（与当前 `glm-5.2`、`MiniMax-M3` 一致） |
| 能力发现               | 列出该模型，并声明视觉（`image` / vision）。纯 `glm-5.2` **不**声明视觉       |
| 翻译模型               | `MiniMax-M3`；`quota_group_id=minimax-official`            |
| 执行模型（effective）    | `glm-5.2`；走现有 GLM Messages 部署与额度组                         |
| 触发                 | Messages 请求含 `image` block（含历史与 `tool_result` 内嵌）         |
| 无图                 | 跳过翻译，effective 仍是 `glm-5.2`                               |
| V1 场景              | 仅 coding agent 截图。拒绝风景、人像、手写、过糊图                          |


回滚：关掉视觉配方或从 discovery 撤下 `glm-5.2-vision` 后，客户端应不再把该名当视觉模型；`glm-5.2` 行为与今日相同。带图打到纯 `glm-5.2` 仍由现有 IMAGE 门控拒绝。

**配置（2026-08-24）：** 探针 A PASS 后，`MiniMax-M3` 的 `supported_features` 已含 `image`。其它 MiniMax 型号与 `glm-5.2` 仍无 `image`。生产 `plans.yaml` / `litellm.yaml` **未**新增 `glm-5.2-vision`：S5 只在探针窗口挂过该名，测完已撤。配方落地前不得把该名写进 discovery。

以后可加其它配方或 Chat 面。V1 只落地这一份 Messages 配方。

### 7.2 「翻译」指什么（Q2 已冻结）

翻译是把像素编成文本模型的工作记忆。视觉模型先做**粗分类**，再按类型选载体（实用子集，不是任意格式）：


| 分类              | 载体                   |
| --------------- | -------------------- |
| 报错 / 终端 / 日志    | `<pre>` 原文           |
| 代码编辑器           | `<pre><code>`，尽量带文件名 |
| Web / IDE / 设置页 | 语义 HTML 草图，几乎无 CSS   |
| 表格              | `<table>`            |
| 其它仍属 coding 截图  | 默认语义草图               |
| 明确不在 V1 范围      | 拒绝翻译                 |


共同约束：

- 根元素 `<visual-evidence>`，禁止 `<html>`、`<script>`、外链 CSS。
- 能看清的字进文本节点；看不清标 `data-uncertain`，禁止脑补。
- 视觉模型只译不解题。执行模型永不接收 image block。
- 注入时声明：这不是仓库源码，不要写成新文件。



### 7.3 对外契约与 IMAGE 门控三态

- 能力发现必须声明合成模型支持视觉。
- 用户看见的流只来自执行模型。V1 视觉配方以非流为主验收；若 OpenCode 开流，仍遵守首字节后不换上游，翻译必须在首字节前结束。
- 剥图与现有 `Feature.IMAGE` 门控：


| 态                   | 条件                                              | 结果                                      |
| ------------------- | ----------------------------------------------- | --------------------------------------- |
| 翻译成功                | 合成模型推迟 IMAGE；选号后剥图写回 `request_kwargs`          | 出发 HTTP 无 image；执行模型只看到译文              |
| 翻译失败 / 翻译器未挂        | 选号后 fail-closed（S5 无 stub 即此态）                  | 客户端明确错误；像素不得送给 GLM                      |
| 视觉关闭，带图打纯 `glm-5.2` | IMAGE 进入 required_features                      | 现有 `FEATURE_UNSUPPORTED`；S5 live 已再确认 |


S5 stub 剥图只用于探针，**不是**占位译文，也不得在生产默认打开。


- 上下文上限（超限策略）：


| 上限           | V1             | 超限                                 |
| ------------ | -------------- | ---------------------------------- |
| 单请求图片张数      | 6              | 视觉 fail-closed，明确错误                |
| 单图字节 / 请求总字节 | 5 MiB / 12 MiB | 同上                                 |
| 单张译文         | 约 4k token     | 先保 `<pre>`/OCR，裁草图；仍超则 fail-closed |
| 记忆注入         | 约 2k token     | fail-open，按相关度丢弃                   |




### 7.3.1 `tool_result` 递归

`extract_required_features()` **已**递归 `tool_result` 内嵌图（S5）。stub 剥图同样递归。真正翻译流水线必须对扫到的每一张图做哈希 / 查表 / 替换 block 类型，不能只剥不译。

### 7.4 多轮与客户端历史

每一轮必须：

1. **全量访问** `messages` 里每一个 image 类 block（含 `tool_result`），不得按「上次扫到第几条」跳过旧消息。网关对客户端是无状态的；跳过旧图会把 base64 送给 GLM。
2. 哈希缓存的意义是：旧图只做「解码 / 哈希 / 查表 / 替换 block 类型」，不再打 M3。复杂度从「每张图一次视觉推理」降到「每张图一次哈希」，而不是从 O(历史图) 降到 O(新图) 的漏扫。
3. 替换时改变 block 类型为 `text`。
4. 缓存键：`sq:vision:{schema_ver}:{sha256}`。同一键必须对应同一段译文。
5. 不靠改客户端本地历史来防 400。

**不采纳「增量只扫 last_scan_index 之后」作为正确性方案。** 入站体积（每轮重传 base64）V1 接受。V2 若做客户端插件，可靠响应 metadata 里的已译哈希做提示，由客户端选择少传；网关侧仍必须能处理「又把原图带回来」的情况。

**不采纳「相似截图用 embedding 复用译文」。** 报错图差一行字就会修错文件；0.95 阈值的假阳性代价高于多打一次 M3。V1 只做精确字节哈希。

### 7.5 译文质量门（采纳结构化校验，拒绝像素密度公式）

交给执行模型之前必须通过白名单校验，否则 fail-closed：

- 能解析为允许的标签集，根节点正确。
- 无 `<script>`、无 `javascript:`、无完整 `<html>` 文档。
- `data-uncertain` 占比过高（例如可见文本几乎全是 uncertain）则拒绝，视为没看清。
- 译文为空或只有空壳标签则拒绝。

不采用「每 1000 像素至少一个字符」这类密度启发式：稀疏 UI、大片空白的 IDE 截图会误杀。

### 7.6 视觉阶段的快速失败

连续翻译失败（超时、上游 5xx、校验失败）达到阈值后，合成模型在短窗口内直接返回明确错误，不再把每个请求卡满视觉超时。这与内核的 deployment cooldown 互补：cooldown 管账号 / deployment；这里管「本模块暂时不可用」。

恢复后走正常翻译。窗口内**禁止**降级为「图片已省略」之类占位译文。

### 7.7 翻译评估集与实测假设

质量门（§7.5）没有标定集会变成空转。视觉翻译档必须先准备 **≥20 张真实 coding 截图**（报错、终端、IDE、Web UI、表格），每张标注期望载体（`<pre>` / 语义草图 / `<table>` / 拒绝）。prompt 迭代以该集为准，不靠零星手工试。

编码前仍须实测：

- **探针 A（2026-08-21）：** 直连 MiniMax Anthropic Messages PASS（`VISION_OK`）。当时网关未给 M3 声明 `image`，故第一证据不走本机 `/v1/messages`。随后仅 M3 写入 `image`。
- **挂点（2026-08-24）：** S1/S2 证明选号后改 `request_kwargs` 到达 Messages 上游；S5 证明合成模型可推迟 IMAGE 并剥图。MiniMax **翻译质量**仍未测，评估集（≥20 张）仍是视觉档前置。
- 按类型选载体后，GLM 修对文件的比例高于自由 caption，且不把草图当项目源码。
- 精确哈希在 OpenCode 多轮里命中率足够。



### 7.8 内部子调用契约

嵌套二次路由在本仓库无先例。规格必须写死，不得只写「再调一次 completion」：


| 项          | V1 契约                                                                                             |
| ---------- | ------------------------------------------------------------------------------------------------- |
| 标记         | 子请求 metadata / `litellm_metadata` 设 `internal_call=true`（及 `internal_kind=vision|memory-extract`） |
| 递归阻断       | 已是 `internal_call` 的请求**禁止**再跑视觉翻译 / 记忆检索 / 记忆抽取                                                  |
| 最大深度       | 1（父可生子；子不得再生）                                                                                     |
| 父子 context | 子调用独立 `litellm_call_id`（如 `{parent}#vision:{hash8}`）与独立 `RequestRoutingContext`；禁止复用父 ctx         |
| quota 排他   | 子调用 `quota_group_id` **不得等于**父请求执行模型的额度组；违反则 fail-closed（配置错误）                                    |
| 失败分类       | 走现有分类器；M3 耗尽只写 M3 的 quota_group                                                                   |


不要把这当成实现细节事后补。

---



## 8. 网关记忆模块



### 8.1 定位

OpenCode、Cursor、以及其它把 Base URL 指到本机网关的 AI app，在**同一工作区**下读同一份笔记。`surface` 只做审计，不做检索主键。

不能同步各家云官方记忆，也不关闭客户端自带记忆。

### 8.2 读路径（Q4 已冻结）

workspace 推断与规范化：

1. **可信来源：** 仅约定头 / metadata（如 `X-Workspace-Root`）视为可信。
2. **规范化：** 绝对路径、展开 `~`、`Path.resolve()`；拒绝空、相对 `..` 逃逸、NUL。符号链接解析到真实路径后再当 scope 键。
3. **弱推断：** 从消息里的文件路径取公共根，仅作候选，必须通过同一规范化；推断失败或不可信 → 视为未知。
4. **未知：** **不检索、不写入**。不用本机全局记忆兜底。

记忆注入是数据不是指令：放在 `<gateway_memory>`，不得升级为 system。跨工作区检索视为漏洞。

转发前用「本轮用户文本 + 已有视觉译文原文 + workspace」检索。命中硬顶约 2k token，放在动态尾部。

顺序：视觉翻译 → 记忆检索。记忆超时或失败：skip。可用极廉价规则跳过检索（问候、空消息、无 workspace），**不**为 V1 再挂分类小模型。

**存储介质（V1 写死）：** 网关本地文件，按规范化后的 workspace 分片；JSONL 或单库 SQLite 二选一，规格里定一种。关键词 / 简单字段匹配。不引入向量库，不写入额度 Redis（`sq:`*）。

### 8.3 写路径（Q5 已冻结）

- 执行成功后**入队**异步抽取，不挡流、不挡 `on_stream_complete` 返回。
- `ManagedStream.on_stream_complete` 只允许 enqueue。现有回调是同步的（`stream_lifecycle.py`），在其中直接打上游会堵住收尾与 lease release。V1 用进程内队列：最大深度（例如 32），满则丢弃任务并打日志（fail-open）；进程退出放弃未完成任务，不持久化抽取作业。
- 非流成功路径同样只入队，不在 `async_log_success_event` 里同步抽取。
- 抽取子调用遵守 §7.8（`internal_call`、深度 1、独立 id、与父执行额度组互斥）。Q5：可配置廉价文本模型；失败不写库。
- 入库前规则脱敏。用户明确「记住……」可直写（仍脱敏）。
- V1 先只读注入（手写 JSONL/SQLite），跨 app 共享验证通过后再开自动抽取。
- 不做多级 ACL；workspace 隔离是唯一强制边界。



### 8.4 与视觉译文缓存


|      | 图像译文缓存        | 网关记忆           |
| ---- | ------------- | -------------- |
| 键    | 图片哈希 + schema | workspace + 语义 |
| 失败   | fail-closed   | fail-open      |
| 关掉模块 | 不再译图          | 不再注入；库可留盘      |


---



## 9. 增量交付

每一档结束时，内核与已上线模块行为不变。观测接现有 `metrics.py`（计数 + max），不加 histogram、不做 APM。

1. **双探针与挂点（V1 均走 Messages）**
  - 探针 A（2026-08-21）：PASS（直连 MiniMax）。M3 已配 `image` feature。
  - 探针 B（2026-08-21）：FAIL（pre-call 改 `data["messages"]` 未到上游）。
  - remount S1 / S2 / S5（2026-08-24）：PASS。挂点 = 选号后 `request_kwargs`；合成模型推迟 IMAGE；纯 `glm-5.2` 带图仍拒。
2. **拆规格**：`pipeline.md` / `vision-compose.md` / `memory.md`（P1 清单见 §16）。挂点已证，允许拆；未拆之前不写 MiniMax 翻译。
3. **Pipeline MVP**：信封、有序列表、总开关、阶段耗时计数。现有测试全绿。
4. **视觉翻译**：评估集 → prompt 迭代 → 单图缓存 → 全量替换（含 `tool_result`）→ 质量门 + 快速失败 + §7.8 子调用。生产才新增 `glm-5.2-vision`。
5. **记忆检索**：规范化 workspace + JSONL/SQLite + 只读注入。
6. **记忆写入**：队列 enqueue + Q5 抽取；不在 `on_stream_complete` 里阻塞调用。
7. **成本归因**：计数，不加 histogram。

不要在 pre-call 里剥图。不要把 S5 stub 当配方打开给真人流量。任务级拆分见 [`plans/2026-08-25-vision-and-memory.md`](./plans/2026-08-25-vision-and-memory.md)。

---



## 10. 与现有纪律的对齐


| 现有约束              | 本提案中的落点                            |
| ----------------- | ---------------------------------- |
| 模型组 ≠ 额度组         | 门面模型；翻译与执行各走各的组                    |
| 禁止默认跨模型降级         | 配方显式；失败不换执行模型、不输出假译文               |
| 无静默语义降级           | 质量门失败 = 拒绝，不是占位 caption            |
| 流式首字节后不换上游        | 可见流只有执行模型                          |
| 同组 1 次 / 跨组 3 次   | 子调用各自计数                            |
| 业务只在 plugin       | 流水线在 `shared_quota_router`         |
| Redis fail-closed | 仅额度内核                              |
| 日志不打 Key / prompt | 现有 `metrics._safe_labels` 同样约束阶段日志 |
| IMAGE 门控          | 合成模型推迟检查；选号后必须剥图。纯 `glm-5.2` 带图仍拒 |


---



## 11. 主要风险与反模式


| 风险                          | 若发生                          | 缓解                                |
| --------------------------- | ---------------------------- | --------------------------------- |
| 模块耦合进 strategy              | 卸模块必须改内核                     | 阶段注册表                             |
| 只扫新消息、不碰历史图                 | 下一轮 GLM 400                  | **每轮全量替换**；缓存只跳过 M3               |
| 熔断后塞占位译文                    | 静默降级                         | 只允许明确错误                           |
| 译文做成完整网页                    | 执行模型改错对象                     | 白名单 + 非源码声明                       |
| 相似图语义缓存命中错图                 | 修错文件                         | V1 仅字节哈希                          |
| 按 app 名做记忆主键                | 再次孤岛                         | surface 仅审计                       |
| 工作区未知时用全局记忆                 | 项目串味                         | 不检索                               |
| 记忆失败中断请求                    | 卸不下模块                        | 超时 skip                           |
| 视觉子调用耗尽却熔断 GLM              | 错杀执行账号                       | 子调用独立 quota_group                 |
| 子调用复用父 `litellm_call_id`    | 父 tried-set / first_byte 被污染 | 独立 request_id 与独立 ctx             |
| 把「G0-B 已验证」当成 pre-call 改 messages 已通 | 剥图未到达上游，GLM 仍 400            | 探针 B FAIL；剥图走 S1 挂点 + S5 IMAGE 推迟 |


---



## 12. 已冻结的决策


| ID  | 决策                              | 说明                                                                                  |
| --- | ------------------------------- | ----------------------------------------------------------------------------------- |
| Q1  | **V1 只做 coding agent 截图**       | 最高价值；文档 OCR / 通用识图后置。明确拒绝风景、人像、手写、过糊图                                               |
| Q2  | **按类型在小集合里选载体**                 | 报错/终端 `<pre>`；UI 语义草图；不是自由 CSS 还原                                                   |
| Q3  | **V1 仅网关全量替换 + 精确缓存**           | 客户端少传图、换模型保留原图，均后置                                                                  |
| Q4  | **按工作区隔离，跨 app 共享**             | 推断失败则不检索、不写入；存储为本地 JSONL/SQLite + 关键词                                               |
| Q5  | **抽取子调用独立于执行额度组**               | 可配置廉价文本模型；不同 `quota_group_id`；独立 request_id；失败 fail-open                            |
| Q6  | **V1 合成模型仅 Anthropic Messages** | 与当前 `glm-5.2` / `MiniMax-M3` 的 `plans.yaml` 对齐。Chat / Responses / Cursor-Chat 不在 V1 |


---



## 13. 成功度量



### 正确性

- 合成模型路径上，执行模型因残留 image block 导致的 400 ≈ 0（测试覆盖历史图、tool_result）。
- 关闭总开关后，现有 unit / contract 测试 100% 通过。
- 视觉模块关闭时记忆仍可注入；记忆关闭时视觉仍可翻译。
- 两个不同客户端、同一 workspace，能读到同一条手写记忆。



### 性能（本机）

现有 `metrics.py` 无 histogram，V1 **不以 P95 为门禁**。用阶段成功/失败计数 + 进程内耗时 max（及日志毫秒）观察：

- 无图：记忆检索有 300ms 上限且 fail-open。
- 有图且缓存命中：不应再付一次视觉模型时延。
- 有图且缓存未命中：附加延迟 ≈ 一次 M3 调用；须在首字节前结束。



### 可靠性

- 视觉连续失败时，合成模型快速返回明确错误，而不是每次打满超时。
- 记忆故障或超时不阻止执行模型。
- M3 额度耗尽不把 GLM 账号标为 `SHARED_QUOTA_EXHAUSTED`。

---



## 14. 客户端兼容（V1）


| 客户端                                         | 协议                 | 图像            | V1 视觉配方             |
| ------------------------------------------- | ------------------ | ------------- | ------------------- |
| OpenCode                                    | Anthropic Messages | `image` block | **主路径**             |
| 其它走本机 `/v1/messages` 且模型名为 `glm-5.2-vision` | Messages           | `image`       | 支持                  |
| Cursor 仅 OpenAI Chat                        | Chat               | `image_url`   | **不在 V1**           |
| Responses                                   | Responses          | —             | **不在 V1**（项目 No-Go） |


V1 协议扫描以 Anthropic `image` 为主，并递归 `tool_result`。Chat `image_url` 可解析但不作为 V1 验收面。记忆只读注入不依赖视觉，Messages/Chat 客户端只要打到本网关且 workspace 可判定即可共享记忆。

---



## 15. 回滚

每个模块：

1. 环境变量 / feature flag 关闭，不必重启才能停注入（与现有 `PROTOCOL_AWARE_GATEWAY_ENABLED` 同一思路；若进程只在启动读 env，则允许重启，但关闭后语义必须等于未部署）。
2. 关闭后不得改额度选号、不得留下「半剥的 image block」。
3. 记忆库可保留；紧急情况允许清空后重来。

紧急顺序：关视觉配方 → 关记忆 → 关流水线总开关 → 跑现有测试。合成模型在视觉关闭后应停止广告 vision 或对带图请求明确失败，避免客户端继续贴图打到纯文本执行模型。

---



## 16. 施工门槛与下一步

**结论：方向通过；挂点与 IMAGE 时序已闭合。** pre-call 剥图仍不可用。允许拆 `pipeline.md` / `vision-compose.md` / `memory.md`。不得把 S5 stub 当 MiniMax 翻译，不得在生产 discovery 广告 `glm-5.2-vision` 直到配方落地。

### P0 — 结果

| 项 | 结果 | 证据 |
| --- | --- | --- |
| 协议矩阵 | Messages-only（Q6），未改 | 提案冻结项 |
| 探针 A | **PASS**（2026-08-21） | 直连 MiniMax Anthropic Messages，HTTP 200，助手文本精确 `VISION_OK`。[`reports/p0-probe-a.md`](./reports/p0-probe-a.md) |
| 探针 B | **FAIL**（pre-call，2026-08-21） | live MiniMax-M3 只回 `pong`。pre-call 改 `data["messages"]` 未到上游。[`reports/p0-probe-b.md`](./reports/p0-probe-b.md) |
| remount S1 | **PASS**（2026-08-24） | 选号后改 `request_kwargs["messages"]`，live MiniMax-M3 助手文本含注入 token（客户端 JSON 无该 token）。[`reports/p0-probe-b-s1.md`](./reports/p0-probe-b-s1.md) |
| remount S2 | **PASS**（2026-08-24） | mock 当 MiniMax 上游。对照：inject 关 → `probe_marker_hit=false`；inject 开 → `true`。出发路径 `/v1/messages`。[`reports/p0-probe-b-s2.md`](./reports/p0-probe-b-s2.md) |
| remount S5 | **PASS**（2026-08-24） | 合成模型推迟 IMAGE，选号后 stub 剥图。live：`glm-5.2` 带图 400；探针窗口 `glm-5.2-vision` 出发无图且 mock hit。[`reports/p0-probe-s5.md`](./reports/p0-probe-s5.md) |
| M3 `image` | **已配置** | 仅 MiniMax-M3 含 `image`；`glm-5.2` 不含。生产未新增 `glm-5.2-vision`。 |

执行计划：[`plans/2026-08-21-p0-probes.md`](./plans/2026-08-21-p0-probes.md)。A 直连 MiniMax；B/S1/S2/S5 以 live 网关 `/v1/messages` 为准（Docker `local-llm-router-litellm-1`，不是 `.venv`）。

2026-08-21 曾用 OpenCode 路径打 `glm-5.2` Messages 得 HTTP 400（Console Go 空 `messages`），与 marker 无关，不能当 B 的证据。该套餐已下线。S5 对照走的是当前 Volc `glm-5.2`：带图为 IMAGE 门控 400。


### P1 — 规格必须写死（本文已给默认，拆 spec 时不得再空着）

- `glm-5.2-vision → glm-5.2` recipe、effective model、discovery、回滚（§7.1）
- `internal_call`、递归阻断、深度 1、父子 ctx、quota 排他（§7.8）
- `tool_result` 递归：提取与 stub 剥图已做；翻译流水线须对每张图哈希替换（§7.3.1）
- 记忆抽取：队列、上限、失败、进程退出；禁止在 `on_stream_complete` 里阻塞上游调用（§8.3）
- workspace 规范化、符号链接、可信来源、未知 scope、注入安全边界（§8.2）
- 图片字节/张数、译文 token、记忆注入上限与超限策略（§7.3）
- 模块集成原则：不改变额度选号语义；选号后允许改 `request_kwargs`（§6.1、§6.3）



### P2 — 后置

HTML 白名单细化、记忆投毒防护、细粒度 ACL、histogram、bulkhead。不能替代 P1。

顺序：P0 挂点已证 → **拆** `pipeline.md` / `vision-compose.md` / `memory.md` → 再编码 MiniMax 翻译与记忆。不要倒回去做 pre-call 剥图。

增量施工方案（差距表、模块、任务）：[`plans/2026-08-25-vision-and-memory.md`](./plans/2026-08-25-vision-and-memory.md)。

---



## 17. 外部评审：采纳与拒绝

针对一次架构评审的处理记录，避免下次把已拒绝项再当作缺口。


| 评审建议                                          | 结论            | 理由                                                      |
| --------------------------------------------- | ------------- | ------------------------------------------------------- |
| 视觉模块 Circuit Breaker + 占位译文降级                 | **部分采纳**      | 要快速失败；**不要**占位译文（静默降级）                                  |
| 流水线改 DAG，PII 与翻译并行                            | **V1 拒绝**     | 三阶段线性足够；记忆必须等译文                                         |
| 只扫 last_scan_index 之后的图                       | **拒绝**        | 无状态网关；漏扫历史图 → GLM 400                                   |
| 记忆多源并行 + 300ms                                | **部分采纳**      | 要超时 fail-open；V1 不必上向量库三路并行                             |
| 相似图 embedding 语义缓存                            | **V1 拒绝**     | 假阳性会修错代码                                                |
| 像素密度校验译文                                      | **拒绝**        | 误杀稀疏 UI                                                 |
| 从执行模型额度预留 15% 给视觉                             | **拒绝**        | 两组 quota 本来独立                                           |
| Bulkhead Semaphore                            | **V2 可选**     | 本机并发低；先用上游超时                                            |
| 完整正则 PII 平台 + 记忆敏感级 ACL                       | **部分采纳**      | 写入前规则脱敏；V1 不做多级 ACL                                     |
| OpenTelemetry 分布式追踪                           | **V1 拒绝**     | 用现有 `metrics.py` + request_id 日志                        |
| 译文附带图像 embedding / Hybrid fusion              | **拒绝**        | 执行模型是纯文本，向量无处消费                                         |
| Self-RAG 分类模型 / HyDE                          | **V1 拒绝**     | 无 workspace 则 skip；不新增模型调用                              |
| WASM / eBPF / 热重载                             | **拒绝**        | 与 G0-B、本机 Python 插件边界不符                                 |
| 成功度量、兼容矩阵、回滚 SOP                              | **采纳**        | 见 §13–15                                                |
| Q1=D、Q2=类型子集、Q3=网关全量替换、Q4=工作区+未知不检索           | **采纳并冻结**     | 见 §12                                                   |
| 「G0-B 已验证」覆盖 messages 改写                      | **纠正**        | pre-call 改 body 未通（探针 B）。选号后 `request_kwargs` 已通（S1/S2）。C2 S3 是 post_call 返回值，不混用 |
| 探针 B 失败则改挂 strategy，且须处理 IMAGE 门控时序           | **已执行**       | §5、S1/S2/S5                                              |
| 子调用独立 request_id，不复用父 ctx                     | **采纳**        | 见 §4、§7.8                                               |
| V1 记忆介质 JSONL/SQLite + 关键词，无向量库               | **采纳并写死**     | 见 §8.2                                                  |
| 抽取模型 / quota_group（Q5）                        | **冻结**        | 见 §8.3、§12                                              |
| IMAGE 门控三态，纯 GLM 带图白捡现有门控                     | **采纳**        | 见 §7.3                                                  |
| 视觉档补 ≥20 张评估集                                 | **采纳**        | 见 §7.7、§9                                               |
| P95 改计数/max，不强制 histogram                     | **采纳**        | 见 §13                                                   |
| 信封「剩余预算」V1 不定义语义                              | **采纳**        | 见 §6.2                                                  |
| 注册表 = 有序列表，非动态插件                              | **采纳**        | 见 §6.1                                                  |
| 流式记忆抽取挂 `on_stream_complete`                  | **部分采纳**      | 只允许 enqueue，禁止在回调里阻塞打上游                                 |
| V1 必须先闭合协议矩阵（Messages vs Chat/Responses）      | **采纳，收窄为 Q6** | 见 §7.1、§14、§16                                          |
| M3 无 `image` feature，探针 A 后必须改 plans + 门控测试   | **已执行**       | A PASS 后仅 M3 加 `image`；合成模型未加。见 §7.1、§16              |
| recipe / effective model / discovery / 回滚必须定义 | **采纳**        | 见 §7.1                                                  |
| `internal_call`、深度、quota 排他                   | **采纳**        | 见 §7.8                                                  |
| 记忆抽取异步生命周期（队列/上限/退出）                          | **采纳**        | 见 §8.3                                                  |
| workspace 规范化与可信来源                            | **采纳**        | 见 §8.2                                                  |
| 上下文字节/token 上限                                | **采纳**        | 见 §7.3                                                  |
| 「只新增文件」改为「不改变额度选号语义」                          | **采纳**        | 见 §2.1、§6.1                                             |
| 方向通过、暂不通过施工评审                                 | **更新**        | 挂点已证，允许拆规格；配方与记忆仍未落地。见文首与 §16                    |


Late fusion 与线性 pipeline 仍保留。**剥图挂点是选号后的 `request_kwargs`，不是 pre-call。** 下一步拆规格，再写 MiniMax 翻译；不要倒回 pre-call 剥图。
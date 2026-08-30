# glm-5.2-vision 与网关记忆 — 增量开发方案

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans，按任务逐项实现。步骤用 checkbox（`- [ ]`）跟踪。
>
> **Commit 纪律：** 本仓库用户规则优先于 writing-plans 的「每步 commit」。**未得到用户明确要求时不要 `git commit` / `git push`。**

**Goal:** 在现有 G0-B 额度网关上落地两件事：对外合成模型 `glm-5.2-vision`（MiniMax-M3 译图 → `glm-5.2` 执行），以及按工作区共享的网关记忆（先只读手写库，再异步抽取）。

> **Status (2026-08-30):** 预置视觉配方与记忆 V1 已编码。可配置槽位见已完成的 [`2026-08-30-composable-vision-recipes.md`](./2026-08-30-composable-vision-recipes.md)；日常维护见 [`../maintenance.md`](../maintenance.md)。下文任务清单保留为历史施工记录。

**Architecture:** 增强层挂在 `async_get_available_deployment` **选号成功之后**，只改 `request_kwargs["messages"]`。不改 Fill First / affinity / tried / lease。视觉 fail-closed；记忆 fail-open。子调用独立 `litellm_call_id` 与独立额度组。V1 合成模型仅 Anthropic Messages。

**Tech Stack:** LiteLLM **v1.90.5**、`plugins/shared_quota_router/`（运行时名 `shared_quota_router`）、本机 Redis（额度 fail-closed）、Docker 镜像 `COPY` 插件、Anthropic Messages。不改 `upstream/litellm`。

**依据：** [`../design-proposal.md`](../design-proposal.md) Q1–Q6 已冻结；P0 探针 A / B / S1 / S2 / S5 已闭合挂点。本文件是施工清单，不是重新开产品取舍。

**非目标：** Chat / Responses 合成模型；S5 stub 当生产配方；pre-call 剥图；向量库；记忆写入额度 Redis `sq:*`；默认跨模型降级；占位 caption；`quota_collectors` / Cookie 登录。

---

## 0. 本文定位

| 文档 | 角色 |
|------|------|
| `docs/framework-upgrade/design-proposal.md` | 已冻结的产品与架构决策 |
| `docs/framework-upgrade/pipeline.md` 等三份规格 | **本方案 F0 才写**；未写之前不实现 MiniMax 翻译 |
| **本文** | 对照**当前代码**的差距、模块、阶段、风险、可执行任务 |

两个子系统在 F1（信封）之后可以并行：视觉配方不依赖记忆；记忆只读不依赖 MiniMax。不要做成「一个大 PR 同时上线译图和自动抽取」。

工作目录：`E:\LiteLLMPro\local-llm-router`。  
解释器：优先 `.\.venv\Scripts\python.exe`；本机亦可用 `F:\anaconda\envs\py312\python.exe`。一律 `$env:PYTHONPATH="plugins"`。  
Live 代理：Docker `local-llm-router-litellm-1`。插件是 **COPY 进镜像**的，改 Python 后必须 `--build`；`config/litellm.yaml` 是 volume。

---

## 1. Current State（代码为准，文档为意向）

额度内核、协议门控、Messages 直连、选号后改 `request_kwargs` 均已在生产路径上工作。框架升级只完成了 **S5 脚手架**，没有配方，没有记忆。

**已证明（不要再测一遍当开工条件）：**

| 探针 | 结果 | 对施工的含义 |
|------|------|----------------|
| A | PASS | MiniMax-M3 能在 Anthropic Messages 上看图 |
| B | FAIL | **禁止**在 `async_pre_call_hook` 里剥图 / 译图 / 注记忆 |
| S1 / S2 | PASS | 选号后改 `request_kwargs["messages"]` 能到达 Messages 上游 |
| S5 | PASS | 合成模型可推迟 IMAGE；选号后剥图出发 HTTP 无图；纯 `glm-5.2` 带图仍 400 |

**当前代码（2026-08-24）：**

- `composed_vision.py`：`S5_COMPOSED_MODELS` + 默认 fail-closed；`S5_STUB_PEEL` 用占位字符串替换 image block（探针专用，默认关）。
- `strategy.py` `get_available_deployment`：选号 / convert 之后调用 `peel_composed_images_on_select`，再注入 S1 marker。
- `async_get_available_deployment` **直接转调同步 select**。同步函数里打 MiniMax 会堵住事件循环。
- `LogicalModelProtocols` **没有** `compose` / `defer_image_gate` 字段；`getattr(..., "defer_image_gate", False)` 是死代码，只有 env 名单生效。
- `protocol_context` 已递归 `tool_result` 扫 IMAGE；`protocol_gates` 用 `capability_features` 推迟合成模型的 IMAGE 检查。
- Discovery 只暴露 `public_protocols`，没有 `image` / vision 元数据。
- Generator：一个 plan model → 一条 `model_list`。生产 **没有** `glm-5.2-vision`。`glm-5.2` **没有** `image`。`MiniMax-M3` **有** `image`。
- `metrics.py` 只有 counter / gauge。
- `stream_lifecycle.ManagedStream.on_stream_complete` 是同步回调；现用于 `on_success` / lease，**不能**在里面同步打抽取模型。
- 不存在 `pipeline.py` / `memory.py` / `vision_translate.py`。三份规格文件也不存在。

S5 stub **不是** MiniMax → `<visual-evidence>` 配方。生产打开 `S5_STUB_PEEL` 等于用假译文糊弄执行模型，违反提案。

---

## 2. Gap Analysis

| # | Feature | 提案 / 规格要求 | 代码实际 | Status |
|---|---------|-----------------|----------|--------|
| 1 | 选号后改 messages 到达上游 | S1 挂点 | `strategy.py` 选号后 mutate | ✅ 已完成 |
| 2 | 合成模型推迟 IMAGE | S5 | `capability_features` + env 名单 | ⚠️ 仅 env；缺 recipe 字段 |
| 3 | 纯 `glm-5.2` 带图拒绝 | 不得偷偷看图 | IMAGE 门控 400（S5 live） | ✅ 已完成 |
| 4 | `tool_result` 递归扫图 | §7.3.1 | extract + stub peel 已递归 | ✅ 扫描已完成；翻译未做 |
| 5 | 信封 + 有序阶段 + 总开关 | §6、§9.3 | 无 | ❌ |
| 6 | `LogicalModelProtocols.compose` | §7.1 recipe | 无；generator 不渲染 compose | ❌ |
| 7 | 生产 `glm-5.2-vision` 部署 | 与 `glm-5.2` 同额度组 | 未配置 | ❌ |
| 8 | MiniMax 翻译子调用 | §7.8 `internal_call` | 无 | ❌ |
| 9 | SHA-256 译文缓存 | §7.4 每轮全量替换 | 无 | ❌ |
| 10 | `<visual-evidence>` 质量门 | §7.5 | 无 | ❌ |
| 11 | 视觉连续失败快速拒绝 | §7.6 | 无 | ❌ |
| 12 | 张数 / 字节 / token 上限 | §7.3 | 无 | ❌ |
| 13 | Discovery 声明 vision | 合成模型广告 image；纯 GLM 不广告 | 仅 protocols | ❌ |
| 14 | ≥20 张评估集 | §7.7 | 无 | ❌ |
| 15 | workspace 规范化 | §8.2 | 无 | ❌ |
| 16 | 本地记忆库 + 关键词检索 | Q4 JSONL/SQLite | 无 | ❌ |
| 17 | `<gateway_memory>` 注入 | 数据不是指令；不升 system | 无 | ❌ |
| 18 | 记忆写入队列 + 抽取 | Q5；禁止阻塞 `on_stream_complete` | 无 | ❌ |
| 19 | 阶段计数 / max 耗时 | §13 | 无 enhance 指标 | ❌ |
| 20 | `defer_image_gate` 配置字段 | S5 注释以为有 | `LogicalModelProtocols` 无此字段 | ⚠️ 死代码 |

图例：✅ 跳过；⚠️ 本方案要补完；❌ 本方案要实现。

---

## 3. 本方案冻结的实现选择

提案留空的「规格里定一种」，这里写死，避免 F0 再争论。与 Q1–Q6 冲突则以提案为准。

| 项 | 选择 | 理由 |
|----|------|------|
| 记忆介质 | **JSONL**，每工作区一个文件 | 无新依赖；Windows 友好；缺文件 = 空库 fail-open |
| 记忆目录 | `GATEWAY_MEMORY_DIR`，默认 `local-llm-router/data/gateway-memory/`（已在 `.gitignore` 的 `data/` 下） | 不进 Redis `sq:*` |
| 记忆文件名 | `sha256(normalized_workspace)[:32].jsonl` | 路径里的盘符/中文不进文件名 |
| 视觉缓存 | **本地文件** `GATEWAY_VISION_CACHE_DIR`，默认 `data/vision-cache/`；逻辑键 `vision:{schema_ver}:{sha256}` | 不占用额度 Redis；Redis flush 不丢译文；缓存未命中只是再译 |
| 合成部署 | `glm-5.2-vision` **作为 plan model 挂在与 `glm-5.2` 相同的 Volc plan**（同一 `quota_group_id`） | Generator 一行一个部署；选号语义不变；像素在出发前剥掉 |
| 执行模型字段 | `compose.execute_model: glm-5.2` 只用于日志、quota 排他校验、文档 | 不在 select 时 remap `model` 字符串 |
| 翻译模型 | `compose.translate_model: MiniMax-M3` | 已声明 `image`，探针 A PASS |
| IMAGE 推迟 | `compose` 非空 ⇒ `defer_image_gate=true`；保留 `S5_COMPOSED_MODELS` 作探针覆盖 | 删掉对不存在字段的 `getattr` 依赖 |
| Discovery | `advertised_features` 含 `image` 时才声明 vision；**配方未开或翻译器未挂时不要把该名写进生产 discovery** | 避免客户端对半成品贴图 |
| 流水线挂点 | 选号仍在同步 `get_available_deployment`；**可 await 的阶段只跑在 `async_get_available_deployment`** | 同步里打 M3 会堵 event loop |
| 同步路径遇未缓存图 | 合成模型 + 有图 + 需要翻译 ⇒ 明确错误（测试用 fake translator 注入） | 单测不必真打 M3 |
| 子调用 HTTP | `internal_call.py` 用 `httpx.AsyncClient` 打所选 MiniMax deployment 的 Anthropic `/v1/messages` | 避免 `litellm.acompletion` 再绕一圈代理造成语义不清；仍走本策略选 MiniMax 账号 |
| `S5_STUB_PEEL` | 保持默认关；生产 `VISION_COMPOSE_ENABLED=true` 时 **忽略 stub**，只走真翻译或 fail-closed | stub 不是配方 |
| 记忆 V1 第一刀 | **只读手写 JSONL**；自动抽取为后续阶段 | 提案 §8.3、§9.5→9.6 |
| Feature flags（全默认 false） | `GATEWAY_ENHANCE_ENABLED` 总开关；`VISION_COMPOSE_ENABLED`；`GATEWAY_MEMORY_ENABLED`；`GATEWAY_MEMORY_EXTRACT_ENABLED` | 关总开关 = 今天的网关 |

Flags 读法与现有 `_env_bool` 一致，加进 `feature_flags.py`。

---

## 4. File map

| 路径 | 职责 |
|------|------|
| `docs/framework-upgrade/pipeline.md` | F0。信封、阶段顺序、flag、不变量 |
| `docs/framework-upgrade/vision-compose.md` | F0。配方、IR、缓存、子调用、质量门、评估集 |
| `docs/framework-upgrade/memory.md` | F0。workspace、JSONL schema、注入、队列 |
| `plugins/shared_quota_router/pipeline.py` | 信封 + 有序 `Stage` protocol + 计时 |
| `plugins/shared_quota_router/internal_call.py` | 独立 call id、quota 排他、深度 1、异步 HTTP |
| `plugins/shared_quota_router/vision_compose.py` | 配方解析、扫图、哈希、翻译、质量门、剥替换、熔断计数 |
| `plugins/shared_quota_router/vision_ir.py` | `<visual-evidence>` 白名单校验 |
| `plugins/shared_quota_router/vision_cache.py` | 文件缓存 get/put |
| `plugins/shared_quota_router/memory_workspace.py` | 可信头 + 规范化 + 弱推断 |
| `plugins/shared_quota_router/memory_store.py` | JSONL 读写（写路径 F5 才用写） |
| `plugins/shared_quota_router/memory_retrieve.py` | 关键词检索 + `<gateway_memory>` 注入 |
| `plugins/shared_quota_router/memory_extract.py` | F5：队列、脱敏、抽取子调用 |
| `plugins/shared_quota_router/composed_vision.py` | **保留** IMAGE 推迟；剥图改为委托 pipeline / 真翻译 |
| `plugins/shared_quota_router/models.py` | `ComposeRecipe`；扩展 `LogicalModelProtocols` |
| `plugins/shared_quota_router/config_schema.py` | 解析 `compose`；校验 execute/translate 存在且额度组不同 |
| `plugins/shared_quota_router/generator.py` | 渲染 `compose` 进 `shared_quota_logical_models` |
| `plugins/shared_quota_router/strategy.py` | async 路径跑 pipeline；select 语义不动 |
| `plugins/shared_quota_router/feature_flags.py` | 四个 enhance flag |
| `plugins/shared_quota_router/discovery.py` | 可选 `advertised_features` / vision |
| `plugins/shared_quota_router/stream_lifecycle.py` | F5：complete 时只 enqueue |
| `plugins/shared_quota_router/callbacks.py` | F5：非流 success 只 enqueue |
| `config/plans.yaml`（gitignored） | F3 末：新增 `glm-5.2-vision` 模型行 + logical_models.compose |
| `config/plans.example.yaml` | 示例，无密钥 |
| `.env.example` | 四个 flag + 两个目录 env，默认关 |
| `tests/unit/test_pipeline_envelope.py` | F1 |
| `tests/unit/test_vision_compose.py` | F3 |
| `tests/unit/test_vision_ir.py` | F3 |
| `tests/unit/test_internal_call.py` | F3 |
| `tests/unit/test_memory_workspace.py` | F4 |
| `tests/unit/test_memory_retrieve.py` | F4 |
| `tests/unit/test_memory_extract_queue.py` | F5 |
| `tests/unit/test_s5_composed_image_gate.py` | 回归：纯 GLM 仍拒；配方关仍 fail-closed |
| `docs/framework-upgrade/fixtures/vision-eval/manifest.json` | 评估集索引；原图放 `raw/`（勿提交含 PII 的截图） |

模块之间 **禁止** `vision_compose` import `memory_*` 或反向；只通过信封字段通信。

---

## 5. Module Breakdown

### M0 — 规格三件套

**Purpose:** 把提案 §16 P1 写成可验收行为，之后编码不再发明语义。  
**Risk:** Low。**Depends On:** 无（挂点已证）。**Estimate:** 0.5–1 天。

Deliverable: `pipeline.md` / `vision-compose.md` / `memory.md`。未完成不得开始 M2 的 MiniMax HTTP。

### M1 — Pipeline 信封

**Purpose:** 总开关 + 有序阶段 + 计时；阶段可为 no-op。关 flag 时现有测试全绿。  
**Risk:** Medium（会改 `strategy.py` 挂点，但 select 算法不改）。**Depends On:** M0。**Estimate:** 1 天。

Key types:

```python
@dataclass
class EnhanceEnvelope:
    model_group: str
    protocol: ApiProtocol | None
    streaming: bool
    messages: list[Any]          # 与 request_kwargs["messages"] 同一对象或同步写回
    workspace: str | None
    visual_evidence: list[str]
    memory_hits: list[str]
    internal_call: bool
    parent_request_id: str
    parent_quota_group_id: str
    stage_ms: dict[str, float]

class Stage(Protocol):
    name: str
    def enabled(self) -> bool: ...
    async def run(self, env: EnhanceEnvelope) -> None: ...
```

顺序写死：`vision` → `memory_retrieve`。（抽取不在这条链上。）

### M2 — 配方配置与 Discovery

**Purpose:** `compose` 进入 plans → generator → runtime；部署名 `glm-5.2-vision` 与 `glm-5.2` 共享额度组；discovery 能声明 vision。  
**Risk:** Medium（config 校验失败会让 `apply` 挂掉）。**Depends On:** M0。可与 M1 部分并行。**Estimate:** 1 天。

**生产广告门槛：** 配置可以先写在 example / 测试 fixture；**真人流量的 `plans.yaml` + discovery 只在 M3 翻译质量门可跑之后才打开。**

### M3 — 视觉翻译配方

**Purpose:** 真 MiniMax 译图、缓存、IR 门、全量替换、快速失败。  
**Risk:** High（嵌套选号、异步挂点、译文质量）。**Depends On:** M0、M1、M2。**Estimate:** 3–4 天（评估集是瓶颈）。

拆刀：M3a 子调用 + 缓存骨架（fake translator）；M3b IR + 评估集 + prompt；M3c 接线 live；M3d 上限与熔断。

### M4 — 记忆只读

**Purpose:** workspace 解析 + JSONL + 注入。无 workspace 则 skip。  
**Risk:** Medium（弱推断误绑定）。**Depends On:** M0、M1。**不依赖 M3。** **Estimate:** 1–2 天。

### M5 — 记忆写入

**Purpose:** 成功后入队；廉价模型抽取；规则脱敏。  
**Risk:** Medium（lease 回调里阻塞会卡死）。**Depends On:** M4。**Estimate:** 1–2 天。

### M6 — 观测与双客户端验收

**Purpose:** 计数、max 耗时、OpenCode 主路径 + 第二客户端同 workspace。  
**Risk:** Low。**Depends On:** M3（视觉）和/或 M4（记忆）按要验收的面。**Estimate:** 1 天。

依赖：`M0 ← M1 ← M3`；`M0 ← M2 ← M3`；`M0 ← M1 ← M4 ← M5`；`M3 ∥ M4`（F1 之后）。

---

## 6. Phase Roadmap

```text
F0 规格          ──► F1 信封/async 挂点 ──► F3 视觉配方 ──► F6a live 视觉
                 │                         ▲
                 └──► F2 compose 配置 ─────┘
                 │
                 └──► F4 记忆只读 ──► F5 记忆抽取 ──► F6b 双 app 共享
```

每一档结束必须：`GATEWAY_ENHANCE_ENABLED=false` 时现有 unit/contract **全绿**；Fill First / 同组 1 次 / 跨组 3 次 / 首字节后不换号 **无行为变化**。

建议可交付切片（各自可停）：

1. **F0+F1**：只有空流水线，生产零行为差。
2. **F2+F3**：`glm-5.2-vision` 可用（记忆仍关）。
3. **F4**：手写 JSONL 跨 app 共享（视觉可关）。
4. **F5**：自动抽取（可最后开）。

---

## 7. Risk Register

| Risk | Probability | Impact | Mitigation |
|------|-------------|--------|------------|
| 在同步 `get_available_deployment` 里打 MiniMax，堵死代理 | High | High | 翻译只 `await` 于 `async_get_available_deployment`；单测注入 fake |
| 子调用复用父 `litellm_call_id`，污染 tried / first_byte | Medium | High | `{parent}#vision:{hash8}`；禁止传入父 ctx |
| MiniMax 耗尽却把 Volc GLM 标 EXHAUSTED | Medium | High | 子调用 `quota_group_id` 必须 ≠ 父组，否则 fail-closed |
| `litellm.acompletion` 从策略内部再入代理，语义混乱 | Medium | High | V1 对翻译用 httpx 直打已选 deployment |
| 生产打开 `S5_STUB_PEEL` | Medium | High | vision flag 开则忽略 stub；文档与 apply 检查 |
| 评估集不足，质量门空转 | High | High | CI 用 3 张合成夹具；live 用 ≥20 张本地截图，不提交 PII |
| OpenCode 不发 `X-Workspace-Root` | High | Medium | 弱推断 tool/文件路径公共根；失败则跳过记忆，不用全局库 |
| 弱推断把两个无关仓库绑成同一 scope | Medium | High | 规范化 + 拒绝 `..`；infer 仅当 ≥2 条绝对路径共享根 |
| JSONL 并发写损坏 | Medium | Medium | F4 只读无此问题；F5 用进程内锁 + append；不跨进程写 |
| Docker 只改了宿主机 plugin，容器仍跑旧 COPY | High | High | 改 Python 必 `--build`；验收以容器内行为为准 |
| 同步测试路径漏跑 pipeline | Medium | Medium | 无图请求 sync 仍可跑记忆；有图无 fake translator 必须 fail-closed，测试会抓住 |
| 把 image 写进 `glm-5.2` 的 `supported_features` | Low | High | 配置校验禁止；S5 回归保留 |

---

## 8. Verification Checklist

开始写代码前：

- [x] 挂点假设已用 S1/S2/S5 验证（不必重做 P0）
- [ ] F0 三份规格已存在且覆盖下方「规格必写清单」
- [ ] 现有测试基线已跑绿（见下方命令）
- [ ] 不准备改 `upstream/litellm`
- [ ] 不准备把记忆写入 Redis `sq:*`

每档结束：

- [ ] `GATEWAY_ENHANCE_ENABLED` 默认 false，现网行为不变
- [ ] 纯 `glm-5.2` + image → 仍 `FEATURE_UNSUPPORTED`
- [ ] 合成模型出发 HTTP 无 image（mock `has_image` / `probe_marker_hit` 或抓包）
- [ ] 视觉失败无占位 caption
- [ ] 记忆失败不 500
- [ ] 改插件后 Docker 已 rebuild

基线命令（F0 前跑一次，记下失败即停）：

```powershell
cd E:\LiteLLMPro\local-llm-router
$env:PYTHONPATH = "plugins"
python -m pytest tests/unit tests/contract -q --tb=line
```

Expected: 全绿。若本机无 `.venv`，用已能跑通该套件的解释器。

---

## 9. 规格必写清单（F0，提案 §16 P1）

三份规格必须写死以下内容，不得留「以后再说」：

**pipeline.md：** 信封字段；阶段顺序；四 flag；`internal_call` 请求禁止再跑阶段；关 flag ≡ 未部署；禁止改 select 语义。

**vision-compose.md：** recipe YAML；effective=glm-5.2；translate=MiniMax-M3；全量历史 + tool_result；缓存键与 schema_ver；IR 白名单与 uncertain 阈值；张数/字节/token 上限；子调用 id / 深度 / quota 排他；熔断阈值；评估集流程；回滚（撤 discovery）。

**memory.md：** `X-Workspace-Root` 为唯一可信头；规范化（resolve、symlink、拒相对逃逸）；未知不检索不写入；JSONL 一行一条的 schema；注入标签 `<gateway_memory>`；2k token 顶；检索 ≤300ms fail-open；F4 只读 / F5 队列深度 32；抽取模型与父额度组互斥。

---

## 10. 可执行任务

### Task 0: 基线测试（不改业务）

**Files:** 无。

- [ ] **Step 1: 跑现有测试**

```powershell
cd E:\LiteLLMPro\local-llm-router
$env:PYTHONPATH = "plugins"
python -m pytest tests/unit tests/contract -q --tb=line
```

Expected: PASS。失败则先修基线，不开始 F0 以外的编码。

---

### Task 1: F0 — `pipeline.md`

**Files:** Create `docs/framework-upgrade/pipeline.md`

- [ ] **Step 1: 按 §9 写规格**，至少包含信封数据类字段表、阶段顺序图、四 flag 真值表、与 `strategy.py` 的挂点（async 选号后，不是 pre-call）。
- [ ] **Step 2: 明确同步 vs 异步：** 生产代理走 async；sync 路径不得阻塞打视觉模型。

---

### Task 2: F0 — `vision-compose.md`

**Files:** Create `docs/framework-upgrade/vision-compose.md`

- [ ] **Step 1: 写入冻结 recipe**（与本文 §3 一致）和 MiniMax system prompt 初稿（coding 截图、只译不解题、根 `<visual-evidence>`）。
- [ ] **Step 2: 定义评估集 `manifest.json` schema：** `id`, `expect_carrier` (`pre|code|html|table|reject`), `notes`。原图不入库。

---

### Task 3: F0 — `memory.md`

**Files:** Create `docs/framework-upgrade/memory.md`

- [ ] **Step 1: 写死 JSONL schema 与检索算法**（大小写折叠、按 token 重叠打分、硬顶 2k）。
- [ ] **Step 2: 写明 OpenCode 若无自定义头时的弱推断规则与失败则 skip。**

完成本 Task 1–3 之前，**不要**实现 MiniMax HTTP。

---

### Task 4: F1 — 信封与空阶段（TDD）

**Files:**
- Create: `local-llm-router/plugins/shared_quota_router/pipeline.py`
- Modify: `local-llm-router/plugins/shared_quota_router/feature_flags.py`
- Modify: `local-llm-router/plugins/shared_quota_router/strategy.py`（仅 async 路径在选号成功后调用 runner；**不要**改 Fill First 循环）
- Test: `local-llm-router/tests/unit/test_pipeline_envelope.py`

- [ ] **Step 1: 写失败测试**

```python
import pytest
from shared_quota_router.feature_flags import clear_flag_cache
from shared_quota_router.pipeline import EnhanceEnvelope, run_pipeline

@pytest.mark.asyncio
async def test_pipeline_noop_when_enhance_disabled(monkeypatch):
    monkeypatch.delenv("GATEWAY_ENHANCE_ENABLED", raising=False)
    clear_flag_cache()
    env = EnhanceEnvelope(
        model_group="glm-5.2",
        protocol=None,
        streaming=False,
        messages=[{"role": "user", "content": "hi"}],
        workspace=None,
        visual_evidence=[],
        memory_hits=[],
        internal_call=False,
        parent_request_id="r1",
        parent_quota_group_id="volc-c",
        stage_ms={},
    )
    original = list(env.messages)
    await run_pipeline(env)
    assert env.messages == original
    assert env.stage_ms == {}

@pytest.mark.asyncio
async def test_pipeline_skips_when_internal_call(monkeypatch):
    monkeypatch.setenv("GATEWAY_ENHANCE_ENABLED", "true")
    clear_flag_cache()
    env = EnhanceEnvelope(
        model_group="glm-5.2-vision",
        protocol=None,
        streaming=False,
        messages=[],
        workspace=None,
        visual_evidence=[],
        memory_hits=[],
        internal_call=True,
        parent_request_id="r1",
        parent_quota_group_id="volc-c",
        stage_ms={},
    )
    await run_pipeline(env)
    assert env.stage_ms == {}
```

- [ ] **Step 2: 跑测试，确认失败（模块不存在）**

```powershell
$env:PYTHONPATH = "plugins"
python -m pytest tests/unit/test_pipeline_envelope.py -v
```

Expected: `ModuleNotFoundError` 或 `cannot import run_pipeline`。

- [ ] **Step 3: 最小实现** — `EnhanceEnvelope`、`run_pipeline` 在 flag 关或 `internal_call` 时直接 return；flag 开时按声明列表调用空 stage（可先空列表）。`feature_flags.py` 增加 `is_gateway_enhance_enabled()` 默认 false。
- [ ] **Step 4: 测试转绿。** 再跑 `tests/unit tests/contract`，必须仍全绿。
- [ ] **Step 5: 把 runner 接到 `async_get_available_deployment`：** 先 `get_available_deployment(...)`，再 `await run_pipeline`，再把 `env.messages` 写回 `request_kwargs["messages"]`。有图的合成模型在 F3 之前仍走现有 `peel_composed_images_on_select`（stub 关 ⇒ fail-closed）。**不要**在这一步删除 S5 行为。

---

### Task 5: F2 — `compose` 配置

**Files:**
- Modify: `models.py`（`ComposeRecipe` + `LogicalModelProtocols.compose`）
- Modify: `config_schema.py` / `generator.py` / `logical_policy.py` 若需透传
- Modify: `composed_vision.py` `defers_image_gate` 读 `logical.compose is not None`
- Modify: `config/plans.example.yaml`（示例即可，注释说明生产勿提前广告）
- Test: `tests/unit/test_compose_recipe_config.py`

- [ ] **Step 1: 失败测试** — 解析

```yaml
logical_models:
  glm-5.2-vision:
    public_protocols: [anthropic_messages]
    advertised_features: [text, streaming, tools, reasoning, image]
    compose:
      execute_model: glm-5.2
      translate_model: MiniMax-M3
```

断言 `lm.compose.execute_model == "glm-5.2"`；`defers_image_gate("glm-5.2-vision", lm) is True`；`defers_image_gate("glm-5.2", glm_lm) is False`。

- [ ] **Step 2: 校验失败用例** — `execute_model == translate_model` 或找不到 translate 模型 ⇒ `ConfigValidationError`。同额度组也拒绝（MiniMax 与 Volc 必须不同 `quota_group_id`）。
- [ ] **Step 3: generator 把 compose 写进 `shared_quota_logical_models`。**
- [ ] **Step 4: 不要在这一步改操作者真实 `plans.yaml` 去广告 `glm-5.2-vision`。** 单测用临时 YAML。

`PlanModelEntry` 仍无 image：`glm-5.2-vision` 部署 `supported_features` 与 `glm-5.2` 相同（无 `image`）。Vision 只来自 logical `advertised_features`。

---

### Task 6: F2 — Discovery 声明 vision

**Files:** `discovery.py`；`tests/unit/test_m1_05_capability_discovery.py`（或新建 `test_discovery_vision.py`）

- [ ] **Step 1:** `ModelCapability` 增加 `advertised_features: frozenset[str] = frozenset()`；`to_capability_dict` 在非空时输出 `features` 列表。
- [ ] **Step 2:** 纯 `glm-5.2` 的 capability **不含** `image`。
- [ ] **Step 3:** 无 compose / flag 关时，即使 YAML 写了 vision 名，测试应覆盖「可配置但不出现在生产目录」——实现上：`VISION_COMPOSE_ENABLED` 为 false 则 discovery **省略** 带 compose 的模型。这样提前写进 plans 也不会被客户端当成能看图。

---

### Task 7: F3a — `internal_call` + 文件缓存（fake translator）

**Files:**
- Create: `internal_call.py`, `vision_cache.py`
- Modify: `vision_compose.py`（或新建后由 pipeline 调用）
- Test: `tests/unit/test_internal_call.py`, `tests/unit/test_vision_cache.py`

- [ ] **Step 1: 测试 call id**

```python
from shared_quota_router.internal_call import child_request_id

def test_child_request_id_distinct():
    pid = "abc-parent"
    a = child_request_id(pid, "vision", "deadbeef")
    assert a.startswith("abc-parent#vision:")
    assert a != pid
```

- [ ] **Step 2: 测试 quota 排他** — 父 `volc-c`、子也 `volc-c` ⇒ 抛 `ProtocolAwareRoutingError`（CONFIGURATION_INVALID）。子 `minimax-official` ⇒ 通过。
- [ ] **Step 3: 测试深度** — metadata `internal_call=true` 的信封不跑 vision stage（复用 Task 4）。
- [ ] **Step 4: 缓存** — 同一 PNG 字节 → 同一 sha256；put/get 往返；schema_ver 不同则 miss。
- [ ] **Step 5: fake translator** — 不打网：有图合成模型，注入 `async def fake_translate(png) -> str` 返回合法 `<visual-evidence><pre>x</pre></visual-evidence>`，断言 messages 无 image、有该文本；`tool_result` 内嵌图同样被替换。
- [ ] **Step 6: 无 fake、无 stub、有图** — 仍 fail-closed（保持 S5 单测意图）。

---

### Task 8: F3b — IR 质量门 + 评估夹具

**Files:** `vision_ir.py`；`tests/unit/test_vision_ir.py`；`docs/framework-upgrade/fixtures/vision-eval/manifest.json`

- [ ] **Step 1: 合法样本通过** — 根 `visual-evidence`，子节点仅 `pre`/`code`/`table`/`p`/`ul`/`ol`/`li`/`span`/`div`（规格白名单以 `vision-compose.md` 为准）。
- [ ] **Step 2: 拒绝** — `<script>`、`<html>`、`javascript:`、空壳、uncertain 占比过高、空字符串。
- [ ] **Step 3: CI 夹具至少 3 条**（终端文本图 / 拒绝风景说明用「非截图」占位标记 / 表格）。真 20 张截图只放本机 `fixtures/vision-eval/raw/`，gitignore，不阻塞 CI。
- [ ] **Step 4:** live prompt 迭代在本机对评估集跑；改 prompt 只改 `vision-compose.md` + 代码里的常量，不放宽质量门去「让 GLM 猜」。

---

### Task 9: F3c — 接线 MiniMax 并生产启用 `glm-5.2-vision`

**Files:** `vision_compose.py`；`strategy.py`；操作者 `config/plans.yaml`；Docker rebuild

- [ ] **Step 1: 选 MiniMax 部署** — 对 `translate_model` 调现有 `get_available_deployment`（子 request_id、`internal_call` metadata）。取出 `api_base` / key **不得打进日志**。
- [ ] **Step 2: `httpx.AsyncClient` POST** `{api_base}/v1/messages`（与现网 MiniMax Anthropic 基址规则一致：LiteLLM 对 Anthropic 会拼 `/v1/messages`，直打时自己拼对，对照探针 A 脚本）。
- [ ] **Step 3: 替换 S5 peel 委托：** `VISION_COMPOSE_ENABLED` 时走翻译；否则合成模型有图 fail-closed。**永远不要**把像素发给 `glm-5.2`。
- [ ] **Step 4: 操作者 plans** — 在 **Volc 那个 plan 的 models 列表**增加 `glm-5.2-vision`（features 与 glm-5.2 相同，无 image）；`logical_models` 增加 compose。然后：

```powershell
python -m shared_quota_router.cli_config apply
# Docker：改了 plugin 必须 build
```

- [ ] **Step 5: live 对照**
  - `glm-5.2` + 图 → 400
  - `glm-5.2-vision` + 图 → 200，mock/抓包出发 `has_image=false`，助手不引用「图片已省略」
  - 无图打 `glm-5.2-vision` → 不调 MiniMax（计数 `vision_translate_skipped`）
- [ ] **Step 6: 同一张图第二轮** — 缓存命中，MiniMax 计数不增加。

---

### Task 10: F3d — 上限与模块熔断

**Files:** `vision_compose.py`；`metrics.py` 计数

- [ ] 超过 6 张 / 5MiB / 12MiB → fail-closed，错误信息可给客户端，不含 base64。
- [ ] 连续失败 N 次（规格写死，建议 3）后窗口内直接拒绝，**禁止**改用 stub 文本。
- [ ] 计数：`enhance_vision_ok` / `enhance_vision_fail` / `enhance_vision_cache_hit` / `enhance_vision_circuit_open`。

---

### Task 11: F4 — workspace + JSONL 只读注入

**Files:** `memory_workspace.py`, `memory_store.py`, `memory_retrieve.py`；对应 unit tests

- [ ] **规范化：** `C:\foo\..\bar` 与 `C:\bar` 相同；`..\..\etc` 相对路径 ⇒ None；空字符串 ⇒ None。
- [ ] **可信头：** `X-Workspace-Root` / metadata `workspace_root`。
- [ ] **弱推断：** 仅绝对路径；少于 2 条则失败。
- [ ] **未知：** `retrieve` 不读盘、不注入。
- [ ] **注入：** 用户消息前或动态尾部插入 **user** 文本块 `<gateway_memory>...</gateway_memory>`（规格定位置；不得改 role=system）。
- [ ] **超时：** 用时间预算 300ms，超时 skip（单测用 fake store sleep）。
- [ ] **手写夹具：** 两行 JSONL，关键词能命中；无关词不命中。
- [ ] **跨路径：** 同一规范化 workspace 的两个「客户端」读同一文件（单测两次 retrieve）。
- [ ] Flag 关：messages 不变。Flag 开、视觉关：仍可注入。

---

### Task 12: F5 — 抽取队列（在 F4 跨 app 手写验证之后）

**Files:** `memory_extract.py`；`stream_lifecycle.py`；`callbacks.py`

- [ ] `on_stream_complete` / `async_log_success_event` **只** `queue.put_nowait`；队列满丢弃 + 打日志。
- [ ] 后台 task 打廉价模型；失败不写库。
- [ ] 抽取 `quota_group_id` ≠ 父执行组。
- [ ] 规则脱敏：API key 形态、`sk-`、Bearer 不入库（测固定字符串）。
- [ ] 进程退出放弃队列，不持久化作业。
- [ ] `GATEWAY_MEMORY_EXTRACT_ENABLED` 默认 false。

---

### Task 13: F6 — 回归与 live 清单

- [ ] `GATEWAY_ENHANCE_ENABLED=false`：`pytest tests/unit tests/contract` 全绿。
- [ ] 视觉开、记忆关：带图 `glm-5.2-vision` 成功；无 `<gateway_memory>`。
- [ ] 视觉关、记忆开：无图请求可注入；带图打纯 `glm-5.2` 仍 400。
- [ ] OpenCode Messages 选 `glm-5.2-vision` 贴一张 IDE 截图，确认修代码依据译文而不是「我看不到图」。
- [ ] 第二客户端同一 `X-Workspace-Root`（或可推断的同一根）读到手写记忆。
- [ ] 写短报告：`docs/framework-upgrade/reports/vision-compose-m3.md`、`memory-m4.md`（无密钥、无完整 prompt、无图 base64）。

---

## 11. 回滚 SOP

1. `VISION_COMPOSE_ENABLED=false`（合成模型从 discovery 消失或带图 fail-closed，取决于 Task 6 实现；两者都比「把图送给 GLM」安全）。
2. `GATEWAY_MEMORY_EXTRACT_ENABLED=false`，然后 `GATEWAY_MEMORY_ENABLED=false`。
3. `GATEWAY_ENHANCE_ENABLED=false`。
4. 从 `plans.yaml` 删除 `glm-5.2-vision` 并 `cli_config apply`。
5. 需要时 rebuild 回退镜像。不要 flush Redis 额度键。记忆 JSONL 可留盘。

---

## 12. 明确不在本方案

- Chat `image_url` 合成模型、Responses。
- 向量检索、embedding 近邻缓存、DAG 流水线。
- 在 `async_pre_call_hook` 里 mutate messages。
- 给 `glm-5.2` 加 `image` feature。
- 把 S5 stub 当生产译文。
- Histogram / OpenTelemetry。
- 自动 `git push`。

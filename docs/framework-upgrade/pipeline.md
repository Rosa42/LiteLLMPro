# 请求增强流水线 — 规格

| 项 | 值 |
| --- | --- |
| 文档类型 | 规格（spec） |
| 状态 | **已落地**（F1 信封 + 视觉/记忆阶段；internal 只认 ContextVar） |
| 日期 | 2026-08-30 |
| 实现落点 | `local-llm-router/plugins/shared_quota_router/pipeline.py` |
| 挂点 | `SharedQuotaRoutingStrategy.async_get_available_deployment`，选号成功之后 |
| 依据 | `design-proposal.md` §5–§6；`plans/2026-08-25-vision-and-memory.md` |

本文冻结信封、阶段顺序、flag 真值表、同步/异步职责。不定义 MiniMax prompt 或 JSONL schema（见 `vision-compose.md` / `memory.md`）。

---

## 1. 不变量

1. 关闭 `GATEWAY_ENHANCE_ENABLED` 后，行为等于未部署增强层：现有 unit / contract 必须全绿；Fill First / affinity / tried / lease / 首字节后不换号不变。
2. 流水线 **不得** 改变额度选号语义。允许在选号成功之后改 `request_kwargs["messages"]`（及若不同对象的 named `messages`）。
3. 阶段之间禁止互相 import 实现；只通过信封传数据。顺序由本文声明，不是 DAG。
4. `sq_trusted_internal` ContextVar 为真的选号 **禁止** 再跑任何增强阶段。最大嵌套深度 1。**禁止**把客户端 metadata `internal_call` 当作此条件（见 `specs/2026-08-28-composable-recipes-design.md` §8.1）。
5. 失败策略属于阶段自身：视觉 fail-closed；记忆 fail-open。内核 Redis fail-closed 不延伸到记忆。
6. 禁止在 `async_pre_call_hook` 里剥图、译图、注入记忆（探针 B FAIL）。
7. 用户可见流只来自执行模型。阶段必须在出发上游 HTTP **之前** 结束。
8. V1 注册表是声明式有序列表 + `Stage` protocol，不做动态插件加载。

---

## 2. Feature flags

全部默认 **false**。读法与现有 `_env_bool` 相同（`1` / `true` / `yes` / `on`）。改 env 后测试须 `clear_flag_cache()`。

| Flag | 作用 |
| --- | --- |
| `GATEWAY_ENHANCE_ENABLED` | 总开关。false ⇒ `run_pipeline` 立即返回，不跑任何阶段 |
| `VISION_COMPOSE_ENABLED` | 视觉阶段。还要求总开关为 true。**只**作用于 `template=vision`（旧 YAML 无 template 视为 vision）；不得用它省略将来的非视觉配方。项目 discovery **省略**视觉门面，除非总开关与本 flag **同时**为 true。stock `GET /v1/models` 不受此过滤（LiteLLM `model_list`） |
| `GATEWAY_MEMORY_ENABLED` | 记忆检索阶段。还要求总开关为 true |
| `GATEWAY_MEMORY_EXTRACT_ENABLED` | 记忆写入/抽取。还要求总开关与 `GATEWAY_MEMORY_ENABLED` 为 true |

真值表：

| 总开关 | vision | memory | extract | 行为 |
| --- | --- | --- | --- | --- |
| 0 | * | * | * | 今天的网关；S5 stub 仍仅探针 env |
| 1 | 0 | 0 | 0 | 空流水线（计时字典可空）；S5 视觉模板有图仍 fail-closed |
| 1 | 1 | 0 | 0 | 只视觉 |
| 1 | 0 | 1 | 0 | 只记忆检索 |
| 1 | 1 | 1 | 0 | 视觉 → 记忆检索 |
| 1 | 1 | 1 | 1 | 检索在选号后；抽取在成功回调入队 |
| 1 | * | 0 | 1 | 抽取 flag 被忽略（检索关则写入也关） |

`VISION_COMPOSE_ENABLED=true` 时 **忽略** `S5_STUB_PEEL`：不得用占位字符串当译文。

---

## 3. 信封

Python 名 `EnhanceEnvelope`。`messages` 必须写回 `request_kwargs["messages"]`；若 LiteLLM 另传 named `messages` 且不是同一对象，两端都要改。

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `model_group` | `str` | 客户端看见的逻辑模型（视觉门面为配方 id，预置实例为 `glm-5.2-vision`） |
| `protocol` | `ApiProtocol \| None` | 公开协议 |
| `streaming` | `bool` | 是否流式 |
| `messages` | `list` | Anthropic Messages 形态；阶段就地改写 |
| `workspace` | `str \| None` | 规范化后的工作区绝对路径；未知为 None |
| `visual_evidence` | `list[str]` | 本轮通过质量门的译文（供记忆检索当查询文本） |
| `memory_hits` | `list[str]` | 本轮注入的记忆正文 |
| `internal_call` | `bool` | 子调用标记 |
| `parent_request_id` | `str` | 父 `litellm_call_id` |
| `parent_quota_group_id` | `str` | 父执行部署的额度组（选号结果） |
| `stage_ms` | `dict[str, float]` | 各阶段耗时毫秒 |
| `headers` | `Mapping \| None` | 可选；供 workspace 读 `X-Workspace-Root` |
| `translator` | 可选注入 | 测试用 fake；生产为 None |

V1 **不为「剩余预算」定义语义**。不要加 `budget_remaining` 字段。

---

## 4. Stage protocol

```python
class Stage(Protocol):
    name: str  # "vision" | "memory_retrieve"
    def enabled(self) -> bool: ...
    async def run(self, env: EnhanceEnvelope) -> None: ...
```

声明顺序（写死）：

1. `vision`（仅当 `is_vision_compose`；其它逻辑名本阶段空操作）
2. `memory_retrieve`

记忆检索必须能读到 `visual_evidence`。无图时 vision 是空操作（不调 MiniMax），记忆仍可跑。

记忆抽取 **不是** 这条链上的 Stage；见 `memory.md`。

`run_pipeline(env)`：

1. 若 `not is_gateway_enhance_enabled()` 或 `env.internal_call` 或 ContextVar `sq_trusted_internal`：return（`stage_ms` 保持空）。
2. 否则按顺序：若 `stage.enabled()` 则计时并 `await stage.run(env)`。
3. 阶段抛出的 `ProtocolAwareRoutingError` 原样向上；记忆阶段不得把 fail-open 变成 raise。

---

## 5. 同步 vs 异步挂点

| 路径 | 职责 |
| --- | --- |
| `get_available_deployment`（sync） | **只**做现有选号 + convert + 现有 S5 peel/fail-closed + S1 marker。**禁止**在此做 MiniMax HTTP |
| `async_get_available_deployment` | 先调用 sync select；成功后若总开关开，则 `await run_pipeline`，再写回 messages |

生产 LiteLLM proxy 走 async 路径。

`async_get_available_deployment` **先调用**同步 `get_available_deployment`，因此不能在同步函数里无条件「有图就 400」（会误伤生产 async）。用进程内 `contextvars.ContextVar`（建议名 `sq_vision_async_select`）区分调用方。详见 `vision-agent-prompt-presets.md` §4.1。

Sync 路径遇到「**视觉模板** + 有图 + `VISION_COMPOSE_ENABLED`」（谓词 `is_vision_compose`，见 `specs/2026-08-28-composable-recipes-design.md`；禁止把任意 `compose` 当成视觉）：

- **公开** sync 入口（Var 为 false）：立刻 `FEATURE_UNSUPPORTED`，`details.vision=sync_path`。V1 **不做**同步读缓存剥图。
- **async 入口内部**的 sync select（Var 为 true）：跳过剥图，由随后的 vision stage 翻译。
- 视觉关：保持 S5（stub 关 ⇒ fail-closed；stub 开仅探针）。

单测用 `env.translator` / 依赖注入在 **async** 测试里覆盖翻译。禁止在同步 `get_available_deployment` 里打 MiniMax HTTP。

---

## 6. 子调用隔离（流水线侧）

信封 `internal_call` 供测试直接构造跳过；生产路径在 strategy 里认 ContextVar，公开请求即使 metadata 带 `internal_call` 也不置位。子调用 id 形态由 `vision-compose.md` / `memory.md` 规定：`{parent}#vision:{hash8}`、`{parent}#memory-extract:{hash8}`。

子调用 `quota_group_id` **不得等于** `parent_quota_group_id`；违反则 fail-closed（配置错误），`reason=CONFIGURATION_INVALID`。不得把父 `RequestRoutingContext` 传入子路由。

---

## 7. 观测

复用 `metrics.py` 的 counter / gauge。V1 阶段耗时写入 `stage_ms` 并 `inc` 成功/失败计数；进程内可用 gauge 记该阶段 max 毫秒。不加 histogram，不引入 OpenTelemetry。日志可带 `parent_request_id` + `stage`，**禁止**打印 API Key、完整 prompt、图片 base64。

建议计数名（实现时可加前缀，但测试应对这些语义）：

- `enhance_pipeline_run`
- `enhance_vision_ok` / `enhance_vision_fail` / `enhance_vision_skipped`
- `enhance_memory_ok` / `enhance_memory_skip`

---

## 8. 回滚

1. `GATEWAY_ENHANCE_ENABLED=false`（或进程重启后读到 false）。
2. 不得留下半剥的 image block：关视觉后，**视觉模板**门面要么从 discovery 消失，要么带图明确失败。非视觉 `compose` 不得走这条省略逻辑（V1 尚无此类模板）。
3. 不 flush Redis `sq:*` 额度键。

---

## 9. 验收（F1）

- [x] flag 默认关时 `run_pipeline` 不改 messages、`stage_ms == {}`
- [x] 可信 ContextVar 子选号：`run_pipeline` 不改 messages、`stage_ms == {}`；客户端 metadata `internal_call` **不能**单独触发该跳过
- [x] `async_get_available_deployment` 在 select 之后调用 runner（可用 spy/计数证明）
- [x] `GATEWAY_ENHANCE_ENABLED=false` 时 `tests/unit` + `tests/contract` 全绿
- [x] 未改 Fill First / 同组 1 次 / 跨组 3 次

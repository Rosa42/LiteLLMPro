# 可配置视觉配方 — 设计规格（V1 契约修订稿）

| 项 | 值 |
| --- | --- |
| 文档类型 | 规格（design spec） |
| 状态 | **V1 已落地**（2026-08-30）。schema / CLI / internal select / outcome / cache / discovery 双 flag 已实现。现网切 `glm-5.3` 是操作者 apply，见 [`../maintenance.md`](../maintenance.md) |
| 日期 | 2026-08-30 |
| 修订 | 第二轮审查 6 个 P0 已写入契约；编码按 §11 反例 + unit/contract 验收 |
| 实现落点 | `config_schema.py` / `generator.py` / `composed_vision.py` / `discovery.py` / `vision_compose.py` / `strategy.py` / `internal_call.py` / `compose_mutator.py` / `cli_config.py`；宿主机 `llm-router.ps1` |
| LiteLLM | 钉死 v1.90.5；不改 `upstream/litellm` 业务逻辑；G0-B |
| 依据 | 现网 `glm-5.2-vision`；`vision-compose.md`；`pipeline.md` 不变量 2；Q6；两轮对抗审查 |

现网 `glm-5.2-vision` 是**视觉模板的一条预置实例**。V1 把槽位变成可配置，并补齐选号、校验、血缘、故障上报与写盘事务。**不做**重思考、**不做**自动 fallback、**不在** LiteLLM 容器里改 YAML。

基线：固定 `glm-5.2-vision` 的现有测试保持全绿，且 §11 配置/运行时反例单测必须绿。编码验收（2026-08-30）：`tests/unit` + `tests/contract` **435 passed, 1 skipped**。

---

## 0. 第一轮裁定（仍成立）

| # | 裁定 |
| --- | --- |
| 1 | V1 无 rethink。V2：worker 之间额度组两两不同；summary 可与某一个 worker 同组。 |
| 2 | CRUD 只在宿主机。不改 compose volume，不把 PUT 放进 proxy。 |
| 3 | `template` 是运行时判别。缺省 ⇒ `vision`。 |
| 4 | 无自动 fallback。译图失败 400。不换逻辑模型。 |
| 5 | Generator **不**发明 `model_list` 行。Mutator 双写 `plans[]` 与 `logical_models`。视觉仅 Messages。 |

---

## 0.1 第二轮 P0 裁定（本修订新增）

| # | 问题 | 契约 |
| --- | --- | --- |
| P0-1 | 槽位不公开 vs strategy 无条件 `model_opts_into_public` | 可信 **internal select API**（进程内 ContextVar）。只绕过 public opt-in。**禁止**用客户端可写的 `internal_call` metadata 作为绕过条件。 |
| P0-2 | 「有任意 enabled 部署」当候选；子选号用纯文本占位 | Translator 必须有 `anthropic_messages` **且** `IMAGE` 的路由。子选号 `required_features={text,image}`。额度组只在这些 eligible 路由上比较。 |
| P0-3 | 门面行可出现在 execute 以外的 plan；generator 把 GLM 名配 MiniMax 钥 | `apply` 校验：门面 enabled Messages 行所在 plan 集合 **等于** execute 的 eligible Messages plan 集合。多余 / 错位行失败。 |
| P0-4 | 槽位可指向另一门面；generator 只展开一层 | 任何带 `compose` 的逻辑名、门面自身，都不得做 execute/translate。显式自引用与环检测。 |
| P0-5 | 子 HTTP 不进 `on_failure`；全局视觉熔断误伤 | 子调用必须 `report_internal_outcome`（原始状态码 + deployment meta）。熔断按 `(translate_model, quota_group_id)` 分桶，禁止进程级全局一开全挡。 |
| P0-6 | `plans.yaml` 非事务；`remove --id` 可删普通模型 | 文件锁 + 内存 candidate + 完整 validate/render + 双文件备份/原子写 + 输出失败回滚。删除前确认视觉门面且无被引用。 |

---

## 1. 目标与非目标

**目标**

- 宿主机创建 / 更新 / 删除视觉门面：`execute_model` + `translate_model`。
- 事务性写入 `config/plans.yaml`，再生成 `config/litellm.yaml`（`apply` 必须带 `--enable-messages-chat-native`）。
- apply 成功后重启 litellm 容器。plugin 代码未改则不必 `--build`。
- 内部译图模型**可以**没有 `public_protocols`；运行时仍能被 **internal select** 选中。
- Fill First / 同模型换账号 / 流式首字节后不换上游不变。
- 预置 `glm-5.2-vision` 同一 `VisionComposeStage`，槽位从配方读。

**非目标**

- 重思考、fallback、容器内管理 API、Chat 视觉。
- 把像素送给 execute。
- 把已有非门面逻辑名改成门面（`glm-5.2`、`MiniMax-M3` 等）。
- 信任 HTTP / LiteLLM metadata 里的 `internal_call`。
- 用 `ascii_safe` 静默改写模型 id。
- 声称 stock `GET /v1/models` 会按 feature flag 省略门面（该端点是 LiteLLM 的 `model_list`，插件过滤不到）。

---

## 2. 运行时判别：`template`

谓词 `is_vision_compose(logical)`：`compose is not None` 且 `template in {None, "vision"}`。其它 `template` → `ConfigValidationError`。

存盘**只**写规范键：`template`、`execute_model`、`translate_model`。读入时允许别名 `reasoning`/`vision`，但与规范键同时出现必须一致；**写出时删除别名**（禁止双重真相）。未知 compose 键 → `ConfigValidationError`（现网 parser 忽略未知键，V1 改为拒绝）。禁止 `fallback`。

| 点 | V1 |
| --- | --- |
| `defers_image_gate` | `is_vision_compose`（`S5_COMPOSED_MODELS` 探针覆盖不变） |
| 项目 discovery 省略 | 视觉模板，且 **并非**（`GATEWAY_ENHANCE_ENABLED` **且** `VISION_COMPOSE_ENABLED`） |
| S5 / VisionComposeStage | 仅视觉模板 |
| stock `/v1/models` | 只要门面在生成的 `model_list` 里就会出现；**不**作为「flag 关则从客户端消失」的验收 |

`pipeline.md` 阶段顺序不变：`vision` → `memory_retrieve`。

---

## 3. 配置真相

整份 `plans.yaml`：`plans[].models` **和** `logical_models`。Generator 只遍历 plan 行，视觉门面把 `litellm_params.model` 上游名换成 `execute_model`。因此门面行必须和 execute 的 Messages plan **血缘一致**（§5.3），否则会把 execute 的模型名配到错误供应商的 `api_base`/`api_key`。

---

## 4. YAML 形状

**逻辑名 / 门面 id**（execute、translate、门面三者）：

```
^[A-Za-z][A-Za-z0-9._-]{1,63}$
```

Validate **拒绝**不合规 id。Generator 对 **model_name / execute_model / translate_model** 禁止 `ascii_safe` 改写；非 ASCII 在 validate 阶段失败，不得变成 `_` 后碰撞。

Plan 上门面行必须带 `facade_role: vision`。这是 apply 可检查的身份，不依赖「改之前的历史」：

- 有 `logical_models[F].compose` ⇒ 每个 enabled `model==F` 的行必须 `facade_role: vision`。
- 无 compose ⇒ 禁止该 id 的任何行带 `facade_role`。
- 给已有**普通**模型（无 `facade_role` 的 plan 行，如 `glm-5.2`）加上 `compose` → 失败。

预置迁移：现网 `glm-5.2-vision` 行可能还没有 `facade_role`。Schema 允许 **仅该 id** 缺省视为 `facade_role: vision`；mutator / 下一次成功 apply **必须写出**该字段。其它 id 不享受缺省。

```yaml
plans:
  - id: volc-c-msg
    models:
      - model: glm-5.2
        upstream_protocol: anthropic_messages
        supported_features: [text, streaming, tools, reasoning]
      - model: glm-5.2-vision
        facade_role: vision
        upstream_protocol: anthropic_messages
        supported_features: [text, streaming, tools, reasoning]  # 无 image

logical_models:
  glm-5.2-vision:
    public_protocols: [anthropic_messages]
    advertised_features: [text, streaming, tools, reasoning, image]
    compose:
      template: vision
      execute_model: glm-5.2
      translate_model: MiniMax-M3
```

`advertised_features` **由公式生成**，禁止手写一套与公式不同的列表（mutator 覆盖；validate 检查相等）：

```
base = execute 的 logical.advertised_features
     若空：execute 所有 eligible Messages 部署 supported_features 的交集
advertised(F) = (base ∪ {image})
```

`streaming`/`tools`/`reasoning` 不得比 execute 的 base 多。部署 `supported_features` 仍不得含 `image`。

---

## 5. 校验（`validate` / `apply` / mutator 同一套）

定义 **eligible 路由**：

```
eligible(doc, model, protocol, features) =
  { (plan.id, quota_group_id) |
    enabled 行 model 匹配
    and resolved_protocol == protocol
    and features ⊆ resolved_features }
```

### 5.1 槽位（P0-2、P0-4）

| 槽位 | eligible 条件 |
| --- | --- |
| `execute_model` | `anthropic_messages` + `{text}`（及其它 execute 部署已有能力，**不含**要求 IMAGE） |
| `translate_model` | `anthropic_messages` + `{text, image}` |

- 二者不必 public opt-in，也不必有 `logical_models` 条目。
- 额度组：`quota_groups(eligible(execute))` 与 `quota_groups(eligible(translate))` **不相交**。不得用「该名字在任意协议下的所有组」比较（Chat-only 行不计入）。
- `execute_model != translate_model`。
- **禁止**槽位指向：门面 id 自身；任何 `logical_models[x].compose is not None` 的 x；任何带 `facade_role` 的 model_group。
- 自引用：`F ∈ {execute, translate}` → 失败。
- 环：把 compose 图看成有向边 `F → execute`、`F → translate`，图中有环 → 失败。V1 禁止 compose 节点做槽位后，环主要来自手改互相指向；仍须显式 DFS，不要只靠「禁止 compose 槽位」的口头推论。

反例（必须失败）：`translate_model: MiniMax-M2.7`（现网 Messages 部署无 IMAGE）；`execute_model: glm-5.2-vision`；两个门面互相 execute。

`compose-vision-slots`：reasoning 候选 = eligible(messages, {text}) 且非门面；vision 候选 = eligible(messages, {text,image}) 且非门面，并与已选 reasoning 额度组不相交。现网 MiniMax-M2.7 **不得**出现在 vision 列表；MiniMax-M3 若有 IMAGE 则可。

### 5.2 门面公开协议

`public_protocols` 恰好 `{anthropic_messages}`。

### 5.3 Plan 血缘（P0-3）

```
E_plans = {plan_id | (plan_id, _) ∈ eligible(execute, messages, {text})}
F_plans = {plan.id | enabled 行 model==F 且 resolved_protocol==messages}
```

必须 `F_plans == E_plans`。任一门面行所在 plan 必须同时有 execute 的 eligible 行。禁止把 `glm-5.2-vision` 放进 MiniMax plan（反例：生成 `anthropic/glm-5.2` + `MINIMAX_*` 凭据）。

update 更换 execute：mutator 在内存里删光旧 `F` 行再按新 execute 注入，**然后**跑完整 validate；不得出现「已删未注入」的中间落盘。

---

## 6. 宿主机 mutator 与事务（P0-6）

子命令仍是 `compose-vision-slots|add|update|remove|export`。`glm-5.2-vision` 的 remove/update 无 `--force` 则拒绝。禁止读写 `.env` / 打印 key。

### 6.1 注入字段

对每个 `plan_id ∈ E_plans` 写入 / 覆盖 `F` 行：

| 字段 | 值 |
| --- | --- |
| `model` | `F`（已通过 id regex） |
| `facade_role` | `vision` |
| `upstream_protocol` | `anthropic_messages` |
| `supported_features` | 该 plan 上 execute 行的 `resolved_features − {image}` |
| `supports_streaming` | 该 plan 上 execute 行的 streaming |
| `litellm_params.timeout`（生成侧） | `max(现网默认 300, MAX_IMAGES * translate_timeout_s + execute_budget_s)`。V1：`MAX_IMAGES=6`，`translate_timeout_s=60`，`execute_budget_s=120` ⇒ **至少 480**。lease TTL = timeout+30，须盖住 6 张串行译图 + 执行。 |
| conversions | 不复制 |

不要注入 Chat plan、disabled plan、translate 所在 plan。

### 6.2 事务协议

对 `plans.yaml` **和** `litellm.yaml`：

1. 获取 `plans.yaml` 文件锁（进程间，Windows 可用 `msvcrt.locking` 或等价；锁文件 `config/plans.yaml.lock`）。
2. 读入 plans → **内存** mutate。
3. `validate_plans_document`。
4. `render_litellm_yaml(..., enable_messages_chat_native=与 llm-router.ps1 相同)`。
5. 时间戳备份 **两份**源文件（`config/backups/`，现网已为 litellm 做；plans 同样做）。
6. 原子替换 `plans.yaml`（temp + replace）。
7. 原子替换 `litellm.yaml`。若步骤 7 失败：用步骤 5 的 plans 备份 **恢复** `plans.yaml`，保留失败前的 litellm，返回非 0。不得留下「新 plans + 旧 litellm」而不报错。
8. 释放锁。提示重启 litellm。

validate 失败：不写任何文件。

### 6.3 remove

在 mutate 前硬检查：

1. `logical_models[id].compose` 为视觉模板，或该 id 的 plan 行带 `facade_role: vision`。否则 **拒绝**（防止 `remove --id glm-5.2`）。
2. 不存在另一门面的 execute/translate 等于该 id。
3. 再从所有 plan 行与 `logical_models` 删除。

---

## 7. Generator

- 只渲染 `plan.models`。血缘错误不得靠 generator「猜对」凭据；必须在 validate 失败。
- 视觉门面：上游名 = `execute_model`（已保证与该 plan 的 execute 同行）。
- 写出 `compose` 仅规范三键；写出 `facade_role`。
- 门面 `timeout` 按 §6.1。
- apply 必须保留 `--enable-messages-chat-native` 语义。

---

## 8. 运行时

挂点不变。Pipeline 不得改 deployment / 释父租约 / 换 `model_group`。

### 8.1 可信 internal select（P0-1）

新增 plugin 内 API（名可 `select_internal_deployment`），**仅** Python 调用：

```
select_internal_deployment(
    model_group,
    *,
    protocol=anthropic_messages,
    required_features=frozenset({text, image}),  # 译图
    parent_request_id,
    parent_quota_group_id,
) -> model_list entry
```

实现约束：

- 用 **ContextVar**（建议 `sq_trusted_internal`）标记本次选号。Var 为真时 strategy **跳过** `model_opts_into_public`，仍执行：协议过滤、`required_features`、enabled、Fill First、quota 状态、冷却、与 `parent_quota_group_id` 互斥。
- **禁止**根据 `request_kwargs` / `litellm_metadata` / `metadata` 里的 `internal_call` 跳过 opt-in。客户端可写该字段。
- `pipeline.is_internal_call`：公开请求即使 metadata 带 `internal_call` 也视为 false。只有 ContextVar 为真才跳过增强阶段（子调用选号期间）。
- **记忆抽取**现网同样靠 metadata `internal_call`。改此谓词时必须让 `memory_extract` 走同一 ContextVar / `select_internal_deployment`，否则抽取子选号会误跑 pipeline 或被 public opt-in 挡住。V1 配方施工把这条列为 **必改回归**，不是可选项。
- 子 `litellm_call_id` 仍为 `{parent}#vision:{hash8}`。

译图选号的 `required_features` **必须**含 `image`。禁止再用纯文本 `"Translate this screenshot."` 作为选号 messages 却不声明 IMAGE——`extract_required_features` 会只得到 TEXT，从而选中 MiniMax-M2.7。允许：trusted API 直接传入 features；或选号 messages 含 image block。V1 规定 **API 显式传 `{text,image}`**，不依赖占位 messages 扫描。

Execute 门面的用户可见选号仍走公开路径（门面有 public opt-in）。

### 8.2 子 HTTP 与 outcome 上报（P0-5）

译图仍走现网 `anthropic_direct` 式本机 URL（像素不进 LiteLLM acompletion）。在释放 child lease **之前**：

```
report_internal_outcome(
    kwargs 含 child call id 与 deployment model_info,
    success: bool,
    status_code: int | None,
    exception: BaseException | None,
)
```

必须进入与 `SharedQuotaCallback.on_success` / `on_failure` **同一套** classifier → deployment cooldown → quota exhaustion 更新。429/401 **先**上报，再把用户可见错误映射为现有视觉 `FEATURE_UNSUPPORTED`。禁止先折叠成 unsupported 导致「只 release lease、不记故障」。

视觉熔断键：`(translate_model, child_quota_group_id)`，窗口语义可沿用现网 3 次 / 60s。一个 translator 账号熔断 **不得** 打开其它 `(model, qg)` 桶。`rejected_scope` / `no_translator` 仍不计入熔断。

成功路径须 `on_success` 等价物，清零该组连续失败。

### 8.3 缓存（高风险）

`vision_cache_digest` 必须包含 `translate_model` 与配方修订（建议 `compose_rev`：template+execute+translate 的稳定字符串，或整数 `prompt_rev` 之外再加 `translator=`）。更换译图槽位后不得命中旧 IR。实现时 **+1 `SCHEMA_VER`**（现网为 3 → 4），旧文件自然 miss。

### 8.4 父 lease（高风险）

6×60s 译图 > 现网 `timeout 300` + 30 的 TTL。V1 同时做：

1. 门面生成 timeout ≥ 480（§6.1）。
2. 每张图译完后 **renew 父 lease**（已有 EXPIRE lua），避免执行尚未开始租约已掉。

无图：不调译图；不延长语义。

失败：质量门 / 超时 / 熔断 / 空 IR / 超限 → 400。不换逻辑模型。不新增 `x-compose-*` 头。

---

## 9. 与冻结纪律

| 纪律 | V1 |
| --- | --- |
| 失败不换执行模型 | 遵守 |
| 流水线不改变选号语义 | 遵守；internal select 只用于子调用 |
| 子调用额度组互斥 | 仅 eligible Messages 路由 |
| Q6 | 仅 Messages |
| 不信任客户端 internal_call | P0-1 |
| Fill First | 子调用故障须进入现有 classifier，否则失效账号会被反复选中 |

---

## 10. 其它规格修订（编码时改文档）

**`vision-compose.md`**：槽位来自配方；digest 含 translator；熔断分桶；子调用 outcome 上报；超时 / 父 lease；`is_vision_compose`。

**`pipeline.md`**：discovery 省略看两个 flag；`is_internal_call` 只认 ContextVar。

**`design-proposal.md`**：不解冻 fallback / Chat 视觉。

---

## 11. 验收

编码验收（2026-08-30）：`tests/unit` + `tests/contract` **435 passed, 1 skipped**。现网切 `glm-5.3` 见 [`../maintenance.md`](../maintenance.md)。

**回归**

- 现网 `glm-5.2-vision` 视觉 unit/contract 全绿；无 `template` 的旧 compose 仍为视觉。
- `GATEWAY_ENHANCE_ENABLED=false` 时现有 unit/contract 全绿。

**配置反例（必须 `ConfigValidationError`，不写盘）**

| 反例 | 挡住的 P0 |
| --- | --- |
| `translate_model: MiniMax-M2.7`（Messages、无 IMAGE） | P0-2 |
| 门面行出现在 MiniMax plan，execute 仍是 glm-5.2 | P0-3 |
| `execute_model: glm-5.2-vision` 或两门面互指 | P0-4 |
| 给 `glm-5.2` 加 `compose` | 身份 / 5.3 |
| `remove` 普通模型 id | P0-6 |
| 非 ASCII / 空格门面 id | 高风险碰撞 |
| compose 含 `fallback` 或未知键 | 双重真相 |
| `advertised_features` 与公式不等 | 高风险 |
| `F_plans != E_plans` | P0-3 |

**Mutator 事务**

- validate 失败：plans 与 litellm 字节不变。
- 模拟 litellm 原子写失败：plans 回滚到备份，不得保持新 plans。
- 并行两个 add：文件锁串行，最终 YAML validate 通过。

**运行时反例**

| 反例 | 期望 |
| --- | --- |
| 内部译图模型无 public opt-in | internal select 成功；公开 `/v1/messages` 打该模型名仍 UNSUPPORTED_PUBLIC_PROTOCOL |
| 请求 metadata `internal_call=true` 打公开 glm-5.2 | **不**跳过 opt-in、**不**跳过 pipeline |
| 子选号未声明 IMAGE | 不得选中无 IMAGE 的 Messages 部署（单测夹具：同组一个有图一个无图） |
| 译图上游 429 | classifier/cooldown 被调用；同账号下一张图不应当健康账号；其它 `(model,qg)` 不因全局熔断全死 |
| 更换 `translate_model` 后同一 png+guide | cache miss，digest 含新 translator |
| 6 张图串行译图 | 父 lease 在执行开始前仍有效（renew 或 TTL） |

**正向**

- add 合法 id（regex）+ glm-5.2 + MiniMax-M3 → 仅注入 execute 的 Messages plans；`facade_role: vision`；advertised 含 image；部署 features 无 image。
- 两 flag 皆真：项目 discovery 含该 id。任一 flag 假：项目 discovery **省略**。不验收 stock `/v1/models` 省略。
- 无图不调译图；有图出发执行 body 无 image。
- apply argv 含 `--enable-messages-chat-native`。

---

## 12. 实施顺序（确认后拆 plan）

1. Schema：id regex、未知键拒绝、规范三键、`facade_role`、eligible 路由、血缘、反嵌套/环、advertised 公式、禁止 fallback。反例单测先红后绿。
2. `select_internal_deployment` + ContextVar；strategy 仅在 Var 下跳过 opt-in。公开路径回归。
3. 译图选号传 `{text,image}`；`report_internal_outcome`；熔断分桶。429 单测。
4. digest 含 translator + SCHEMA_VER+1；门面 timeout 与父 lease renew。
5. discovery 两个 flag。
6. Mutator 事务 + remove 类型检查；generator 禁止改写 id。
7. Vision 阶段读配方槽位（硬编码名只作为预置 YAML 值）。
8. 同步 `vision-compose.md` / `pipeline.md`。

不要先做管理 UI / rethink。不要在 1–3 完成前把「任意 Messages 模型当槽位」接到生产路径。

---

## 附录 A — 重思考（V2，不可按本附录施工）

第一轮附录仍有效，并追加：槽位同样走 internal select 与 eligible 路由；worker 禁止 compose 门面；写盘走同一事务；子调用必须 outcome 上报。OpenCode `stream=true` 不是 V2 验收。

---

## 附录 B — 已删除 / 禁止回潮

- 唯一来源 `logical_models`；容器内 PUT；pipeline 内 fallback。
- 「有任意 enabled 部署即可做译图槽位」。
- 用 metadata `internal_call` 跳过 public opt-in。
- 全局进程级视觉熔断挡住所有 translator。
- 先改 plans 再 apply、无锁无回滚。
- `remove --id` 不检查是否门面。
- 验收「flag 关则 stock `/v1/models` 消失」。
- 存盘同时写 `reasoning` 与 `execute_model`。
- `ascii_safe` 把门面 id 改成 `_`。

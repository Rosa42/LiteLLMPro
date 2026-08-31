# 请求增强层维护方案

| 项 | 值 |
| --- | --- |
| 文档类型 | 维护（operator / agent） |
| 日期 | 2026-08-30 |
| 范围 | 视觉配方 V1 + 增强流水线 flag / 回滚 / 回归 |
| 契约 | [`specs/2026-08-28-composable-recipes-design.md`](./specs/2026-08-28-composable-recipes-design.md) |
| 施工计划 | [`plans/2026-08-30-composable-vision-recipes.md`](./plans/2026-08-30-composable-vision-recipes.md)（**已完成**） |

本文告诉操作者和后续 AGENT：**现在代码在哪、怎么改槽位、怎么回滚、不要碰什么。** 不重新开产品取舍。重思考 / fallback / 容器内 PUT 仍禁止。

日常使用说明（开 flag、客户端、CLI 步骤）：[`local-llm-router/USAGE.md`](../../local-llm-router/USAGE.md) §4；Windows 套餐文档 [`local-llm-router/docs/配置套餐与启动.md`](../../local-llm-router/docs/配置套餐与启动.md) §7。

---

## 1. 当前状态（2026-08-30）

| 能力 | 状态 |
|------|------|
| 预置门面 `glm-5.2-vision`（译图 → 执行） | **已落地**；同一 `VisionComposeStage` |
| 槽位可配置（`execute_model` / `translate_model`） | **已落地**；宿主机 CLI 双写 `plans.yaml` 再 apply |
| `template` 判别、IMAGE 资格、血缘、反嵌套 | **已落地** |
| 可信 internal select（ContextVar）+ 子调用 outcome | **已落地** |
| 缓存 digest 含译图模型；`SCHEMA_VER = 4` | **已落地** |
| 项目 discovery 省略门面 | 须 **同时** `GATEWAY_ENHANCE_ENABLED` 与 `VISION_COMPOSE_ENABLED` |
| 现网把 execute 切到 `glm-5.3` | **操作者动作**，不是代码缺口。库存是 `glm-5.3`，没有单独 id `glm-5.3-flash` |
| rethink / 自动 fallback / 容器内改 YAML | **不做** |

回归（编码验收）：`local-llm-router` 下 `$env:PYTHONPATH="plugins"`，`tests/unit` + `tests/contract` **435 passed, 1 skipped**（2026-08-30）。

LiteLLM **v1.90.5**；业务只在 `plugins/shared_quota_router/`。

---

## 2. 视觉门面：日常操作

工作目录：`local-llm-router`。apply **必须**带 `--enable-messages-chat-native`（`llm-router.ps1` 封装已带）。

```powershell
cd E:\LiteLLMPro\local-llm-router

# 看当前 execute / 译图候选（译图必须 Messages + IMAGE；MiniMax-M2.7 不会出现）
.\scripts\llm-router.ps1 compose-vision-slots

# 预置门面改执行模型（无 --Force 会拒绝改 glm-5.2-vision）
.\scripts\llm-router.ps1 compose-vision-update `
  -Id glm-5.2-vision -Execute glm-5.3 -Vision MiniMax-M3 -Force

# 新建门面
.\scripts\llm-router.ps1 compose-vision-add `
  -Id my-vision -Execute glm-5.3 -Vision MiniMax-M3

# 删除自定义门面（普通模型 id 会拒绝）
.\scripts\llm-router.ps1 compose-vision-remove -Id my-vision
```

等价 Python：

```powershell
python -m shared_quota_router.cli_config compose-vision-update `
  --id glm-5.2-vision --execute glm-5.3 --vision MiniMax-M3 --force
```

apply 成功后：

1. 打开生成的 `config/litellm.yaml`：门面 `model_name` 仍是配方 id；`litellm_params.model` 是 `anthropic/<execute_model>`；凭证是 **execute 所在 plan**（Volc）的 env，不是 MiniMax。
2. **只改 yaml**：重建 / 重启 litellm 容器即可，不必 `--build`。
3. **改了 plugin Python**：必须 `--build`（插件是 COPY 进镜像的）。

`remove` 不得指向 `glm-5.2` / `MiniMax-M3` 等非门面逻辑名。

---

## 3. Flag 与回滚

`.env` 默认增强关闭（见 `.env.example`）。现网若要视觉：

```env
GATEWAY_ENHANCE_ENABLED=true
VISION_COMPOSE_ENABLED=true
```

| 意图 | 做法 |
|------|------|
| 关掉整层增强（视觉+记忆） | `GATEWAY_ENHANCE_ENABLED=false` 后重启进程 |
| 只关视觉 | `VISION_COMPOSE_ENABLED=false`（总开关仍可开给记忆） |
| 关视觉后 discovery | 项目 `GET /v1/router/model-capabilities` **省略**视觉门面 |
| stock `GET /v1/models` | **不会**按 flag 省略（LiteLLM `model_list`）；不要当验收 |
| 额度 Redis | **禁止** flush `sq:*` |

译图失败对用户仍是 400 `FEATURE_UNSUPPORTED`，**不换**执行模型。

---

## 4. 运行时纪律（改代码时）

- 子选号只用 `select_internal_deployment` + ContextVar `sq_trusted_internal`。**禁止**用客户端 metadata `internal_call` 跳过 public opt-in 或 pipeline。
- 译图 `required_features` 必须含 `image`。
- 子 HTTP 先 `report_internal_outcome`（原始状态码），再映射用户可见错误。熔断键 `(translate_model, quota_group_id)`。
- 换译图槽位后旧 IR 不得命中：digest 含 translator；`SCHEMA_VER` 不兼容时再 +1。
- 缓存目录：`GATEWAY_VISION_CACHE_DIR`（未设则默认 data 目录）。不进 Redis `sq:*`。
- 门面 timeout ≥ 480；多图译完 renew 父 lease。

---

## 5. 回归命令

```powershell
cd E:\LiteLLMPro\local-llm-router
$env:PYTHONPATH="plugins"
F:\anaconda\envs\py312\python.exe -m pytest tests/unit tests/contract -q
```

现网带图抽查：客户端打门面 id（如 `glm-5.2-vision`）；无图不调译图；有图则出发执行 body 无 image。不要用 stock `/v1/models` 判断 flag 省略。

---

## 6. 明确不要做

- 重思考多模型汇总、pipeline 内自动 fallback、Chat 视觉。
- 在 LiteLLM 容器里 PUT / 手改 `plans.yaml`。
- 把已有非门面逻辑名改成门面。
- 用 `ascii_safe` 静默改写模型 id。
- 信任 HTTP / LiteLLM metadata 里的 `internal_call`。

V2 rethink 见规格附录 A；**不可按该附录直接施工**，须另开确认后的 plan。

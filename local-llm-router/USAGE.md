# 投入实际使用指南

本仓库已实现 **被动共享额度路由**（阶段 1–15 主路径），以及 **可配置的视觉组合门面**（V1）。可在本机作为 OpenAI Chat / Anthropic Messages 中转使用。

视觉槽位怎么切、flag / 回滚、禁止项：仓库根目录 [`docs/framework-upgrade/maintenance.md`](../docs/framework-upgrade/maintenance.md)。  
Windows 套餐与启动细节：[`docs/配置套餐与启动.md`](./docs/配置套餐与启动.md)。

## 1. 准备环境

- Docker Desktop 运行中，或本机用 `.\scripts\llm-router.ps1 start`（Windows 推荐）
- 复制环境变量：

```powershell
cd E:\LiteLLMPro\local-llm-router
Copy-Item .env.example .env
```

编辑 `.env`（**必填**）：

```env
LITELLM_MASTER_KEY=<至少32字节随机串>
REDIS_PASSWORD=<强密码>
# 不要设置 DATABASE_URL（个人 core 模式）

OPENCODE_GO_BASE_URL=https://你的OpenCode兼容端点/v1
OPENCODE_GO_KEY_A=sk-...
OPENCODE_GO_KEY_B=sk-...

VOLC_CODING_BASE_URL=https://你的火山兼容端点/v1
VOLC_CODING_KEY_C=sk-...
```

用视觉门面时还需要译图上游（库存是 MiniMax Messages）：

```env
MINIMAX_ANTHROPIC_BASE_URL=https://api.minimaxi.com/anthropic
MINIMAX_API_KEY=...
```

模型 ID / base URL 以控制台或上游 `/models` 为准。`config/litellm.yaml` 由 `apply` 从 `plans.yaml` 生成，不要当手改源。

## 2. 启动

Windows 推荐：

```powershell
.\scripts\llm-router.ps1 init     # 首次
.\scripts\llm-router.ps1 apply
.\scripts\llm-router.ps1 start
.\scripts\llm-router.ps1 status
```

Docker：

```powershell
docker compose --env-file .env -f deploy/docker-compose.yaml --profile core up -d --build
```

验证：

```powershell
curl http://127.0.0.1:4000/health/liveliness
curl -H "Authorization: Bearer $env:LITELLM_MASTER_KEY" http://127.0.0.1:4000/v1/models
```

`/v1/models` 列出的是 LiteLLM `model_list`（随你的 `plans.yaml` 变化），**不要**用它判断视觉 flag 是否关闭。能力与协议看：

```text
GET http://127.0.0.1:4000/v1/router/model-capabilities
Authorization: Bearer <LITELLM_MASTER_KEY>
```

## 3. 接入客户端

| 配置项 | 值 |
|--------|-----|
| API Base | `http://127.0.0.1:4000/v1` |
| API Key | 与 `LITELLM_MASTER_KEY` 相同 |
| 文本模型 | 如 `glm-5.2`、`glm-5.3`、`MiniMax-M2.7`、`MiniMax-M3`（以你库存为准） |
| 视觉组合门面 | 预置逻辑名 `glm-5.2-vision`（OpenCode 常写 `local-litellm-anthropic/glm-5.2-vision`） |

纯文本可用 Chat Completions。**视觉门面 V1 只走 Anthropic Messages**（`/v1/messages`）。Chat 带图不是本配方验收面。

```bash
curl http://127.0.0.1:4000/v1/chat/completions \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"glm-5.2","messages":[{"role":"user","content":"hello"}],"max_tokens":64}'
```

OpenCode 示例见 `docs/配置套餐与启动.md` §5。

## 4. 视觉组合模型（可配置）

对外仍是一个逻辑名（默认 `glm-5.2-vision`）：带图时 MiniMax 译图，剥像素后再交给执行模型。执行槽位和译图槽位可在**宿主机**改，不要进 Docker 容器改 `plans.yaml`。

现网要真正译图，`.env` **同时**打开（改完重启进程）：

```env
GATEWAY_ENHANCE_ENABLED=true
VISION_COMPOSE_ENABLED=true
```

默认都是关的（见 `.env.example`）。关总开关后行为回到未部署增强层。

```powershell
cd E:\LiteLLMPro\local-llm-router

# 看当前可当 execute / 译图的候选
# 译图必须 Messages + IMAGE（MiniMax-M3 可以，MiniMax-M2.7 不会出现）
.\scripts\llm-router.ps1 compose-vision-slots

# 预置门面改执行模型（改 glm-5.2-vision 必须 -Force）
.\scripts\llm-router.ps1 compose-vision-update `
  -Id glm-5.2-vision -Execute glm-5.3 -Vision MiniMax-M3 -Force

# 新建 / 删除自定义门面（不能 remove 普通模型 id）
.\scripts\llm-router.ps1 compose-vision-add -Id my-vision -Execute glm-5.3 -Vision MiniMax-M3
.\scripts\llm-router.ps1 compose-vision-remove -Id my-vision
```

apply 之后：

1. 打开生成的 `config/litellm.yaml`：门面 `model_name` 仍是配方 id；`litellm_params.model` 是 `anthropic/<execute_model>`；凭证跟 **execute 所在 plan**（Volc），不是 MiniMax。
2. **只改 yaml**：重建 / 重启 litellm 即可，不必 `--build`。
3. **改了 plugin Python**：必须 `--build`。

库存执行槽位是 `glm-5.3`（没有单独 id `glm-5.3-flash`）。无图请求不调译图。译图失败对用户是 400，**不会**自动换执行模型。

回滚整层增强：`GATEWAY_ENHANCE_ENABLED=false` 后重启。不要 flush Redis `sq:*`。

## 5. 运行时行为（额度层）

1. 优先打满 priority 更小的套餐（Fill First）
2. 同一会话尽量粘同一 deployment（缓存友好）
3. 某套餐任一模型返回**高置信额度耗尽** → 该账号全部模型停用，请求切到其他套餐
4. 普通短期限流只冷却当前 deployment
5. 流式已开始输出后**不会**静默拼接另一上游
6. Worker 按退避探测耗尽账号是否恢复（**不会**武断假设固定五小时必恢复）

## 6. 测试与自检

```powershell
cd E:\LiteLLMPro\local-llm-router
$env:PYTHONPATH="plugins"
# 建议：F:\anaconda\envs\py312\python.exe -m pytest tests/unit tests/contract -q
python -m pytest tests/unit tests/contract -q
```

本地 mock 上游（不耗真实额度）：

```powershell
python -m shared_quota_router.mock_provider --port 18080
# 将 OPENCODE_GO_BASE_URL=http://127.0.0.1:18080/v1 并配合 X-Mock-Scenario
```

现网带图抽查：客户端打门面 id；不要用 stock `/v1/models` 判断 flag 省略。

## 7. 停止

```powershell
.\scripts\llm-router.ps1 stop
# 或
docker compose --env-file .env -f deploy/docker-compose.yaml --profile core down
```

## 8. 限制（请知悉）

- 一期为**被动**额度感知，不做爬取余额页/预测剩余
- 分类器依赖错误文案与状态码；建议用真实失败日志校准 `classifiers/`
- 生产公网暴露未做；默认仅本机 loopback
- 升级走 `docs/upgrades.md`，勿用 latest 镜像
- 视觉 V1 无 rethink、无 pipeline 自动 fallback、无 Chat 视觉
- 视觉 CRUD 只在宿主机；禁止容器内 PUT 当正式流程

更细运维见 `docs/operations.md`。纪律与回滚见 [`docs/framework-upgrade/maintenance.md`](../docs/framework-upgrade/maintenance.md)。

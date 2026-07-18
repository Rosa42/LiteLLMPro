# 投入实际使用指南

本仓库已实现 **被动共享额度路由**（阶段 1–15 主路径），可在本机作为 OpenAI 兼容中转使用。

## 1. 准备环境

- Docker Desktop 运行中  
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

> 模型 ID / base URL 必须以控制台或 `/models` 为准；`config/litellm.yaml` 中 `openai/kimi-k3` 等需与上游一致。

## 2. 启动

```powershell
docker compose --env-file .env -f deploy/docker-compose.yaml --profile core up -d --build
```

验证：

```powershell
curl http://127.0.0.1:4000/health/liveliness
curl -H "Authorization: Bearer $env:LITELLM_MASTER_KEY" http://127.0.0.1:4000/v1/models
```

应只看到：`kimi-k3`、`glm-5.2`。

## 3. 接入 Coding Agent / OpenCode / 任意 OpenAI SDK

| 配置项 | 值 |
|--------|-----|
| API Base | `http://127.0.0.1:4000/v1` |
| API Key | 与 `LITELLM_MASTER_KEY` 相同 |
| Model | `kimi-k3` 或 `glm-5.2` |

示例：

```bash
curl http://127.0.0.1:4000/v1/chat/completions \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"kimi-k3","messages":[{"role":"user","content":"hello"}],"max_tokens":64}'
```

## 4. 运行时行为（你应有的预期）

1. 优先打满 priority 更小的套餐（Fill First）  
2. 同一会话尽量粘同一 deployment（缓存友好）  
3. 某套餐任一模型返回**高置信额度耗尽** → 该账号全部模型停用，请求切到其他套餐  
4. 普通短期限流只冷却当前 deployment  
5. 流式已开始输出后**不会**静默拼接另一上游  
6. Worker 按退避探测耗尽账号是否恢复（**不会**武断假设固定五小时必恢复）

## 5. 测试与自检

```powershell
.\.venv\Scripts\pytest -q
# 单元 + 集成场景 A–F + mock e2e + C0/C1
```

本地 mock 上游（不耗真实额度）：

```powershell
.\.venv\Scripts\python -m shared_quota_router.mock_provider --port 18080
# 将 OPENCODE_GO_BASE_URL=http://127.0.0.1:18080/v1 并配合 X-Mock-Scenario
```

## 6. 停止

```powershell
docker compose --env-file .env -f deploy/docker-compose.yaml --profile core down
```

## 7. 限制（请知悉）

- 一期为**被动**额度感知，不做爬取余额页/预测剩余  
- 分类器依赖错误文案与状态码；建议用真实失败日志校准 `classifiers/`  
- 生产公网暴露未做；默认仅本机 loopback  
- 升级走 `docs/upgrades.md`，勿用 latest 镜像  

更细运维见 `docs/operations.md`。

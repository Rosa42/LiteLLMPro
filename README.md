# local-llm-router

本地运行的 OpenAI API 兼容中转站：在 **LiteLLM Proxy** 上叠加 **共享 Coding Plan 额度路由**。

客户端只看到逻辑模型：

```text
kimi-k3
glm-5.2
```

底层将多个套餐账号（OpenCode Go A/B、火山 Coding Plan C 等）按 **模型组** 聚合、按 **QuotaGroup（账号共享额度）** 熔断与故障转移。

## 设计原则（摘要）

| 原则 | 说明 |
|------|------|
| 不重度 Fork | 业务在 `plugins/shared_quota_router/` |
| 版本钉死 | 默认 `LITELLM_VERSION=v1.90.5` |
| 双重聚类 | 模型组 ≠ 额度组 |
| fail-closed | Redis 异常不得盲选 |
| 流式安全 | 首字节后禁止切换拼接 |
| 被动额度 | 一期不做爬取/预测余额 |

完整约束见 [AGENTS.md](./AGENTS.md)。

## 包布局

```text
plugins/shared_quota_router/   # 源码
# 镜像内路径 /app/shared_quota_router
# Python import 名: shared_quota_router
```

`pyproject.toml` 将 `plugins/` 映射为包根，本地开发：

```bash
# Windows PowerShell
cd local-llm-router
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
pytest
```

## 版本

单一来源：[`config/versions.env`](./config/versions.env)

```text
LITELLM_VERSION=v1.90.5
```

上游源码以 git submodule 置于 `upstream/litellm`（只读，禁止业务提交）。

## Docker（M0）

```bash
cp .env.example .env
# 编辑 .env，填入足够长的 LITELLM_MASTER_KEY 与 REDIS_PASSWORD

# from repo root (so .env is found)
docker compose --env-file .env -f deploy/docker-compose.yaml --profile core config
docker compose --env-file .env -f deploy/docker-compose.yaml --profile core up -d --build
```

- **core** 配置：litellm + redis + quota-worker  
- **full** 配置：另加 postgres + caddy（`127.0.0.1:4000`）

M0 阶段路由可使用 LiteLLM 原生 `simple-shuffle`。  
**阶段 7 起必须注入自定义 Shared Quota 策略**（见启动说明与 `config/litellm.yaml` 注释）。  
M2a 验收禁止仍仅使用 simple-shuffle。

## 文档索引

| 文档 | 路径 |
|------|------|
| 总设计 | `../升级版的开发设计方案.md` |
| 分阶段方案 v0.2 | `../docs/分阶段开发方案.md` |
| Task 1–9 | `../docs/tasks/阶段1-9-任务拆解.md` |
| 上游管理 | `docs/upstream.md` |

## 状态

当前：工程骨架与 M0 配置落地中（阶段 1+）。

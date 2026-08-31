# local-llm-router (LiteLLMPro)

本地运行的 OpenAI API 兼容中转站：在 **[LiteLLM](https://github.com/BerriAI/litellm) Proxy** 上叠加 **共享 Coding Plan 额度路由**。

> **许可与归属**：本仓库插件/脚本/文档为 MIT（见 [LICENSE](./LICENSE)）。  
> 上游 LiteLLM 开源部分亦为 MIT（见 [upstream/litellm/LICENSE](./upstream/litellm/LICENSE) 与 [NOTICE](./NOTICE)）。  
> `enterprise/` 目录若存在则适用商业许可，勿当作 MIT 分发。

客户端只看到逻辑模型（示例，以你的 `plans.yaml` 为准）：

```text
ark-code-latest
glm-5.2
glm-5.3
glm-5.2-vision    # 组合门面：译图 + 执行；槽位可配置
MiniMax-M3
MiniMax-M2.7
```

底层将多个套餐账号（OpenCode Go A/B、火山 Coding Plan 等）按 **模型组** 聚合、按 **QuotaGroup（账号共享额度）** 熔断与故障转移。

## 快速启动（Windows 本机）

```powershell
cd local-llm-router
.\scripts\llm-router.ps1 init          # 首次
# 编辑 .env 填入上游 BaseURL / API Key
.\scripts\llm-router.ps1 apply
.\scripts\llm-router.ps1 start
.\scripts\llm-router.ps1 status
```

详情：[`USAGE.md`](./USAGE.md) · [`docs/配置套餐与启动.md`](./docs/配置套餐与启动.md) · 日常：`.\scripts\llm-router.ps1 start|stop|status`

客户端：`http://127.0.0.1:4000/v1`，Key 使用 `.env` 中 `LITELLM_MASTER_KEY`（不是上游 Key）。

视觉门面（V1 已落地）：逻辑名默认 `glm-5.2-vision`，走 Anthropic Messages。切执行/译图模型用宿主机 `compose-vision-*`，见 [`USAGE.md`](./USAGE.md) §4。纪律与回滚：[`../docs/framework-upgrade/maintenance.md`](../docs/framework-upgrade/maintenance.md)。

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

## 协议感知网关（进行中）

多协议（Chat / Messages / Responses）设计与任务进度：

| 文档 | 说明 |
|------|------|
| [docs/tasks.md](./docs/tasks.md) | **任务板**（Done/TODO，权威进度） |
| [docs/protocol-aware-multi-api-gateway-plan.md](./docs/protocol-aware-multi-api-gateway-plan.md) | 设计方案 |
| [docs/model-capability-discovery.md](./docs/model-capability-discovery.md) | 能力发现 API |

**已完成：** Phase 0 + G0（G0-B）+ M1–M4 MVP-GATE；C1–C3 转换代码（默认关闭）。  
**进行中 / 阻塞：** 统一对外 API 的转换上线 — 见 `docs/phase-reports/remaining-dev-plan.md`（Phase 4 路径探针）。  
**未做：** 流式转换（C4 No-Go）、Responses 直连/转换（C5 No-Go）。

能力发现（勿依赖 stock `/v1/models` 的自定义元数据）：

```text
GET http://127.0.0.1:4000/v1/router/model-capabilities
```

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

## 自定义路由（阶段 7+）

唯一注册入口：`shared_quota_router.bootstrap.register(router)`。

Proxy 通过环境变量延迟挂载（`load_config` 之后）：

```text
LITELLM_WORKER_STARTUP_HOOKS=shared_quota_router.bootstrap:register_proxy_startup
```

扩展点说明：`docs/extension-points-v1.90.5.md`。

契约测试：

```bash
pip install -e ".[dev,contract]"
pytest tests/contract/test_c0_routing.py -q
```

## 文档索引

| 文档 | 路径 |
|------|------|
| **使用说明** | [USAGE.md](./USAGE.md) |
| Windows 套餐 / 启动 / 视觉 CLI | [docs/配置套餐与启动.md](./docs/配置套餐与启动.md) |
| 本机运维摘要 | [docs/operations.md](./docs/operations.md) |
| 视觉槽位维护 / flag / 回滚 | [../docs/framework-upgrade/maintenance.md](../docs/framework-upgrade/maintenance.md) |
| 总设计 | `../升级版的开发设计方案.md` |
| 分阶段方案 v0.2 | `../docs/分阶段开发方案.md` |
| Task 1–9 | `../docs/tasks/阶段1-9-任务拆解.md` |
| 上游管理 | `docs/upstream.md` |

## 状态

**阶段 1–15 主路径已交付**（被动共享额度路由）。  
**视觉组合门面 V1 已交付**（可配置 execute / 译图槽位；宿主机 CLI）。  
投入使用请读 **[USAGE.md](./USAGE.md)**。  
测试：`PYTHONPATH=plugins` 后 `pytest tests/unit tests/contract -q`。

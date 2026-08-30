# 运维手册（个人本地）

## 启动（core：无 Postgres）

```bash
cd local-llm-router
cp .env.example .env
# 编辑 .env：
# - LITELLM_MASTER_KEY ≥32 字节随机
# - REDIS_PASSWORD
# - OPENCODE_GO_* / VOLC_CODING_* 真实 Key 与 api_base
# - 保持 DATABASE_URL 注释掉（core）

docker compose --env-file .env -f deploy/docker-compose.yaml --profile core up -d --build
```

健康检查：

```bash
curl http://127.0.0.1:4000/health/liveliness
sh scripts/smoke-test.sh
```

## 客户端配置

OpenAI 兼容：

| 项 | 值 |
|----|-----|
| Base URL | `http://127.0.0.1:4000/v1` |
| API Key | 与 `LITELLM_MASTER_KEY` 相同 |
| 模型 | `kimi-k3` 或 `glm-5.2` |

## 服务说明

| 服务 | 作用 |
|------|------|
| litellm | Proxy + 自定义路由 + 失败回调 |
| redis | QuotaGroup / affinity / lease 状态 |
| quota-worker | EXHAUSTED 探测恢复 |

## 常用命令

```bash
docker compose --env-file .env -f deploy/docker-compose.yaml --profile core logs -f litellm
docker compose --env-file .env -f deploy/docker-compose.yaml --profile core ps
docker compose --env-file .env -f deploy/docker-compose.yaml --profile core down
```

## 安全

- 仅绑定 `127.0.0.1`
- 勿提交 `.env`
- 日志勿打印 Authorization / 完整 Prompt

## 视觉门面（增强层）

维护入口：仓库根目录 [`docs/framework-upgrade/maintenance.md`](../../docs/framework-upgrade/maintenance.md)。

切 execute / 译图必须在**宿主机**跑 `compose-vision-*`（不要进容器改 YAML）。只改 yaml 时重建 litellm 即可；改了 plugin 才 `--build`。

```powershell
cd E:\LiteLLMPro\local-llm-router
.\scripts\llm-router.ps1 compose-vision-update -Id glm-5.2-vision -Execute glm-5.3 -Vision MiniMax-M3 -Force
```

回滚整层增强：`.env` 设 `GATEWAY_ENHANCE_ENABLED=false` 后重启。不要 flush Redis `sq:*`。

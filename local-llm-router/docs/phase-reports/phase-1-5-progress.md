# 阶段进度报告（开工首批）

日期：2026-07-18

## 已完成

### 阶段 1（仓库骨架）

- 创建 `local-llm-router/` 目录树（plugins/config/deploy/tests/…）
- `pyproject.toml`（requires-python>=3.11，import 布局 `shared_quota_router`）
- `README.md` / `AGENTS.md` / `.gitignore` / `.env.example`
- `config/accounts.example.yaml`、`logging.yaml`、`versions.env`、`patches/README.md`

### 阶段 2–3（上游 + 版本钉死）

- git submodule：`upstream/litellm`
- checkout **v1.90.5**，commit `0430743f2f…`
- `config/versions.env`：`LITELLM_VERSION=v1.90.5` + `LITELLM_GIT_SHA`

### 阶段 4（Compose / M0 配置）

- `deploy/Dockerfile.litellm`、`Dockerfile.worker`
- `deploy/docker-compose.yaml`（profiles: core / full）
- `config/litellm.yaml`（simple-shuffle，无跨模型 fallback）
- `docker compose --env-file .env -f deploy/docker-compose.yaml --profile core config` → **OK**
- 未强制 `compose up`（镜像拉取体积大；M0 运行验收可按需执行）

### 阶段 5–6（部分实现 + 单测）

- `models.py`：含 `RequestRoutingContext` 四字段与语义
- `registry.py`：model_info / pick_probe_deployment
- `state_store.py`：sq: keys、mark_exhausted、fail-closed
- `lease.py`：TTL=timeout+30、acquire/release Lua 语义
- `classifiers/*`：FailureKind 唯一权威在 `classifiers/base.py`；generic_openai 主路径
- 单元测试：**14 passed**

## 命令与结果

```text
pip install -e ".[dev]"
pytest -q
# 14 passed

docker compose --env-file .env -f deploy/docker-compose.yaml --profile core config
# OK

git submodule + checkout v1.90.5
# HEAD 0430743f2f (detached)
```

## 未解决风险

1. **M0 `compose up` 未跑**：需拉取 `ghcr.io/berriai/litellm:v1.90.5`，网络/时间成本高。
2. 阶段 7+ 路由策略、回调、Worker 尚未实现。
3. Redis 真实 Lua 仅在 FakeRedis 模拟；接入真实 redis 客户端后需再验。
4. 供应商专用分类器仍为骨架，避免假高置信。

## 下一阶段计划

1. （可选）`compose up` 验证容器内 `import shared_quota_router`
2. 阶段 7：`strategy.py` + `bootstrap.py` + C0
3. 阶段 8：`callbacks.py` + first_byte 置位 + C1
4. 阶段 9：`recovery_worker.py` → M2a

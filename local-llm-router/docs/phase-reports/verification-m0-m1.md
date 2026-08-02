# 本阶段验证报告（阶段 1–6 / M0 + M1 逻辑层）

日期：2026-07-18  
范围：在进入阶段 7 前，确认当前交付可验收。

---

## 结论

| 里程碑 | 结论 | 说明 |
|--------|------|------|
| **M0 工程底座** | **通过** | compose 可运行；插件可 import；健康检查 200；仅 `127.0.0.1:4000`；逻辑模型仅 `kimi-k3` / `glm-5.2` |
| **M1 状态层（单测）** | **通过** | 14 项单元测试全绿；Context / Store / Lease / Classifier / Registry 覆盖验收要点 |
| **进入阶段 7** | **允许** | 控制面（strategy/callback/worker 业务）尚未实现，属阶段 7–9 范围 |

---

## 验证矩阵

### A. 本地（阶段 5–6）

| 检查项 | 结果 |
|--------|------|
| `pytest -q` | **14 passed** |
| `import shared_quota_router` | OK `0.1.0` |
| `RequestRoutingContext` 四字段与上限语义 | 单测通过 |
| fail-closed（Redis DOWN） | 单测通过 |
| lease TTL = timeout + 30 | 单测通过 |
| 耗尽 ≠ 短 429 | 单测通过 |
| AUTH / CONTENT_POLICY / BAD_REQUEST | 单测通过 |
| registry `model_info` + `pick_probe_deployment` | 单测通过 |
| FailureKind 唯一归属 `classifiers/base.py` | 代码审查通过 |

### B. 版本与上游（阶段 2–3）

| 检查项 | 结果 |
|--------|------|
| submodule tag | **v1.90.5** |
| commit SHA | `0430743f2fd4005898506e00bc62dd47bcff6fc9` |
| `LITELLM_VERSION` 单一来源 | `config/versions.env` |
| 镜像 digest（构建解析） | `sha256:7f50fa44310ad1a5258b7e20796f19c742cd90a7c223fefd85c4aafd3198a5fa` |

### C. Docker M0 运行态（阶段 4）

| 检查项 | 结果 |
|--------|------|
| `compose config` | OK |
| `compose up --build` core | OK |
| redis | **healthy**；**未**映射宿主机端口 |
| litellm | **healthy**；`127.0.0.1:4000->4000` |
| quota-worker | **Up**；可 `import shared_quota_router` |
| `GET /health/liveliness` | **200** `"I'm alive!"` |
| 容器内 `import shared_quota_router` | **ok 0.1.0** |
| `GET /v1/models`（Master Key） | 仅 **`kimi-k3`、`glm-5.2`** |
| 默认无跨模型 fallback | `litellm.yaml` 无 fallback 规则；`simple-shuffle`（M0 允许） |
| `.env` 不进 Git | `.gitignore` 含 `.env` |

启动命令（复现）：

```bash
# 确保 .env 中 DATABASE_URL 已注释（core 不启用 Postgres）
docker compose --env-file .env -f deploy/docker-compose.yaml --profile core up -d --build
```

---

## 验证中发现并已修复的问题

| # | 问题 | 修复 |
|---|------|------|
| 1 | `.env` 含 `DATABASE_URL` → LiteLLM 启动时 prisma 连 `postgres:5432` 失败，服务不监听 4000 | 注释 core 场景的 `DATABASE_URL`；更新 `.env.example` 说明 |
| 2 | quota-worker 使用官方镜像 entrypoint，`CMD python` 被当成 litellm 子命令 → 重启循环 | `Dockerfile.worker` 设置 `ENTRYPOINT []` |
| 3 | worker 日志无输出 | `PYTHONUNBUFFERED=1` + `flush=True` |

---

## 阶段 1–6 验收勾选（摘要）

### 阶段 1

- [x] 目录结构完整  
- [x] `.env` ignore；示例无真实 Key  
- [x] requires-python ≥3.11  
- [x] import 名 `shared_quota_router`  
- [x] AGENTS 强制约束齐全  

### 阶段 2–3

- [x] submodule 存在且指向 v1.90.5  
- [x] 无 latest/main/rc 作为默认  
- [x] 版本单源 + SHA + digest  

### 阶段 4 / M0

- [x] compose 可运行 + healthy  
- [x] 插件可 import  
- [x] 回环绑定  
- [x] Redis 密码  
- [x] 无真实 Secret 入库  
- [x] 允许 simple-shuffle 至阶段 7  
- [x] 无默认跨模型 fallback  

### 阶段 5–6 / M1（逻辑）

- [x] 数据模型与 Context  
- [x] Redis store + fail-closed + lease（单测/FakeRedis）  
- [x] 分类器主路径与 fixtures 行为  
- [ ] 真实 Redis 进程内集成（未做，阶段 12 加压前可补；M1 以单测为门禁）

---

## 明确不在本阶段范围（不应阻塞阶段 7）

- 自定义 routing strategy / C0  
- failure callback / first_byte / C1  
- recovery worker 真实探测  
- 真实上游 Key 调用  
- e2e 场景 A–F  

---

## 建议

1. **可以进入阶段 7**。  
2. 本地开发请保持 core 配置下 **不设置 DATABASE_URL**。  
3. 验证后的栈可保留或停止：  
   `docker compose --env-file .env -f deploy/docker-compose.yaml --profile core down`

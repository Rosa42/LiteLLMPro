# 阶段 7 完成报告：自定义路由策略 + C0

日期：2026-07-18

## 变更文件清单

| 路径 | 说明 |
|------|------|
| `plugins/shared_quota_router/strategy.py` | 候选过滤、Fill First、affinity、tried、fail-closed |
| `plugins/shared_quota_router/bootstrap.py` | **唯一** `register()` + proxy 延迟注册 |
| `plugins/shared_quota_router/__init__.py` | 导出 register |
| `docs/extension-points-v1.90.5.md` | 扩展点调研与挂载决策 |
| `tests/unit/test_strategy.py` | 选路单测 |
| `tests/contract/test_c0_routing.py` | C0 契约 |
| `config/litellm.yaml` | 注释：策略由 bootstrap 挂载 |
| `deploy/docker-compose.yaml` | `LITELLM_WORKER_STARTUP_HOOKS` + `REDIS_URL` |
| `.env.example` / `README.md` / `pyproject.toml` | 文档与 contract 可选依赖 |

## 实现说明

1. **选路算法**：模型组候选 → 过滤 disabled / provider / quota / deployment cooldown / tried / first_byte → 排序 affinity → priority → inflight → last_success → id → lease → mark_tried。
2. **挂载**：官方 `router.set_custom_routing_strategy`；**无业务 patch**。
3. **Proxy**：`LITELLM_WORKER_STARTUP_HOOKS=shared_quota_router.bootstrap:register_proxy_startup` 延迟等待 `llm_router`。
4. **C0**：可导入 CustomRoutingStrategyBase；可挂载；`model_info` 自定义字段在选路结果可读。

## 命令与测试结果

```text
pytest tests/unit tests/contract -q
# 27 passed

# C0 可复制命令（需 litellm==1.90.5）
pip install "litellm==1.90.5"
pytest tests/contract/test_c0_routing.py -q
# 4 passed
```

## 验收勾选（阶段 7）

- [x] 不可用 QuotaGroup 不被选中  
- [x] 同 request tried set 不重选同 group  
- [x] priority / affinity  
- [x] 双重冷却（deployment cooldown 不拖垮同组其他 model）  
- [x] Redis DOWN fail-closed（抛 StateStoreError / NoAvailableDeployment）  
- [x] C0 全绿  
- [x] 无业务 patch；register 唯一入口  
- [x] 无默认跨模型 fallback  

## 未解决风险

1. Proxy 延迟注册依赖 asyncio 任务时序；若极端竞态首请求早于注册，可能仍走 simple-shuffle 一次——可在阶段 8 回调侧增加“未注册则硬失败”观测。  
2. 生产 Redis 客户端为同步 `redis.Redis`；高并发可换 asyncio redis（后续优化）。  
3. Docker 运行态未在本阶段重跑（本机 Docker 曾断连）；配置已写入 compose。

## 下一阶段计划（阶段 8）

- `callbacks.py`：success/failure、熔断矩阵、affinity 清理  
- `first_byte_sent` 置位 + 硬开关  
- `callback_instance` + C1 契约  

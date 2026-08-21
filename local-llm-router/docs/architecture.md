# 架构摘要

```text
Client → LiteLLM Proxy (shared_quota_router)
              │
              ├─ strategy: Fill First + affinity + tried
              ├─ callbacks: classify → EXHAUSTED / cooldown / DISABLED
              └─ Redis state (sq:*)
              │
         recovery_worker probes EXHAUSTED groups
              │
     OpenCode A/B · Volc C (shared quota per account)
```

双重聚类：

- **model_group**：`kimi-k3` / `glm-5.2` 候选集合  
- **quota_group_id**：账号共享额度熔断边界  

扩展点：`docs/extension-points-v1.90.5.md`。

## 协议感知网关（状态）

| 阶段 | 状态 |
|------|------|
| Phase 0 + G0（G0-B 元数据集成） | **已完成** |
| M1 协议域模型 / 配置 / 生成器 / 能力发现 | **已完成** |
| M2 租约前协议过滤 | **已完成** |
| M3 端点门控（Chat 启用；Messages/Responses 受控） | **已完成** |
| M4 观测与特性开关 / MVP 验收 | **已完成（MVP-GATE PASSED）** |
| C1–C3 转换契约 / 试点 / 熔断隔离 | **代码完成，默认 flag off** |
| 统一对外 + 转换上线 | **进行中** — 见 `docs/phase-reports/remaining-dev-plan.md` |

权威任务板：`docs/tasks.md` §0。  
设计方案：`docs/protocol-aware-multi-api-gateway-plan.md`。  
能力发现：`GET /v1/router/model-capabilities`（见 `docs/model-capability-discovery.md`）。  
差距总结：`docs/phase-reports/unified-api-vs-multi-protocol-progress.md`。

**现状要点：** 同协议多供应商 Chat 路由已可用；跨协议转换运行时默认关闭；staging 启用前须通过 remaining-dev-plan Phase 4 探针。

## 框架升级（规划中）

请求增强流水线（视觉合成、跨软件记忆、可插拔阶段）见仓库根目录 [`docs/framework-upgrade/`](../../docs/framework-upgrade/)。与上文协议感知网关互补：本文件描述已实现的额度与协议层；框架升级文档描述尚未落地的请求增强层。
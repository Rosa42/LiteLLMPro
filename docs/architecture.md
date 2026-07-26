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

## 协议感知网关（进行中）

| 阶段 | 状态 |
|------|------|
| Phase 0 + G0（G0-B 元数据集成） | **已完成** |
| M1 协议域模型 / 配置 / 生成器 / 能力发现 | **已完成** |
| M2 租约前协议过滤 | **未开始** |
| M3 端点门控（Messages / Responses） | **未开始** |
| M4 观测与特性开关 / MVP 验收 | **未开始** |

权威任务板：`docs/tasks.md` §0。  
设计方案：`docs/protocol-aware-multi-api-gateway-plan.md`。  
能力发现：`GET /v1/router/model-capabilities`（见 `docs/model-capability-discovery.md`）。

**现状要点：** 配置与发现已能声明/列出 `public_protocols`；运行时 strategy **尚未**按协议过滤部署。
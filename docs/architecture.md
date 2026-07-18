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

# P0 修复方案

> 来源：`docs/audit-vs-original-design.md` P0 清单  
> 日期：2026-07-18

## 目标

| ID | 问题 | 修复目标 |
|----|------|----------|
| P0-1 | tried / first_byte 仅挂 kwargs，跨重试不可靠 | 按 `request_id` 写入 Redis，strategy/callback 共用 |
| P0-2 | 耗尽后 affinity 未清理 | 建立 qg→session 反向索引；熔断/DISABLED 时批量删除 |
| P0-3 | 流式首字节后禁切未硬绑定 | first_byte 落 Redis；选路强制重载；首字节后选路直接失败 |

## 设计

### Redis 键

```text
sq:reqctx:{request_id}
  JSON: { request_id, tried_quota_groups: [], first_byte_sent: bool, max_quota_groups: 3 }
  TTL: 默认 360s（请求超时 + 缓冲，可配置）

sq:affinity:{session_hash} -> deployment_id   (已有, TTL 2h)
sq:affinity-meta:{session_hash} -> quota_group_id  (新增，同 TTL)
sq:affinity-idx:{quota_group_id} -> SET of session_hash  (新增，同 TTL)
```

### 数据流

```text
选路 get_available_deployment
  → resolve request_id
  → load reqctx from Redis (merge into kwargs._shared_quota_context)
  → if first_byte_sent: raise NoAvailableDeploymentError (禁切)
  → filter/rank/select
  → mark_tried + save reqctx

流式首 chunk / streaming hook
  → mark_first_byte
  → first_byte_sent=True + save reqctx 立即落盘

失败 on_failure
  → load reqctx
  → mark_tried + save
  → EXHAUSTED/DISABLED → clear_affinity_for_quota_group
```

### 不改 LiteLLM 核心

仍无 patch；靠「选路入口硬失败」挡住 first_byte 后的跨 deployment 重试。  
若 Router 在 first_byte 后仍调用 `async_get_available_deployment`，将得到无候选并失败，而不是拼另一上游。

## 验收

- [x] 单测：reqctx 跨「模拟重试」保留 tried / first_byte  
- [x] 单测：set_affinity 后 clear_affinity_for_quota_group 清空  
- [x] 单测：first_byte 后 strategy 抛 NoAvailableDeploymentError  
- [x] 鉴权 DISABLED 同步清 affinity  
- [x] 全量 pytest **54 passed**  

## 非目标（本轮不做）

- Prometheus / cosign / 100 并发  
- 全量状态 Lua 化  
- 供应商真实文案库  

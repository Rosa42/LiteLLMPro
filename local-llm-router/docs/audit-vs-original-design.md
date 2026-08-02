# 实现 vs 原始设计方案 — 一致性与完整性审计

> 对照：`升级版的开发设计方案.md`  
> 代码库：`local-llm-router/`  
> 测试基线：`pytest` **49 passed**（审计日）  
> 审计日期：2026-07-18

---

## 1. 总评

| 维度 | 评分 | 说明 |
|------|------|------|
| 目标与原则对齐 | **高** | 不重度 Fork、插件边界、v1.90.5、双重聚类、被动额度、fail-closed 方向正确 |
| 核心功能完整度（逻辑层） | **中高** | 选路 / 分类 / 熔断 / 恢复 / 租约 均有实现与单测 |
| 生产路径闭环（Proxy 运行时） | **中** | 组件齐，但若干「请求内协作」依赖 LiteLLM kwargs 语义，未做全栈 Proxy e2e 证明 |
| 运维与升级 | **中** | 文档/脚本有；cosign、完整升级契约矩阵、100 并发未落地 |
| 与 §23 验收清单 | **部分通过** | 逻辑可证项多；运行态/签名/Redis 持久化实机项依赖本机 Docker+真实 Key |

**一句话：**  
实现是**忠实于一期被动共享额度方案的可用骨架**，主路径在进程内测试中成立；相对原文，缺口集中在 **运行时硬绑定（流式禁切是否真正挡住 LiteLLM 重试）、affinity 清理、Redis 全量原子/审计、供应商专用分类深度、完整 e2e/加压/镜像签名**。

**是否可投入个人本机使用：** 可以，但应知悉「中等风险点」并先用小流量验证真实错误文案。  
**是否达到原文「系统交付」全部勾选：** 尚未。

---

## 2. 原则与架构（§1–3）

| 设计要求 | 实现状态 | 证据 / 差距 |
|----------|----------|-------------|
| 本地 OpenAI 兼容中转 | ✅ | LiteLLM Proxy + `config/litellm.yaml` |
| 逻辑模型仅 kimi-k3 / glm-5.2 | ✅ | model_list 仅两名；`/v1/models` 曾在 M0 验证为两模型 |
| 三套餐 × 两模型 | ✅ | litellm.yaml 6 个 deployment |
| 不重度 Fork | ✅ | 业务仅 `plugins/shared_quota_router/`；无业务 patch |
| 官方 Strategy + Callback | ✅ | `set_custom_routing_strategy` + `callback_instance` |
| Bootstrap 注册 | ✅ | `LITELLM_WORKER_STARTUP_HOOKS` 延迟注册 |
| 最小 patch 机制 | ⚠️ 预留 | `patches/README.md` 有；当前无 patch 文件（合理） |
| 子模块上游 | ✅ | `upstream/litellm@v1.90.5` |
| 固定正式版 | ✅ | `config/versions.env` + 镜像 tag |

---

## 3. 数据模型与 Redis（§4、§12）

| 设计 | 实现 | 差距 |
|------|------|------|
| Provider / QuotaGroup / Deployment / Window | ✅ models.py | Window 仅字段，无运营写回（与 §21/§22 一致 defer） |
| QuotaGroup 状态机 AVAILABLE→EXHAUSTED→PROBING | ✅ | recovery_worker + store |
| Redis keys `sq:provider/quota/window/deployment/affinity/lease/probe-lock` | ⚠️ 大部分 | **`sq:audit` 未实现**（代码注释 defer） |
| 状态变更 Lua/事务 | ⚠️ 部分 | **仅 lease 有 Lua**；quota mark_exhausted 等为 get-modify-set + revision |
| revision 乐观锁 | ✅ | mark_exhausted 支持 expected_revision |
| fail-closed | ✅ | StateStoreError；策略不盲选 |
| Redis 重启后状态可恢复 | ⚠️ | AOF 已配；**未做「重启后仍 EXHAUSTED」专项测试** |

---

## 4. 路由算法（§6–7）

| 设计 | 实现 | 差距 |
|------|------|------|
| 候选过滤：disabled/provider/quota/cooldown | ✅ strategy | |
| Fill First（priority 小优先） | ✅ | |
| Session affinity（2h） | ✅ | set_affinity TTL=7200 |
| Failover / tried_quota_groups | ✅ | context + select mark_tried |
| 同 group 最多 1 次、最多 3 group | ✅ | RequestRoutingContext |
| 并发租约 | ✅ lease.py | |
| 耗尽后清除该账号 affinity | ❌ **未真正实现** | `clear_affinity_for_deployment_prefix` 仅为 debug 桩；熔断时**不清理** Redis affinity |
| 请求级切换与 LiteLLM `num_retries` 协同 | ⚠️ | yaml `num_retries: 2`；tried 依赖 **同一 kwargs 上的 context**。若 LiteLLM 重试不共用 context 对象，tried/first_byte 可能丢失或重复选 |
| first_byte 后禁止切换 | ⚠️ 逻辑有、硬接线弱 | strategy 会拒绝 first_byte；**`should_allow_retry` 未被 LiteLLM 调用**。流式后若 Router 仍重试，依赖 context 是否带上 first_byte |

---

## 5. 错误分类与熔断（§8）

| 设计 | 实现 | 差距 |
|------|------|------|
| FailureKind 全枚举 | ✅ base.py | |
| 熔断范围表 | ⚠️ 主路径 | EXHAUSTED / SHORT / AUTH / DISABLED / PROVIDER_OUTAGE / policy/bad 有；**UNKNOWN「最多切一次」未单独编码** |
| 高置信才整组耗尽 | ✅ | confidence 门槛 + 低置信降级 short |
| Provider 适配器 opencode/volc | ⚠️ 骨架 | **实质走 generic_openai**；无真实供应商样例规则库 |
| 分类器 fixtures | ⚠️ | tests 内联样例；`tests/fixtures/` 目录空 |

---

## 6. 流式（§9）

| 设计 | 实现 | 差距 |
|------|------|------|
| 跟踪 first_byte_sent | ✅ | mark_first_byte + context |
| 首字节后禁止切换拼接 | ⚠️ | 单测/场景 F 覆盖**门禁函数**；**无「真 SSE 经 LiteLLM Proxy 中途失败」e2e** |
| 失败后更新状态供下次请求 | ✅ | on_failure 仍写 store |

---

## 7. 恢复探针（§10–11）

| 设计 | 实现 | 差距 |
|------|------|------|
| reset_at / Retry-After | ⚠️ | mark_exhausted 支持 reset_at；Retry-After→reset_at 在分类器有，回调透传 |
| 无 reset_at 不臆造五小时 | ✅ | 退避 5m/15m/30m/60m，上限参考 2h |
| 探针最小 tokens、短 prompt、15s | ✅ | recovery_worker |
| 单 group 单探针锁 | ✅ | probe-lock NX |
| 不经用户 Router | ✅ | 直连 HTTP |
| PROBING 超时回退 | ✅ | |
| 扫描周期约 1 分钟 | ✅ | DEFAULT_SCAN_INTERVAL=60 |

---

## 8. 工程目录与部署（§13–15、§20）

| 设计 | 实现 | 差距 |
|------|------|------|
| 目录树 | ✅ 大体一致 | 无 `uv.lock`（用 pip/pyproject） |
| Docker compose redis/postgres/caddy | ✅ | profile core/full |
| 仅 127.0.0.1 | ✅ | |
| Redis 密码 | ✅ | |
| 固定镜像版本 | ✅ | digest 已记录 |
| cosign 校验 | ❌ | 文档提，**脚本未强制 verify** |
| Master Key ≥32 | ⚠️ | 文档要求；**无启动校验** |
| 日志脱敏 | ⚠️ | logging 策略声明；**无统一脱敏中间件** |

---

## 9. 可观测（§16）

| 设计 | 实现 | 差距 |
|------|------|------|
| 指标名一套 | ⚠️ | metrics.py 进程内计数器，**非 Prometheus exporter** |
| 结构化 JSON 日志 | ⚠️ | 有字段化 logger；非强制 JSON 格式 |
| 禁止敏感标签 | ✅ | metrics 过滤 key 类标签 |

---

## 10. 测试（§17）

| 设计 | 实现 | 差距 |
|------|------|------|
| 单元：候选/状态机/分类/affinity/探测/首字节/租约 | ✅ 大部分 | 「全量 Redis 原子变更」弱 |
| 契约 §17.2 全矩阵 | ⚠️ 部分 | **有 C0/C1**；缺 Proxy Hook 加载、流式错误回调、**Router 重试重选**的正式契约项 |
| 场景 A–F | ✅ 进程内 integration | **非真实 HTTP 上游经 Proxy** |
| 100 并发 | ❌ | 未做 |
| upgrade 测试目录 | ⚠️ 空 | `tests/upgrade/` 空 |

---

## 11. 范围边界（§21–22、§25）

| 项 | 状态 |
|----|------|
| 被动额度 only | ✅ 符合 |
| 未做 quota_collectors / Cookie / 预测余额 | ✅ 符合 |
| 无默认跨模型 fallback | ✅ litellm.yaml 无 fallback |
| 二阶段主动采集 | ✅ 正确未做 |

---

## 12. §23 验收清单逐条

| 验收项 | 判定 | 说明 |
|--------|------|------|
| 客户端只能看到 kimi-k3 和 glm-5.2 | ✅ 配置级 / 曾运行验证 | 改 yaml 可破；默认符合 |
| 三个套餐均可加载 | ✅ 配置 | 真 Key 需用户填 |
| 同名模型可跨套餐路由 | ✅ 单测+C0/C1 | |
| 同一套餐共享 QuotaGroup | ✅ | |
| A/kimi 耗尽后 A/glm 不可用 | ✅ 集成场景 A | |
| 请求能切换到 B 或 C | ✅ 集成/C1 | |
| 瞬时限流不误熔断账号 | ✅ 场景 B | |
| 流式首字节后不切换拼接 | ⚠️ 逻辑+单测 | **缺 Proxy 级 e2e 证明** |
| QuotaGroup 可自动探测恢复 | ✅ 单测场景 E | 真 HTTP 探针依赖上游 |
| Redis 重启后状态可恢复 | ⚠️ 设计有 AOF | **未专项验证** |
| LiteLLM 升级契约通过 | ⚠️ 部分 | C0/C1 非 §17.2 全量 |
| 版本固定且镜像签名验证 | ⚠️ 固定有 / **签名无** | |
| 数据库可备份回滚 | ⚠️ 脚本有 | core 模式可不用 PG |
| API Key 不进日志/Git | ✅ 原则+gitignore | 无全面审计 |

---

## 13. 不一致 / 风险清单（按严重度）

### P0 — 影响「真用时行为正确性」

1. **流式禁切未与 LiteLLM 重试硬绑定** → **已修（2026-07-18）**  
   `first_byte` 写入 `sq:reqctx`；strategy 选路入口硬拒绝。见 `docs/p0-fix-plan.md`。  
2. **耗尽后 affinity 未清理** → **已修**  
   `sq:affinity-idx` + `clear_affinity_for_quota_group`；`mark_exhausted` / AUTH DISABLED 调用。  
3. **RequestRoutingContext 跨重试持久化依赖 kwargs 同一性** → **已修**  
   `sq:reqctx:{request_id}` 存 tried + first_byte；strategy/callback 加载合并。

### P1 — 完整度 / 可运维

4. Redis 状态变更未全面 Lua 化（仅 lease）  
5. `sq:audit` 缺失  
6. 供应商分类器无真实样例深度  
7. 无 100 并发 / 无 Proxy 全链路 e2e（mock→litellm→熔断）  
8. cosign / 镜像签名流程未自动化  
9. Prometheus 未落地（仅内存 counter）  
10. `uv.lock` 无；`tests/fixtures`、`tests/upgrade` 空

### P2 — 文档/体验

11. upgrade 脚本偏 checklist，非全自动  
12. Master Key 长度无运行时强制校验  

---

## 14. 一致性结论矩阵（功能块）

```text
目标与原则 .......... ████████████ 对齐
目录与版本 .......... ████████████ 对齐
配置 model_list ..... ████████████ 对齐
选路 Fill First ..... ███████████░ 对齐（affinity 清理弱）
失败分类熔断 ........ ██████████░░ 主路径对齐（供应商深度弱）
流式边界 ............ ████████░░░░ 逻辑有，运行时证明不足
恢复探针 ............ ███████████░ 对齐
Redis 模型 .......... █████████░░░ 键大体有，原子/审计不足
测试 ................ █████████░░░ 单测强，全栈/并发弱
运维升级 ............ ████████░░░░ 文档有，签名/自动化弱
二阶段不做 .......... ████████████ 正确 defer
```

---

## 15. 建议补齐顺序（若要对齐原文「交付」）

1. **P0：** 耗尽时清理 affinity（按 quota_group 建反向索引或 SCAN 可接受的本机规模实现）  
2. **P0：** 将 tried/first_byte 写入 Redis `sq:reqctx:{request_id}` TTL，strategy/callback 共用，摆脱 kwargs 同一性  
3. **P0：** 用 mock_provider + compose 跑一条「stream_fail_after + 确认响应未拼接」真实 Proxy e2e  
4. **P1：** 采集真实 OpenCode/火山 429 文案进 fixtures 与 classifier  
5. **P1：** 补 §17.2 剩余契约项；可选小规模并发  
6. **P1：** cosign verify 写入 upgrade/smoke（环境允许时）  

---

## 16. 最终判断

| 问题 | 答案 |
|------|------|
| 与原始方案方向是否一致？ | **是**，原则与一期范围一致，未跑偏到重度 Fork 或二阶段采集。 |
| 是否完整实现原文？ | **否**，约 **逻辑完整度 75–85%**，**运行时证明与运维硬化 50–60%**。 |
| 能否投入个人实际使用？ | **可以有条件使用**（小流量 + 自备真实 Key + 观察熔断日志）；不建议在未补 P0 前当作「零风险生产中枢」。 |
| 最大偏差点？ | **流式/重试协作与 affinity 清理、Redis 全量原子与审计、全栈 e2e/签名。** |

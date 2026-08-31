# 网关框架升级

本目录存放 **LiteLLM 本地中转站** 从「共享额度路由」演进到「可插拔请求增强网关」的设计与计划。

与 `docs/分阶段开发方案.md`（阶段 1–9 额度路由）互补：那份文档覆盖已落地的配额 / 协议网关；这里覆盖后续能力。

实现仍落在 `local-llm-router/plugins/shared_quota_router/`，不修改 `upstream/litellm` 业务逻辑。挂载方式继续走 G0-B（pre-call / strategy / callback）。

主文档状态：**方向通过；挂点与 IMAGE 时序已闭合（S1 / S2 / S5）。** 视觉配方 V1（可配置 execute / 译图槽位）与网关记忆 V1 **已编码落地**。pre-call 剥图仍不可用。Q1–Q6 已冻结。日常操作见 [`maintenance.md`](./maintenance.md)。

## 文档地图

| 文件 | 类型 | 内容 |
|------|------|------|
| [maintenance.md](./maintenance.md) | **维护方案（当前操作入口）** | 视觉门面切槽位、flag/回滚、回归命令、禁止项 |
| [design-proposal.md](./design-proposal.md) | 设计提案 | 可插拔模块、多模型组合（图像翻译）、网关层共享记忆 |
| [plans/2026-08-21-p0-probes.md](./plans/2026-08-21-p0-probes.md) | P0 探针执行计划 | 探针 A（M3 识图）/ 探针 B（pre-call 改 messages） |
| [plans/2026-08-25-vision-and-memory.md](./plans/2026-08-25-vision-and-memory.md) | 增量施工方案（历史） | 预置 `glm-5.2-vision` + 网关记忆 |
| [plans/2026-08-30-composable-vision-recipes.md](./plans/2026-08-30-composable-vision-recipes.md) | 施工计划 | 可配置视觉配方 V1；**已完成**（2026-08-30） |
| [reports/p0-probe-a.md](./reports/p0-probe-a.md) | P0 报告 | 探针 A = PASS（直连 MiniMax `VISION_OK`） |
| [reports/p0-probe-b.md](./reports/p0-probe-b.md) | P0 报告 | 探针 B = FAIL（网关 MiniMax-M3 回 `pong`，marker 未到上游） |
| [reports/p0-probe-b-s1.md](./reports/p0-probe-b-s1.md) | P0 报告 | remount S1 = PASS（选号后 `request_kwargs`，MiniMax 回显 token） |
| [reports/p0-probe-b-s2.md](./reports/p0-probe-b-s2.md) | P0 报告 | remount S2 = PASS（mock 上游 `probe_marker_hit` 对照 + 注入） |
| [reports/p0-probe-s5.md](./reports/p0-probe-s5.md) | P0 报告 | remount S5 = PASS（`glm-5.2` 带图仍拒；合成模型选号后剥图到达 mock） |
| [pipeline.md](./pipeline.md) | 规格 | 信封 + 阶段契约；增减模块的不变量 |
| [vision-compose.md](./vision-compose.md) | 规格 | 视觉翻译配方、全量历史替换 |
| [vision-agent-prompt-presets.md](./vision-agent-prompt-presets.md) | 优化方案 v2（A/B/C 已编码） | 视觉不变量 + AGENT 译图预设；V1 = 头/UA/Read-tool 指纹的 OpenCode + generic |
| [memory.md](./memory.md) | 规格 | 本地 AI app 共享的网关记忆 |
| [specs/2026-08-28-composable-recipes-design.md](./specs/2026-08-28-composable-recipes-design.md) | 设计规格 | 可配置视觉配方 V1 契约；P0 已实现 |
| [inquiries/2026-08-30-homogeneous-imm-rethink.md](./inquiries/2026-08-30-homogeneous-imm-rethink.md) | **发散稿（不可施工）** | 同权 IMM 重思考；与附录 A 异构 rethink 不是一回事 |

挂点与 IMAGE 时序已闭合。**不要**在 pre-call 里剥图，也**不要**把 S5 stub 当配方。不要在本目录外另开平行设计稿。实现任务也不要塞进 `local-llm-router/docs/tasks.md` 的协议网关任务板。

现网若要把预置门面的 execute 从 `glm-5.2` 换成库存 `glm-5.3`，走 [`maintenance.md`](./maintenance.md) 的宿主机 CLI，然后重建 litellm 容器。不要开 rethink / 管理 UI。

## 与现有文档的关系

| 现有文档 | 关系 |
|----------|------|
| `升级版的开发设计方案.md` | 额度路由总设计；本升级不取代它 |
| `docs/分阶段开发方案.md` | 阶段 1–9 执行基线 |
| `local-llm-router/docs/architecture.md` | 当前已实现架构摘要 |
| `local-llm-router/docs/adr/ADR-protocol-gateway-integration-boundary.md` | G0-B 边界；流水线挂在该边界内 |
| `local-llm-router/USAGE.md` | **使用说明**（启动、客户端、切视觉槽位） |
| `local-llm-router/docs/配置套餐与启动.md` | Windows 套餐 / 启动；含视觉门面客户端配置 |
| `local-llm-router/docs/operations.md` | 个人本机运维手册 |
| `local-llm-router/AGENTS.md` | 实现约束 |

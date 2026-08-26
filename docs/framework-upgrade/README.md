# 网关框架升级

本目录存放 **LiteLLM 本地中转站** 从「共享额度路由」演进到「可插拔请求增强网关」的设计与计划。

与 `docs/分阶段开发方案.md`（阶段 1–9 额度路由）互补：那份文档覆盖已落地的配额 / 协议网关；这里覆盖后续能力。

实现仍落在 `local-llm-router/plugins/shared_quota_router/`，不修改 `upstream/litellm` 业务逻辑。挂载方式继续走 G0-B（pre-call / strategy / callback）。

主文档状态：**方向通过；挂点与 IMAGE 时序已闭合（S1 / S2 / S5）。** pre-call 剥图仍不可用。施工方案已拟定（视觉配方 + 网关记忆）。Q1–Q6 已冻结。未写三份规格之前不实现 MiniMax 翻译。

## 文档地图

| 文件 | 类型 | 内容 |
|------|------|------|
| [design-proposal.md](./design-proposal.md) | **设计提案（当前主文档）** | 可插拔模块、多模型组合（图像翻译）、网关层共享记忆 |
| [plans/2026-08-21-p0-probes.md](./plans/2026-08-21-p0-probes.md) | **P0 探针执行计划** | 探针 A（M3 识图）/ 探针 B（pre-call 改 messages） |
| [plans/2026-08-25-vision-and-memory.md](./plans/2026-08-25-vision-and-memory.md) | **增量施工方案** | `glm-5.2-vision` 配方 + 网关记忆；对照当前代码的差距与分阶段任务 |
| [reports/p0-probe-a.md](./reports/p0-probe-a.md) | **P0 报告** | 探针 A = PASS（直连 MiniMax `VISION_OK`） |
| [reports/p0-probe-b.md](./reports/p0-probe-b.md) | **P0 报告** | 探针 B = FAIL（网关 MiniMax-M3 回 `pong`，marker 未到上游） |
| [reports/p0-probe-b-s1.md](./reports/p0-probe-b-s1.md) | **P0 报告** | remount S1 = PASS（选号后 `request_kwargs`，MiniMax 回显 token） |
| [reports/p0-probe-b-s2.md](./reports/p0-probe-b-s2.md) | **P0 报告** | remount S2 = PASS（mock 上游 `probe_marker_hit` 对照 + 注入） |
| [reports/p0-probe-s5.md](./reports/p0-probe-s5.md) | **P0 报告** | remount S5 = PASS（`glm-5.2` 带图仍拒；合成模型选号后剥图到达 mock） |
| [pipeline.md](./pipeline.md) | 规格 | 信封 + 阶段契约；增减模块的不变量 |
| [vision-compose.md](./vision-compose.md) | 规格 | 视觉翻译配方、全量历史替换 |
| [memory.md](./memory.md) | 规格 | 本地 AI app 共享的网关记忆 |

挂点与 IMAGE 时序已闭合。下一步按 [`plans/2026-08-25-vision-and-memory.md`](./plans/2026-08-25-vision-and-memory.md)：先写三份规格，再编码。不要在 pre-call 里剥图，也不要把 S5 stub 当配方。不要在本目录外另开平行设计稿。实现任务也不要塞进 `local-llm-router/docs/tasks.md` 的协议网关任务板。

## 与现有文档的关系

| 现有文档 | 关系 |
|----------|------|
| `升级版的开发设计方案.md` | 额度路由总设计；本升级不取代它 |
| `docs/分阶段开发方案.md` | 阶段 1–9 执行基线 |
| `local-llm-router/docs/architecture.md` | 当前已实现架构摘要 |
| `local-llm-router/docs/adr/ADR-protocol-gateway-integration-boundary.md` | G0-B 边界；流水线挂在该边界内 |
| `local-llm-router/AGENTS.md` | 实现约束 |

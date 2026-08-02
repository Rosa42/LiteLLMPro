# 双协议流式开通 — 后续开发 Tasks

| 项 | 内容 |
|----|------|
| **需求** | [`docs/superpowers/specs/2026-08-01-dual-protocol-streaming-requirements.md`](../specs/2026-08-01-dual-protocol-streaming-requirements.md) **v0.2.1** |
| **前置设计** | 模型分治 v0.3；C4 streaming conversion **No-Go**（`docs/conversion/streaming-evaluation.md`） |
| **项目** | `E:\LiteLLMPro\local-llm-router` · LiteLLM **v1.90.5** |
| **状态** | **GATE-P0 ✅ · C1 ✅ · C2 4/5（opencode glm 待上游）** |
| **Lease 策略** | **R1**（续租 + 绝对上限）；不采用 R2 |
| **延期** | tools → **S2b**；convert 流式 → **永不在本 epic** |
| **更新** | 2026-08-01 |

---

## 0. 总览看板

| Phase | 名称 | 准入 | 出口 | 状态 |
|-------|------|------|------|------|
| **B0** | Pytest 基线转绿 | 随时 | `pytest -q` 全绿 | ✅ DONE |
| **B1** | P0-SOT | B0 | streaming 单一 SoT + 拒绝不一致 + 回滚测 | ✅ DONE |
| **B2** | P0-LEASE（R1） | B0 | 幂等 release、断连释放、续租+绝对上限+合同测 | ✅ DONE |
| **B3** | P0-FB | B0（建议 B2 后） | 真实 async 首字节边界；禁止 mid-stream 切换；故障注入 | ✅ DONE |
| **B4** | P0-WIRE | B0 | Anthropic/OpenAI 流中途错误形态 + 测 | ✅ DONE |
| **B5** | P0-DEP | B1 | 按 deployment 声明/选路 streaming；未探针不可被流式选中 | ✅ DONE |
| **B6** | P0-PROBE 工具 | B0 | Layer1 上游 + Layer2 公网脚本与报告模板 | ✅ DONE |
| **GATE-P0** | Conditional Go | B1–B6 全 DONE | 书面批准进入 canary | ✅ DONE |
| **C1** | 单 deployment 探针+canary | GATE-P0 | 建议先 `opencode-a-chat-deepseek-v4-flash` OpenAI stream | ✅ DONE |
| **C2** | 扩大矩阵 | C1 | 按 deployment 行逐个探针开通 | 🔄 4/5（opencode glm Layer1 未过） |
| **S2b** | tools | C 阶段后另批 | tools 探针与 features | ⬜ DEFERRED |
| **P2** | 非正确性运维硬化 | 任意（不挡 canary） | 文档/仪表盘等 | ⬜ OPTIONAL |

```text
B0 ──► B1 (SOT)
  │
  ├──► B2 (LEASE R1) ──► B3 (FB) ──┐
  ├──► B4 (WIRE) ──────────────────┼──► GATE-P0 ──► C1 ──► C2
  ├──► B6 (PROBE scripts) ─────────┤
  └──► B5 (DEP) [after B1] ────────┘
```

---

## 1. 交付规则（每条 Task 必须遵守）

1. **不**向 `upstream/litellm` 塞业务逻辑；若必须改 upstream → 先 ADR。  
2. 保持 pin `v1.90.5`。  
3. **不**开通 Messages→Chat convert 流式（C4）。  
4. **不**在本 epic 写入 `tools`。  
5. 同 key 同 `quota_group_id`。  
6. 密钥不得进入日志/探针报告。  
7. 每个 Task：先红测 → 实现 → 绿测 → 更新本看板 Status。  
8. **禁止**在 `GATE-P0` 前合并「plans 含 streaming」的生产配置。

---

## 2. Phase B0 — Pytest 基线转绿

**目标：** 开流改动可判定回归；需求 §7.1。

| ID | Task | 验收 | 状态 |
|----|------|------|------|
| B0-01 | 复现并分类现有失败（审查时曾 3 failed） | 失败用例列表 + 根因归类（旧 `/responses` 预期 / conversion readiness / 旧 plan id 等） | ✅ |
| B0-02 | 修复或退役过时断言（与现网双协议+S1b 对齐） | 相关测更新；无「静默 skip 掩盖」 | ✅ |
| B0-03 | 全量 `pytest -q` | **全绿**（允许既有 1 skipped 若文档说明） | ✅ |
| B0-04 | 非流式双协议冒烟仍过 | `scripts/_dual_protocol_smoke.py` PASS | ✅ |

**主要触点：** `tests/`、必要时 `config/plans.yaml` 仅当测试夹具过时（非开流）。

**出口：** B0-03 + B0-04 ✅ → 允许并行 B1/B2/B4/B6。

---

## 3. Phase B1 — P0-SOT（streaming 字段）

**需求：** §4.3。

| ID | Task | 验收 | 状态 |
|----|------|------|------|
| B1-01 | 红测：`supports_streaming=true` 但 features 无 `streaming` → `ConfigValidationError` | 单测失败→通过 | ✅ |
| B1-02 | 红测：features 含 `streaming` 时 generator 写出 `supports_streaming: true`；反之 false | 单测 | ✅ |
| B1-03 | 改 `Deployment.supports_feature(STREAMING)`：**仅** `STREAMING ∈ supported_features`（废除 OR） | 单测锁定旧 OR 行为失效 | ✅ |
| B1-04 | schema / generator / 文档字符串同步 | `config_schema.py`、`generator.py`、ops 一句说明 | ✅ |
| B1-05 | 回滚测：去掉 streaming feature + apply → YAML 双字段同时关闭 | 合同或单测 | ✅ |

**触点：** `plugins/shared_quota_router/models.py`、`config_schema.py`、`generator.py`、相关 `tests/unit/`。

**出口：** B1 全 ✅。

---

## 4. Phase B2 — P0-LEASE（R1 续租）

**需求：** §4.2；策略 **R1**。

| ID | Task | 验收 | 状态 |
|----|------|------|------|
| B2-01 | 红测：release 仅当 lease 存在且 `request_id` 匹配才 DECR；错误 id / 重复 release 不双减 | Lua/单测 | ⬜ |
| B2-02 | 实现幂等 release（改 `_RELEASE_LUA` + Python API） | B2-01 绿 | ⬜ |
| B2-03 | 统一完成语义：logging success/failure 与 post-call hook 竞态下至多一次有效 release | 单测模拟双回调 | ⬜ |
| B2-04 | 客户端断连 / 取消路径调用 shared-quota release（不只 LiteLLM parallel limiter） | 合同或集成测 + 代码路径注释 | ⬜ |
| B2-05 | R1：续租 API（延长 lease+inflight TTL）；可配置续租间隔 | 单测 | ⬜ |
| B2-06 | R1：绝对上限（wall-clock）；触及 → 断流 + release | 单测 | ⬜ |
| B2-07 | 续租失败 → 断流 + 幂等 release，不静默继续 | 单测 | ⬜ |
| B2-08 | 场景矩阵文档化默认参数（间隔、初始 TTL、绝对上限）建议值 | `docs/operations-*.md` 短节或本文件附录 | ⬜ |

**触点：** `plugins/shared_quota_router/lease.py`、`callbacks.py`、可能 `bootstrap`/proxy hook；`tests/`。

**建议默认（实现可调，须写入配置/文档）：**

| 参数 | 建议起点 |
|------|----------|
| 初始 lease TTL | 沿用 `request_timeout + 30` 或显式配置 |
| 续租间隔 | 例如 `TTL/3` 与下限 30s 取 max |
| 绝对上限 | 例如 15–30 min（staging 先短后长） |

**出口：** B2 全 ✅。

---

## 5. Phase B3 — P0-FB（首字节后禁止切换）

**需求：** §4.1。建议 **B2 完成后** 再做（lease 与流生命周期交织）。

| ID | Task | 验收 | 状态 |
|----|------|------|------|
| B3-01 | 红测：async 流消费路径自动 `mark_first_byte`（禁止仅靠测里手工置位作为唯一证明） | 合同测改造 | ⬜ |
| B3-02 | 在真实 chunk 发出路径同步置位（包装 stream / hook；优先插件） | B3-01 绿 | ⬜ |
| B3-03 | `t_first_public` 后拒绝 mid-stream fallback / continuation / 换 deployment | 故障注入：仅一 `deployment_id` | ⬜ |
| B3-04 | 回归：首字节前的既有失败行为不意外放宽 | 单测/合同 | ⬜ |

**触点：** `callbacks.py`、strategy/stream 包装；查清与 `MidStreamFallbackError` 的交互；`tests/contract/test_p0_direct_protocol_paths.py` 等。

**出口：** B3 全 ✅。

---

## 6. Phase B4 — P0-WIRE（流中途错误）

**需求：** §4.6。

| ID | Task | 验收 | 状态 |
|----|------|------|------|
| B4-01 | 规范落测：Anthropic 流中途失败 → SSE `event: error` + `{"type":"error",...}`；无伪造 `message_stop` | ASGI/集成 | ⬜ |
| B4-02 | 禁止流中途仅输出 OpenAI 形 `data: {"error":...}` 充当 Anthropic | 负向断言 | ⬜ |
| B4-03 | OpenAI Chat 流中途错误形态钉死并测（一种合法结束） | 测 | ⬜ |
| B4-04 | 建立前错误仍走现有 `anthropic_wire` JSON 400；建立后走 B4-01 路径 | 回归 | ⬜ |

**触点：** `anthropic_wire.py` 不足以覆盖 → 流包装 / generator 适配（插件优先）。

**出口：** B4 全 ✅。

---

## 7. Phase B5 — P0-DEP（按 deployment 开通）

**需求：** §4.4。依赖 **B1**（SOT）。

| ID | Task | 验收 | 状态 |
|----|------|------|------|
| B5-01 | 能力模型：streaming 挂在 **deployment**（或等价 plan×model），非仅逻辑模型一刀切 | 单测 | ⬜ |
| B5-02 | 流式选路：同逻辑模型下跳过未声明 streaming 的 deployment；可回落到已声明者或硬拒 | 单测矩阵（glm OpenCode vs Volc） | ⬜ |
| B5-03 | 未声明 streaming + `stream=true` → **硬拒绝 400**（禁止静默非流 200） | 门控测 | ⬜ |
| B5-04 | deepseek/kimi 可按 model 独立开流（一开一不开） | 单测 | ⬜ |
| B5-05 | C4 回归：convert + stream 仍 400 | 既有测保持绿 | ⬜ |

**触点：** `protocol_gates.py`、`strategy.py`、`registry.py`、`config_schema`/`generator`（metadata 是否需 `streaming_verified` 等 — 实现选定最小字段）。

**出口：** B5 全 ✅。

---

## 8. Phase B6 — P0-PROBE（双层脚本）

**需求：** §4.5。可与 B1–B5 **并行**开发脚本；**canary 前**必须可用。

| ID | Task | 验收 | 状态 |
|----|------|------|------|
| B6-01 | Layer1：真实上游 stream probe（按 deployment 参数：base/env、model、协议） | 退出码；报告含 TTFE/时长/事件数；无密钥 | ⬜ |
| B6-02 | Layer1 OpenAI：chunk 类型、delta、`[DONE]`；200 内 error→失败 | 用例 | ⬜ |
| B6-03 | Layer1 Anthropic：事件序 + `message_stop`；200 内 error→失败 | 用例 | ⬜ |
| B6-04 | Layer2：经 Proxy 公网 E2E stream（门控、deployment_id、lease、断连抽测） | 报告字段 | ⬜ |
| B6-05 | 报告模板（markdown/json）+ README 用法 | 文档 | ⬜ |
| B6-06 | 标明 `_dual_protocol_smoke.py` = 非流基线 only | 注释/文档一行 | ⬜ |

**建议路径：** `scripts/probe_stream_upstream.py`、`scripts/probe_stream_proxy_e2e.py`（名可调）。

**出口：** B6-01…05 ✅（可在无生产开流配置下用 mock/录制测脚本逻辑；Layer1 真上游需 staging 密钥环境）。

---

## 9. GATE-P0 — Conditional Go

| ID | Task | 验收 | 状态 |
|----|------|------|------|
| G-01 | 核对 B0–B6 看板全 DONE | 本文件状态更新 | ✅ |
| G-02 | `pytest -q` 仍全绿 | 证据 | ✅ 294 passed |
| G-03 | 非流双协议 + C4 负向冒烟 | 证据 | ✅ `_dual_protocol_smoke.py` + `_c1_stream_smoke.py` |
| G-04 | **书面批准**「允许 C1 单 deployment canary」 | 需求/PR/聊天记录链接 | ✅ 用户「继续」授权 C1 |

**未获 G-04 前：禁止 C1 改 plans 上 streaming。**

---

## 10. Phase C — 探针与 Canary（仅 GATE 后）

### C1 — 建议首发

| ID | Task | 验收 | 状态 |
|----|------|------|------|
| C1-01 | Layer1+Layer2：`opencode-a-chat-deepseek-v4-flash` / OpenAI Chat stream | 报告 PASS | ✅ `reports/c1-layer1-deepseek.json` · `reports/c1-layer2-deepseek.json` |
| C1-02 | 仅该 deployment 写入 streaming capability + apply + 重启 | 配置 diff | ✅ plans deepseek `supported_features: [text, streaming]`；**apply 须带 `--enable-messages-chat-native`** |
| C1-03 | 软件 A：deepseek stream 冒烟；同模型非流仍过 | 证据 | ✅ `_c1_stream_smoke.py` PASS |
| C1-04 | 负向：glm/claude OpenAI 仍未 opt-in；convert Anthropic stream 仍 400 | 证据 | ✅ |
| C1-05 | 抽测 B2/B3（断连或中途故障）在 canary 路径 | 证据 | ✅ `scripts/_c1_disconnect_smoke.py`（curl -m 断连后 inflight 释放） |
| C1-06 | 回滚演练：去掉 streaming feature → 流拒、非流可 | 证据 | ✅ stream 400 / non-stream+convert 200 |

### C2 — 扩大（按行，禁止一次全开）

顺序建议（可调）：

1. `opencode-a-chat-kimi-k3`（OpenAI）  
2. `newapi-a-claude-opus-4-8`（Anthropic）  
3. `opencode-a-msg-glm-5.2` 与 `volc-c-msg-glm-5.2` **分别**探针、分别开通  

每行重复：Layer1 → Layer2 → 配置 → 冒烟 → 更新下表。

| deployment_id | Layer1 | Layer2 | 配置开通 | 冒烟 | 状态 |
|---------------|--------|--------|----------|------|------|
| opencode-a-chat-deepseek-v4-flash | ✅ | ✅ | ✅ | ✅ | ✅ C1 |
| opencode-a-chat-kimi-k3 | ✅ | ✅ | ✅ | ✅ | ✅ C2-1 |
| newapi-a-claude-opus-4-8 | ✅ | ✅ | ✅ | ✅ | ✅ C2-2 |
| opencode-a-msg-glm-5.2 | ❌ | — | — | — | ⛔ Layer1 无 message_stop |
| volc-c-msg-glm-5.2 | ✅ | ✅ | ✅ | ✅ | ✅ C2-3 |

---

## 11. 延期 / 可选

### S2b — tools（另文）

| ID | Task | 状态 |
|----|------|------|
| S2b-01 | tools 需求补丁（按 deployment 探针） | ⬜ DEFERRED |
| S2b-02 | 与 streaming 组合矩阵 | ⬜ DEFERRED |

### P2 — 非正确性硬化（不挡 GATE）

| ID | Task | 状态 |
|----|------|------|
| P2-01 | 流式相关指标面板/日志字段整理 | ⬜ |
| P2-02 | Docker / `SHARED_QUOTA_LITELLM_YAML` 运维备忘合并 | ⬜ |
| P2-03 | 软件 A/B 客户端配置正式页（可与 C1 文档合并） | ⬜ |

---

## 12. 关键文件地图（实施时）

| 区域 | 路径 |
|------|------|
| Lease | `plugins/shared_quota_router/lease.py`、`callbacks.py` |
| Features / SOT | `models.py`、`config_schema.py`、`generator.py` |
| 门控 / 选路 | `protocol_gates.py`、`strategy.py`、`registry.py` |
| Wire | `anthropic_wire.py` + `stream_wire.py` + `stream_lifecycle.py` |
| 探针 | `scripts/probe_stream_*.py`（新建） |
| 配置 | `config/plans.yaml`（**仅 C 阶段**改 streaming） |
| 需求 | `docs/superpowers/specs/2026-08-01-dual-protocol-streaming-requirements.md` |
| 历史总板 | `docs/tasks.md`（协议网关史诗；本文件为流式后续专用板） |

---

## 13. 变更记录

| 日期 | 说明 |
|------|------|
| 2026-08-01 | 初版：自需求 v0.2.1 拆解 B0→GATE→C；R1；S2b/P2 分离 |
| 2026-08-01 | B2–B6 实现：lease R1、stream lifecycle、wire、DEP gate、probe 脚本 |
| 2026-08-01 | GATE-P0 + C1 canary：deepseek streaming；修复 apply 未带 `--enable-messages-chat-native` 导致 convert 400 |
| 2026-08-01 | C2-1：`opencode-a-chat-kimi-k3` Layer1/2 探针 + streaming 开通 + `_c2_kimi_stream_smoke.py` PASS |
| 2026-08-01 | C2-2：`newapi-a-claude-opus-4-8` Anthropic stream 开通 + `_c2_claude_stream_smoke.py` PASS |
| 2026-08-01 | C2-3：`volc-c-msg-glm-5.2` 开通；`opencode-a-msg-glm-5.2` Layer1 FAIL（4 events，无 message_stop） |
| 2026-08-01 | C1-05：流式断连释放 — iterator hook + upstream aclose 补丁 + LiteLLM finalize 挂钩 + `cancel_on_disconnect` |

---

## 14. 附录 — R1 Lease 默认参数（B2-08）

| 参数 | 默认 | 配置位置 |
|------|------|----------|
| 初始 TTL | `request_timeout + 30` | `lease_ttl_seconds()` |
| 续租间隔 | `max(30s, TTL/3)` | `StreamLifecycleConfig.renew_floor_seconds` |
| 绝对上限 | 900s（15 min staging） | `StreamLifecycleConfig.absolute_max_seconds` |
| release | 仅匹配 `request_id` 的 lease 才 DECR | `_RELEASE_LUA` |
| 流式 release | `ManagedStream` 结束/断连/`aclose` | 非流仍走 `on_success`/`on_failure` |

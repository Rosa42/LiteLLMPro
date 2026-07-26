# C1–C5 Cross-Protocol Conversion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 MVP 直连协议网关之上，按设计分阶段落地跨协议转换：契约（C1）→ 单方向非流式文本试点（C2）→ 转换路径熔断隔离（C3）→ 流式转换评估（C4）→ Responses 直连启用评估（C5）。

**Architecture:** 延续 G0-B（metadata 注入 + 自定义路由策略）。选择层在租约前同时评估 `direct` 与显式 `convert` 候选，**direct 恒优先于 convert**。转换仅在 `allow_conversion` + 方向白名单 + fidelity 通过时进入候选。请求/响应转换落在项目侧 adapter（`plugins/shared_quota_router/conversion/`），不修改 `upstream/litellm`。生产默认关闭：`PROTOCOL_CONVERSION_ENABLED=false`，直至 C2 验收证据齐备。

**Tech Stack:** Python 3.12、LiteLLM v1.90.5（pin）、Redis 配额状态、pytest、现有 `shared_quota_router` 插件（`models` / `registry` / `strategy` / `callbacks` / `protocol_*` / `feature_flags`）。

**Design sources:**
- `docs/protocol-aware-multi-api-gateway-plan.md` §6.4–6.6, §8.4–8.6, §9.4–9.6, §10.4–10.5, §11.5, §12.3–12.4, §14–15
- `docs/tasks.md` §12 (C1–C5)
- `docs/adr/ADR-protocol-gateway-integration-boundary.md` (G0-B; G0-A 仅作转换失败时的 fallback)
- `docs/phase-reports/protocol-gateway-mvp.md` §8

## Global Constraints

- 禁止向 `upstream/litellm` 添加业务逻辑；LiteLLM pin 保持 `v1.90.5`。
- `model_group` 与 `quota_group_id` 语义分离；禁止默认跨模型 fallback。
- Redis 失败 fail-closed；首可见字节后禁止切换部署 / 协议 / 模型。
- 同一请求每 `quota_group` 最多尝试一次，最多三个 quota group。
- 永不从模型名 / provider / URL / `openai/` 前缀推断协议。
- 不静默降级语义：`dropped_fields` 非策略允许则拒绝。
- 不记录 API key、Authorization、完整 prompt/response。
- Conversion metrics 仅在真实转换路径上递增；C1 之前保持 dormant。
- **Kickoff gate（C0 / Task 0）：** 产品确认 `PROTOCOL_AWARE_GATEWAY_ENABLED=true` 且 Chat 稳定后，将 `docs/tasks.md` 中 **C0 → DONE**。**禁止在 C0 完成前** 修改 `plugins/shared_quota_router/` 的转换域模型或运行时。C0 记录之后：C1 契约/文档/单测可先于 C2 挂载落地；预发 `PROTOCOL_CONVERSION_ENABLED=true` 须先完成 C3-01（或书面风险接受）。
- 任务 ID 真源：`docs/tasks.md` §0 / §12（`C0`…`C-CLOSE`）。本 plan Task N 映射见下表。
- 中文注释；提交信息英文 concise；未明确要求不 commit / 不 push。

### Plan Task ↔ tasks.md ID

| Plan | tasks.md |
|------|----------|
| Task 0 | C0 |
| Task 1 | C1-01 |
| Task 2 | C1-02 |
| Task 3 | C1-03 |
| Task 4–5 | C1-04（含 C1 收口报告） |
| Task 6 | C2-01 |
| Task 7 | C2-02 |
| Task 8 | C2-03 |
| Task 9 | C2-04 |
| Task 10 | C2-05 |
| Task 11 | C3-01 |
| Task 12 | C4-01 |
| Task 13 | C5-01 |
| Task 14 | C-CLOSE |

---

## Current State & Gap Analysis

| # | Feature | Design Says | Code Does | Status |
|---|---------|-------------|-----------|--------|
| 1 | Direct protocol filter pre-lease | M2 | `strategy.filter_by_capability` | ✅ Done |
| 2 | Public opt-in / Messages/Responses gates | M3 | `protocol_gates.py` | ✅ Done |
| 3 | Route/reject metrics + gateway flag | M4 | `protocol_observability` / `feature_flags` | ✅ Done |
| 4 | `FidelityClass` / `ConversionCapability` | §9.4 | 不存在 | ❌ C1 |
| 5 | `RouteMode` + `RouteCandidate` | §9.5 / §10 | 仅隐式 direct | ❌ C1 |
| 6 | `allow_conversion` / `conversion_policy` in plans | §8.4–8.6 | `LogicalModelProtocols` 仅 `public_protocols` | ❌ C1 |
| 7 | Deployment `conversions: []` in model_info | §8.5–8.6 | `Deployment` 无 conversions 字段 | ❌ C1 |
| 8 | Ranking `route_mode_rank` (direct=0, convert=1) | §10.5 | 仅 affinity/priority | ❌ C1→C2 |
| 9 | Request/response converter + fixtures | §12.3 / C2 | 无 `conversion/` 包 | ❌ C2 |
| 10 | `PROTOCOL_CONVERSION_ENABLED` | ops / C2 | 仅 gateway flag | ❌ C2 |
| 11 | Conversion metrics live | M4 reserved names | counters always 0 | ❌ C2 |
| 12 | Circuit key includes adapter direction | C3 / §14 | 按 provider/deployment/quota_group | ❌ C3 |
| 13 | Streaming conversion + lease buffering | §12.4 / §15.3 / C4 | N/A | ❌ C4 (evaluate) |
| 14 | Direct Responses provider verification | C5 / M3-03 | endpoint controlled disabled | ❌ C5 (evaluate) |

**Dependency:** `C0 → C1 → C2 → C3`（硬链）。`C2-01`∥`C2-02`；`C2-03` 硬依赖 `C1-02`（可与 flag/spike 并行）。`C4` 硬依赖 `C2-05`，`C3` 为 recommended。`C5` 与转换正交：仅需 `C0`/`MVP-GATE`，**禁止先做 Responses conversion**。

**Recommended pilot direction (product kickoff 可改):**  
`anthropic_messages` (public) → `openai_chat` (upstream)  
理由：当前 plan set 以 `openai_chat` 部署为主；对外暴露 Messages 时最有可能需要转换。反向 `openai_chat → anthropic_messages` 作为第二优先级，C1 契约两者都定义，C2 只实现一条。

---

## File Map (decomposition)

| Path | Responsibility |
|------|----------------|
| `plugins/shared_quota_router/models.py` | `FidelityClass`, `RouteMode`, `ConversionCapability`, `RouteCandidate`; extend `Feature`, `Deployment`, `LogicalModelProtocols` |
| `plugins/shared_quota_router/conversion/__init__.py` | 包导出 |
| `plugins/shared_quota_router/conversion/contracts.py` | 方向注册表、fidelity 矩阵、`ConvertedRequest`/`ConvertedResponse` |
| `plugins/shared_quota_router/conversion/registry.py` | 从 Deployment + logical policy 解析可转换方向 |
| `plugins/shared_quota_router/conversion/adapters/base.py` | `ProtocolConverter` 协议 |
| `plugins/shared_quota_router/conversion/adapters/messages_to_chat.py` | C2 试点 adapter |
| `plugins/shared_quota_router/conversion/dispatch.py` | 按方向选择 adapter；生产门控 |
| `plugins/shared_quota_router/config_schema.py` | 解析 `allow_conversion` / `conversion_policy` / deployment conversions |
| `plugins/shared_quota_router/generator.py` | 写出 conversions metadata；默认 conversion off |
| `plugins/shared_quota_router/registry.py` | 解析 conversions；`resolve_route(...)` |
| `plugins/shared_quota_router/strategy.py` | 租约前 direct∪convert 过滤；`route_mode_rank` |
| `plugins/shared_quota_router/callbacks.py` | convert 路径 request/response hook；确定性失败不重试 |
| `plugins/shared_quota_router/feature_flags.py` | `PROTOCOL_CONVERSION_ENABLED` |
| `plugins/shared_quota_router/protocol_observability.py` | 激活 conversion counters |
| `plugins/shared_quota_router/state_store.py` / classifiers | C3：转换失败隔离键 |
| `tests/unit/test_c1_conversion_contracts.py` | C1 |
| `tests/unit/test_c2_messages_to_chat_pilot.py` | C2 |
| `tests/fixtures/conversion/` | 请求/响应/usage/error fixtures |
| `tests/unit/test_c3_conversion_circuit_isolation.py` | C3 |
| `docs/conversion/` | C4/C5 评估报告 + ops |
| `.env.example` | 新 flag |

---

## Phase Roadmap

```text
C0 Kickoff
   │
   ├──────────────────────────────► C5 Responses direct eval (orthogonal; no conversion)
   │
   ▼
C1 contracts/schema
   │
   ▼
C2 pilot (flag-off default) ──► C3 circuit isolation
   │                               │
   │                               └──rec──► C4 stream eval (hard dep: C2-05)
   │
   └──────────────────────────────────────────► C-CLOSE (after C4/C5 evaluated)
```

| Phase | Module | Risk | Estimate |
|-------|--------|------|----------|
| C0 | Kickoff gate | Low | 0.5d |
| C1 | Domain + config contracts | Low | 1–2d |
| C2 | One non-streaming text adapter + routing | High | 3–5d |
| C3 | Circuit / retry isolation | Medium | 1–2d |
| C4 | Streaming conversion evaluation | High | 2–4d (report, may stop) |
| C5 | Direct Responses enablement eval | Medium | 2–3d (report, may stop) |

---

## Risk Register

| Risk | Probability | Impact | Mitigation |
|------|-------------|--------|------------|
| G0-B 无法在 v1.90.5 完成请求/响应双向转换 | Medium | High | C2-02 / Plan Task 7 spike；失败则升 G0-A thin front adapter，不改 upstream |
| LiteLLM 内部已有翻译被误当作已验证 conversion | High | High | 每方向独立 fixture；禁止“有 litellm 翻译即启用” |
| 转换失败污染 direct 熔断 | Medium | High | C3-01 隔离键含 `route_mode` + direction；确定性失败不触电路；预发开 flag 前完成 C3 |
| 流式 buffering 超出租约/首字节语义 | High | High | C4 评估优先；未证明前 `streaming: false` |
| Responses 未直连就做 conversion | Low | High | C5 硬门禁：先 direct，后 conversion（另开任务） |
| 静默 drop tools/reasoning | Medium | High | fidelity=`unsupported`/`lossy_unsafe` 在租约前拒绝 |

---

### Task 0: Kickoff Gate (no runtime conversion code) — tasks **C0**

**Files:**
- Modify: `docs/tasks.md` §0 (status note only after kickoff)
- Read: `docs/phase-reports/protocol-gateway-mvp.md`, ops metrics

- [ ] **Step 1: Confirm product kickoff**

确认：
1. 生产（或预发）已开 `PROTOCOL_AWARE_GATEWAY_ENABLED=true` 且 Chat 直连稳定。
2. 选定试点方向（默认 `anthropic_messages → openai_chat`）与试点 logical model。
3. 明确生产 conversion 在验收前必须保持关闭；预发开 flag 须 C3-01（或书面风险接受）。

- [ ] **Step 2: Record kickoff decision**

在 `docs/tasks.md` §0：将 **C0 → DONE**（证据列写方向/模型/日期）；编码开始时将 **C1-01 → IN PROGRESS**（不要把整段 C1–C5 标成 IN PROGRESS）。

- [ ] **Step 3: Commit (only if user requests)**

```bash
git add docs/tasks.md
git commit -m "docs: kick off C0 conversion backlog after MVP"
```
---

### Task 1 (C1): Fidelity + ConversionCapability domain model

**Files:**
- Modify: `plugins/shared_quota_router/models.py`
- Create: `tests/unit/test_c1_conversion_contracts.py`
- Consumes: existing `ApiProtocol`, `Feature`
- Produces: `FidelityClass`, `RouteMode`, `ConversionCapability`, `RouteCandidate`; extended `Feature`

- [ ] **Step 1: Write failing tests for enums and capability**

```python
# tests/unit/test_c1_conversion_contracts.py
from shared_quota_router.models import (
    ApiProtocol,
    ConversionCapability,
    Feature,
    FidelityClass,
    RouteMode,
    parse_fidelity_class,
)


def test_fidelity_enum_values():
    assert FidelityClass.EQUIVALENT.value == "equivalent"
    assert FidelityClass.LOSSY_SAFE.value == "lossy_safe"
    assert FidelityClass.LOSSY_UNSAFE.value == "lossy_unsafe"
    assert FidelityClass.UNSUPPORTED.value == "unsupported"


def test_parse_fidelity_rejects_unknown():
    try:
        parse_fidelity_class("safe_for_text_tools_non_streaming")
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_conversion_capability_direction_is_asymmetric():
    cap = ConversionCapability(
        source=ApiProtocol.ANTHROPIC_MESSAGES,
        target=ApiProtocol.OPENAI_CHAT,
        request_features=frozenset({Feature.TEXT}),
        response_features=frozenset({Feature.TEXT}),
        streaming=False,
        fidelity=FidelityClass.EQUIVALENT,
    )
    assert cap.source != cap.target
    assert cap.streaming is False
    assert RouteMode.CONVERT.value == "convert"
    assert RouteMode.DIRECT.value == "direct"


def test_post_mvp_features_exist_for_fidelity_matrix():
    # Used by contracts; extraction may still only emit TEXT/TOOLS/STREAMING in MVP paths
    assert Feature.REASONING.value == "reasoning"
    assert Feature.PROMPT_CACHE.value == "prompt_cache"
    assert Feature.STRUCTURED_OUTPUT.value == "structured_output"
    assert Feature.IMAGE.value == "image"
```

- [ ] **Step 2: Run test to verify it fails**

```powershell
Set-Location E:\LiteLLMPro\local-llm-router
.\.venv\Scripts\python.exe -m pytest tests/unit/test_c1_conversion_contracts.py -v
```

Expected: FAIL (`ConversionCapability` / `FidelityClass` import errors).

- [ ] **Step 3: Implement domain types in `models.py`**

在 `Feature` 中追加（解析可用；默认请求提取仍可不自动加入，除非显式出现）：

```python
class Feature(str, Enum):
    TEXT = "text"
    STREAMING = "streaming"
    TOOLS = "tools"
    REASONING = "reasoning"
    PROMPT_CACHE = "prompt_cache"
    STRUCTURED_OUTPUT = "structured_output"
    IMAGE = "image"
    PARALLEL_TOOL_CALLS = "parallel_tool_calls"
    CITATIONS = "citations"


class FidelityClass(str, Enum):
    EQUIVALENT = "equivalent"
    LOSSY_SAFE = "lossy_safe"
    LOSSY_UNSAFE = "lossy_unsafe"
    UNSUPPORTED = "unsupported"


class RouteMode(str, Enum):
    DIRECT = "direct"
    CONVERT = "convert"


def parse_fidelity_class(value: Any) -> FidelityClass:
    if isinstance(value, FidelityClass):
        return value
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"invalid fidelity: {value!r}")
    try:
        return FidelityClass(value.strip().lower())
    except ValueError as exc:
        known = ", ".join(f.value for f in FidelityClass)
        raise ValueError(f"unknown fidelity {value!r}; expected one of: {known}") from exc


@dataclass(frozen=True, slots=True)
class ConversionCapability:
    """source = public protocol; target = upstream protocol (design §9.4)."""

    source: ApiProtocol
    target: ApiProtocol
    request_features: frozenset[Feature]
    response_features: frozenset[Feature]
    streaming: bool
    fidelity: FidelityClass

    def supports_request_features(self, required: frozenset[Feature]) -> bool:
        return required <= self.request_features


@dataclass(frozen=True, slots=True)
class RouteCandidate:
    deployment: Deployment  # forward ref / order types carefully
    route_mode: RouteMode
    conversion: ConversionCapability | None = None
```

并扩展：

```python
@dataclass(slots=True)
class Deployment:
    # ... existing fields ...
    conversions: tuple[ConversionCapability, ...] = ()


@dataclass(frozen=True, slots=True)
class LogicalModelProtocols:
    model_group: str
    public_protocols: frozenset[ApiProtocol] = frozenset()
    allow_conversion: bool = False
    # allowed directions: (source, target) pairs from conversion_policy.allowed
    allowed_conversions: frozenset[tuple[ApiProtocol, ApiProtocol]] = frozenset()
```

- [ ] **Step 4: Re-run tests — expect PASS**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/test_c1_conversion_contracts.py -v
```

- [ ] **Step 5: Commit (if requested)**

```bash
git add plugins/shared_quota_router/models.py tests/unit/test_c1_conversion_contracts.py
git commit -m "feat(c1): add conversion fidelity and capability domain types"
```

---

### Task 2 (C1): Directional fidelity matrix contracts

**Files:**
- Create: `plugins/shared_quota_router/conversion/__init__.py`
- Create: `plugins/shared_quota_router/conversion/contracts.py`
- Modify: `tests/unit/test_c1_conversion_contracts.py`
- Produces: `feature_fidelity(direction, feature)`, `assert_conversion_allowed(...)`, `ConvertedRequest`

- [ ] **Step 1: Write failing matrix tests**

```python
from shared_quota_router.conversion.contracts import (
    DIRECTION_MESSAGES_TO_CHAT,
    feature_fidelity,
    validate_request_against_fidelity,
)
from shared_quota_router.models import ApiProtocol, Feature, FidelityClass
from shared_quota_router.protocol_errors import ProtocolAwareRoutingError


def test_reasoning_is_lossy_unsafe_on_messages_to_chat():
    assert (
        feature_fidelity(DIRECTION_MESSAGES_TO_CHAT, Feature.REASONING)
        is FidelityClass.LOSSY_UNSAFE
    )


def test_prompt_cache_unsupported_across_conversion():
    assert (
        feature_fidelity(DIRECTION_MESSAGES_TO_CHAT, Feature.PROMPT_CACHE)
        is FidelityClass.UNSUPPORTED
    )


def test_text_only_non_streaming_accepted():
    validate_request_against_fidelity(
        source=ApiProtocol.ANTHROPIC_MESSAGES,
        target=ApiProtocol.OPENAI_CHAT,
        required_features=frozenset({Feature.TEXT}),
        stream=False,
    )


def test_tools_rejected_in_c2_pilot_matrix():
    try:
        validate_request_against_fidelity(
            source=ApiProtocol.ANTHROPIC_MESSAGES,
            target=ApiProtocol.OPENAI_CHAT,
            required_features=frozenset({Feature.TEXT, Feature.TOOLS}),
            stream=False,
        )
        assert False
    except ProtocolAwareRoutingError as exc:
        assert exc.reason.value == "feature_unsupported"
```

- [ ] **Step 2: Run — expect FAIL (module missing)**

- [ ] **Step 3: Implement `conversion/contracts.py`**

```python
"""Directional conversion contracts (C1).

source = public protocol; target = upstream protocol.
Initial matrix is conservative: text-only non-streaming for pilot directions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from shared_quota_router.models import (
    ApiProtocol,
    Feature,
    FidelityClass,
)
from shared_quota_router.protocol_errors import (
    ProtocolAwareRoutingError,
    ProtocolRoutingReason,
)

Direction = tuple[ApiProtocol, ApiProtocol]

DIRECTION_MESSAGES_TO_CHAT: Direction = (
    ApiProtocol.ANTHROPIC_MESSAGES,
    ApiProtocol.OPENAI_CHAT,
)
DIRECTION_CHAT_TO_MESSAGES: Direction = (
    ApiProtocol.OPENAI_CHAT,
    ApiProtocol.ANTHROPIC_MESSAGES,
)

# Per-direction feature fidelity. Missing feature ⇒ UNSUPPORTED.
_FIDELITY: dict[Direction, dict[Feature, FidelityClass]] = {
    DIRECTION_MESSAGES_TO_CHAT: {
        Feature.TEXT: FidelityClass.EQUIVALENT,
        Feature.STREAMING: FidelityClass.UNSUPPORTED,  # until C4
        Feature.TOOLS: FidelityClass.UNSUPPORTED,  # C2 pilot
        Feature.REASONING: FidelityClass.LOSSY_UNSAFE,
        Feature.PROMPT_CACHE: FidelityClass.UNSUPPORTED,
        Feature.STRUCTURED_OUTPUT: FidelityClass.UNSUPPORTED,
        Feature.IMAGE: FidelityClass.UNSUPPORTED,
        Feature.PARALLEL_TOOL_CALLS: FidelityClass.UNSUPPORTED,
        Feature.CITATIONS: FidelityClass.UNSUPPORTED,
    },
    DIRECTION_CHAT_TO_MESSAGES: {
        Feature.TEXT: FidelityClass.EQUIVALENT,
        Feature.STREAMING: FidelityClass.UNSUPPORTED,
        Feature.TOOLS: FidelityClass.UNSUPPORTED,
        Feature.REASONING: FidelityClass.LOSSY_UNSAFE,
        Feature.PROMPT_CACHE: FidelityClass.UNSUPPORTED,
        Feature.STRUCTURED_OUTPUT: FidelityClass.UNSUPPORTED,
        Feature.IMAGE: FidelityClass.UNSUPPORTED,
        Feature.PARALLEL_TOOL_CALLS: FidelityClass.UNSUPPORTED,
        Feature.CITATIONS: FidelityClass.UNSUPPORTED,
    },
}

_REJECT_FIDELITIES = frozenset(
    {FidelityClass.LOSSY_UNSAFE, FidelityClass.UNSUPPORTED}
)


def feature_fidelity(direction: Direction, feature: Feature) -> FidelityClass:
    return _FIDELITY.get(direction, {}).get(feature, FidelityClass.UNSUPPORTED)


def validate_request_against_fidelity(
    *,
    source: ApiProtocol,
    target: ApiProtocol,
    required_features: frozenset[Feature],
    stream: bool,
) -> None:
    direction = (source, target)
    if direction not in _FIDELITY:
        raise ProtocolAwareRoutingError(
            f"no conversion contract for {source.value} -> {target.value}",
            reason=ProtocolRoutingReason.NO_COMPATIBLE_DEPLOYMENT,
            protocol=source,
        )
    features = set(required_features)
    if stream:
        features.add(Feature.STREAMING)
    for feat in features:
        klass = feature_fidelity(direction, feat)
        if klass in _REJECT_FIDELITIES:
            raise ProtocolAwareRoutingError(
                f"conversion {source.value}->{target.value} rejects feature "
                f"{feat.value} ({klass.value})",
                reason=ProtocolRoutingReason.FEATURE_UNSUPPORTED,
                protocol=source,
                details={"feature": feat.value, "fidelity": klass.value},
            )


@dataclass(slots=True)
class ConvertedRequest:
    payload: dict[str, Any]
    warnings: list[str] = field(default_factory=list)
    dropped_fields: list[str] = field(default_factory=list)


@dataclass(slots=True)
class ConvertedResponse:
    payload: dict[str, Any]
    warnings: list[str] = field(default_factory=list)
    dropped_fields: list[str] = field(default_factory=list)
```

- [ ] **Step 4: Tests PASS**

- [ ] **Step 5: Commit (if requested)**

```bash
git add plugins/shared_quota_router/conversion tests/unit/test_c1_conversion_contracts.py
git commit -m "feat(c1): add directional conversion fidelity matrix"
```

---

### Task 3 (C1): Config schema — allow_conversion + conversions

**Files:**
- Modify: `plugins/shared_quota_router/config_schema.py`
- Modify: `plugins/shared_quota_router/registry.py` (`deployment_from_model_entry`)
- Modify: `plugins/shared_quota_router/generator.py`
- Modify: `tests/unit/test_config_schema.py` (or extend `test_c1_conversion_contracts.py`)
- Produces: validated `LogicalModelProtocols.allow_conversion` / `allowed_conversions`; `Deployment.conversions`

- [ ] **Step 1: Failing validation tests**

```python
def test_reject_conversion_while_allow_conversion_false():
    raw = {
        "plans": [...],  # minimal openai_chat plan for model X
        "logical_models": {
            "claude-pilot": {
                "public_protocols": ["anthropic_messages", "openai_chat"],
                "allow_conversion": False,
                "conversion_policy": {
                    "allowed": [
                        {"from": "anthropic_messages", "to": "openai_chat", "fidelity": "equivalent"}
                    ]
                },
            }
        },
    }
    # load_plans_document / validate → ConfigValidationError
```

```python
def test_reject_duplicate_conversion_directions_on_deployment():
    # model_info.protocol.conversions with two identical from/to → error
```

```python
def test_accept_explicit_allowlist_when_allow_conversion_true():
    # allow_conversion: true + matching deployment conversions → ok
```

校验规则（设计 §8.6）必须全部实现：
1. 重复方向拒绝
2. `streaming: true` 但无 streaming adapter 声明 → 拒绝（C1 阶段一律要求 `streaming: false`）
3. fidelity 必须是 `FidelityClass` 四值之一（拒绝设计草稿里的 `safe_for_text_tools_non_streaming` 字符串）
4. `allow_conversion: false` 却声明 `conversion_policy.allowed` 或 deployment conversions → 拒绝
5. public protocol 仅靠 conversion 可达时，必须有 matching `allowed` + deployment conversion + target upstream
6. conversion target 协议必须存在 capable deployment

- [ ] **Step 2: Implement parsers**

扩展 `_parse_logical_models` 读取：

```yaml
logical_models:
  claude-pilot:
    public_protocols: [anthropic_messages, openai_chat]
    allow_conversion: true
    conversion_policy:
      allowed:
        - from: anthropic_messages
          to: openai_chat
          fidelity: equivalent
```

扩展 generator `model_info`：

```yaml
protocol:
  upstream: openai_chat
  conversions:
    - from: anthropic_messages
      to: openai_chat
      streaming: false
      fidelity: equivalent
      features:
        request: [text]
        response: [text]
```

默认：现有 plans **不** 写 conversions；`allow_conversion` 缺省 `false`。

- [ ] **Step 3: Wire `registry.deployment_from_model_entry`**

解析 `model_info.protocol.conversions` → `tuple[ConversionCapability, ...]`。

- [ ] **Step 4: Run**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/test_c1_conversion_contracts.py tests/unit/test_config_schema.py tests/unit/test_generator.py tests/unit/test_registry.py -q
```

Expected: PASS

- [ ] **Step 5: Commit (if requested)**

```bash
git commit -m "feat(c1): validate and emit explicit conversion policy metadata"
```

---

### Task 4 (C1): `resolve_route` — direct preferred over convert

**Files:**
- Modify: `plugins/shared_quota_router/registry.py` or create `conversion/registry.py`
- Modify: `plugins/shared_quota_router/strategy.py` (`filter_by_capability` / ranking)
- Test: `tests/unit/test_c1_conversion_contracts.py`

**Interfaces:**
- Produces:

```python
def resolve_route(
    deployment: Deployment,
    *,
    public_protocol: ApiProtocol,
    required_features: frozenset[Feature],
    stream: bool,
    logical: LogicalModelProtocols | None,
    conversion_enabled: bool,
) -> RouteCandidate | None: ...
```

- [ ] **Step 1: Failing tests**

```python
def test_direct_wins_when_both_direct_and_convert_exist():
    # same model_group: dep_direct (upstream=messages) + dep_convert (upstream=chat, conversion messages→chat)
    # resolve / select → RouteMode.DIRECT


def test_convert_candidate_only_when_no_direct_and_policy_allows():
    ...


def test_convert_filtered_when_flag_off_even_if_configured():
    # conversion_enabled=False → only direct
```

- [ ] **Step 2: Implement resolve + ranking key change**

```python
# ranking key extension (design §10.5)
route_mode_rank = 0 if candidate.route_mode is RouteMode.DIRECT else 1
return (route_mode_rank, affinity_rank, priority, inflight, last_success, deployment_id)
```

`filter_by_capability` 改为返回 `list[RouteCandidate]`（或并行保留 Deployment 列表 + mode 映射）。**租约前**调用 fidelity 校验；失败候选直接丢弃，不记 tried、不占 lease。

- [ ] **Step 3: Tests PASS + existing M2/M3 regression**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/test_m2_protocol_routing.py tests/unit/test_m3_endpoint_gates.py tests/unit/test_c1_conversion_contracts.py -q
```

- [ ] **Step 4: Commit (if requested)**

```bash
git commit -m "feat(c1): resolve direct-vs-convert routes before lease"
```

---

### Task 5 (C1 Completion): Mark C1 done in tasks board

- [ ] Update `docs/tasks.md` §12 C1 checklist + §0 evidence
- [ ] Write short completion note under `docs/phase-reports/conversion-c1.md`
- [ ] Full unit subset green

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit -q --tb=line
```

---

### Task 6 (C2): Feature flag + observability activation hooks

**Files:**
- Modify: `plugins/shared_quota_router/feature_flags.py`
- Modify: `plugins/shared_quota_router/protocol_observability.py`
- Modify: `.env.example`
- Create: `docs/operations-protocol-conversion.md`
- Test: `tests/unit/test_c2_messages_to_chat_pilot.py`

- [ ] **Step 1: Tests**

```python
def test_conversion_flag_defaults_false(monkeypatch):
    monkeypatch.delenv("PROTOCOL_CONVERSION_ENABLED", raising=False)
    clear_flag_cache()
    assert is_protocol_conversion_enabled() is False


def test_record_conversion_increments_reserved_counters():
    # call record_conversion_success / failure → get_counter(CONVERSION_METRIC_NAMES[0]) > 0
```

- [ ] **Step 2: Implement**

```python
def is_protocol_conversion_enabled() -> bool:
    """Production default false until C2 acceptance evidence."""
    return _env_bool("PROTOCOL_CONVERSION_ENABLED", default=False)
```

```python
def record_conversion_result(*, direction: str, result: str, reason: str | None = None) -> None:
    inc("shared_quota_protocol_conversion_total", direction=direction, result=result)
    if result == "failure":
        inc(
            "shared_quota_protocol_conversion_failure_total",
            direction=direction,
            reason=(reason or "unknown")[:64],
        )
```

Ops 文档写明：主回滚 = `PROTOCOL_CONVERSION_ENABLED=false`；不影响 Redis；direct 流量不受影响。

---

### Task 7 (C2): Spike — can G0-B host request/response conversion?

**Files:**
- Create: `docs/phase-reports/conversion-c2-spike-g0b.md`
- Possibly tiny harness under `tests/contract/`

- [ ] **Step 1: Spike checklist**

验证在 LiteLLM v1.90.5 上：
1. `async_pre_call_hook` 能否在 Messages 路径把 body 改写成 Chat 上游所需字段，且 `call_type`/adapter 前缀与选中 deployment 一致。
2. success / failure callback 能否拿到上游 Chat 响应并改回 Anthropic Messages 形状返回客户端（若 proxy 已序列化则评估是否需自定义 logger success hook）。
3. 若任一项失败 → **Stop**：升级到 G0-A thin front adapter（项目侧），仍不改 `upstream/litellm`；修订本 plan Task 8 的挂载点后继续。

- [ ] **Step 2: 写 spike 报告（go / no-go）**

---

### Task 8 (C2): Pilot adapter `anthropic_messages → openai_chat` (text-only, non-stream)

**Files:**
- Create: `plugins/shared_quota_router/conversion/adapters/base.py`
- Create: `plugins/shared_quota_router/conversion/adapters/messages_to_chat.py`
- Create: `plugins/shared_quota_router/conversion/dispatch.py`
- Create: `tests/fixtures/conversion/messages_to_chat/`
  - `request_basic.json`, `request_system_multiturn.json`
  - `response_basic.json`, `response_usage.json`, `response_error.json`
  - `finish_reason_map.json`
- Test: `tests/unit/test_c2_messages_to_chat_pilot.py`

**Interfaces:**
- Produces:

```python
class ProtocolConverter(Protocol):
    direction: Direction
    def convert_request(self, public_payload: dict[str, Any]) -> ConvertedRequest: ...
    def convert_response(self, upstream_payload: dict[str, Any]) -> ConvertedResponse: ...
    def convert_error(self, upstream_error: dict[str, Any]) -> dict[str, Any]: ...
```

- [ ] **Step 1: Fixture-driven failing tests**

```python
def test_convert_request_basic_text():
    adapter = MessagesToChatConverter()
    public = load_fixture("request_basic.json")
    out = adapter.convert_request(public)
    assert out.dropped_fields == []
    assert "messages" in out.payload
    assert out.payload["model"]  # upstream model filled by dispatch, or placeholder


def test_reject_tools_in_request():
    public = {"model": "x", "messages": [], "tools": [{"name": "t"}]}
    # dispatch/validate raises ProtocolAwareRoutingError FEATURE_UNSUPPORTED


def test_convert_response_maps_usage_and_finish_reason():
    ...


def test_convert_error_preserves_anthropic_shape():
    ...
```

- [ ] **Step 2: Implement minimal converter**

映射范围（C2 仅此）：
- `system` / `messages` 角色与 content 文本块
- `max_tokens` ↔ `max_tokens`
- usage: `input_tokens`/`output_tokens` ↔ `prompt_tokens`/`completion_tokens`
- stop_reason ↔ finish_reason（查表）
- 错误：Chat error → Anthropic `type=error` 包装

明确 **不实现**：tools、images、reasoning、streaming、cache_control。

若 `dropped_fields` 非空且字段不在 allowlist（C2 allowlist 为空）→ raise。

- [ ] **Step 3: Unit tests PASS**

---

### Task 9 (C2): Wire selection + dispatch (still production-disabled by default)

**Files:**
- Modify: `strategy.py` — 选中 `RouteCandidate` 后把 `route_mode` / direction 写入 metadata（双桶）
- Modify: `callbacks.py` — pre_call 若 `route_mode=convert` 且 flag on → `convert_request`；success → `convert_response`
- Modify: `protocol_gates.py` — conversion 路径仍要求 public opt-in；不因 conversion 自动放开 Responses
- Test: unit + 必要时 contract

Metadata keys（建议）：

```python
ROUTE_MODE_META_KEY = "shared_quota_route_mode"      # direct | convert
CONVERSION_DIR_META_KEY = "shared_quota_conversion"  # "anthropic_messages>openai_chat"
```

规则：
- flag off → 永不选 convert（即使配置了）
- 确定性 conversion/config 错误 → `should_allow_retry` False；不更新 circuit（沿用 `ProtocolAwareRoutingError`）
- `record_route_selection(..., route_mode="convert")` + `record_conversion_result`

- [ ] **Step 1: Integration-style unit test with fake registry**

两部署同 model：仅 Chat upstream + conversion 声明；Messages 请求在 flag on 时选 convert；flag off 时 `UNSUPPORTED_PUBLIC_PROTOCOL` 或 no compatible（与现有 Messages gate 一致）。

- [ ] **Step 2: Implement wiring**

- [ ] **Step 3: Regression**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit tests/contract -q --tb=line
```

Expected: all prior green; new C2 tests green; conversion counters仍仅在显式调用测试中非零。

---

### Task 10 (C2): Acceptance evidence pack (keep prod disabled)

**Files:**
- Create: `docs/phase-reports/conversion-c2-pilot.md`
- Optional: `config/plans.conversion-pilot.example.yaml`（无密钥）

报告必须包含：
1. 方向、logical model、fixture 列表与命令
2. 明确 **生产默认 `PROTOCOL_CONVERSION_ENABLED=false`**
3. 未覆盖能力清单（tools/stream/reasoning/…）
4. 回滚步骤
5. Go/No-Go：文本非流式契约全绿后，**预发 `flag=true` 仍须 C3-01 完成**（或报告内附 dated risk-acceptance）；生产同理

- [ ] Update `docs/tasks.md` C2-01..C2-05 → DONE（证据链接）
---

### Task 11 (C3): Conversion-path circuit isolation

**Files:**
- Modify: `plugins/shared_quota_router/callbacks.py`
- Modify: `plugins/shared_quota_router/state_store.py`（若需新 key 维度）
- Modify: `plugins/shared_quota_router/classifiers/base.py`（可选 `FailureKind.CONVERSION_DETERMINISTIC`）
- Test: `tests/unit/test_c3_conversion_circuit_isolation.py`

**Rules (design §14 + tasks C3):**
1. 健康度隔离维度：`deployment_id` + `upstream_protocol` + `adapter_direction`（或 `route_mode=convert`）
2. Conversion 适配器确定性失败（映射失败、dropped_fields、fidelity reject）→ **不** 打开 provider/quota circuit；**不** 跨部署重试
3. Upstream 真实故障（5xx、quota）仍按原 classifier，但 cooldown key 不得拖垮同 deployment 的 **direct** 路径

- [ ] **Step 1: Failing tests**

```python
def test_deterministic_conversion_failure_does_not_mark_quota_exhausted():
    ...


def test_convert_path_cooldown_does_not_block_direct_same_deployment():
    # same deployment_id serves direct chat; convert direction cooled down
    ...


def test_no_retry_on_conversion_mapping_error():
    assert callback.should_allow_retry({"exception": ProtocolAwareRoutingError(...)}) is False
```

- [ ] **Step 2: Implement isolation**

建议 Redis cooldown 键：

```text
cooldown:dep:{deployment_id}:direct
cooldown:dep:{deployment_id}:convert:{source}>{target}
```

`on_failure`：若 metadata `route_mode=convert` 且异常为 conversion deterministic → 只写 convert 键 + conversion_failure metric。

- [ ] **Step 3: Tests PASS + M2 no-route 回归**

- [ ] **Step 4: Update tasks.md C3 → DONE**

---

### Task 12 (C4): Evaluate streaming conversion (go/no-go report) — tasks **C4-01**

**Depends on:** C2-05 hard; C3-01 recommended before failure-path eval  
**Blocks:** Any `streaming: true` on conversion capabilities

**Files:**
- Create: `docs/conversion/streaming-evaluation.md`
- Create: `tests/unit/test_c4_streaming_conversion_eval.py`（契约/红灯测试，默认不启用流式 capability）
- Modify: `conversion/contracts.py` only if evaluation promotes a feature（默认不改 streaming=false）

**Evaluation checklist (must all pass before any `streaming: true`):**

| # | Invariant | Evidence |
|---|-----------|----------|
| 1 | First **converted** visible event defines first-byte | test + design §15.3 |
| 2 | Lease held across adapter buffering | lease not released early |
| 3 | Configurable max buffer latency (test-derived) | config knob documented |
| 4 | Event order preserved | fixture |
| 5 | Backpressure / cancellation | test |
| 6 | Tool deltas (if ever in scope) | else N/A + still unsupported |
| 7 | Usage + mid-stream failure shaping | test |
| 8 | Never splice second upstream after visible output | assert |

- [ ] **Step 1: Write red tests that document required behavior**（可先 `@pytest.mark.skip` 或 expect reject streaming convert）

- [ ] **Step 2: Spike buffering on G0-B vs need G0-A stream adapter**

- [ ] **Step 3: Publish go/no-go**

默认预期：**No-Go** 直到缓冲与首字节语义在契约测试中证明。保持矩阵 `Feature.STREAMING = UNSUPPORTED`。

- [ ] **Step 4: tasks.md C4 → DONE (evaluated)** 附报告链接；若 No-Go，运行时代码不启用。

---

### Task 13 (C5): Evaluate direct Responses enablement (no conversion) — tasks **C5-01**

**Depends on:** C0 / MVP-GATE only (orthogonal to C2/C3; may parallel C4)  
**Blocks:** Public `/v1/responses` opt-in; forbids Responses conversion in this epic

**Files:**
- Create: `docs/conversion/responses-direct-evaluation.md`
- Extend: `tests/contract/test_p0_direct_protocol_paths.py` 或新 `test_c5_responses_direct_eval.py`
- Docs: `docs/enabling-messages-responses.md`（更新启用条件）

**Hard gates:**
1. 至少一个 deployment `upstream_protocol: openai_responses` 且 contract 证明上游路径正确（非 Chat bridge）。
2. 验证 reasoning / tools / usage / streaming events / errors（按 provider 实际能力声明）。
3. logical model 显式 `public_protocols: [openai_responses]`。
4. **禁止** 在本任务引入 `openai_chat ↔ openai_responses` conversion。

- [ ] **Step 1: Inventory** — 当前 plans 是否已有 Responses 上游？若无，报告写明 blocker。

- [ ] **Step 2: Contract harness against mock Responses provider**（可复用 P0 mock）

- [ ] **Step 3: Go/No-Go**

- Go：开启文档化步骤 + 生成器允许 opt-in + M3 gate 解除该 model。
- No-Go：保持 controlled disable。

- [ ] **Step 4: tasks.md C5 → DONE (evaluated)**

---

### Task 14: Conversion epic closure

- [ ] Update `docs/tasks.md` §0：C1–C3 实现状态；C4/C5 评估结论
- [ ] Ensure conversion metrics：未启用 flag 时 production 路径保持 0
- [ ] Full suite

```powershell
Set-Location E:\LiteLLMPro\local-llm-router
.\.venv\Scripts\python.exe -m pytest tests/ -q --tb=line
```

- [ ] Optional ADR addendum：若 C2 spike 升到 G0-A，写 `docs/adr/ADR-conversion-adapter-boundary.md`

---

## Verification Checklist (before coding C2+)

- [ ] Kickoff gate passed (Task 0)
- [ ] Existing `tests/` green on current MVP
- [ ] C1 fidelity matrix reviewed against design §6.5 (reasoning lossy_unsafe, prompt_cache unsupported)
- [ ] No Responses conversion scoped in C1–C5
- [ ] Production flags default false
- [ ] Risk mitigations for circuit isolation scheduled in C3 before any prod conversion traffic

---

## Spec Coverage Self-Review

| Spec / Task | Plan mapping |
|-------------|--------------|
| C1 directional contracts + fidelity | Task 1–5 |
| C1 allowlisting | Task 3 |
| C2 one non-streaming text direction | Task 6–10（默认 messages→chat） |
| C2 fixtures request/response/usage/finish/error | Task 8 |
| C2 prod disabled until evidence | Task 6, 10 |
| C3 circuit isolation + no retry deterministic | Task 11 |
| C4 streaming evaluation | Task 12 |
| C5 Responses direct before any Responses conversion | Task 13 |
| Direct ≻ convert ranking | Task 4 |
| No silent drop | Task 2, 8 (`dropped_fields`) |
| G0-B / no upstream edits | Global + Task 7 |

**Placeholder scan:** none intentional — spike outcomes may amend Task 8 mount points only via written spike report.

**Type consistency:** `ConversionCapability.source/target`, `RouteMode`, `ConvertedRequest.dropped_fields`, `PROTOCOL_CONVERSION_ENABLED` used uniformly across tasks.

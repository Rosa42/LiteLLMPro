# 视觉译图 Prompt 的 AGENT 适配 — 优化方案

| 项 | 值 |
| --- | --- |
| 文档类型 | **优化方案**（评审后闭合；按 A/B/C 施工） |
| 状态 | **A/B/C 已编码**；C 指纹来自 2026-08-27 本网关 OpenCode 实包 |
| 版本 | v2（相对 2026-08-26 草案） |
| 日期 | 2026-08-26 |
| 实现落点 | `shared_quota_router/` 视觉阶段内；不改 `upstream/litellm`；不改额度选号 |
| 公开协议 | 仍仅 `anthropic_messages` |
| 依据 | `pipeline.md` §5；`vision-compose.md` IR 契约；外部评审 + 工作树核验 |

本文解决两件事，且必须按这个顺序落地：

1. **先闭合现网视觉不变量**（像素不得进执行模型；缓存键与 MiniMax POST 用同一份 guide）。
2. **再做 AGENT 预设**：有图时先识别已适配客户端，命中则用其抽取/附言；否则走通用层。

不改变：选号后改 `messages`、视觉 fail-closed、记忆 fail-open、G0-B 边界。

---

## 0. 相对草案改了什么

| 草案问题 | v2 决定 |
| --- | --- |
| 同步 `get_available_deployment` 在视觉开时跳过剥图，可能把图送给 `glm-5.2` | 公共同步入口 fail-closed。**不得**在同步函数里无条件 400：async 入口会先调它。用 `contextvars` 区分 |
| 草案允许 sync 缓存命中后同步剥图 | **V1 不做**。同步有图一律 400，降低与 digest 变更纠缠 |
| `extract_guide(messages)` 无图引用 | 改为 `extract_guide(messages, image_ref)`；改写前一次性抽出全部 guide |
| 缓存 `sha256(png \|\| guide)`，stage 与 MiniMax 各抽一次 guide | digest 纳入 `agent_id` + `prompt_rev` + 该图 guide；`translator(png, guide)`；禁止在 POST 时重抽 |
| messages 指纹写进 V1 | **默认关闭**。V1 OpenCode 只认显式头和 UA `opencode/` |
| 等抓包才动任何代码 | **反对**。A（不变量）不依赖 OpenCode |
| system 附言无上限 | 硬顶 **1000** 字符 |
| `X-Agent-Client: generic` | **允许**，强制走通用层 |
| 引导文案语言 | 用户原文原样进 `task`/`context`；共用 system 维持英文 |

---

## 1. 问题与产品冻结

不同 coding AGENT 的 Anthropic Messages 差很多：system 是否塞工具说明书、图在顶层还是 `tool_result`、当前任务夹在哪一轮。全局「最后一条 user 纯文本 + 更早轮次截断」只覆盖「一句话 + 一张图」。

| 选择 | 值 |
| --- | --- |
| V1 适配清单 | **OpenCode** 一份预设；Cursor / Claude Code / Cline / Codex **不做** |
| 未匹中 | **一律 generic**，不猜 |
| 识别顺序 | **显式头覆盖 → User-Agent →（C 阶段才开的指纹）→ generic** |
| 误认 vs 漏认 | 误认代价更高。没把握走 generic。禁止把用户聊天里的 “OpenCode” 当特征 |
| OpenCode V1 匹配 | `X-Agent-Client: opencode`、UA token `opencode/`，或实包指纹（同一 user 列表里：image + `Called the Read tool with the following input:` + `Image read successfully`）。用户聊天提到 OpenCode 不算 |

---

## 2. 目标与非目标

**目标**

- 合成模型带图时，打 MiniMax 之前得到 `agent_id ∈ {opencode, generic, …}` 和 `match` 来源。
- `generic` 是完整范式，不是空壳。
- 预设只改两件事：**(a) 按图抽出 task/context；(b) 共用 IR system 后追加短附言**。
- 输出契约不变：合法 `<visual-evidence>`；禁止答题；禁止像素进 `glm-5.2`。
- 识别/抽取失败走 generic，**不** 400。译图失败仍 fail-closed。
- 新增 AGENT = 新预设 + 夹具，不改选号、不改 IR 白名单。

**非目标（V1）**

- 按 AGENT 换翻译模型或额度组。
- 把网关记忆、工作区路径、完整工具说明书送给 MiniMax。
- 自适应压图。
- 出站 MiniMax 伪装成 AGENT UA。
- 为识别改 OpenCode 源码。
- 同步路径做缓存命中剥图。
- 在无抓包时根据「工具名组合 / system 自报身份」做指纹。

---

## 3. 分层

```text
IR 契约（所有预设共用，vision-compose.md）
        │
        ▼
识别：headers → agent_id   （V1 不读 messages 指纹）
        │
        ├─ opencode：逐图抽取 + 短附言（仅头/UA 命中）
        └─ generic：逐图抽取 + 无产品名附言
        │
        ▼
对每张图：guide = extract_guide(snapshot, image_ref)
        │
        ▼
MiniMax：system = 契约 ∪ 附言；user = [image, guide]
digest = sha256(png ‖ agent_id ‖ prompt_rev ‖ guide)
```

同一请求只选一个预设。禁止 UA 与指纹加权融合。

---

## 4. 现网不变量（A，先于预设）

这些不是「预设功能」，是视觉配方已经写进规格、代码未兑现的洞。A 未合并不进入 B。

### 4.1 同步路径不得漏像素

调用关系：

```text
LiteLLM async 选号
  → async_get_available_deployment()
      → get_available_deployment()     # 同步；今日视觉开时 peel 直接 return
      → await run_pipeline()           # 真正译图
LiteLLM / SDK 同步选号
  → get_available_deployment()         # 无 pipeline → 图仍在 → 出发 glm-5.2
```

**禁止**：在 `get_available_deployment` 里写「视觉开 + 有图 ⇒ 无条件 FEATURE_UNSUPPORTED」。async 入口会先走进这个函数，生产路径会被误杀。

**规定**：用进程内 `contextvars.ContextVar`（建议名 `sq_vision_async_select`，默认 `False`）。

| 调用方 | Var | 合成模型 + 有图 + 视觉开 |
| --- | --- | --- |
| `async_get_available_deployment` 在调用 sync select **之前** set True，`finally` reset | True | peel 跳过，交给 pipeline |
| 其它对 `get_available_deployment` 的调用（含 MiniMax 子选号） | False | **立即** `FEATURE_UNSUPPORTED`，`details.vision=sync_path`；**禁止** stub peel |

子调用 messages 无图时 peel 本就是空操作。若子调用误带图，同步 fail-closed 是正确的。

V1 **不做**「sync 缓存命中则同步替换」。比 `pipeline.md` 旧句更严，有意为之。

`test_s5_peel_skipped_when_vision_compose_on` 必须改成：跳过 peel **仅当** async-select var 为 True；公共 sync 则 raise。

### 4.2 改写前快照 + translator 只收一份 guide

今日分叉：

- stage 用改写前的全局 `guide_text_from_messages(env.messages)` 做缓存键；
- `_MiniMaxTranslator.__call__(png)` 在 POST 时再抽一次，此时前面的图已变成 `<visual-evidence>`。

多图时第二张 MiniMax 会吃到第一张译文。

**规定**：

1. 进入视觉阶段后、**任何** image→text 改写之前，遍历全部 image block，得到 `list[(ImageRef, png, guide)]`。guide 只来自这份未改写快照。
2. `Translator = Callable[[bytes, str], Awaitable[str]]`，即 `(png, guide)`。
3. `_MiniMaxTranslator` **禁止**再调 `guide_text_from_messages`。
4. 注入的 fake translator 同样收 `(png, guide)`，测试可断言 guide。

### 4.3 缓存键

`schema_ver` 升到 **3**（键公式不兼容 v2 文件；旧文件自然 miss，不要读 v2 当命中）。

```text
digest = sha256(
  png
  || b"\0agent\0" || agent_id.encode("utf-8")
  || b"\0rev\0"   || str(prompt_rev).encode("ascii")
  || b"\0guide\0" || guide.strip().encode("utf-8")
)
文件：{schema_ver}_{digest}.txt
```

`prompt_rev` 是预设整数，改抽取规则或附言时 +1，不必每次都加 `schema_ver`。IR 白名单不兼容时仍加 `schema_ver`。

A 阶段若尚未有注册表：digest 仍走该公式，`agent_id="generic"`，`prompt_rev=1`。禁止再落地 `sha256(png||guide)`。

---

## 5. 预设接口

```python
@dataclass(frozen=True, slots=True)
class ImageRef:
    ordinal: int            # 文档序，0-based，含 tool_result 递归
    message_index: int      # messages 下标
    path: tuple[int, ...]   # 嵌套 content 列表下标，例如 (1,) 或 (2, 0)

class AgentPreset(Protocol):
    id: str                 # "generic" | "opencode"
    prompt_rev: int
    def match_header(self, headers: Mapping[str, Any]) -> bool: ...
    def match_messages(self, messages: list) -> bool: ...
    def extract_guide(self, messages: list, image_ref: ImageRef) -> str: ...
    def system_addendum(self) -> str: ...
```

| 规则 | 值 |
| --- | --- |
| `generic.match_header` / `match_messages` | 恒 false（只当 fallback） |
| OpenCode V1 `match_messages` | 恒 false |
| `extract_guide` | 返回已脱敏、已截断的 `task:` / `context:` 文本（可缺一段）；**不含** image 字节 |
| `system_addendum` | 可空。`strip` 后 **> 1000 字符则视为该预设异常**：本请求整单回退 generic（含缓存键用 generic），记 warning |
| 抽取抛错 | 同上，整单 generic；**不** 400 |

`MAX_TASK_CHARS=1500`，`MAX_CONTEXT_CHARS=2000`，沿用现网；密钥脱敏沿用 `memory_extract.redact`。

### 5.1 识别顺序

请求头来自已有 `collect_request_headers`（`proxy_server_request.headers` ∪ 顶层 `headers`，键小写）。

1. 读 `x-agent-client`（大小写不敏感）。值为已注册 id：
   - `generic` → 强制 generic（`match=header_force`），**不再**看 UA。
   - `opencode` → opencode（`match=header`）。
2. 未知值（如 `cursor`）→ 当没这个头，继续。
3. 否则按注册表（不含 generic）调用 `match_header`。V1 仅 OpenCode：UA 用 **token** 匹配，大小写不敏感，模式为 `opencode/`（斜杠必须有，避免 `opencode` 出现在普通词里）。命中 `match=ua`。
4. 否则若指纹开关打开（默认开；`VISION_AGENT_FINGERPRINTS=false` 可关）才调用 `match_messages`。命中需同时满足：同一条 user content 列表里有 image、`Called the Read tool with the following input:`、以及恰好为 `Image read successfully` 的 text。不递归进 `tool_result`。
5. 否则 generic，`match=fallback`。

`internal_call=true` 不进入识别、不译图（已有 pipeline 行为）。

出站 MiniMax `User-Agent` 保持网关默认，**不得**写成 `opencode/`。

### 5.2 显式头运营

头名：`X-Agent-Client`。V1 允许值：`opencode`、`generic`。

不要求官方 OpenCode 带这个头。带了就赢。探针、自建客户端、文档里的可选配置都可以用。建议写进本仓库 OpenCode 对接说明，作为比指纹稳的运营手段。

### 5.3 generic 抽取（完整 fallback）

对 `image_ref` 指向的那张图：

- **task**：从该 message **向前**（含本条）找最近一条「人类任务句」：`role=user`，纯文本为主，**不是** `tool_result` 堆出来的整段命令输出。取 `redact` 后 1500 字。找不到则 task 空。
- **context**：仅 (1) 与该图 **同一 content 列表**（含所在 `tool_result` 内层）里的短 text；(2) 再往前至多 2 条 user 纯文本。拼起来 `redact` 后取尾 2000 字。不要把整份 system、工具 schema 塞进去。
- 无任何文本：guide 为空，MiniMax user 文本回落到现有短指令（只译图、不答题）。

这比今日「全文当 context」更窄，作为通用范式：陌生客户端宁可少上下文，不要把 bash 日志当 task。

### 5.4 OpenCode 抽取（B，不依赖指纹）

在 generic 之上只固定这些结构性差异（不依赖「像 OpenCode 的句子」）：

- 图经常在 `tool_result.content[]`：`path` 会指向内层；context **优先**内层图注，不要把整个 tool 回执当 task。
- system 附言（≤1000，建议远短于上限）英文，要点：
  - 截图可能来自 TUI/终端/浏览器工具回执；
  - 忽略 spinner、模型名条、窗框 chrome；
  - 优先抄 traceback、命令、路径、测试失败摘要；
  - 不要扮演 OpenCode、不要输出 patch、不要调用工具。

抓包之后允许改抽取细节并 `prompt_rev += 1`；不得在无夹具时加 messages 指纹。

### 5.5 组装

```text
system = SHARED_IR_CONTRACT + ("\n" + addendum if addendum else "")
user   = [image_block, {"type":"text","text": _translate_user_text(guide)}]
```

`_translate_user_text` 保持现网：有 guide 则包一层「extract evidence, do not answer」；无 guide 则短 Translate 句。用户中文任务保持中文，不翻译成英文。

---

## 6. 失败、日志、指标

| 情况 | 行为 |
| --- | --- |
| 无法识别 | generic，译图继续 |
| 显式头未知 | 当没这个头 |
| 抽取抛错 / 附言超 1000 | generic 整单回退；不 400 |
| 公共 sync + 合成模型 + 有图 + 视觉开 | `FEATURE_UNSUPPORTED`，`vision=sync_path` |
| MiniMax / IR 门 / 超限 / 熔断 | 现有 fail-closed |
| `internal_call` | 不识别、不译图 |

日志一行：`enhance_vision agent=<id> match=<header|header_force|ua|fingerprint|fallback|extract_error> outbound_has_image=false`。

禁止：UA 全串、图片、完整 system、API Key、工作区完整路径（若需排障只打 `agent_id`）。

指标：现有 `enhance_vision_*` 外加 `enhance_vision_agent` counter，label `agent_id`、`match`。

---

## 7. 代码落点

保持 G0-B。视觉阶段读 `EnhanceEnvelope.headers` 与 `env.messages`。不新增 HTTP 入口，不改 Fill First。

| 模块 | 职责 |
| --- | --- |
| `pipeline.py` 或小模块 `vision_async_flag.py` | `ContextVar`；async 入口 set/reset |
| `composed_vision.py` | peel：async-select 则 defer；否则有图则 400 |
| `vision_compose.py` | 快照、逐图、`translator(png, guide)`、digest；调用 `resolve_preset` |
| `vision_cache.py` | `SCHEMA_VER = 3` |
| `vision_agents/types.py` | `ImageRef`、Protocol |
| `vision_agents/generic.py` | fallback 预设 |
| `vision_agents/opencode.py` | 头/UA 匹配 + 抽取 + 附言；`match_messages` return False |
| `vision_agents/detect.py` | 识别顺序 |
| `vision_agents/registry.py` | 显式有序列表，generic 不参与抢配 |

A 可以先在 `vision_compose.py` 内把 digest/translator/sync 修好，B 再把抽取迁到 `vision_agents/`。禁止 A、C 混在一个 PR 里。

---

## 8. 测试

**A（不变量）**

| 用例 | 期望 |
| --- | --- |
| `get_available_deployment` + 视觉开 + 合成模型 + 有图 | `FEATURE_UNSUPPORTED`，`vision=sync_path`，messages 仍有 image |
| `async_get_available_deployment` 同条件 + fake translator | 200 语义：无残留 image，有 `<visual-evidence>` |
| 子调用 `internal_call` 选 MiniMax（无图） | 不 400 |
| 两张图、不同同圈文本 | 两次 POST 的 `guide` 不同；第二次 **不含** 第一张 IR |
| 同一 PNG、不同 guide | 缓存不命中 |
| fake `translator(png, guide)` | 调用参数含 guide，不再是单参 |

**B（预设骨架）**

| 用例 | 期望 |
| --- | --- |
| 无头无 UA + 图 | `generic`；guide 含同圈文本 / 最近 user 句 |
| `X-Agent-Client: opencode` | 即使 messages 不像 OpenCode 也走 opencode 抽取与附言 |
| `X-Agent-Client: generic` | 即使 UA 是 `opencode/1.0` 也走 generic |
| `X-Agent-Client: cursor` | generic |
| UA `opencode/x.y` | opencode |
| 用户文本 “I use OpenCode”、无头无 UA | generic |
| 同一 PNG + 相同 guide，generic vs opencode（附言不同） | 两次 MiniMax（缓存隔离） |
| 附言 > 1000 | 回退 generic |
| 抽取器 raise | 回退 generic，不 400 |
| `internal_call` | 不识别 |
| `proxy_server_request.headers` 里的 `User-Agent` / `X-Agent-Client` 经 `async_get_available_deployment` 到达信封 | 命中对应预设 |
| IR 门 | 与现有 `vision_ir` 相同 |

**C（抓包后）**

脱敏夹具：`tests/fixtures/vision_agents/opencode/`（无密钥、无完整家目录路径）。至少一张顶层 image、一张 `tool_result` 内图（若抓包证明只有其中一种，夹具跟着事实走，删掉未验证假设）。指纹测试仅在开关打开且夹具证明 ≥2 条**结构性**强特征后添加。

---

## 9. 施工阶段

### A — 现网不变量（可立即编码）

1. `ContextVar` + 同步 fail-closed + 改写现有 peel 单测。
2. 改写前快照、逐图 guide、`translator(png, guide)`。
3. digest 含 `agent_id`+`prompt_rev`+guide；`SCHEMA_VER=3`。
4. 契约：sync 400；async 无残留 image。

### B — 预设骨架（不写指纹）

1. `ImageRef` + 注册表 + generic 逐图抽取。
2. OpenCode：仅头/UA；抽取 + 短附言。
3. §8 B 表测试；附言硬顶；header 经 strategy 的测试。

### C — 本网关实包后已开指纹

2026-08-27：真实 OpenCode 1.18.5 `--pure` 经 tap 转发到 `127.0.0.1:4000/v1/messages`（glm-5.2 因无 image 能力返回 400，请求体仍完整）。

1. 夹具 `live.json`（title 预请求）与 `live-2.json`（主请求）。
2. **UA 已校准**：`opencode/1.18.5 ai-sdk/provider-utils/… runtime/bun/…`，另有 `x-session-id` / `x-session-affinity`。无 `X-Agent-Client`。
3. 生产 Read 截图是 **同一条 user content 里的顶层 image**，前面两段 text 为 Read 包装语，**不是** `tool_result`。golden 里的 `read_screenshot` 仍只是测试桩。
4. `match_messages` 要求上述三条同时成立；用户句 “I use OpenCode”、仅 `tool_result` 内图、仅顶层配图均不命中。
5. 抽取 `prompt_rev=2`：task 丢掉 Read 包装与 title 句；路径按脱敏规则剥掉家目录。
6. `VISION_AGENT_FINGERPRINTS` **默认开**；设 `false` 可关。

---

## 10. 反模式

| 不要 | 原因 |
| --- | --- |
| 在 `get_available_deployment` 无条件「有图则 400」 | 会杀掉 async 生产路径 |
| 视觉开时 peel return，sync 不补 400 | 像素进 `glm-5.2` |
| POST 时从 `env.messages` 重抽 guide | 多图串 IR |
| 缓存键不含 `agent_id`/`prompt_rev` | 跨预设串译文 |
| 用户句子含 “OpenCode” 当指纹 | 误认 |
| 无夹具写死工具名组合 | 未验证假设进生产 |
| 识别失败 400 | 识别不是视觉能力失败 |
| 在 `async_pre_call_hook` 剥图/改 prompt | 探针 B FAIL |
| `S5_STUB_PEEL` 当译文 | 配方禁止 |
| 子调用复用父 `litellm_call_id` | 已冻结隔离 |
| 等 C 才修 A | 现网不变量继续破 |

---

## 11. 与现有规格

| 文档 | 关系 |
| --- | --- |
| `pipeline.md` §5 | 以本文 §4.1 为准：ContextVar；V1 不做 sync 缓存剥图 |
| `vision-compose.md` | IR/上限/熔断不变。缓存公式改为 §4.3；逐图 guide；translator 双参；MiniMax system = 契约 ∪ 附言 |
| `design-proposal.md` | Q1–Q6 不变 |

头/UA 命中，或实包 Read-tool 指纹命中时，用 OpenCode 附言 + 去包装抽取。用户聊天提到 OpenCode 仍走 generic。

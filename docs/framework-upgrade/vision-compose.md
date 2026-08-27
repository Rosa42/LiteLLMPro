# 视觉组合配方 glm-5.2-vision — 规格

| 项 | 值 |
| --- | --- |
| 文档类型 | 规格（spec） |
| 状态 | **可施工** |
| 日期 | 2026-08-25 |
| 实现落点 | `vision_compose.py` / `vision_ir.py` / `vision_cache.py` / `internal_call.py` |
| 公开协议 | **仅** `anthropic_messages`（Q6） |
| 依据 | `design-proposal.md` §7；计划冻结项 |

V1 只做 coding agent 截图。风景、人像、手写、过糊图必须拒绝翻译（fail-closed），禁止占位 caption。

---

## 1. 配方

| 项 | V1 值 |
| --- | --- |
| 对外逻辑模型 | `glm-5.2-vision` |
| `public_protocols` | `[anthropic_messages]` |
| `advertised_features` | `[text, streaming, tools, reasoning, image]` |
| 执行模型 | `glm-5.2`（现有 Volc Messages 部署与额度组） |
| 翻译模型 | `MiniMax-M3`（`quota_group_id=minimax-official`） |
| 触发 | Messages 含 `image` / `image_url` block（含历史与 `tool_result` 内嵌） |
| 无图 | 跳过 MiniMax；effective 仍是该合成部署上的 GLM 账号 |

`plans.yaml`：

```yaml
# 与 glm-5.2 同一 Volc plan 的 models 列表增加：
#   - model: glm-5.2-vision
#     upstream_protocol: anthropic_messages
#     supported_features: [text, streaming, tools, reasoning]  # 无 image

logical_models:
  glm-5.2-vision:
    public_protocols: [anthropic_messages]
    advertised_features: [text, streaming, tools, reasoning, image]
    compose:
      execute_model: glm-5.2
      translate_model: MiniMax-M3
```

规则：

- `compose` 非空 ⇒ `defer_image_gate=true`。保留 env `S5_COMPOSED_MODELS` 作探针覆盖。
- 合成部署 **不得** 在 `supported_features` 声明 `image`（执行模型吃不到像素）。
- 纯 `glm-5.2` **不得** 声明 `image`。
- `execute_model` 与 `translate_model` 必须是已存在的逻辑模型，且所属 `quota_group_id` **不同**。`apply` 时 `ConfigValidationError`。
- 不在 select 时把 `model` 字符串 remap 成 `glm-5.2`；选号对象就是 `glm-5.2-vision` 的部署（与 glm-5.2 同额度组）。
- `VISION_COMPOSE_ENABLED=false` 时 discovery **省略** 一切带 `compose` 的模型。生产广告只在翻译器可跑之后打开。

---

## 2. 每轮全量替换（Q3）

每一轮必须访问 `messages` 里每一个 image 类 block（含 `tool_result` 递归）。不得按「上次扫到第几条」跳过旧消息。

改写开始前对 messages 做只读遍历，为每张图算出 `guide`（`extract_guide(messages, image_ref)`，见 `vision-agent-prompt-presets.md`）。禁止在替换 image 之后再从 `messages` 抽 guide。

对每一张图：

1. 取出原始字节（base64 decode；失败则 fail-closed）。
2. 用该图已算出的 `guide`、当前 `agent_id`、`prompt_rev` 做 digest（§4）。
3. 查缓存 `vision:{schema_ver}:{digest}`。命中则跳过 MiniMax。
4. 未命中则调翻译子调用 `translator(png, guide)` → 质量门 → 写入缓存。
5. 将该 block **类型改为 `text`**，`text` 为译文（可外包一层说明，见 §6）。不得残留 `image` / `image_url`。

缓存只跳过视觉模型调用，不跳过扫描。

---

## 3. 上限（超限 fail-closed）

| 上限 | V1 | 超限 |
| --- | --- | --- |
| 单请求图片张数 | 6 | `FEATURE_UNSUPPORTED`，明确错误，不含 base64 |
| 单图字节 | 5 MiB | 同上 |
| 请求图片总字节 | 12 MiB | 同上 |
| 单张译文 | 约 4k token（实现用 `len(text)//4` 近似，阈值 4000） | 先保 `<pre>` 文本，裁其它；仍超则 fail-closed |

错误 `details` 可含 `vision_limit: images|bytes|tokens`，禁止塞图片数据。

---

## 4. 缓存

- 目录：`GATEWAY_VISION_CACHE_DIR`，默认 `local-llm-router/data/vision-cache/`（已 gitignore 的 `data/`）。
- 文件名：`{schema_ver}_{sha256}.txt`（或等价，一文件一篇译文）。
- `schema_ver`：整数常量。IR 白名单不兼容时 +1。当前 **3**（digest 含 `agent_id` + `prompt_rev` + 逐图 guide；相对 v2 的 `sha256(png||guide)` 不兼容）。
- digest 公式见 `vision-agent-prompt-presets.md` §4.3。预设文案变更靠 `prompt_rev`，不必每次加 `schema_ver`。
- 不写入 Redis `sq:*`。
- put/get 必须同一 `schema_ver` 才命中。

---

## 5. 中间表示与质量门

根元素必须是 `<visual-evidence>`。禁止 `<html>`、`<script>`、外链 CSS、`javascript:`。

按粗分类选载体（视觉模型输出，门只校验结构）：

| 分类 | 载体 |
| --- | --- |
| 报错 / 终端 / 日志 | `<pre>` |
| 代码编辑器 | `<pre><code>` |
| Web / IDE / 设置页 | 语义 HTML 草图 |
| 表格 | `<table>` |
| 其它 coding 截图 | 默认语义草图 |
| 明确不在 V1（风景、人像、手写、过糊） | 模型应输出拒绝；门若收到空壳也拒绝 |

允许的标签集合（实现白名单）：

`visual-evidence`, `pre`, `code`, `table`, `thead`, `tbody`, `tr`, `th`, `td`, `p`, `ul`, `ol`, `li`, `span`, `div`, `strong`, `em`, `br`

允许属性：`data-uncertain`、`data-file`（文件名猜测）。禁止 `onclick`、`style` 中的 `url(`、`href` 以 `javascript:` 开头。

MiniMax 仍可能发明 chrome 标签（例如 `<status-bar>`）。质量门把**非危险**的未知标签改写成 `div`、丢掉未知属性后再校验；`html` / `script` / `iframe` / `javascript:` 仍 fail-closed。

拒绝条件：

- 不能解析为单一根 `visual-evidence`
- 出现危险标签，或改写后仍有白名单外标签
- 译文为空或只有空壳（根下无可见文本）
- 可见文本中带 `data-uncertain` 的字符占比 ≥ 0.8
- 含 `<script>` 或完整 HTML 文档

质量门失败 = fail-closed，**禁止**改用「图片已省略」或 S5 stub。

---

## 6. 注入包装

替换后的 text block 建议前缀（执行模型须知道这不是仓库源码）：

```text
<visual-evidence>
...
</visual-evidence>
```

若质量门已保证根元素，不要再套第二层根。可在同一 text 前加一行纯文本：

`[gateway visual translation — not repository source; do not write this as a new file]`

该行不计入 IR 解析（解析只取 `<visual-evidence>...</visual-evidence>` 子串）。

---

## 7. MiniMax system prompt（冻结初稿）

子调用 `system`：

```text
You translate coding-agent screenshots into structured working memory for a text-only model.
The user message includes the screenshot plus that model's current task and recent conversation.
Use the task and context to decide what to transcribe faithfully and what to keep brief.
Output ONLY one XML fragment. Root element must be <visual-evidence>.
Do not output <html>, <script>, markdown fences, or a solution to the user's problem.
Do not answer the task. Extract visual evidence the text-only model will need.
Choose a carrier:
- terminal / traceback / logs → <pre> with exact visible text
- editor / code → <pre><code> and data-file if a path is visible
- IDE / browser / settings UI → a sketch using ONLY the tags below (div/p/span/ul)
- table → <table>
Allowed tags ONLY: visual-evidence, pre, code, table, thead, tbody, tr, th, td, p, ul, ol, li, span, div, strong, em, br.
Map chrome (status bar, tab bar, address bar, buttons) to <div>, <p>, or <span>.
Never invent tag names such as status-bar, header, nav, or button.
If the image is a landscape, portrait, handwriting, or too blurry to read, output:
<visual-evidence data-reject="out-of-scope"></visual-evidence>
Unreadable glyphs: wrap in <span data-uncertain="1">...</span>. Do not guess.
```

`data-reject` 视为质量门失败（空壳 / 拒绝）。

`system` = 上表共用契约 + 可选 AGENT 附言（见 `vision-agent-prompt-presets.md`；附言硬顶 1000 字符）。

用户消息：单张图（Anthropic image block）+ **该图的** task/context 引导（不含 image 字节、不含网关记忆、不含其它图已写成的 IR）。任务用来决定保真抄哪些可见内容；禁止让 MiniMax 直接回答用户问题。无用户文本时才用短指令请它产出 `<visual-evidence>`。多图则每张一次子调用，guide 在改写前按 `image_ref` 分别抽出。

`translator` 签名为 `(png, guide)`。禁止在 HTTP POST 时从已改写的 `env.messages` 重抽 guide。

缓存 digest 为 `sha256(png ‖ agent_id ‖ prompt_rev ‖ guide)`（`schema_ver` 3）。同一张图、不同用户问题或不同预设不得命中旧译文。


---

## 8. 子调用契约（§7.8）

| 项 | V1 |
| --- | --- |
| 标记 | metadata / `litellm_metadata`：`internal_call=true`，`internal_kind=vision` |
| 递归 | 已是 internal 的请求禁止再跑 pipeline |
| 深度 | 1 |
| 子 id | `{parent}#vision:{hash8}`，`hash8` 为图片 sha256 前 8 位 |
| Context | 独立 `RequestRoutingContext`；禁止复用父 ctx |
| HTTP | `httpx.AsyncClient` POST 到所选 MiniMax deployment 的 Anthropic Messages URL（与探针 A 一致：基址无尾 `/v1` 则自行拼 `/v1/messages`） |
| 选号 | 对 `MiniMax-M3` 调现有 `get_available_deployment`（子 request_id）。子调用不设置 async-select ContextVar；无图时 peel 为空操作 |
| quota | 子 `quota_group_id` ≠ 父执行组，否则 `CONFIGURATION_INVALID` |
| 失败 | 走现有分类器；M3 耗尽只写 M3 组 |

不要用 `litellm.acompletion` 从策略内部再绕本机代理。

测试可注入 `translator(png, guide)` callable，不打网。

---

## 9. 模块熔断

进程内计数：连续翻译失败（超时、上游 5xx、质量门失败、超限以外的翻译错误）达到 **3** 次后，打开熔断 **60 秒**。窗口内合成模型带图直接 `FEATURE_UNSUPPORTED`（消息：vision composer temporarily unavailable），**禁止** stub 译文。成功一次则清零。超限（张数/字节）不计入熔断（那是请求错误）。

---

## 10. 错误映射

| 情况 | reason | HTTP |
| --- | --- | --- |
| 公共 sync 选号 + 合成模型 + 有图 + 视觉开 | `FEATURE_UNSUPPORTED`（`details.vision=sync_path`） | 400 |
| 质量门失败 / MiniMax 拒绝 / 超时 | `FEATURE_UNSUPPORTED` | 400 |
| 父子额度组相同 | `CONFIGURATION_INVALID` | 400 |
| 纯 `glm-5.2` 带图 | 现有 IMAGE 门控（不变） | 400 |

---

## 11. 评估集

路径：`docs/framework-upgrade/fixtures/vision-eval/`

`manifest.json` 每项：

```json
{
  "id": "term-001",
  "expect_carrier": "pre",
  "notes": "Python traceback in terminal"
}
```

`expect_carrier`：`pre` | `code` | `html` | `table` | `reject`。

CI：至少 3 条**合成夹具**（文本生成的最小 PNG 或固定 fixture 文件），跑 IR 门 + 可选 fake translator。  
Live：操作者准备 ≥20 张真实 coding 截图于 `raw/`（不提交 PII）。prompt 迭代以该集为准。

---

## 12. 验收

- [ ] 出发 HTTP 无 image（含 `tool_result`）
- [ ] 同一 sha256 第二轮不调 MiniMax
- [ ] 纯 `glm-5.2` 带图仍 400
- [ ] `VISION_COMPOSE_ENABLED` 时忽略 `S5_STUB_PEEL`
- [ ] 质量门失败无占位 caption
- [ ] M3 耗尽不把 Volc 组标 `SHARED_QUOTA_EXHAUSTED`

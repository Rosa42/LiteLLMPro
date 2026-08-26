# 网关层记忆 — 规格

| 项 | 值 |
| --- | --- |
| 文档类型 | 规格（spec） |
| 状态 | **可施工** |
| 日期 | 2026-08-25 |
| 实现落点 | `memory_workspace.py` / `memory_store.py` / `memory_retrieve.py` / `memory_extract.py` |
| 介质 | **JSONL**（计划冻结；不用 SQLite、不用向量库） |
| 依据 | `design-proposal.md` §8；Q4 / Q5 |

记忆属于网关与工作区，不属于某一个 app。`surface` 只做审计字段，不做检索主键。未知工作区：**不检索、不写入**，不用全局库兜底。

V1 分两刀：先只读手写 JSONL（F4），跨 app 验证后再开自动抽取（F5）。

---

## 1. Flags

见 `pipeline.md`。检索：`GATEWAY_ENHANCE_ENABLED` ∧ `GATEWAY_MEMORY_ENABLED`。  
抽取：还要 `GATEWAY_MEMORY_EXTRACT_ENABLED`。检索关则抽取关。

失败策略：**fail-open**。超时、缺文件、解析坏行、抽取失败 → skip，不挡执行模型。

---

## 2. Workspace 规范化

### 2.1 可信来源（唯一）

1. HTTP 头 `X-Workspace-Root`（大小写不敏感查找）
2. 请求 metadata `workspace_root`

二者都有则以头为准。值必须是非空字符串。

### 2.2 规范化算法

对候选字符串：

1. strip；拒绝空、含 NUL、含 `://`（不把 URL 当路径）。
2. 展开 `~`（`Path.expanduser`）。
3. 若相对路径：拒绝（不得用 cwd 猜）。
4. `Path.resolve()`（解析符号链接到真实路径）。
5. 解析后仍含 `..` 段或不是绝对路径 → None。
6. Windows 上比较与存储用 resolve 后的字符串；JSONL 文件名用 sha256。

无法规范化 → 视为未知（None）。

### 2.3 弱推断（仅当可信来源缺失）

从 `messages` 里收集看起来像**绝对路径**的字符串（Windows `X:\...` 或 POSIX `/...`，长度 ≥ 3）。来源：text、`tool_use`/`tool_result` 的 JSON 字符串。

- 少于 **2** 条绝对路径 → 失败，不推断。
- 将每条 resolve（失败的丢掉）。
- 取剩余路径的公共父目录；若公共根是盘符根（`C:\`）或 POSIX `/` → 失败。
- 公共根再走 §2.2；失败则 None。

推断结果不得比可信头更优先。

### 2.4 未知

`workspace is None`：检索不读盘、不注入；抽取不入队。

---

## 3. JSONL 存储

- 目录：`GATEWAY_MEMORY_DIR`，默认 `local-llm-router/data/gateway-memory/`。
- 文件：`{sha256(normalized_workspace.encode("utf-8")).hexdigest()[:32]}.jsonl`
- 缺文件 = 空库（不是错误）。
- **禁止**写入 Redis `sq:*`。

每行一个 JSON 对象：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `id` | string | 是 | 稳定 id（手写可用 uuid） |
| `ts` | string | 是 | ISO-8601 UTC |
| `kind` | string | 是 | `note` / `decision` / `fact`；未知 kind 当 `note` |
| `text` | string | 是 | 记忆正文，已脱敏 |
| `surface` | string | 否 | `opencode` / `cursor` / `unknown`；审计 |
| `source` | string | 否 | `hand` / `extract` |

坏行（JSON 失败、缺 `text`）跳过并打日志，不 fail 整次检索。

手写示例：

```json
{"id":"n1","ts":"2026-08-25T00:00:00Z","kind":"note","text":"This repo pins LiteLLM v1.90.5","surface":"hand","source":"hand"}
```

---

## 4. 检索与注入

查询文本 = 本轮用户文本（所有 user text block 拼接）+ `envelope.visual_evidence` 拼接 + workspace 路径字符串。

算法（V1 关键词，无向量）：

1. 将查询与每条 `text` 做大小写折叠。
2. tokenize：按非字母数字切分，丢掉长度 < 2 的 token。
3. 得分 = 查询 token 集合与记忆 token 集合的交集大小。
4. 得分为 0 的丢掉。
5. 按得分降序；累加 `len(text)//4` 近似 token，硬顶 **2000**。超出的丢弃（fail-open）。
6. 问候/空 user 文本且无视觉译文：可跳过检索（规则：user 可见文本 strip 后长度 < 8 且无 visual_evidence）。

超时：**300ms** 墙钟。超时 skip，记 `enhance_memory_skip`。

注入：

- 位置：`messages` 中**第一条 `role=user` 的 content 列表开头**插入一个 `type=text` block。若 content 是 string，改成 list：`[memory_block, {type:text, text: original}]`。
- **不得**改 `role=system`，不得把记忆升为指令。
- 包装：

```text
<gateway_memory>
...concatenated notes...
</gateway_memory>
```

内部每条一行或用 `\n---\n` 分隔。这是数据不是指令。

跨工作区读文件视为漏洞：路径必须是 §3 由规范化 workspace 算出的那一个文件。

---

## 5. 写入路径（F5，F4 不要实现抽取 HTTP）

### 5.1 入队

`ManagedStream.on_stream_complete` 与非流 `async_log_success_event` **只允许** `queue.put_nowait`。禁止在这些回调里同步/阻塞调用上游模型。

- 进程内 `asyncio.Queue` 最大深度 **32**。满则丢弃任务并打日志（fail-open）。
- 进程退出放弃未完成任务，不持久化抽取作业。
- 无 workspace 的成功请求不入队。
- `GATEWAY_MEMORY_EXTRACT_ENABLED` 为 false 不入队。

### 5.2 抽取子调用

| 项 | V1 |
| --- | --- |
| `internal_kind` | `memory-extract` |
| 子 id | `{parent}#memory-extract:{hash8}` |
| 模型 | 环境变量 `GATEWAY_MEMORY_EXTRACT_MODEL`，默认廉价文本逻辑名（操作者配置，例如现有小模型）；必须与父执行额度组不同 |
| quota | 与父 `parent_quota_group_id` 互斥，否则不写库 |
| 失败 | 不写库 |

用户明确「记住……」可直写 `text`（仍脱敏），不必抽模型；V1 可用规则：user 文本匹配 `(记住|please remember|remember that)\s+(.+)` 时把捕获组当 note。

### 5.3 脱敏（入库前）

丢弃或打码：

- `sk-` 后跟 ≥10 字母数字
- `Bearer ` 后非空白
- `ark-` 后 ≥8 字母数字
- `api_key` / `apikey` 赋值形态

整段被打码后若 `text` strip 为空则不写。

---

## 6. 与视觉的关系

顺序：vision 阶段 → memory_retrieve。记忆查询应包含已通过质量门的译文原文。关掉视觉时记忆仍可注入；关掉记忆时视觉仍可翻译。

---

## 7. 验收

**F4：**

- [ ] 未知 workspace 不读盘、messages 无 `<gateway_memory>`
- [ ] 同一规范化路径的两个 retrieve 读同一 JSONL
- [ ] 关键词命中 / 不命中
- [ ] 超时 skip
- [ ] flag 关不注入
- [ ] 注入块不是 system

**F5：**

- [ ] complete 回调不直接 await 抽取 HTTP（测试可用 spy：回调返回时抽取未开始或仅 enqueue）
- [ ] 队列满丢弃
- [ ] 脱敏后无 `sk-` 明文
- [ ] 抽取失败不写库

# AGENT 强制约束

本仓库实现基于 LiteLLM 的共享 Coding Plan 智能路由。执行任何变更前先读本文件与 `docs/` 下方案文档。

## 版本与上游

1. 初始 LiteLLM 固定为 **v1.90.5**（正式稳定版）。
2. 禁止使用 `latest` / `main` / `nightly` / `rc` / `dev` 作为生产默认。
3. **禁止**向 `upstream/litellm` submodule 提交业务修改。
4. 业务逻辑只允许在 `plugins/shared_quota_router/`（运行时 import 名：`shared_quota_router`）。
5. 优先 Custom Routing Strategy + CustomLogger；必要时仅允许**最小注册补丁**（补丁内无额度业务逻辑）。

## 路由与额度

6. **模型组 ≠ 额度组**：同名模型跨套餐聚合；熔断按 `quota_group_id`（账号级）。
7. 同一 QuotaGroup 任一模型额度耗尽 → **停用该账号全部模型**。
8. **禁止**把所有 429 当作套餐耗尽；仅高置信度可标记 `SHARED_QUOTA_EXHAUSTED`。
9. Redis 不可用 → **fail-closed**（不得静默视为全部 AVAILABLE / 盲选）。
10. **禁止默认跨模型降级**（无用户明确允许时不得配置 Kimi→GLM fallback）。

## 流式与重试

11. 流式 **首字节已发送后** 禁止透明切换上游、禁止拼接另一模型输出。
12. 同请求：同一 quota_group 最多尝试 1 次；不同 group 最多 3 次。
13. CONTENT_POLICY / BAD_REQUEST 等客户端错误：**不**跨账号重试。

## 范围（第一阶段）

14. **被动额度 only**：上游失败 → 分类 → 熔断 → 切换 → 探测恢复。
15. **禁止**实现：`quota_collectors`、Cookie 登录、未公开余额页、预测剩余额度（见总设计 §22，二阶段另议）。

## 安全与流程

16. 禁止写入或提交真实 Secret；`.env` 必须 ignore。
17. 日志禁止输出 Authorization Header、完整 Prompt、API Key。
18. 默认只监听 `127.0.0.1`；Redis 启用密码。
19. Master Key 至少 32 字节随机值。
20. **禁止**自动 `git push`，除非用户显式授权。
21. 不得跳过测试；不得关闭类型检查来“通过”构建。
22. 不得吞掉未知错误。

## 阶段输出

每阶段结束提交：变更文件清单、实现说明、执行命令、测试结果、未解决风险、下一阶段计划。

## 参考

- 总设计：`../升级版的开发设计方案.md`
- 分阶段方案：`../docs/分阶段开发方案.md`
- Task 拆解：`../docs/tasks/阶段1-9-任务拆解.md`
- 框架升级（流水线 / 视觉合成 / 记忆）：`../docs/framework-upgrade/`
- **增强层维护（切视觉槽位 / flag / 回滚）：** `../docs/framework-upgrade/maintenance.md`

## 请求增强（视觉配方）

23. 视觉门面 CRUD **只在宿主机**（`compose-vision-*` / `cli_config`）。禁止容器内 PUT 或手改 volume 里的 YAML 当正式流程。
24. 子选号只走进程内 ContextVar `sq_trusted_internal`。禁止用客户端 metadata `internal_call` 跳过 public opt-in 或增强流水线。
25. 译图失败 400，**禁止**自动换执行模型 / pipeline fallback。V1 **禁止** rethink。

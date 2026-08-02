# 回滚

1. `sh scripts/rollback.sh v1.90.5`（或上一稳定版）  
2. 恢复对应 `config/litellm.yaml` / `.env` 变量名  
3. 重建 compose  
4. smoke-test  

DB：仅代码问题时可保留已向前兼容的 schema；迁移失败再用备份恢复。禁止猜测编辑 `_prisma_migrations`。

# 阶段 8–15 交付报告

日期：2026-07-18  
版本：`0.1.1`

## 完成范围

| 阶段 | 内容 | 状态 |
|------|------|------|
| 8 | callbacks、熔断矩阵、first_byte、metrics、C1 | ✅ |
| 9 | recovery_worker、probe 退避、Dockerfile.worker | ✅ |
| 10 | mock_provider HTTP 场景 | ✅ |
| 11 | C0+C1 契约 + 单测扩展 | ✅ |
| 12 | 场景 A–F 集成测试 | ✅ |
| 13 | 流式 first_byte 禁切换（单测 + 场景 F） | ✅ |
| 14 | upgrade/rollback/backup/sync/smoke 脚本 | ✅ |
| 15 | architecture/operations/upgrades/rollback/provider-errors/USAGE | ✅ |

## 测试结果

```text
pytest -q
49 passed
```

覆盖：unit / contract(C0+C1) / integration(A–F) / e2e(mock HTTP)。

## 使用入口

- **上手**：`USAGE.md`
- **运维**：`docs/operations.md`

## 已知限制

1. Docker 运行态需本机 Docker Desktop 可用时再 `compose up`  
2. 真实供应商错误文案需用线上样本校准分类器  
3. 探针需正确 `api_base` + Key 环境变量  

## 验收对照（§23 精简）

- [x] 客户端逻辑模型 kimi-k3 / glm-5.2（配置）  
- [x] 三套餐 deployment 可加载  
- [x] 同名模型跨套餐选路  
- [x] 共享 QuotaGroup 熔断  
- [x] 短期限流不整组熔断  
- [x] 流式首字节后不切换  
- [x] 自动探测恢复  
- [x] 契约 C0/C1  
- [x] 版本钉死 v1.90.5  
- [x] Key 不进 Git  

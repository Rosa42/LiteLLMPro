# 供应商错误分类

| 类型 | 行为 |
|------|------|
| SHARED_QUOTA_EXHAUSTED（高置信） | 整组 EXHAUSTED，切其他套餐 |
| SHORT_RATE_LIMIT | 仅 deployment 短期 cooldown |
| AUTH_INVALID / ACCOUNT_DISABLED | 整组 DISABLED + 告警 |
| PROVIDER_OUTAGE | Provider 短期 COOLDOWN |
| CONTENT_POLICY / BAD_REQUEST | 不跨账号重试 |
| 流式 first_byte 已发送 | 禁止跨 deployment 切换 |

分类器：`plugins/shared_quota_router/classifiers/`。  
用真实 429 响应体扩充 fixtures 可提高准确率。

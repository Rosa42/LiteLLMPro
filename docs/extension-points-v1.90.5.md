# LiteLLM v1.90.5 扩展点调研（阶段 7 / C0）

## CustomRoutingStrategyBase

- 位置：`litellm.types.router.CustomRoutingStrategyBase`（亦从 `litellm.router` 再导出）
- 方法：
  - `async def async_get_available_deployment(model, messages=None, input=None, specific_deployment=False, request_kwargs=None)`
  - `def get_available_deployment(...)`（同步）
- 返回：`router.model_list` 中的一个 **dict** 元素

## 挂载方式

```python
router.set_custom_routing_strategy(strategy_instance)
```

实现上对 Router 实例 `setattr` 替换 `get_available_deployment` / `async_get_available_deployment`。

**无需**改 LiteLLM 源码即可在 SDK/测试中挂载。

## Proxy 挂载（无业务 patch）

`LITELLM_WORKER_STARTUP_HOOKS` 在 `proxy_startup_event` **早期**执行（在 `load_config` 之前）。  
因此使用 **延迟任务** 等待 `proxy_server.llm_router` 就绪后再 `register()`：

```text
LITELLM_WORKER_STARTUP_HOOKS=shared_quota_router.bootstrap:register_proxy_startup
```

格式：`module.path:function_name`。

## 契约运行方式

```bash
# 方案 A：安装与 pin 对齐的 litellm
pip install "litellm==1.90.5"

# 方案 B：PYTHONPATH 含 upstream（需可 import litellm 包）
# 本仓库 submodule 布局为 upstream/litellm/litellm/
set PYTHONPATH=plugins;upstream/litellm

pytest tests/contract/test_c0_routing.py -q
```

## 决策

| 项 | 选择 |
|----|------|
| 是否需要业务 patch | **否**（注册走官方 API + startup hook） |
| 唯一注册入口 | `shared_quota_router.bootstrap.register` |
| M0 routing_strategy | 可保留 simple-shuffle；注册自定义策略后方法被替换 |

## Model capability discovery (M1-05)

Stock `GET /v1/models` (v1.90.5) does **not** surface custom `model_info.public_protocols`.

Project-owned endpoints (mounted in `register_proxy_startup` via `discovery_routes.mount_discovery_routes`):

- `GET /v1/router/model-capabilities`
- `GET /shared-quota/v1/model-capabilities`

See `docs/model-capability-discovery.md`.

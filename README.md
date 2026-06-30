# Model Fallback Plugin

模型降级容灾插件。当 QwenPaw 主模型调用失败时，自动切换到用户配置的备用模型链。

## 功能

- 主模型失败时自动降级到备用模型
- 支持多级降级（主 → 备1 → 备2 → ...）
- 每个模型可独立配置重试次数
- 区分瞬时错误（触发降级）和永久错误（如 401 认证失败，不降级）
- 提供 HTTP API 动态管理降级配置

## 安装

1. 将 `model-fallback/` 文件夹放到 QwenPaw 插件目录：
   `~/.qwenpaw/plugins/model-fallback/`

2. 重启 QwenPaw：`qwenpaw restart`

3. 通过 HTTP API 配置降级链：

```bash
# 查询当前配置
curl http://localhost:8000/api/plugin/model-fallback/config

# 配置降级链
curl -X POST http://localhost:8000/api/plugin/model-fallback/config \
  -H "Content-Type: application/json" \
  -d '{
    "enabled": true,
    "max_retries_per_model": 2,
    "fallback_chain": [
      {"provider_id": "deepseek_official", "model": "deepseek-chat"},
      {"provider_id": "anthropic", "model": "claude-sonnet-4-20250514"}
    ]
  }'

# 查看所有可用 Provider（用于选择合适的 provider_id）
curl http://localhost:8000/api/plugin/model-fallback/providers
```

4. 修改配置后重启 QwenPaw 生效。

## 工作原理

```
用户发消息 → Agent.create_model_and_formatter()
                    ↓
              被 FallbackChatModel 包装
                    ↓
              ┌─ 主模型（当前 active model）
              │   └── 重试 N 次后仍失败
              ↓
              ├─ 备用模型 1（用户配置的第一个 fallback）
              │   └── 重试 N 次后仍失败
              ↓
              ├─ 备用模型 2
              │   └── ...
              ↓
              └─ 全部失败 → 报错
```

## 配置说明

| 字段 | 类型 | 说明 |
|------|------|------|
| `enabled` | bool | 是否启用降级 |
| `max_retries_per_model` | int | 每个模型的重试次数（默认 2） |
| `fallback_chain` | array | 备用模型列表，按顺序尝试 |
| `retryable_status_codes` | array | 触发降级的 HTTP 状态码 |
| `exclude_status_codes` | array | 不降级的 HTTP 状态码（如 401） |

## 注意事项

- 插件通过 monkey-patch `model_factory.create_model_and_formatter` 实现
- 如果 QwenPaw 核心代码大幅变更，可能需要更新插件
- 降级时会在日志中打印 `[model-fallback]` 前缀的消息

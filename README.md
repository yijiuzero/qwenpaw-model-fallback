# Model Fallback Plugin / 模型降级插件

**English** | [中文](#zh-cn)

Auto-failover plugin for OpenClaw. When the primary model fails, seamlessly switch to backup models. Built-in circuit breaker prevents repeatedly hammering dead endpoints.

---

## <span id="zh-cn">中文说明</span>

模型降级容灾插件。当主模型调用失败时，自动切换到备用模型链，内置熔断器保护。

---

## Features / 功能

- 🇬🇧 Auto-fallback to backup models on primary failure
- 🇨🇳 主模型失败时自动降级到备用模型
- 🔄 Multi-level cascade (primary → backup 1 → backup 2 → ...) / 支持多级降级
- 🔁 Per-model retry config / 每个模型可独立配置重试次数
- 🔥 Circuit breaker (skip dead endpoints, auto-recover) / 熔断器保护
- 🖥️ Console UI configuration / 可视化配置
- 💬 Slash command `/fallback` / 对话命令
- 🌐 HTTP API for dynamic management

---

## Installation / 安装

```bash
# Clone the plugin to OpenClaw plugins directory / 克隆到插件目录
git clone https://github.com/yijiuzero/qwenpaw-model-fallback.git \
  ~/.openclaw/plugins/model-fallback

# Or download manually / 或手动下载放到:
# ~/.openclaw/plugins/model-fallback/

# Restart OpenClaw / 重启 OpenClaw
```

---

## Configuration / 配置

### Console UI / 控制台界面

Go to **Console → Tool Settings → Model Fallback** and fill in:

1. **① Base URL** — API endpoint, e.g. `https://api.deepseek.com` / API 地址
2. **① API Key** — Secret key / API 密钥
3. **① Model** — Model name, e.g. `deepseek-chat` / 模型名称

### Slash Command / 对话命令

```
/fallback                      Show status / 查看配置
/fallback on|off               Enable/disable / 启用/禁用
/fallback add <p>/<m>          Add backup / 添加备用模型
/fallback remove <index>       Remove by index / 移除
/fallback clear                Clear all / 清空
/fallback retries <n>          Set retries / 设置重试次数
/fallback cb                   Circuit breaker config / 熔断器配置
```

---

## Architecture / 工作原理

```
User message / 用户发消息
        ↓
   Agent.model() called
        ↓
   FallbackChatModel wraps the call
        ↓
   ┌─ Primary model / 主模型
   │   └── Retry N times / 重试 N 次
   ↓
   ├─ Backup model 1 / 备用模型 1
   │   └── Retry N times / 重试 N 次
   ↓
   ├─ Backup model 2 / 备用模型 2
   ↓
   └─ All failed → Error / 全部失败 → 报错
```

### Circuit Breaker / 熔断器

```
CLOSED ──(consecutive failures ≥ threshold)──► OPEN
  OPEN ──(timeout seconds later)──────────────► HALF_OPEN
  HALF_OPEN ──(successes ≥ threshold)────────► CLOSED
  HALF_OPEN ──(failure)──────────────────────► OPEN
```

---

## Error Handling / 错误处理

| Status Code | Behavior / 行为 |
|-------------|-----------------|
| 401, 403    | ❌ Fatal, no cascade / 致命错误，不降级 |
| 429, 5xx    | ✅ Transient, retry → cascade / 临时错误，重试后降级 |
| ConnectionError | ✅ Cascade to backup / 触发降级 |
| Timeout     | ✅ Cascade to backup / 触发降级 |

---

## License / 许可证

MIT

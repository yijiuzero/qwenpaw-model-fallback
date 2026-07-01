# Model Fallback - ClawHub 发布指南 / Publishing Guide

## 📋 发布前检查 / Pre-publish Checklist

确保 `openclaw` 分支有以下文件：

```
model-fallback/
├── openclaw.plugin.json    ✅ 已适配
├── backend/
│   ├── main.py            ✅ 已双语
│   └── __init__.py
├── fallback_model.py       ✅ 核心逻辑
└── README.md               ✅ 已双语
```

---

## 🚀 上传到 ClawHub / Publishing to ClawHub

### 方式一：CLI 上传（推荐）

```bash
# 1. 安装 ClawHub CLI
npm i -g clawhub

# 2. 登录 ClawHub
clawhub login

# 3. 发布插件
clawhub package publish ./model-fallback \
  --name "@yijiuzero/model-fallback" \
  --version "1.2.0"
```

### 方式二：GitHub Actions CI

在插件仓库里添加 workflow：

```yaml
# .github/workflows/publish.yml
jobs:
  publish:
    uses: openclaw/clawhub/.github/workflows/skill-publish.yml@main
    with:
      owner: yijiuzero
      dry_run: false
    secrets:
      clawhub_token: ${{ secrets.CLAWHUB_TOKEN }}
```

---

## 📝 插件包命名 / Package Naming

ClawHub 插件使用 npm 风格的包名格式：

```
@你的用户名/插件名
  ↓     ↓
scope  package
```

建议命名：

```
@yijiuzero/model-fallback
```

---

## ⚙️ 需要改一下的地方

当前的 `openclaw.plugin.json` 要加一个字段：

```json
{
  "id": "model-fallback",
  "name": "Model Fallback / 模型降级",
  "version": "1.2.0",
  "kind": "provider-router",
  "entry": {
    "backend": "backend/main.py"
  },
  "activation": {
    "onStartup": true
  }
}
```

如果要加自定义图标，再加：

```json
{
  "icon": "https://你的图标地址/icon.png"
}
```

---

## 🔗 有用链接 / Useful Links

| 资源 | 地址 |
|------|------|
| 🌐 ClawHub 网站 | https://clawhub.io |
| 📖 发布文档 | https://github.com/openclaw/clawhub/blob/main/docs/publishing.md |
| 🛡️ 安全审核 | https://github.com/openclaw/clawhub/blob/main/docs/security-audits.md |
| 💬 Discord | https://discord.gg/clawd |
| 📦 GitHub 仓库 | https://github.com/yijiuzero/qwenpaw-model-fallback |

---

## 📢 其他推广渠道

除了 ClawHub，还可以发到：

| 平台 | 说明 |
|------|------|
| 🐦 **X/Twitter** | #OpenClaw #Plugin 标签推广 |
| 💬 **Discord** | OpenClaw 官方 Discord 的 #plugin-showcase 频道 |
| 📝 **Reddit** | r/OpenClaw 子版块 |
| 🐙 **GitHub** | 直接在仓库做 Release |
| 📺 **YouTube** | 录个演示视频 |

---

> 如果上传遇到问题，在 Discord 的 #clawhub 频道问就行！
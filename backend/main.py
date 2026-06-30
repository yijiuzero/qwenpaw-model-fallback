# -*- coding: utf-8 -*-
"""Model Fallback Plugin - Backend Entry Point

安装后，在 QwenPaw Console → 工具设置 → Model Fallback 中配置。
"""

import json
import logging
import sys
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import APIRouter

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Ensure plugin root is importable
# ---------------------------------------------------------------------------

_PLUGIN_DIR = Path(__file__).resolve().parent.parent
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))

from fallback_model import (
    CircuitBreakerPluginConfig,
    FallbackPluginConfig,
    FallbackSlotConfig,
    FallbackSlotByUrl,
    install_patch,
)

# ---------------------------------------------------------------------------
# HTTP API router
# ---------------------------------------------------------------------------

_router = APIRouter()


@_router.get("/config")
async def get_config():
    """Return the plugin's current configuration."""
    cfg = _read_plugin_config()
    cb = cfg.circuit_breaker
    return {
        "enabled": cfg.enabled,
        "max_retries_per_model": cfg.max_retries_per_model,
        "fallback_chain": [s.model_dump() for s in cfg.fallback_chain],
        "fallback_slots": [s.model_dump() for s in cfg.fallback_slots],
        "circuit_breaker": {
            "failure_threshold": cb.failure_threshold,
            "success_threshold": cb.success_threshold,
            "timeout_seconds": cb.timeout_seconds,
        },
    }


@_router.get("/providers")
async def list_providers():
    """List available providers and models for configuration."""
    from qwenpaw.providers import ProviderManager
    try:
        mgr = ProviderManager.get_instance()
        infos = await mgr.list_provider_info()
        result = {}
        for info in infos:
            result[info.id] = {
                "name": info.name,
                "models": [
                    {"id": m.id, "name": m.name}
                    for m in getattr(info, "models", [])
                ],
            }
        return result
    except Exception as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# Read config from QwenPaw's global config.json
# ---------------------------------------------------------------------------


def _get_global_config_path() -> Path:
    from qwenpaw.constant import WORKING_DIR
    return Path(WORKING_DIR) / "config.json"


def _load_global_config() -> Dict[str, Any]:
    p = _get_global_config_path()
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_global_config(data: Dict[str, Any]) -> None:
    p = _get_global_config_path()
    p.write_text(
        json.dumps(data, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def _read_plugin_config() -> FallbackPluginConfig:
    """Build config from multiple sources, preferring Console tool config."""
    cfg = FallbackPluginConfig()

    # 1) Try agent-level tool config from Console UI
    try:
        from qwenpaw.plugins.api import get_tool_config
        tool_conf = get_tool_config("fallback")
        if isinstance(tool_conf, dict):
            if "enabled" in tool_conf:
                cfg.enabled = bool(tool_conf["enabled"])
            if "max_retries_per_model" in tool_conf:
                cfg.max_retries_per_model = max(
                    1, int(tool_conf["max_retries_per_model"])
                )

            # 解析 flat 配置字段 → fallback_slots
            slots = []
            for i in (1, 2, 3):  # 最多 3 个备用模型
                model_key = f"fallback_model_{i}"
                url_key = f"fallback_url_{i}"
                key_key = f"fallback_key_{i}"
                model_name = tool_conf.get(model_key, "").strip()
                base_url = tool_conf.get(url_key, "").strip()
                api_key = tool_conf.get(key_key, "").strip()
                if model_name and base_url and api_key:
                    slots.append(FallbackSlotByUrl(
                        model_name=model_name,
                        base_url=base_url,
                        api_key=api_key,
                    ))
            cfg.fallback_slots = slots

            # 也兼容旧格式的 fallback_chain JSON
            chain_str = tool_conf.get("fallback_chain", "")
            if isinstance(chain_str, str) and chain_str.strip():
                try:
                    parsed = json.loads(chain_str)
                    if isinstance(parsed, list):
                        cfg.fallback_chain = [
                            FallbackSlotConfig(**s) for s in parsed
                        ]
                except (json.JSONDecodeError, TypeError):
                    pass
            elif isinstance(chain_str, list):
                cfg.fallback_chain = [
                    FallbackSlotConfig(**s) for s in chain_str
                ]

            # 熔断器
            cb = cfg.circuit_breaker
            if "cb_failure_threshold" in tool_conf:
                cb.failure_threshold = max(1, int(tool_conf["cb_failure_threshold"]))
            if "cb_timeout_seconds" in tool_conf:
                cb.timeout_seconds = max(5, int(tool_conf["cb_timeout_seconds"]))
            if "cb_success_threshold" in tool_conf:
                cb.success_threshold = max(1, int(tool_conf["cb_success_threshold"]))
            return cfg
    except Exception:
        import traceback
        logger.debug("model-fallback: get_tool_config failed: %s",
                     traceback.format_exc())

    # 2) Fallback: global config.plugins.model-fallback
    global_cfg = _load_global_config()
    plugin_data = global_cfg.get("plugins", {}).get("model-fallback", {})
    try:
        return FallbackPluginConfig(**plugin_data)
    except Exception:
        return cfg


def _save_plugin_config(plugin_cfg: FallbackPluginConfig) -> None:
    """Persist config to global config.json."""
    cfg = _load_global_config()
    if "plugins" not in cfg:
        cfg["plugins"] = {}
    cb = plugin_cfg.circuit_breaker
    cfg["plugins"]["model-fallback"] = {
        "enabled": plugin_cfg.enabled,
        "max_retries_per_model": plugin_cfg.max_retries_per_model,
        "fallback_chain": [s.model_dump() for s in plugin_cfg.fallback_chain],
        "fallback_slots": [s.model_dump() for s in plugin_cfg.fallback_slots],
        "circuit_breaker": {
            "failure_threshold": cb.failure_threshold,
            "success_threshold": cb.success_threshold,
            "timeout_seconds": cb.timeout_seconds,
        },
    }
    _save_global_config(cfg)


# ---------------------------------------------------------------------------
# Slash command handler
# ---------------------------------------------------------------------------


class _FallbackCommandHandler:
    """Handles /fallback slash command.

    Usage:
        /fallback                     Show current config
        /fallback on                  Enable fallback
        /fallback off                 Disable fallback
        /fallback add <provider>/<model>   Add a fallback model
        /fallback remove <index>      Remove fallback by index
        /fallback clear               Clear all fallback entries
        /fallback retries <n>         Set retry count per model
    """

    command_name = "fallback"
    description = "管理模型降级链"

    async def handle(self, args: str, **kwargs) -> str:
        cfg = _read_plugin_config()
        parts = args.strip().split(maxsplit=1)
        subcmd = parts[0].lower() if parts else ""
        rest = parts[1] if len(parts) > 1 else ""

        if not subcmd:
            return self._show_status(cfg)

        if subcmd == "on":
            cfg.enabled = True
            _save_plugin_config(cfg)
            return "✅ Model Fallback 已启用。重启后生效。"

        if subcmd == "off":
            cfg.enabled = False
            _save_plugin_config(cfg)
            return "🔕 Model Fallback 已禁用。重启后生效。"

        if subcmd == "add":
            if not rest or "/" not in rest:
                return ("❌ 格式：/fallback add <provider_id>/<model_id>\n"
                        "例如：/fallback add deepseek_official/deepseek-chat")
            provider_id, model_id = rest.split("/", 1)
            cfg.fallback_chain.append(
                FallbackSlotConfig(
                    provider_id=provider_id.strip(), model=model_id.strip()
                )
            )
            _save_plugin_config(cfg)
            return (f"✅ 已添加备用模型 #{len(cfg.fallback_chain)}: "
                    f"{provider_id}/{model_id}\n重启后生效。")

        if subcmd == "remove":
            try:
                idx = int(rest) - 1
                removed = cfg.fallback_chain.pop(idx)
                _save_plugin_config(cfg)
                return f"✅ 已移除: {removed.provider_id}/{removed.model}"
            except (ValueError, IndexError):
                return (f"❌ 无效序号。"
                        f"当前共 {len(cfg.fallback_chain)} 个备用模型。")

        if subcmd == "clear":
            cfg.fallback_chain = []
            _save_plugin_config(cfg)
            return "🗑️ 已清空所有备用模型。"

        if subcmd == "retries":
            try:
                cfg.max_retries_per_model = max(1, int(rest))
                _save_plugin_config(cfg)
                return (f"✅ 每模型重试次数已设为 "
                        f"{cfg.max_retries_per_model}。重启后生效。")
            except ValueError:
                return "❌ 请输入有效数字。例如：/fallback retries 3"

        # ── 熔断器子命令 ──
        if subcmd == "cb":
            return self._show_circuit_breaker_config(cfg)

        if subcmd == "cb-reset":
            return "🔁 熔断器状态在重启后自动重置。如需手动重置，请重启 QwenPaw。"

        return f"❌ 未知子命令：{subcmd}\n{self.help()}"

    def _show_status(self, cfg: FallbackPluginConfig) -> str:
        lines = ["📋 **Model Fallback 配置**"]
        lines.append(f"状态: {'✅ 启用' if cfg.enabled else '🔕 禁用'}")
        lines.append(f"每模型重试: {cfg.max_retries_per_model} 次")
        lines.append("")

        # 显示 url 模式的备用模型
        if cfg.fallback_slots:
            lines.append("⬇️ 备用模型（URL模式）:")
            for i, slot in enumerate(cfg.fallback_slots, 1):
                lines.append(
                    f"  {i}. `{slot.model_name}` ← {slot.base_url}"
                )
            lines.append("")

        # 显示 provider_id 模式的备用模型
        if cfg.fallback_chain:
            lines.append("⬇️ 降级链（Provider模式）:")
            for i, slot in enumerate(cfg.fallback_chain, 1):
                lines.append(f"  {i}. `{slot.provider_id}` / `{slot.model}`")
            lines.append("")

        if not cfg.fallback_slots and not cfg.fallback_chain:
            lines.append("⬇️ 降级链: (空)")

        # 熔断器
        cb = cfg.circuit_breaker
        lines.append(f"🔌 熔断器: 失败{cb.failure_threshold}次/超时{cb.timeout_seconds}秒/恢复{cb.success_threshold}次成功")
        return "\n".join(lines)

    def help(self) -> str:
        return (
            "**/fallback** 命令：\n"
            "/fallback                    查看配置\n"
            "/fallback on|off            启用/禁用\n"
            "/fallback add <p>/<m>       添加备用模型\n"
            "/fallback remove <序号>      移除备用模型\n"
            "/fallback clear             清空降级链\n"
            "/fallback retries <n>       设置每模型重试次数\n"
            "/fallback cb                查看熔断器配置\n"
            "/fallback cb-reset          重置熔断器状态（需重启）\n"
        )

    def _show_circuit_breaker_config(self, cfg: FallbackPluginConfig) -> str:
        cb = cfg.circuit_breaker
        lines = ["🔌 **熔断器配置**"]
        lines.append(f"失败阈值: {cb.failure_threshold} 次连续失败")
        lines.append(f"超时恢复: {cb.timeout_seconds} 秒")
        lines.append(f"恢复成功阈值: {cb.success_threshold} 次连续成功")
        lines.append("")
        lines.append("📊 **当前端点状态（重启后可见）**:")
        lines.append("  重启后首次调用时生成。")
        lines.append("  通过 /fallback cb-reset 提示可在日志中查看。")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Plugin entry point
# ---------------------------------------------------------------------------


class _ModelFallbackPlugin:
    """Plugin class - QwenPaw PluginLoader calls ``register(api)``."""

    def register(self, api) -> None:
        """Called by PluginLoader when this plugin is loaded."""
        logger.info("model-fallback: registering plugin.")

        # Startup hook – patches model_factory
        api.register_startup_hook(
            hook_name="model-fallback-install-patch",
            callback=self._on_startup,
            priority=50,
        )

        # HTTP API
        api.register_http_router(
            _router, prefix="/config", tags=["model-fallback"]
        )

        # Slash command
        api.register_control_command(
            handler=_FallbackCommandHandler(),
            priority_level=10,
        )

        logger.info("model-fallback: plugin registered successfully.")

    def _on_startup(self) -> None:
        """Startup hook – patches model_factory."""
        _dbg = Path("/tmp/model_fallback_debug.log")
        _dbg.write_text(f"_on_startup CALLED at {__import__('datetime').datetime.now()}\n")
        try:
            cfg = _read_plugin_config()
            success = install_patch(_PLUGIN_DIR, cfg)
            if success:
                logger.info("model-fallback: patch installed successfully.")
            else:
                logger.info(
                    "model-fallback: patch skipped "
                    "(disabled or empty fallback chain)."
                )
        except Exception as e:
            logger.error(
                "model-fallback: startup hook failed: %s",
                e,
                exc_info=True,
            )


# Required export
plugin = _ModelFallbackPlugin()

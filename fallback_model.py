# -*- coding: utf-8 -*-
"""Model Fallback Plugin for QwenPaw
=====================================

A plugin that wraps the model creation chain with failover logic.
When the primary LLM call fails, it automatically cascades through
a user-configured list of fallback models.

Two config modes:
1. **provider_id + model** — for advanced users: specify a QwenPaw
   provider ID and model name directly (used by /fallback slash cmd)
2. **url + key + model** — for normal users: just fill in Base URL,
   API Key, and model name in Console UI. Plugin auto-matches to
   existing providers or creates a dynamic OpenAI-compatible client.

Architecture:
- Startup hook: patches `model_factory.create_model_and_formatter`
- FallbackChatModel: ChatModelBase wrapper trying models in sequence
- CircuitBreaker: per-model health monitor to skip dead endpoints quickly
"""

import asyncio
import json
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, AsyncGenerator, Literal, Optional, Type, TYPE_CHECKING

from agentscope.formatter import FormatterBase
from agentscope.model import ChatModelBase
from agentscope.model._model_response import ChatResponse
from pydantic import BaseModel

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Circuit Breaker
# ---------------------------------------------------------------------------


class CircuitState(Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass
class CircuitBreakerConfig:
    """熔断器配置"""
    failure_threshold: int = 4
    success_threshold: int = 2
    timeout_seconds: float = 60.0


class CircuitBreaker:
    """熔断器实例"""

    def __init__(self, config: CircuitBreakerConfig | None = None) -> None:
        self._config = config or CircuitBreakerConfig()
        self._state = CircuitState.CLOSED
        self._consecutive_failures = 0
        self._consecutive_successes = 0
        self._last_opened_at: float | None = None
        self._lock = threading.Lock()

    def allow_request(self) -> bool:
        with self._lock:
            if self._state == CircuitState.CLOSED:
                return True
            if self._state == CircuitState.OPEN:
                if self._last_opened_at is not None:
                    elapsed = time.monotonic() - self._last_opened_at
                    if elapsed >= self._config.timeout_seconds:
                        self._state = CircuitState.HALF_OPEN
                        logger.info(
                            "Circuit breaker HALF_OPEN after %.1fs",
                            elapsed,
                        )
                        return True
                return False
            return True

    def record_success(self) -> None:
        with self._lock:
            if self._state == CircuitState.HALF_OPEN:
                self._consecutive_successes += 1
                if self._consecutive_successes >= self._config.success_threshold:
                    self._state = CircuitState.CLOSED
                    self._consecutive_failures = 0
                    self._consecutive_successes = 0
                    self._last_opened_at = None
                    logger.info("Circuit breaker CLOSED (recovered).")
            else:
                self._consecutive_failures = 0

    def record_failure(self) -> None:
        with self._lock:
            if self._state == CircuitState.HALF_OPEN:
                self._state = CircuitState.OPEN
                self._consecutive_successes = 0
                self._last_opened_at = time.monotonic()
                logger.info(
                    "Circuit breaker re-OPEN (half-open probe failed)."
                )
                return
            self._consecutive_failures += 1
            if self._consecutive_failures >= self._config.failure_threshold:
                self._state = CircuitState.OPEN
                self._last_opened_at = time.monotonic()
                logger.info(
                    "Circuit breaker OPEN after %d consecutive failures.",
                    self._consecutive_failures,
                )

    @property
    def state(self) -> CircuitState:
        with self._lock:
            return self._state

    def get_stats(self) -> dict:
        with self._lock:
            return {
                "state": self._state.value,
                "consecutive_failures": self._consecutive_failures,
                "consecutive_successes": self._consecutive_successes,
                "last_opened_at": self._last_opened_at,
                "failure_threshold": self._config.failure_threshold,
                "success_threshold": self._config.success_threshold,
                "timeout_seconds": self._config.timeout_seconds,
            }


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


class FallbackSlotConfig(BaseModel):
    """Fallback by provider_id + model (advanced mode)."""
    provider_id: str
    model: str


class FallbackSlotByUrl(BaseModel):
    """Fallback by Base URL + API Key + model name (Console UI mode)."""
    model_name: str
    base_url: str
    api_key: str


class CircuitBreakerPluginConfig(BaseModel):
    failure_threshold: int = 4
    success_threshold: int = 2
    timeout_seconds: float = 60.0


class FallbackPluginConfig(BaseModel):
    """Full plugin configuration."""
    enabled: bool = True
    max_retries_per_model: int = 1
    # 高级模式：provider_id + model（slash 命令用）
    fallback_chain: list[FallbackSlotConfig] = []
    # 用户友好模式：url + key + model（Console UI 用）
    fallback_slots: list[FallbackSlotByUrl] = []
    retryable_status_codes: list[int] = [429, 500, 502, 503, 504]
    exclude_status_codes: list[int] = [401, 403, 404]
    circuit_breaker: CircuitBreakerPluginConfig = field(
        default_factory=CircuitBreakerPluginConfig
    )


def _load_config(config_dir: Path) -> FallbackPluginConfig:
    cfg_path = config_dir / "config.json"
    if not cfg_path.exists():
        return FallbackPluginConfig()
    try:
        data = json.loads(cfg_path.read_text(encoding="utf-8"))
        return FallbackPluginConfig(**data)
    except Exception as e:
        logger.warning("model-fallback: failed to load config: %s", e)
        return FallbackPluginConfig()


def _save_config(config_dir: Path, cfg: FallbackPluginConfig) -> None:
    config_dir.mkdir(parents=True, exist_ok=True)
    cfg_path = config_dir / "config.json"
    cfg_path.write_text(
        json.dumps(
            {
                "enabled": cfg.enabled,
                "max_retries_per_model": cfg.max_retries_per_model,
                "fallback_chain": [s.model_dump() for s in cfg.fallback_chain],
                "fallback_slots": [s.model_dump() for s in cfg.fallback_slots],
                "retryable_status_codes": cfg.retryable_status_codes,
                "exclude_status_codes": cfg.exclude_status_codes,
                "circuit_breaker": {
                    "failure_threshold": cfg.circuit_breaker.failure_threshold,
                    "success_threshold": cfg.circuit_breaker.success_threshold,
                    "timeout_seconds": cfg.circuit_breaker.timeout_seconds,
                },
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Provider resolution for url-based fallback slots
# ---------------------------------------------------------------------------

# 常见 provider 的 domain → provider_id 映射
_DOMAIN_PROVIDER_MAP: list[tuple[re.Pattern, str]] = [
    (re.compile(r"deepseek\.", re.I), "deepseek_official"),
    (re.compile(r"anthropic\.", re.I), "anthropic"),
    (re.compile(r"openai\.com", re.I), "openai"),
    (re.compile(r"api\.openai", re.I), "openai"),
    (re.compile(r"openrouter\.", re.I), "openrouter"),
    (re.compile(r"moonshot|kimi", re.I), "kimi"),
    (re.compile(r"gemini|googleapis\.com", re.I), "gemini"),
    (re.compile(r"ollama", re.I), "ollama"),
]


def _match_provider_by_url(base_url: str) -> str | None:
    """Try to match *base_url* to an existing QwenPaw provider id."""
    for pattern, provider_id in _DOMAIN_PROVIDER_MAP:
        if pattern.search(base_url):
            return provider_id
    return None


def _resolve_fallback_slot(
    slot: FallbackSlotByUrl,
) -> tuple[str, str] | tuple[None, ChatModelBase, FormatterBase]:
    """Resolve a url-based fallback slot.

    Returns either:
      (provider_id, model_name)  — if matched to existing provider
      (None, model, formatter)   — if created a dynamic model
    """
    provider_id = _match_provider_by_url(slot.base_url)
    if provider_id is not None:
        return (provider_id, slot.model_name)

    # 没匹配到 → 动态创建 OpenAI-compatible 模型
    return _create_dynamic_model(slot)


def _create_dynamic_model(
    slot: FallbackSlotByUrl,
) -> tuple[None, ChatModelBase, FormatterBase]:
    """Create a raw OpenAIChatModel for a custom base URL."""
    from agentscope.model._openai_model import OpenAIChatModel
    from qwenpaw.agents.model_factory import _create_formatter_instance

    model = OpenAIChatModel(
        model_name=slot.model_name,
        api_key=slot.api_key,
        client_kwargs={"base_url": slot.base_url.rstrip("/") + "/"},
    )
    formatter = _create_formatter_instance(OpenAIChatModel)
    return (None, model, formatter)


# ---------------------------------------------------------------------------
# FallbackChatModel
# ---------------------------------------------------------------------------


class FallbackEndpoint:
    """A single model endpoint in the fallback chain."""

    __slots__ = (
        "provider_id", "model_name", "model", "formatter",
        "circuit_breaker",
    )

    def __init__(
        self,
        provider_id: str,
        model_name: str,
        model: ChatModelBase,
        formatter: FormatterBase,
        circuit_breaker: CircuitBreaker | None = None,
    ) -> None:
        self.provider_id = provider_id
        self.model_name = model_name
        self.model = model
        self.formatter = formatter
        self.circuit_breaker = circuit_breaker


class FallbackChatModel(ChatModelBase):
    """ChatModel that cascades through a list of backup models."""

    def __init__(
        self,
        primary: ChatModelBase,
        primary_formatter: FormatterBase,
        fallback_endpoints: list[FallbackEndpoint],
        max_retries: int = 1,
        cb_config: CircuitBreakerConfig | None = None,
    ) -> None:
        super().__init__(
            model_name="fallback",
            stream=bool(getattr(primary, "stream", True)),
        )
        self.primary = primary
        self.primary_formatter = primary_formatter
        self.fallback_endpoints = fallback_endpoints
        self.max_retries = max_retries
        self._cb_config = cb_config or CircuitBreakerConfig()

        self._primary_cb = CircuitBreaker(self._cb_config)
        for ep in self.fallback_endpoints:
            if ep.circuit_breaker is None:
                ep.circuit_breaker = CircuitBreaker(self._cb_config)

    # ------------------------------------------------------------------
    def _is_cascadable(self, exc: Exception) -> bool:
        """True if this error should trigger fallback to next model."""
        status = getattr(exc, "status_code", None)
        if status:
            if status in (401, 403):
                return False
            if status in (429, 500, 502, 503, 504):
                return True
        name = type(exc).__name__
        if "ConnectionError" in name or "Timeout" in name:
            return True
        return True

    async def _try_endpoint(
        self,
        endpoint,
        messages: list[dict],
        tools: list[dict] | None,
        tool_choice,
        structured_model,
        stream: bool = False,
        **kwargs: Any,
    ) -> ChatResponse | AsyncGenerator[ChatResponse, None]:
        """Call *endpoint* with retries."""
        last_exc: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            try:
                if stream:
                    resp = await endpoint.model(
                        messages,
                        tools=tools,
                        tool_choice=tool_choice,
                        structured_model=structured_model,
                        stream=True,
                        **kwargs,
                    )
                else:
                    resp = await endpoint.model(
                        messages,
                        tools=tools,
                        tool_choice=tool_choice,
                        structured_model=structured_model,
                        **kwargs,
                    )
                if endpoint.circuit_breaker is not None:
                    endpoint.circuit_breaker.record_success()
                return resp
            except Exception as e:
                last_exc = e
                if not self._is_cascadable(e):
                    raise
                logger.warning(
                    "model-fallback: attempt %d/%d on %s/%s failed: %s",
                    attempt,
                    self.max_retries,
                    endpoint.provider_id,
                    endpoint.model_name,
                    e,
                )
                if endpoint.circuit_breaker is not None:
                    endpoint.circuit_breaker.record_failure()
                if attempt < self.max_retries:
                    await asyncio.sleep(min(2 ** attempt, 10))
        raise last_exc  # exhausted retries

    # ------------------------------------------------------------------
    async def __call__(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        tool_choice: Literal["auto", "none", "required"] | str | None = None,
        structured_model: Type[BaseModel] | None = None,
        stream: bool = False,
        **kwargs: Any,
    ) -> ChatResponse | AsyncGenerator[ChatResponse, None]:
        last_error: Exception | None = None
        skipped_endpoints: list[str] = []

        # ── 1) 主模型 ──
        primary_allowed = self._primary_cb.allow_request()
        if primary_allowed:
            try:
                ep = FallbackEndpoint(
                    "__primary__", "primary",
                    self.primary, self.primary_formatter,
                    circuit_breaker=self._primary_cb,
                )
                return await self._try_endpoint(
                    ep, messages, tools, tool_choice,
                    structured_model, stream=stream, **kwargs,
                )
            except Exception as exc:
                last_error = exc
                if not self._is_cascadable(exc):
                    raise
                logger.warning(
                    "model-fallback: primary failed after %d retries: %s. "
                    "Cascading…",
                    self.max_retries,
                    exc,
                )
        else:
            skipped_endpoints.append("__primary__/primary")
            logger.info(
                "model-fallback: primary is CIRCUIT-OPEN, skipping."
            )

        # ── 2) 熔断器过滤 ──
        available_endpoints: list[FallbackEndpoint] = []
        for ep in self.fallback_endpoints:
            cb = ep.circuit_breaker
            if cb is not None and not cb.allow_request():
                skipped_endpoints.append(f"{ep.provider_id}/{ep.model_name}")
                logger.info(
                    "model-fallback: %s/%s is CIRCUIT-OPEN, skipping.",
                    ep.provider_id, ep.model_name,
                )
            else:
                available_endpoints.append(ep)

        if not available_endpoints:
            all_count = len(self.fallback_endpoints)
            skip_count = len(skipped_endpoints)
            raise RuntimeError(
                f"All {all_count} fallback models exhausted "
                f"({skip_count} circuit-open). "
                f"Last error: {last_error}"
            )

        # ── 3) 依次尝试 ──
        for endpoint in available_endpoints:
            logger.info(
                "model-fallback: trying fallback %s/%s",
                endpoint.provider_id, endpoint.model_name,
            )
            try:
                result = await self._try_endpoint(
                    endpoint, messages, tools, tool_choice,
                    structured_model, stream=stream, **kwargs,
                )
                logger.info(
                    "model-fallback: fallback %s/%s succeeded",
                    endpoint.provider_id, endpoint.model_name,
                )
                return result
            except Exception as exc:
                last_error = exc
                if not self._is_cascadable(exc):
                    raise
                logger.warning(
                    "model-fallback: fallback %s/%s failed: %s",
                    endpoint.provider_id, endpoint.model_name, exc,
                )

        raise RuntimeError(
            f"All {1 + len(self.fallback_endpoints)} models in the fallback "
            f"chain exhausted ({len(skipped_endpoints)} circuit-open). "
            f"Last error: {last_error}"
        )

    def get_circuit_breaker_stats(self) -> dict[str, dict]:
        stats: dict[str, dict] = {}
        stats["__primary__"] = self._primary_cb.get_stats()
        for ep in self.fallback_endpoints:
            key = f"{ep.provider_id}/{ep.model_name}"
            if ep.circuit_breaker is not None:
                stats[key] = ep.circuit_breaker.get_stats()
            else:
                stats[key] = {"state": "no_circuit_breaker"}
        return stats


# ---------------------------------------------------------------------------
# Patch helper
# ---------------------------------------------------------------------------


def _build_fallback_endpoints(
    cfg: FallbackPluginConfig,
    cb_config: CircuitBreakerConfig,
) -> list[FallbackEndpoint]:
    """Build endpoints from both ``fallback_chain`` and ``fallback_slots``."""
    from qwenpaw.providers import ProviderManager
    from qwenpaw.agents.model_factory import _create_formatter_instance

    manager = ProviderManager.get_instance()
    endpoints: list[FallbackEndpoint] = []

    # 1) provider_id + model 模式（高级）
    for slot in cfg.fallback_chain:
        try:
            provider = manager.get_provider(slot.provider_id)
            if provider is None:
                logger.warning(
                    "model-fallback: provider '%s' not found, skipping.",
                    slot.provider_id,
                )
                continue
            model = provider.get_chat_model_instance(slot.model)
            formatter = _create_formatter_instance(model.__class__)
            endpoints.append(
                FallbackEndpoint(
                    slot.provider_id, slot.model, model, formatter,
                    circuit_breaker=CircuitBreaker(cb_config),
                )
            )
        except Exception as e:
            logger.warning(
                "model-fallback: failed to create endpoint %s/%s: %s",
                slot.provider_id, slot.model, e,
            )

    # 2) url + key + model 模式（用户友好）
    for i, slot in enumerate(cfg.fallback_slots):
        try:
            result = _resolve_fallback_slot(slot)
            if len(result) == 2:
                provider_id, model_name = result
                provider = manager.get_provider(provider_id)
                if provider is None:
                    logger.warning(
                        "model-fallback: matched provider '%s' not found,"
                        " skipping slot %d.",
                        provider_id, i,
                    )
                    continue
                model = provider.get_chat_model_instance(model_name)
                formatter = _create_formatter_instance(model.__class__)
                endpoints.append(
                    FallbackEndpoint(
                        provider_id, model_name, model, formatter,
                        circuit_breaker=CircuitBreaker(cb_config),
                    )
                )
            else:
                _, model, formatter = result
                endpoints.append(
                    FallbackEndpoint(
                        f"custom_{i}", slot.model_name, model, formatter,
                        circuit_breaker=CircuitBreaker(cb_config),
                    )
                )
            logger.info(
                "model-fallback: resolved slot %d: %s → %s/%s",
                i, slot.base_url,
                endpoints[-1].provider_id, endpoints[-1].model_name,
            )
        except Exception as e:
            logger.warning(
                "model-fallback: failed to resolve slot %d (%s): %s",
                i, slot.base_url, e,
            )

    return endpoints


def install_patch(
    config_dir: Path,
    cfg: FallbackPluginConfig | None = None,
) -> bool:
    """Patch ``model_factory.create_model_and_formatter``."""
    if cfg is None:
        cfg = _load_config(config_dir)
    if not cfg.enabled:
        logger.info("model-fallback: disabled, patch skipped.")
        return False
    if not cfg.fallback_chain and not cfg.fallback_slots:
        logger.info(
            "model-fallback: no fallback configured, patch skipped."
        )
        return False

    cb_config = CircuitBreakerConfig(
        failure_threshold=cfg.circuit_breaker.failure_threshold,
        success_threshold=cfg.circuit_breaker.success_threshold,
        timeout_seconds=cfg.circuit_breaker.timeout_seconds,
    )

    endpoints = _build_fallback_endpoints(cfg, cb_config)
    if not endpoints:
        logger.warning(
            "model-fallback: no valid fallback endpoints built,"
            " patch skipped."
        )
        return False

    # Monkey-patch
    import qwenpaw.agents.model_factory as mf

    _original_create = mf.create_model_and_formatter
    _patched_endpoints = endpoints
    _patched_max_retries = cfg.max_retries_per_model
    _patched_cb_config = cb_config

    def _patched_create(agent_id=None):
        model, formatter = _original_create(agent_id=agent_id)
        return (
            FallbackChatModel(
                primary=model,
                primary_formatter=formatter,
                fallback_endpoints=_patched_endpoints,
                max_retries=_patched_max_retries,
                cb_config=_patched_cb_config,
            ),
            formatter,
        )

    mf.create_model_and_formatter = _patched_create

    # Also patch direct references that may have been imported
    # before our monkey-patch (Python's import caching issue)
    _modules_to_patch = [
        "qwenpaw.agents.react_agent",
        "qwenpaw.agents.context.light_context_manager",
        "qwenpaw.agents.memory.reme_light_memory_manager",
    ]
    for _mod_name in _modules_to_patch:
        try:
            import importlib
            _mod = importlib.import_module(_mod_name)
            if hasattr(_mod, "create_model_and_formatter"):
                _mod.create_model_and_formatter = _patched_create
        except Exception:
            pass

    logger.info(
        "model-fallback: patch installed. Endpoints: %s",
        ", ".join(
            f"{e.provider_id}/{e.model_name}" for e in endpoints
        ),
    )
    return True

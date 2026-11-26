"""Adapter registry & alias resolution for Kiki Heavy-Path agents."""

from __future__ import annotations

from typing import Iterable, Optional

from .antigravity import AntigravityAdapter
from .base import AdapterInfo, HeavyTaskAdapter
from .claude_code import ClaudeCodeAdapter
from .codex import CodexAdapter
from .deepseek_harness import DeepSeekHarnessAdapter
from .internal_agent import InternalAgentAdapter
from .kcode import KCodeAdapter
from .qoder import QoderAdapter
from .workbuddy import WorkBuddyAdapter
from .zcode import ZCodeAdapter


def _normalize_key(raw: str) -> str:
    return raw.strip().lower().replace("-", "_").replace(" ", "_")


class AdapterRegistry:
    """Central registry for all built-in and external Heavy-Path adapters."""

    def __init__(self, adapters: Optional[Iterable[HeavyTaskAdapter]] = None) -> None:
        self._adapters: dict[str, HeavyTaskAdapter] = {}
        self._aliases: dict[str, str] = {}
        if adapters is not None:
            for adapter in adapters:
                self.register(adapter)

    def register(self, adapter: HeavyTaskAdapter) -> None:
        canonical = _normalize_key(adapter.agent_type)
        self._adapters[canonical] = adapter
        self._aliases[canonical] = canonical
        for alias in adapter.aliases:
            self._aliases[_normalize_key(alias)] = canonical

    def resolve_type(self, agent_type: Optional[str], default: str = "internal_agent") -> str:
        raw = (agent_type or "").strip()
        if not raw:
            return _normalize_key(default)
        key = _normalize_key(raw)
        if key in self._aliases:
            return self._aliases[key]
        raise KeyError(
            f"Unknown heavy task adapter '{agent_type}'. "
            f"Available adapters: {', '.join(self._adapters.keys())}"
        )

    def try_resolve_type(self, agent_type: Optional[str]) -> Optional[str]:
        raw = (agent_type or "").strip()
        if not raw:
            return None
        key = _normalize_key(raw)
        return self._aliases.get(key)

    def get(self, agent_type: Optional[str], default: str = "internal_agent") -> HeavyTaskAdapter:
        canonical = self.resolve_type(agent_type, default=default)
        return self._adapters[canonical]

    def list_infos(self) -> list[AdapterInfo]:
        return [adapter.probe() for adapter in self._adapters.values()]

    def list_types(self) -> list[str]:
        return list(self._adapters.keys())


def create_default_registry(
    *,
    gemini_model: Optional[str] = None,
    gemini_api_key_configured: bool = True,
) -> AdapterRegistry:
    """Build the default registry containing all Kiki Heavy-Path adapters."""
    return AdapterRegistry(
        adapters=[
            InternalAgentAdapter(
                default_model=gemini_model,
                api_key_configured=gemini_api_key_configured,
            ),
            CodexAdapter(),
            ClaudeCodeAdapter(),
            AntigravityAdapter(),
            DeepSeekHarnessAdapter(),
            ZCodeAdapter(),
            KCodeAdapter(),
            QoderAdapter(),
            WorkBuddyAdapter(),
        ]
    )

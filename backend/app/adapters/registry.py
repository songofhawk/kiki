"""Adapter registry & alias resolution for Kiki Heavy-Path agents."""

from __future__ import annotations

from typing import Iterable, Optional
import re

from .antigravity import AntigravityAdapter
from .base import AdapterInfo, HeavyTaskAdapter
from .claude_code import ClaudeCodeAdapter
from .codex import CodexAdapter
from .deepseek_harness import DeepSeekHarnessAdapter
from .doubao_work import DoubaoWorkAdapter
from .gemini_agent import GeminiAgentAdapter
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

    def resolve_type(self, agent_type: Optional[str], default: str = "gemini_agent") -> str:
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

    def detect_requested_agent(self, text: str) -> Optional[str]:
        """Detect an explicitly named Heavy-Path adapter from user natural-language text."""
        clean = (text or "").strip()
        if not clean:
            return None

        # Exclude overly generic aliases that could match ordinary prose
        generic_aliases = {"builtin", "default", "ark", "cbc", "dsh", "agy"}
        candidates: list[tuple[str, str]] = []
        for canonical, adapter in self._adapters.items():
            names = [canonical, adapter.agent_type, *adapter.aliases]
            for name in names:
                normalized = _normalize_key(name)
                if not normalized or normalized in generic_aliases:
                    continue
                # Build human-readable variants (e.g. claude_code -> claude code / claude-code)
                for variant in {name.strip(), name.strip().replace("_", " "), name.strip().replace("_", "-")}:
                    if len(variant) >= 3:
                        candidates.append((variant, canonical))

        # Match longest candidate first so "claude code" beats "claude"
        candidates.sort(key=lambda item: len(item[0]), reverse=True)
        for variant, canonical in candidates:
            escaped = re.escape(variant)
            if variant.isascii():
                pattern = rf"(?<![A-Za-z0-9_]){escaped}(?![A-Za-z0-9_])"
            else:
                pattern = escaped
            if re.search(pattern, clean, flags=re.IGNORECASE):
                return canonical
        return None

    def get(self, agent_type: Optional[str], default: str = "gemini_agent") -> HeavyTaskAdapter:
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
    """Build the default registry containing all 10 Kiki Heavy-Path adapters."""
    return AdapterRegistry(
        adapters=[
            GeminiAgentAdapter(
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
            DoubaoWorkAdapter(),
        ]
    )

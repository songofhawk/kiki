"""Heavy-Path agent adapters package for Kiki."""

from .antigravity import AntigravityAdapter
from .base import (
    AdapterContext,
    AdapterInfo,
    AdapterResult,
    ExecutionProfile,
    HeavyTaskAdapter,
    StreamParseState,
    SubprocessCLIAdapter,
    ThinkingLevel,
    estimate_thinking_level,
)
from .claude_code import ClaudeCodeAdapter
from .codex import CodexAdapter
from .deepseek_harness import DeepSeekHarnessAdapter
from .internal_agent import InternalAgentAdapter
from .kcode import KCodeAdapter
from .qoder import QoderAdapter
from .registry import AdapterRegistry, create_default_registry
from .workbuddy import WorkBuddyAdapter
from .zcode import ZCodeAdapter

__all__ = [
    "AdapterContext",
    "AdapterInfo",
    "AdapterRegistry",
    "AdapterResult",
    "AntigravityAdapter",
    "ClaudeCodeAdapter",
    "CodexAdapter",
    "DeepSeekHarnessAdapter",
    "ExecutionProfile",
    "HeavyTaskAdapter",
    "InternalAgentAdapter",
    "KCodeAdapter",
    "QoderAdapter",
    "StreamParseState",
    "SubprocessCLIAdapter",
    "ThinkingLevel",
    "WorkBuddyAdapter",
    "ZCodeAdapter",
    "create_default_registry",
    "estimate_thinking_level",
]

"""Core abstractions for Kiki Heavy-Path Agent Adapters.

Provides:
- Automatic task complexity estimation -> (model, thinking_level) selection
- Unified AdapterContext bridging TaskManager, SQLite DB, EventBus, and HITL
- SubprocessCLIAdapter base class with streaming stdout/stderr/NDJSON parsing,
  process lifecycle cancellation, session resume, and HITL bridge
"""

from __future__ import annotations

import abc
import asyncio
import json
import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Literal, Optional

if TYPE_CHECKING:
    from ..config import Settings
    from ..db import Database
    from ..llm import KikiLLMClient
    from ..sse import EventBus
    from ..tools import WorkspaceToolExecutor

ThinkingLevel = Literal["low", "medium", "high"]
AdapterCategory = Literal["builtin", "cli", "hybrid"]

# Heuristic signals for automatic task complexity & thinking-strength selection.
_HIGH_COMPLEXITY_PATTERNS = [
    r"架构|重构|refactor|architect|migration|迁移|安全审计|security\s*audit",
    r"性能优化|benchmark|profile|并发|race\s*condition|死锁|内存泄漏",
    r"全仓|整个项目|跨模块|多文件|end[- ]to[- ]end|e2e|根因|root\s*cause",
    r"设计方案|技术方案|深度分析|deep\s*research|系统设计|算法优化",
]
_LOW_COMPLEXITY_PATTERNS = [
    r"^(总结|概括|列出|查看|解释|翻译|格式化|检查|summarize|list|explain|format|check)\b",
    r"简单介绍|一句话|快速看|目录结构|git\s*status|README",
]


@dataclass(frozen=True)
class ExecutionProfile:
    """Resolved model and thinking/reasoning intensity for a heavy task run."""

    model: str
    thinking_level: ThinkingLevel
    reason: str = ""


@dataclass(frozen=True)
class AdapterInfo:
    """Public metadata returned by GET /api/adapters for UI & discovery."""

    id: str
    name: str
    vendor: str
    description: str
    category: AdapterCategory
    available: bool
    binary_path: Optional[str]
    install_hint: str
    default_model: str
    models: dict[str, str]  # {"low": ..., "medium": ..., "high": ...}
    supports_resume: bool = True
    supports_streaming: bool = True
    supports_hitl: bool = True
    aliases: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "vendor": self.vendor,
            "description": self.description,
            "category": self.category,
            "available": self.available,
            "binary_path": self.binary_path,
            "install_hint": self.install_hint,
            "default_model": self.default_model,
            "models": dict(self.models),
            "supports_resume": self.supports_resume,
            "supports_streaming": self.supports_streaming,
            "supports_hitl": self.supports_hitl,
            "aliases": list(self.aliases),
        }


@dataclass
class AdapterResult:
    """Normalized result returned by an adapter upon completion."""

    summary: str
    raw_output: str = ""
    session_id: Optional[str] = None
    model_used: Optional[str] = None
    thinking_level_used: Optional[ThinkingLevel] = None
    metadata: dict[str, Any] = field(default_factory=dict)


def estimate_thinking_level(prompt: str) -> tuple[ThinkingLevel, str]:
    """Automatically choose thinking intensity (low/medium/high) from prompt complexity."""
    text = (prompt or "").strip()
    if not text:
        return "medium", "默认中等思考强度"

    high_hits = sum(
        1 for pat in _HIGH_COMPLEXITY_PATTERNS if re.search(pat, text, flags=re.IGNORECASE)
    )
    if high_hits >= 1 or len(text) >= 420 or text.count("\n") >= 8:
        return "high", "检测到复杂架构/重构/深度分析或长需求，自动提升至高思考强度"

    low_hits = sum(
        1 for pat in _LOW_COMPLEXITY_PATTERNS if re.search(pat, text, flags=re.IGNORECASE)
    )
    if low_hits >= 1 and len(text) <= 120:
        return "low", "检测到轻量检索/摘要类任务，自动采用低延迟模式"

    return "medium", "标准工程/分析任务，自动采用均衡思考强度"


@dataclass
class AdapterContext:
    """Execution context passed by TaskManager into an adapter."""

    run_id: str
    topic_id: str
    topic_title: str
    user_content: str
    workspace_root: Path
    artifacts_dir: Path
    db: "Database"
    event_bus: "EventBus"
    tool_executor: "WorkspaceToolExecutor"
    llm: "KikiLLMClient"
    settings: Optional["Settings"] = None
    model_override: Optional[str] = None
    thinking_override: Optional[ThinkingLevel] = None
    max_steps: int = 10
    timeout_sec: float = 600.0
    _hitl_handler: Optional[
        Callable[[str, list[dict[str, Any]]], Awaitable[dict[str, Any]]]
    ] = None
    _publish_run_update: Optional[Callable[[str], Optional[dict[str, Any]]]] = None
    _step_counter: int = 0
    session_id: Optional[str] = None

    def next_step_number(self) -> int:
        self._step_counter += 1
        return self._step_counter

    def update_current_step(self, label: str) -> None:
        self.db.update_session_run(self.run_id, current_step=label)
        if self._publish_run_update:
            self._publish_run_update(self.run_id)

    def start_step(self, action_type: str, detail: str) -> dict[str, Any]:
        step_num = self.next_step_number()
        step_log = self.db.add_step_log(
            run_id=self.run_id,
            step_number=step_num,
            action_type=action_type,
            detail=detail,
            status="in_progress",
        )
        self.event_bus.publish("step_log", {"run_id": self.run_id, "step_log": step_log})
        self.update_current_step(detail)
        return step_log

    def finish_step(
        self,
        step_id: int,
        *,
        status: Literal["success", "failed"] = "success",
        detail: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        updated = self.db.update_step_log(step_id, status=status, detail=detail)
        if updated:
            self.event_bus.publish("step_log", {"run_id": self.run_id, "step_log": updated})
        return updated

    def emit_raw_log(
        self,
        line: str,
        stream: Literal["stdout", "stderr", "ndjson"] = "stdout",
    ) -> None:
        self.event_bus.append_raw_log(self.run_id, line, stream=stream)

    def emit_event_log(self, payload: dict[str, Any]) -> None:
        self.emit_raw_log(json.dumps(payload, ensure_ascii=False), stream="ndjson")

    def register_artifact(
        self,
        filename: str,
        content: str,
        file_type: str = "markdown",
        description: str = "",
    ) -> str:
        return self.tool_executor.write_artifact(
            run_id=self.run_id,
            filename=filename,
            content=content,
            file_type=file_type,
            description=description,
        )

    async def request_hitl_approval(
        self,
        prompt: str,
        options: Optional[list[dict[str, Any]]] = None,
    ) -> dict[str, Any]:
        if self._hitl_handler is None:
            raise RuntimeError("HITL handler is not configured on AdapterContext")
        return await self._hitl_handler(prompt, options or [])


class HeavyTaskAdapter(abc.ABC):
    """Abstract base class for all Kiki Heavy-Path execution adapters."""

    agent_type: str = ""
    display_name: str = ""
    vendor: str = ""
    description: str = ""
    category: AdapterCategory = "cli"
    install_hint: str = ""
    aliases: tuple[str, ...] = ()
    supports_resume: bool = True
    supports_streaming: bool = True
    supports_hitl: bool = True

    # Tiered model map: override in subclasses
    model_tiers: dict[ThinkingLevel, str] = {
        "low": "default",
        "medium": "default",
        "high": "default",
    }
    env_model_var: Optional[str] = None

    def select_profile(
        self,
        prompt: str,
        model_override: Optional[str] = None,
        thinking_override: Optional[ThinkingLevel] = None,
    ) -> ExecutionProfile:
        """Select optimal (model, thinking_level) for the given prompt."""
        if thinking_override in ("low", "medium", "high"):
            level: ThinkingLevel = thinking_override
            reason = f"用户指定思考强度: {level}"
        else:
            level, reason = estimate_thinking_level(prompt)

        env_model = os.getenv(self.env_model_var, "").strip() if self.env_model_var else ""
        if model_override and model_override.strip():
            model = model_override.strip()
        elif env_model:
            model = env_model
        else:
            model = self.model_tiers.get(level) or self.model_tiers["medium"]

        return ExecutionProfile(model=model, thinking_level=level, reason=reason)

    @abc.abstractmethod
    def probe(self) -> AdapterInfo:
        """Inspect local availability and return public adapter metadata."""

    @abc.abstractmethod
    async def execute(self, ctx: AdapterContext) -> AdapterResult:
        """Execute a heavy task and return normalized AdapterResult."""

    async def cancel(self, run_id: str) -> None:
        """Best-effort cancellation hook for active runs."""
        return None


@dataclass
class StreamParseState:
    """Mutable accumulator populated while streaming subprocess output."""

    session_id: Optional[str] = None
    final_text_parts: list[str] = field(default_factory=list)
    stdout_lines: list[str] = field(default_factory=list)
    stderr_lines: list[str] = field(default_factory=list)
    active_step_id: Optional[int] = None
    pending_hitl_prompt: Optional[str] = None
    pending_hitl_options: list[dict[str, Any]] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)

    def best_summary(self) -> str:
        if self.final_text_parts:
            return "\n".join(p for p in self.final_text_parts if p.strip()).strip()
        non_empty = [line.strip() for line in self.stdout_lines if line.strip()]
        if non_empty:
            return non_empty[-1]
        return ""


class SubprocessCLIAdapter(HeavyTaskAdapter):
    """Reusable base adapter for external CLI-driven coding/workspace agents."""

    binary_candidates: tuple[str, ...] = ()
    env_bin_var: Optional[str] = None
    npx_package: Optional[str] = None
    category: AdapterCategory = "cli"

    def __init__(self) -> None:
        self._active_procs: dict[str, asyncio.subprocess.Process] = {}

    def resolve_binary(self) -> Optional[str]:
        """Locate the CLI binary via env override or PATH lookup."""
        if self.env_bin_var:
            override = os.getenv(self.env_bin_var, "").strip()
            if override:
                found = shutil.which(override)
                if found:
                    return found
                if Path(override).exists():
                    return override
        for candidate in self.binary_candidates:
            found = shutil.which(candidate)
            if found:
                return found
        return None

    def probe(self) -> AdapterInfo:
        bin_path = self.resolve_binary()
        return AdapterInfo(
            id=self.agent_type,
            name=self.display_name,
            vendor=self.vendor,
            description=self.description,
            category=self.category,
            available=bin_path is not None,
            binary_path=bin_path,
            install_hint=self.install_hint,
            default_model=self.model_tiers["medium"],
            models=dict(self.model_tiers),
            supports_resume=self.supports_resume,
            supports_streaming=self.supports_streaming,
            supports_hitl=self.supports_hitl,
            aliases=self.aliases,
        )

    @abc.abstractmethod
    def build_command(
        self,
        binary: str,
        ctx: AdapterContext,
        profile: ExecutionProfile,
    ) -> list[str]:
        """Build the argv list for initial headless execution."""

    def build_resume_command(
        self,
        binary: str,
        ctx: AdapterContext,
        profile: ExecutionProfile,
        session_id: str,
        decision_payload: dict[str, Any],
    ) -> Optional[list[str]]:
        """Build the argv list to resume a session after HITL approval."""
        return None

    def build_env(self, ctx: AdapterContext, profile: ExecutionProfile) -> dict[str, str]:
        """Construct subprocess environment variables."""
        env = os.environ.copy()
        env.setdefault("NO_COLOR", "1")
        env.setdefault("CI", "1")
        return env

    def parse_json_event(
        self,
        event: dict[str, Any],
        state: StreamParseState,
        ctx: AdapterContext,
    ) -> None:
        """Default NDJSON event parser; subclasses can override for tool-specific schemas."""
        # Capture session_id from common field names
        for key in ("session_id", "sessionId", "thread_id", "conversation_id"):
            val = event.get(key)
            if isinstance(val, str) and val.strip():
                state.session_id = val.strip()
                ctx.session_id = state.session_id

        ev_type = str(event.get("type") or event.get("event") or "").lower()

        # Tool use / action steps
        if ev_type in ("tool_use", "tool_call", "item.started", "command_execution"):
            tool_name = str(
                event.get("name")
                or event.get("tool")
                or (event.get("item") or {}).get("type")
                or "tool_call"
            )
            detail = f"{self.display_name} 正在执行 {tool_name}..."
            if state.active_step_id is not None:
                ctx.finish_step(state.active_step_id, status="success")
            step = ctx.start_step("tool_call", detail)
            state.active_step_id = step["id"]
            return

        if ev_type in ("tool_result", "item.completed") and state.active_step_id is not None:
            is_err = bool(event.get("is_error") or event.get("error"))
            ctx.finish_step(state.active_step_id, status="failed" if is_err else "success")
            state.active_step_id = None

        # HITL / approval requests emitted in structured stream
        if ev_type in ("permission_request", "approval_required", "hitl_required"):
            prompt = str(
                event.get("prompt")
                or event.get("message")
                or f"{self.display_name} 请求人工确认操作。"
            )
            state.pending_hitl_prompt = prompt
            raw_opts = event.get("options")
            if isinstance(raw_opts, list):
                state.pending_hitl_options = raw_opts
            return

        # Text / result extraction
        for field_name in ("result", "output_text", "text", "content", "message"):
            val = event.get(field_name)
            if isinstance(val, str) and val.strip():
                if ev_type in ("result", "final", "turn.completed", "assistant", "message", ""):
                    state.final_text_parts.append(val.strip())
                    break

    def parse_stdout_line(
        self,
        line: str,
        state: StreamParseState,
        ctx: AdapterContext,
    ) -> None:
        """Process a single stdout line (JSON or plain text)."""
        stripped = line.strip()
        if not stripped:
            return
        state.stdout_lines.append(stripped)

        if stripped.startswith("{") and stripped.endswith("}"):
            try:
                parsed = json.loads(stripped)
            except json.JSONDecodeError:
                ctx.emit_raw_log(stripped, stream="stdout")
                return
            if isinstance(parsed, dict):
                ctx.emit_raw_log(stripped, stream="ndjson")
                self.parse_json_event(parsed, state, ctx)
                return

        ctx.emit_raw_log(stripped, stream="stdout")

    def parse_stderr_line(
        self,
        line: str,
        state: StreamParseState,
        ctx: AdapterContext,
    ) -> None:
        stripped = line.strip()
        if not stripped:
            return
        state.stderr_lines.append(stripped)
        ctx.emit_raw_log(stripped, stream="stderr")

    async def _run_process(
        self,
        argv: list[str],
        ctx: AdapterContext,
        profile: ExecutionProfile,
        state: StreamParseState,
    ) -> int:
        ctx.emit_event_log(
            {
                "event": "cli_spawn",
                "adapter": self.agent_type,
                "argv": argv,
                "model": profile.model,
                "thinking_level": profile.thinking_level,
            }
        )
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(ctx.workspace_root),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=self.build_env(ctx, profile),
        )
        self._active_procs[ctx.run_id] = proc

        async def _drain_stdout() -> None:
            if proc.stdout is None:
                return
            while True:
                raw = await proc.stdout.readline()
                if not raw:
                    break
                line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                self.parse_stdout_line(line, state, ctx)

        async def _drain_stderr() -> None:
            if proc.stderr is None:
                return
            while True:
                raw = await proc.stderr.readline()
                if not raw:
                    break
                line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                self.parse_stderr_line(line, state, ctx)

        try:
            await asyncio.wait_for(
                asyncio.gather(_drain_stdout(), _drain_stderr(), proc.wait()),
                timeout=ctx.timeout_sec,
            )
            return proc.returncode if proc.returncode is not None else 0
        except asyncio.CancelledError:
            await self._terminate_proc(proc)
            raise
        except asyncio.TimeoutError:
            await self._terminate_proc(proc)
            raise TimeoutError(
                f"{self.display_name} 执行超时（>{int(ctx.timeout_sec)}s）"
            )
        finally:
            self._active_procs.pop(ctx.run_id, None)

    @staticmethod
    async def _terminate_proc(proc: asyncio.subprocess.Process) -> None:
        if proc.returncode is not None:
            return
        try:
            proc.terminate()
            await asyncio.wait_for(proc.wait(), timeout=3.0)
        except Exception:
            try:
                proc.kill()
                await proc.wait()
            except Exception:
                pass

    async def cancel(self, run_id: str) -> None:
        proc = self._active_procs.get(run_id)
        if proc is not None:
            await self._terminate_proc(proc)

    async def execute(self, ctx: AdapterContext) -> AdapterResult:
        profile = self.select_profile(
            ctx.user_content,
            model_override=ctx.model_override,
            thinking_override=ctx.thinking_override,
        )
        binary = self.resolve_binary()
        if not binary:
            raise RuntimeError(
                f"未检测到 {self.display_name} CLI（查找命令: {', '.join(self.binary_candidates)}）。"
                f"请先安装：{self.install_hint}"
            )

        init_step = ctx.start_step(
            "adapter_init",
            f"已启动 {self.display_name}（模型: {profile.model}，思考强度: {profile.thinking_level}）",
        )
        ctx.emit_event_log(
            {
                "event": "profile_selected",
                "adapter": self.agent_type,
                "model": profile.model,
                "thinking_level": profile.thinking_level,
                "reason": profile.reason,
            }
        )

        state = StreamParseState()
        argv = self.build_command(binary, ctx, profile)
        returncode = await self._run_process(argv, ctx, profile, state)

        if state.active_step_id is not None:
            ctx.finish_step(state.active_step_id, status="success" if returncode == 0 else "failed")
            state.active_step_id = None

        # Handle HITL resume loop if the CLI requested human confirmation
        if state.pending_hitl_prompt and self.supports_hitl:
            hitl_step = ctx.start_step(
                "hitl_wait",
                f"等待主人拍板：{state.pending_hitl_prompt[:50]}",
            )
            decision = await ctx.request_hitl_approval(
                state.pending_hitl_prompt,
                state.pending_hitl_options,
            )
            approved = bool(decision.get("approved", False))
            ctx.finish_step(
                hitl_step["id"],
                status="success",
                detail=f"主人已拍板：{'同意' if approved else '拒绝'} ({decision.get('decision', '')})",
            )
            if approved and state.session_id:
                resume_argv = self.build_resume_command(
                    binary, ctx, profile, state.session_id, decision
                )
                if resume_argv:
                    state.pending_hitl_prompt = None
                    state.pending_hitl_options = []
                    returncode = await self._run_process(resume_argv, ctx, profile, state)
                    if state.active_step_id is not None:
                        ctx.finish_step(
                            state.active_step_id,
                            status="success" if returncode == 0 else "failed",
                        )
                        state.active_step_id = None

        ctx.finish_step(init_step["id"], status="success" if returncode == 0 else "failed")

        if returncode != 0:
            err_tail = "\n".join(state.stderr_lines[-5:]) or "\n".join(state.stdout_lines[-3:])
            raise RuntimeError(
                f"{self.display_name} 退出码 {returncode}: {err_tail or '未知错误'}"
            )

        summary = state.best_summary() or f"{self.display_name} 已完成任务。"
        return AdapterResult(
            summary=summary,
            raw_output="\n".join(state.stdout_lines),
            session_id=state.session_id,
            model_used=profile.model,
            thinking_level_used=profile.thinking_level,
            metadata={"adapter": self.agent_type, "binary": binary},
        )

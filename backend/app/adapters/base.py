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
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import time
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
    history: list[dict[str, Any]] = field(default_factory=list)
    topic_summary: Optional[str] = None
    kiki_workspace_root: Optional[Path] = None
    topic_dir: Optional[Path] = None
    shared_dir: Optional[Path] = None
    rules_dir: Optional[Path] = None
    extra_dirs: list[Path] = field(default_factory=list)
    rule_files: list[Path] = field(default_factory=list)
    max_steps: int = 10
    timeout_sec: float = 600.0
    _hitl_handler: Optional[
        Callable[[str, list[dict[str, Any]]], Awaitable[dict[str, Any]]]
    ] = None
    _publish_run_update: Optional[Callable[[str], Optional[dict[str, Any]]]] = None
    _emit_interim_message: Optional[Callable[[str], Optional[dict[str, Any]]]] = None
    _step_counter: int = 0
    session_id: Optional[str] = None
    last_emitted_message_id: Optional[str] = None
    last_emitted_text: Optional[str] = None

    def build_workspace_context_block(self) -> str:
        """Return a concise workspace directory guide for cross-agent collaboration."""
        if not (self.topic_dir or self.shared_dir or self.rules_dir or self.extra_dirs):
            return ""
        lines: list[str] = [
            "【Kiki 工作空间与协作目录】",
            f"- 当前主工作目录 (cwd): {self.workspace_root}",
        ]
        if self.topic_dir is not None:
            lines.append(f"- 当前话题目录: {self.topic_dir}")
            lines.append(f"- 话题产物目录 (artifacts): {self.artifacts_dir}")
            lines.append(
                f"- 图片/截图交付约定: 如需向用户展示截图、登录二维码或生成的图片，"
                f"请直接保存为图片文件（.png/.jpg）到 {self.artifacts_dir}，切勿在回复中输出 base64 编码。"
            )
        if self.shared_dir is not None:
            lines.append(f"- 共享资源目录 (shared): {self.shared_dir}")
        if self.rules_dir is not None:
            if self.rule_files:
                rule_names = ", ".join(str(p) for p in self.rule_files[:6])
                lines.append(f"- 规则目录 (rules): {self.rules_dir}（可用规则文件: {rule_names}）")
            else:
                lines.append(f"- 规则目录 (rules): {self.rules_dir}")
        custom_extras = [
            p
            for p in self.extra_dirs
            if p not in {self.topic_dir, self.shared_dir, self.rules_dir, self.workspace_root}
        ]
        if custom_extras:
            lines.append("- 其他已挂载目录: " + ", ".join(str(p) for p in custom_extras))
        return "\n".join(lines)

    def build_task_prompt(self, max_history: int = 14) -> str:
        """Build an execution prompt enriched with workspace context and recent topic history."""
        ws_block = self.build_workspace_context_block()
        if not self.history:
            if not ws_block:
                return self.user_content
            return (
                f"当前话题：{self.topic_title}\n"
                f"{ws_block}\n\n"
                f"用户当前任务：{self.user_content}"
            )
        from ..llm import KikiLLMClient

        history_text = KikiLLMClient._format_context_lines(self.history[-max_history:])
        header_parts = [f"当前话题：{self.topic_title}"]
        if self.topic_summary and self.topic_summary.strip():
            header_parts.append(f"话题摘要：{self.topic_summary.strip()}")
        if ws_block:
            header_parts.append(ws_block)
        header = "\n".join(header_parts)
        return (
            f"{header}\n"
            f"本话题近期对话与任务记录（按时间从早到晚，供理解指代与上下文）：\n"
            f"{history_text}\n\n"
            f"用户当前任务：{self.user_content}"
        )

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
        max_len = 8192
        if len(line) > max_len:
            line = f"{line[:max_len]}… [truncated {len(line) - max_len} chars]"
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

    def register_binary_artifact(
        self,
        filename: str,
        data: bytes,
        file_type: str = "image",
        description: str = "",
    ) -> dict[str, Any]:
        return self.tool_executor.write_binary_artifact(
            run_id=self.run_id,
            filename=filename,
            data=data,
            file_type=file_type,
            description=description,
        )

    def emit_interim_message(self, content: str) -> Optional[dict[str, Any]]:
        cleaned = (content or "").strip()
        if not cleaned or self._emit_interim_message is None:
            return None
        msg = self._emit_interim_message(cleaned)
        if msg and msg.get("id"):
            self.last_emitted_message_id = str(msg["id"])
            self.last_emitted_text = cleaned
        return msg

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
    extra_binary_paths: tuple[str, ...] = ()
    env_bin_var: Optional[str] = None
    npx_package: Optional[str] = None
    category: AdapterCategory = "cli"

    def __init__(self) -> None:
        self._active_procs: dict[str, asyncio.subprocess.Process] = {}

    @staticmethod
    def _is_executable_file(path: str) -> bool:
        try:
            p = Path(path)
            return p.is_file() and os.access(p, os.X_OK)
        except OSError:
            return False

    @staticmethod
    def _common_bin_dirs() -> list[str]:
        dirs = [
            "/opt/homebrew/bin",
            "/usr/local/bin",
            os.path.expanduser("~/.local/bin"),
            os.path.expanduser("~/bin"),
            os.path.expanduser("~/.npm-global/bin"),
            os.path.expanduser("~/.pnpm"),
            os.path.expanduser("~/.bun/bin"),
            os.path.expanduser("~/.cargo/bin"),
            os.path.expanduser("~/.volta/bin"),
        ]
        try:
            nvm_bins = sorted(
                glob.glob(os.path.expanduser("~/.nvm/versions/node/*/bin")),
                reverse=True,
            )
            dirs.extend(nvm_bins)
        except OSError:
            pass
        return dirs

    def resolve_binary(self) -> Optional[str]:
        """Locate the CLI binary via env override, PATH, common bin dirs, or app bundle paths."""
        if self.env_bin_var:
            override = os.getenv(self.env_bin_var, "").strip()
            if override:
                found = shutil.which(override)
                if found:
                    return found
                expanded = os.path.expanduser(override)
                if self._is_executable_file(expanded):
                    return expanded
        for candidate in self.binary_candidates:
            found = shutil.which(candidate)
            if found:
                return found
        for bin_dir in self._common_bin_dirs():
            for candidate in self.binary_candidates:
                candidate_path = os.path.join(bin_dir, candidate)
                if self._is_executable_file(candidate_path):
                    return candidate_path
        for raw_pattern in self.extra_binary_paths:
            expanded_pattern = os.path.expanduser(raw_pattern)
            try:
                matches = sorted(glob.glob(expanded_pattern), reverse=True)
            except OSError:
                matches = []
            if not matches and self._is_executable_file(expanded_pattern):
                return expanded_pattern
            for matched in matches:
                if self._is_executable_file(matched):
                    return matched
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

    @staticmethod
    def _detect_macos_system_proxies() -> dict[str, str]:
        """Read macOS system proxy (scutil --proxy) when env proxy vars are absent."""
        if sys.platform != "darwin":
            return {}
        try:
            proc = subprocess.run(
                ["scutil", "--proxy"],
                capture_output=True,
                text=True,
                timeout=1.5,
                check=False,
            )
            if proc.returncode != 0 or not proc.stdout:
                return {}
        except Exception:
            return {}

        text = proc.stdout

        def _val(key: str) -> Optional[str]:
            m = re.search(rf"^\s*{re.escape(key)}\s*:\s*(.+)$", text, flags=re.MULTILINE)
            return m.group(1).strip() if m else None

        proxies: dict[str, str] = {}
        if _val("HTTPEnable") == "1":
            host, port = _val("HTTPProxy"), _val("HTTPPort")
            if host and port:
                proxies["HTTP_PROXY"] = f"http://{host}:{port}"
        if _val("HTTPSEnable") == "1":
            host, port = _val("HTTPSProxy"), _val("HTTPSPort")
            if host and port:
                proxies["HTTPS_PROXY"] = f"http://{host}:{port}"
        if _val("SOCKSEnable") == "1":
            host, port = _val("SOCKSProxy"), _val("SOCKSPort")
            if host and port:
                proxies["ALL_PROXY"] = f"socks5://{host}:{port}"
        return proxies

    def build_env(self, ctx: AdapterContext, profile: ExecutionProfile) -> dict[str, str]:
        """Construct subprocess environment variables with Kiki workspace paths."""
        env = os.environ.copy()
        env.setdefault("NO_COLOR", "1")
        env.setdefault("CI", "1")
        env["KIKI_TOPIC_ID"] = ctx.topic_id
        env["KIKI_PRIMARY_DIR"] = str(ctx.workspace_root)
        env["KIKI_ARTIFACTS_DIR"] = str(ctx.artifacts_dir)
        if ctx.kiki_workspace_root is not None:
            env["KIKI_WORKSPACE_ROOT"] = str(ctx.kiki_workspace_root)
        if ctx.topic_dir is not None:
            env["KIKI_TOPIC_DIR"] = str(ctx.topic_dir)
        if ctx.shared_dir is not None:
            env["KIKI_SHARED_DIR"] = str(ctx.shared_dir)
        if ctx.rules_dir is not None:
            env["KIKI_RULES_DIR"] = str(ctx.rules_dir)
        if ctx.extra_dirs:
            env["KIKI_EXTRA_DIRS"] = os.pathsep.join(str(p) for p in ctx.extra_dirs)
        if not any(
            env.get(k)
            for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")
        ):
            for k, v in self._detect_macos_system_proxies().items():
                env.setdefault(k, v)
                env.setdefault(k.lower(), v)
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
        max_keep = 8192
        state.stdout_lines.append(
            stripped if len(stripped) <= max_keep else f"{stripped[:max_keep]}…"
        )

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
                "cwd": str(ctx.workspace_root),
                "extra_dirs": [str(p) for p in ctx.extra_dirs],
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
            limit=4 * 1024 * 1024,
        )
        self._active_procs[ctx.run_id] = proc

        async def _iter_lines(stream: Optional[asyncio.StreamReader]) -> Any:
            if stream is None:
                return
            buf = bytearray()
            while True:
                chunk = await stream.read(65536)
                if not chunk:
                    if buf:
                        yield bytes(buf).decode("utf-8", errors="replace").rstrip("\r\n")
                    break
                buf.extend(chunk)
                while True:
                    nl = buf.find(b"\n")
                    if nl < 0:
                        break
                    raw_line = bytes(buf[:nl])
                    del buf[: nl + 1]
                    yield raw_line.decode("utf-8", errors="replace").rstrip("\r")

        async def _drain_stdout() -> None:
            async for line in _iter_lines(proc.stdout):
                self.parse_stdout_line(line, state, ctx)

        async def _drain_stderr() -> None:
            async for line in _iter_lines(proc.stderr):
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

    async def after_process_success(
        self,
        ctx: AdapterContext,
        state: StreamParseState,
    ) -> None:
        """Optional hook called after CLI exits with returncode 0 (e.g. artifact extraction)."""
        return None

    async def execute(self, ctx: AdapterContext) -> AdapterResult:
        profile = self.select_profile(
            ctx.user_content,
            model_override=ctx.model_override,
            thinking_override=ctx.thinking_override,
        )
        binary = self.resolve_binary()
        if not binary:
            label = (
                self.display_name
                if self.display_name.upper().endswith("CLI")
                else f"{self.display_name} CLI"
            )
            raise RuntimeError(
                f"未检测到 {label}（查找命令: {', '.join(self.binary_candidates)}）。"
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
        state.extra["started_at"] = time.time()
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
                status="success" if approved else "failed",
                detail=f"主人已拍板：{'同意' if approved else '拒绝'} ({decision.get('decision', '')})",
            )
            if not approved:
                ctx.finish_step(init_step["id"], status="failed")
                raise asyncio.CancelledError("用户拒绝了人工确认操作，任务已终止")
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

        await self.after_process_success(ctx, state)

        summary = state.best_summary() or f"{self.display_name} 已完成任务。"
        return AdapterResult(
            summary=summary,
            raw_output="\n".join(state.stdout_lines),
            session_id=state.session_id,
            model_used=profile.model,
            thinking_level_used=profile.thinking_level,
            metadata={"adapter": self.agent_type, "binary": binary},
        )

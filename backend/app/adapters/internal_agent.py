"""Built-in Internal Agent adapter (Kiki native Heavy-Path executor)."""

from __future__ import annotations

import asyncio
import json
import math
from typing import Any, Optional

from ..desktop.computer_use import (
    COMPUTER_USE_TOOL_DECLARATIONS,
    COMPUTER_USE_TOOL_NAMES,
    ComputerUseExecutor,
)
from ..llm import KikiLLMClient, parse_retry_after_seconds
from ..tools import AGENT_TOOL_DECLARATIONS
from .base import (
    AdapterContext,
    AdapterInfo,
    AdapterResult,
    ExecutionProfile,
    HeavyTaskAdapter,
    ThinkingLevel,
)

AGENT_SYSTEM_PROMPT = """你是 Kiki 的后台重型执行引擎 (internal_agent)。
你可以使用工作区、联网与桌面 ComputerUse 工具（list_files, read_file, write_artifact, run_shell, web_search, fetch_webpage, computer_screenshot, computer_action, request_human_approval, request_desktop_takeover）来完成用户的复杂任务。

执行规范：
1. 先探索再操作，合理调用工具获取真实信息；需要最新资讯、文档或外部资料时，优先调用 `web_search` 与 `fetch_webpage`。
2. 若需要产出报告、分析文档或代码，使用 `write_artifact` 保存为产物文件。
3. 若任务涉及高风险操作、破坏性变更或需要用户明确拍板确认，必须调用 `request_human_approval` 挂起等待用户决策。
4. 若任务遇到必须人工在服务器桌面亲自操作的场景（如登录/二步验证、图形验证码、滑块校验、OAuth 授权、桌面软件弹窗或系统权限确认），必须立即调用 `request_desktop_takeover` 挂起并交由主人接管服务器桌面（切勿尝试绕过验证码），待主人处理完成后继续执行。
5. **ComputerUse 桌面视觉操作与自媒体发布规范**（当可用 `computer_screenshot` / `computer_action` 时）：
   - 坐标系统统一为 `0..1000` 归一化网格，`(0, 0)` 为屏幕左上角，`(1000, 1000)` 为右下角；每次操作前先观察当前截图，操作后根据返回的最新截图确认状态再进行下一步。
   - 发布自媒体文章/视频（如小红书、微信公众号、抖音、B站、知乎、YouTube、X 等）推荐流程：
     a) 先在工作区准备好正文、标题、标签或核实本地视频/封面图路径；
     b) 优先使用 `computer_action(action="open_url", url=...)` 直接打开创作者后台，或 `open_app` 激活目标桌面应用；
     c) 若页面需要登录、扫码、二步验证或人机验证码，立即调用 `request_desktop_takeover` 请主人处理后继续；
     d) 输入中文标题、长文正文或简介时，先点击输入框，再调用 `computer_action(action="type", text=...)`（会自动通过剪贴板粘贴以保证中文与多行文本完整准确）；
     e) 上传本地视频或封面图弹出 macOS 文件选择框时，使用 `computer_action(action="select_file_in_dialog", file_path=...)` 选择文件，随后可用 `action="wait"` 等待视频上传与转码完成；
     f) 在点击最终「发布 / Publish / 提交」按钮前（除非用户明确说明“直接发布无需确认”），先带 `save_artifact=True` 截取草稿预览图，再调用 `request_human_approval` 请主人确认无误后发布。
6. 任务完成时，用简洁、信息密度高的中文给出最终总结（**严格控制在 200 字以内**），直接说明结论与产出物。
7. 你当前**不具备**真实图片生成（PNG/JPG/WebP）工具；若用户要求生成图片或插画，**严禁**用 ASCII 字符画或 Markdown 文档冒充图片，应直接诚实说明无法生成真实图片，并建议用户改用支持生图的执行引擎（例如 Codex）。
"""


def _action_type_for_tool(tool_name: str) -> str:
    if tool_name == "run_shell":
        return "shell_exec"
    if tool_name == "write_artifact":
        return "file_edit"
    if tool_name == "request_human_approval":
        return "hitl_wait"
    if tool_name == "request_desktop_takeover":
        return "desktop_takeover"
    if tool_name in COMPUTER_USE_TOOL_NAMES:
        return "computer_use"
    return "tool_call"


def _step_label_for_tool(tool_name: str, args: dict[str, Any]) -> str:
    if tool_name == "list_files":
        return f"正在浏览目录 {args.get('path', '.')}..."
    if tool_name == "read_file":
        return f"正在读取文件 {args.get('path', '')}..."
    if tool_name == "write_artifact":
        return f"正在生成产物 {args.get('filename', 'artifact')}..."
    if tool_name == "run_shell":
        cmd = str(args.get("command", ""))
        short_cmd = cmd if len(cmd) <= 45 else cmd[:45] + "…"
        return f"正在执行命令 `{short_cmd}`..."
    if tool_name == "web_search":
        q = str(args.get("query", ""))
        short_q = q if len(q) <= 36 else q[:36] + "…"
        return f"正在联网搜索「{short_q}」..."
    if tool_name == "fetch_webpage":
        u = str(args.get("url", ""))
        short_u = u if len(u) <= 42 else u[:42] + "…"
        return f"正在读取网页 {short_u}..."
    if tool_name == "computer_screenshot":
        return "正在截取桌面屏幕观察画面..."
    if tool_name == "computer_action":
        act = str(args.get("action") or "").strip().lower()
        if act == "open_url":
            u = str(args.get("url", ""))
            short_u = u if len(u) <= 42 else u[:42] + "…"
            return f"正在浏览器打开 {short_u}..."
        if act == "open_app":
            return f"正在打开应用 {args.get('app_name', '')}..."
        if act in ("click", "double_click", "right_click", "move"):
            verb = {"click": "点击", "double_click": "双击", "right_click": "右键点击", "move": "移动鼠标到"}[act]
            return f"正在{verb}屏幕 ({args.get('x', '?')}, {args.get('y', '?')})..."
        if act == "drag":
            return f"正在拖拽 ({args.get('x', '?')}, {args.get('y', '?')}) → ({args.get('end_x', '?')}, {args.get('end_y', '?')})..."
        if act == "scroll":
            return f"正在滚动屏幕 (dx={args.get('delta_x', 0)}, dy={args.get('delta_y', 0)})..."
        if act == "type":
            txt = str(args.get("text", ""))
            return f"正在输入文本（{len(txt)} 字）..."
        if act == "key":
            mods = args.get("modifiers") or []
            combo = "+".join([*(str(m) for m in mods), str(args.get("key", ""))])
            return f"正在按键 `{combo}`..."
        if act == "wait":
            return f"正在等待 {args.get('seconds', 1)}s 观察页面状态..."
        if act == "select_file_in_dialog":
            fp = str(args.get("file_path", ""))
            short_fp = fp if len(fp) <= 42 else "…" + fp[-40:]
            return f"正在文件选择框中选中 {short_fp}..."
        if act == "switch_space":
            return "正在切换桌面空间..."
        if act in ("get_clipboard", "set_clipboard"):
            return "正在读写服务器剪贴板..."
        return f"正在执行桌面操作 {act or 'action'}..."
    if tool_name == "request_human_approval":
        prompt = str(args.get("prompt", "等待人工确认"))
        return f"等待主人拍板：{prompt[:50]}"
    if tool_name == "request_desktop_takeover":
        reason = str(args.get("reason", "需要接管服务器桌面"))
        return f"等待主人接管桌面：{reason[:50]}"
    return f"正在执行工具 {tool_name}..."


_NON_RETRYABLE_ERROR_MARKERS = (
    "limit: 0",
    "per day",
    "requests per day",
    "daily quota",
    "rpd",
    "billing",
    "401",
    "403",
    "404",
    "unauthorized",
    "invalid api key",
    "api key not valid",
    "not found",
)

_RATE_LIMIT_ERROR_MARKERS = (
    "429",
    "resource_exhausted",
    "too_many_requests",
    "too many requests",
    "rate limit",
    "quota exceeded",
    "quota",
)

_TRANSIENT_OVERLOAD_MARKERS = (
    "503",
    "service_unavailable",
    "high demand",
    "overloaded",
    "server disconnected",
    "timed out",
    "timeout",
    "temporarily unavailable",
)


def _classify_retryable_llm_error(
    exc: Exception,
    retry_index: int,
    *,
    max_wait_seconds: float = 90.0,
) -> Optional[tuple[float, str]]:
    """Return (wait_seconds, reason_label) if exc is a short-lived transient LLM error."""
    raw = str(exc or "")
    lower = raw.lower()
    if any(marker in lower for marker in _NON_RETRYABLE_ERROR_MARKERS):
        return None

    is_rate_limit = any(marker in lower for marker in _RATE_LIMIT_ERROR_MARKERS)
    is_overload = isinstance(exc, (TimeoutError, asyncio.TimeoutError)) or any(
        marker in lower for marker in _TRANSIENT_OVERLOAD_MARKERS
    )
    if not (is_rate_limit or is_overload):
        return None

    parsed_delay = parse_retry_after_seconds(raw)
    if parsed_delay is not None:
        if parsed_delay > max_wait_seconds:
            return None
        buffer_sec = 1.5 if is_rate_limit else 1.0
        wait_seconds = min(max_wait_seconds, max(1.0, parsed_delay + buffer_sec))
    elif is_rate_limit:
        defaults = (15.0, 30.0, 45.0)
        wait_seconds = min(max_wait_seconds, defaults[min(retry_index, len(defaults) - 1)])
    else:
        defaults = (4.0, 10.0, 20.0)
        wait_seconds = min(max_wait_seconds, defaults[min(retry_index, len(defaults) - 1)])

    reason = "上游触发限流" if is_rate_limit else "上游服务暂时繁忙"
    return wait_seconds, reason


class InternalAgentAdapter(HeavyTaskAdapter):
    """Built-in Kiki internal agent."""

    max_turn_retries: int = 3
    max_retry_wait_seconds: float = 90.0
    retry_countdown_tick_seconds: float = 5.0

    agent_type = "internal_agent"
    display_name = "Kiki 内置 (Internal Agent)"
    vendor = "Kiki"
    description = (
        "Kiki 默认内置异步执行引擎，支持受控工作区读写、Shell、联网检索、"
        "ComputerUse 桌面视觉操作（如发布自媒体文章/视频）、产物生成与 HITL 人工拍板/桌面接管。"
    )
    category = "builtin"
    install_hint = "在根目录 .env 中配置 GEMINI_API_KEY 即可使用"
    aliases = ("internal", "builtin", "default", "gemini_agent", "gemini")
    env_model_var = "GEMINI_MODEL"
    model_tiers: dict[ThinkingLevel, str] = {
        "low": "gemini-3.5-flash-lite",
        "medium": "gemini-3.8-flash",
        "high": "gemini-3.8-flash",
    }

    def __init__(self, default_model: Optional[str] = None, api_key_configured: bool = True) -> None:
        self._default_model = default_model
        self._api_key_configured = api_key_configured

    def select_profile(
        self,
        prompt: str,
        model_override: Optional[str] = None,
        thinking_override: Optional[ThinkingLevel] = None,
    ) -> ExecutionProfile:
        profile = super().select_profile(
            prompt,
            model_override=model_override or self._default_model,
            thinking_override=thinking_override,
        )
        return profile

    def probe(self) -> AdapterInfo:
        default_m = self._default_model or self.model_tiers["medium"]
        return AdapterInfo(
            id=self.agent_type,
            name=self.display_name,
            vendor=self.vendor,
            description=self.description,
            category="builtin",
            available=bool(self._api_key_configured),
            binary_path="in-process",
            install_hint=self.install_hint,
            default_model=default_m,
            models=dict(self.model_tiers),
            supports_resume=True,
            supports_streaming=True,
            supports_hitl=True,
            aliases=self.aliases,
        )

    async def _sleep_with_countdown(
        self,
        ctx: AdapterContext,
        *,
        wait_seconds: float,
        reason: str,
        attempt: int,
        max_retries: int,
    ) -> None:
        tick = max(0.05, float(self.retry_countdown_tick_seconds))
        remaining = max(0.0, float(wait_seconds))
        while remaining > 0.0:
            step_sleep = min(tick, remaining)
            await asyncio.sleep(step_sleep)
            remaining = max(0.0, remaining - step_sleep)
            if remaining >= 1.0:
                ctx.update_current_step(
                    f"{reason}，还需等待 {math.ceil(remaining)} 秒后自动继续（第 {attempt}/{max_retries} 次重试）..."
                )

    async def _call_agent_turn_with_retry(
        self,
        ctx: AdapterContext,
        turn_kwargs: dict[str, Any],
        *,
        turn: int,
    ) -> Any:
        max_retries = max(0, int(self.max_turn_retries))
        for retry_index in range(max_retries + 1):
            try:
                return await ctx.llm.run_agent_turn(**turn_kwargs)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if retry_index >= max_retries:
                    raise
                classified = _classify_retryable_llm_error(
                    exc,
                    retry_index,
                    max_wait_seconds=self.max_retry_wait_seconds,
                )
                if classified is None:
                    raise
                wait_seconds, reason = classified
                attempt = retry_index + 1
                wait_secs_display = math.ceil(wait_seconds)
                wait_step = ctx.start_step(
                    "rate_limit_wait",
                    f"{reason}，等待 {wait_secs_display} 秒后自动继续（第 {attempt}/{max_retries} 次重试）...",
                )
                ctx.emit_event_log(
                    {
                        "event": "llm_retry_wait",
                        "turn": turn,
                        "attempt": attempt,
                        "max_retries": max_retries,
                        "wait_seconds": round(wait_seconds, 2),
                        "reason": reason,
                        "error": str(exc)[:240],
                    }
                )
                try:
                    await self._sleep_with_countdown(
                        ctx,
                        wait_seconds=wait_seconds,
                        reason=reason,
                        attempt=attempt,
                        max_retries=max_retries,
                    )
                except asyncio.CancelledError:
                    ctx.finish_step(
                        wait_step["id"],
                        status="failed",
                        detail="等待重试期间任务已取消",
                    )
                    raise
                ctx.finish_step(
                    wait_step["id"],
                    status="success",
                    detail=f"冷却结束，正在自动继续第 {turn} 步（第 {attempt}/{max_retries} 次重试）",
                )
                ctx.update_current_step(
                    f"冷却结束，正在自动重试第 {turn} 步（第 {attempt}/{max_retries} 次）..."
                )

    async def execute(self, ctx: AdapterContext) -> AdapterResult:
        profile = self.select_profile(
            ctx.user_content,
            model_override=ctx.model_override,
            thinking_override=ctx.thinking_override,
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

        cu_enabled = bool(
            ctx.desktop_manager is not None
            and (ctx.settings.computer_use_enabled if ctx.settings else True)
        )
        active_tools = (
            [*AGENT_TOOL_DECLARATIONS, *COMPUTER_USE_TOOL_DECLARATIONS]
            if cu_enabled
            else list(AGENT_TOOL_DECLARATIONS)
        )
        cu_executor = (
            ComputerUseExecutor(
                ctx.desktop_manager,
                ctx.tool_executor,
                max_width=ctx.settings.computer_use_screenshot_width if ctx.settings else 1280,
                quality=ctx.settings.computer_use_screenshot_quality if ctx.settings else 70,
            )
            if cu_enabled and ctx.desktop_manager is not None
            else None
        )

        prompt_text = ctx.build_task_prompt()
        if prompt_text != ctx.user_content:
            input_data: Any = prompt_text
        else:
            input_data = f"当前话题：{ctx.topic_title}\n用户任务：{ctx.user_content}"
        previous_interaction_id: Optional[str] = None
        final_text: str = ""
        completed_cleanly = False
        used_computer_use = False
        step_budget = ctx.max_steps
        cu_max_steps = max(
            ctx.max_steps,
            ctx.settings.computer_use_max_steps if ctx.settings else 30,
        )

        turn = 0
        while turn < step_budget:
            if turn == 0:
                early_steers = ctx.drain_steering_messages()
                if early_steers:
                    early_prompt = ctx.format_steering_prompt(early_steers)
                    if early_prompt and isinstance(input_data, str):
                        input_data = f"{input_data}\n\n{early_prompt}"
                        ctx.emit_event_log(
                            {"event": "steering_injected", "turn": 1, "count": len(early_steers)}
                        )
            turn += 1
            ctx.emit_event_log({"event": "llm_turn", "turn": turn})
            turn_kwargs: dict[str, Any] = {
                "input_data": input_data,
                "tools": active_tools,
                "system_instruction": AGENT_SYSTEM_PROMPT,
                "previous_interaction_id": previous_interaction_id,
                "thinking_level": profile.thinking_level,
            }
            if ctx.model_override:
                turn_kwargs["model"] = profile.model
            interaction = await self._call_agent_turn_with_retry(
                ctx,
                turn_kwargs,
                turn=turn,
            )
            previous_interaction_id = getattr(interaction, "id", None)
            ctx.session_id = previous_interaction_id

            steps = getattr(interaction, "steps", None) or []
            function_calls = [s for s in steps if getattr(s, "type", None) == "function_call"]

            if not function_calls:
                late_steers = ctx.drain_steering_messages()
                if late_steers:
                    steering_prompt = ctx.format_steering_prompt(late_steers)
                    if steering_prompt:
                        ctx.emit_event_log(
                            {"event": "steering_injected", "turn": turn, "count": len(late_steers)}
                        )
                        steer_step = ctx.start_step(
                            "user_steer",
                            "收到主人补充指令，继续整合处理...",
                        )
                        ctx.finish_step(steer_step["id"], status="success")
                        if turn >= step_budget:
                            step_budget = min(step_budget + 4, 40)
                        input_data = steering_prompt
                        continue
                final_text = getattr(interaction, "output_text", None) or ""
                completed_cleanly = True
                break

            function_results: list[dict[str, Any]] = []
            for call in function_calls:
                call_id = getattr(call, "id", f"call-{ctx._step_counter + 1}")
                tool_name = getattr(call, "name", "unknown")
                raw_args = getattr(call, "arguments", None)
                args = raw_args if isinstance(raw_args, dict) else {}

                step_label = _step_label_for_tool(tool_name, args)
                action_type = _action_type_for_tool(tool_name)
                step_log = ctx.start_step(action_type, step_label)

                ctx.emit_event_log(
                    {"event": "tool_call", "tool": tool_name, "arguments": args}
                )

                if tool_name == "request_human_approval":
                    prompt = str(args.get("prompt") or "Kiki 需要你拍板确认下一步操作。")
                    raw_options = args.get("options")
                    options = raw_options if isinstance(raw_options, list) else []
                    decision_payload = await ctx.request_hitl_approval(prompt, options)

                    approved = bool(decision_payload.get("approved", False))
                    decision_val = str(decision_payload.get("decision", ""))
                    comment = decision_payload.get("comment") or ""
                    result_str = json.dumps(
                        {
                            "approved": approved,
                            "decision": decision_val,
                            "comment": comment,
                        },
                        ensure_ascii=False,
                    )
                    ctx.finish_step(
                        step_log["id"],
                        status="success",
                        detail=f"主人已拍板：{'同意' if approved else '拒绝'} ({decision_val})",
                    )
                    function_results.append(
                        {
                            "type": "function_result",
                            "call_id": call_id,
                            "name": tool_name,
                            "result": result_str,
                            "is_error": False,
                        }
                    )
                elif tool_name == "request_desktop_takeover":
                    reason = str(args.get("reason") or "Kiki 需要你接管服务器桌面完成手动操作。")
                    instructions = str(args.get("instructions") or "").strip() or None
                    raw_url = str(args.get("url") or "").strip()
                    safe_url = raw_url if raw_url.startswith(("http://", "https://")) else None
                    takeover_options = [
                        {"label": "我已处理完毕，继续执行", "value": "takeover_done", "approved": True, "style": "primary"},
                        {"label": "无法处理，终止此步骤", "value": "takeover_cancel", "approved": False, "style": "danger"},
                    ]
                    ctx.event_bus.set_hitl_meta(
                        ctx.run_id,
                        {
                            "kind": "desktop_takeover",
                            "instructions": instructions,
                            "url": safe_url,
                        },
                    )
                    decision_payload = await ctx.request_hitl_approval(reason, takeover_options)

                    completed = bool(decision_payload.get("approved", False))
                    decision_val = str(decision_payload.get("decision", ""))
                    comment = decision_payload.get("comment") or ""
                    result_str = json.dumps(
                        {
                            "desktop_takeover_completed": completed,
                            "decision": decision_val,
                            "user_note": comment,
                        },
                        ensure_ascii=False,
                    )
                    ctx.finish_step(
                        step_log["id"],
                        status="success" if completed else "failed",
                        detail=f"桌面接管已结束：{'主人已处理完毕' if completed else '主人取消处理'}",
                    )
                    function_results.append(
                        {
                            "type": "function_result",
                            "call_id": call_id,
                            "name": tool_name,
                            "result": result_str,
                            "is_error": False,
                        }
                    )
                elif tool_name in COMPUTER_USE_TOOL_NAMES:
                    if not used_computer_use:
                        used_computer_use = True
                        step_budget = cu_max_steps
                    if cu_executor is None:
                        result_text = json.dumps(
                            {"ok": False, "error": "ComputerUse 桌面控制当前未启用或不可用。"},
                            ensure_ascii=False,
                        )
                        ctx.finish_step(step_log["id"], status="failed")
                        ctx.emit_event_log(
                            {
                                "event": "tool_result",
                                "tool": tool_name,
                                "is_error": True,
                                "preview": result_text[:240],
                            }
                        )
                        function_results.append(
                            {
                                "type": "function_result",
                                "call_id": call_id,
                                "name": tool_name,
                                "result": result_text,
                                "is_error": True,
                            }
                        )
                    else:
                        cu_res = await cu_executor.execute(
                            run_id=ctx.run_id,
                            name=tool_name,
                            arguments=args,
                        )
                        ctx.finish_step(
                            step_log["id"],
                            status="failed" if cu_res.is_error else "success",
                        )
                        ctx.emit_event_log(
                            {
                                "event": "tool_result",
                                "tool": tool_name,
                                "is_error": cu_res.is_error,
                                "has_screenshot": bool(cu_res.image_bytes),
                                "preview": cu_res.text[:240],
                            }
                        )
                        function_results.append(
                            {
                                "type": "function_result",
                                "call_id": call_id,
                                "name": tool_name,
                                "result": cu_res.to_function_result_value(),
                                "is_error": cu_res.is_error,
                            }
                        )
                else:
                    result_str, is_error = await ctx.tool_executor.execute(
                        run_id=ctx.run_id,
                        name=tool_name,
                        arguments=args,
                    )
                    ctx.finish_step(
                        step_log["id"],
                        status="failed" if is_error else "success",
                    )
                    ctx.emit_event_log(
                        {
                            "event": "tool_result",
                            "tool": tool_name,
                            "is_error": is_error,
                            "preview": result_str[:240],
                        }
                    )
                    function_results.append(
                        {
                            "type": "function_result",
                            "call_id": call_id,
                            "name": tool_name,
                            "result": result_str,
                            "is_error": is_error,
                        }
                    )

            mid_steers = ctx.drain_steering_messages()
            if mid_steers:
                steering_prompt = ctx.format_steering_prompt(mid_steers)
                if steering_prompt:
                    ctx.emit_event_log(
                        {"event": "steering_injected", "turn": turn, "count": len(mid_steers)}
                    )
                    steer_step = ctx.start_step(
                        "user_steer",
                        "已将主人补充指令注入当前任务，调整后续步骤...",
                    )
                    ctx.finish_step(steer_step["id"], status="success")
                    if turn >= step_budget - 1:
                        step_budget = min(step_budget + 4, 40)
                    function_results.append({"type": "text", "text": steering_prompt})

            input_data = function_results

        if not completed_cleanly:
            ctx.emit_event_log({"event": "max_steps_reached", "max_steps": step_budget})
            try:
                synth_kwargs: dict[str, Any] = {
                    "input_data": input_data,
                    "tools": [],
                    "system_instruction": (
                        f"{AGENT_SYSTEM_PROMPT}\n"
                        "注意：已达到最大工具调用步数上限，请直接基于已有信息用 200 字以内总结当前进展与剩余事项，不要再调用工具。"
                    ),
                    "previous_interaction_id": previous_interaction_id,
                    "thinking_level": profile.thinking_level,
                }
                if ctx.model_override:
                    synth_kwargs["model"] = profile.model
                synth = await self._call_agent_turn_with_retry(
                    ctx,
                    synth_kwargs,
                    turn=turn + 1,
                )
                previous_interaction_id = getattr(synth, "id", None) or previous_interaction_id
                final_text = getattr(synth, "output_text", None) or ""
            except Exception:
                final_text = ""
            if not final_text:
                final_text = f"已达到最大执行步数上限（{step_budget} 步），任务尚未完全收敛，请展开步骤日志查看进展。"

        actual_model = getattr(ctx.llm, "last_used_model", None) or profile.model
        return AdapterResult(
            summary=final_text or "后台任务已完成。",
            raw_output=final_text,
            session_id=previous_interaction_id,
            model_used=actual_model,
            thinking_level_used=profile.thinking_level,
            metadata={
                "adapter": self.agent_type,
                "max_steps_reached": not completed_cleanly,
                "used_computer_use": used_computer_use,
            },
        )

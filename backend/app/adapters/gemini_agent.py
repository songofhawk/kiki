"""Built-in Gemini Agent adapter (Kiki native Heavy-Path executor)."""

from __future__ import annotations

import json
from typing import Any, Optional

from ..tools import AGENT_TOOL_DECLARATIONS
from .base import (
    AdapterContext,
    AdapterInfo,
    AdapterResult,
    ExecutionProfile,
    HeavyTaskAdapter,
    ThinkingLevel,
)

AGENT_SYSTEM_PROMPT = """你是 Kiki 的后台重型执行引擎 (gemini_agent)。
你可以使用工作区工具（list_files, read_file, write_artifact, run_shell, request_human_approval）来完成用户的复杂任务。

执行规范：
1. 先探索再操作，合理调用工具获取真实信息。
2. 若需要产出报告、分析文档或代码，使用 `write_artifact` 保存为产物文件。
3. 若任务涉及高风险操作、破坏性变更或需要用户明确拍板确认，必须调用 `request_human_approval` 挂起等待用户决策。
4. 任务完成时，用简洁、信息密度高的中文给出最终总结（**严格控制在 200 字以内**），直接说明结论与产出物。
"""


def _action_type_for_tool(tool_name: str) -> str:
    if tool_name == "run_shell":
        return "shell_exec"
    if tool_name == "write_artifact":
        return "file_edit"
    if tool_name == "request_human_approval":
        return "hitl_wait"
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
    if tool_name == "request_human_approval":
        prompt = str(args.get("prompt", "等待人工确认"))
        return f"等待主人拍板：{prompt[:50]}"
    return f"正在执行工具 {tool_name}..."


class GeminiAgentAdapter(HeavyTaskAdapter):
    """Built-in Kiki agent powered by Google Gemini Interactions API."""

    agent_type = "gemini_agent"
    display_name = "Kiki 内置 (Gemini Agent)"
    vendor = "Google Gemini"
    description = "Kiki 默认内置异步执行引擎，支持受控工作区读写、Shell、产物生成与 HITL 人工拍板。"
    category = "builtin"
    install_hint = "在根目录 .env 中配置 GEMINI_API_KEY 即可使用"
    aliases = ("gemini", "builtin", "default")
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
            available=True,
            binary_path="in-process",
            install_hint=self.install_hint,
            default_model=default_m,
            models=dict(self.model_tiers),
            supports_resume=True,
            supports_streaming=True,
            supports_hitl=True,
            aliases=self.aliases,
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

        input_data: Any = f"当前话题：{ctx.topic_title}\n用户任务：{ctx.user_content}"
        previous_interaction_id: Optional[str] = None
        final_text: str = ""

        for turn in range(1, ctx.max_steps + 1):
            ctx.emit_event_log({"event": "llm_turn", "turn": turn})
            interaction = await ctx.llm.run_agent_turn(
                input_data=input_data,
                tools=AGENT_TOOL_DECLARATIONS,
                system_instruction=AGENT_SYSTEM_PROMPT,
                previous_interaction_id=previous_interaction_id,
                thinking_level=profile.thinking_level,
            )
            previous_interaction_id = getattr(interaction, "id", None)
            ctx.session_id = previous_interaction_id

            steps = getattr(interaction, "steps", None) or []
            function_calls = [s for s in steps if getattr(s, "type", None) == "function_call"]

            if not function_calls:
                final_text = getattr(interaction, "output_text", None) or ""
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

            input_data = function_results

        return AdapterResult(
            summary=final_text or "后台任务已完成。",
            raw_output=final_text,
            session_id=previous_interaction_id,
            model_used=profile.model,
            thinking_level_used=profile.thinking_level,
            metadata={"adapter": self.agent_type},
        )

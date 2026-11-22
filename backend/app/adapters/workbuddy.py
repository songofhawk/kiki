"""Tencent WorkBuddy / CodeBuddy CLI Heavy-Path Adapter for Kiki.

Runs WorkBuddy / CodeBuddy CLI (`codebuddy` / `cbc` / `workbuddy`) in headless
print mode with `--output-format stream-json`, deep-thinking level propagation,
optional permission bypass flag, and `-r` session resume after HITL approval.
"""

from __future__ import annotations

import os
from typing import Any, Optional

from .base import (
    AdapterContext,
    ExecutionProfile,
    StreamParseState,
    SubprocessCLIAdapter,
    ThinkingLevel,
)


class WorkBuddyAdapter(SubprocessCLIAdapter):
    """Heavy-Path adapter backed by Tencent WorkBuddy / CodeBuddy CLI."""

    agent_type = "workbuddy"
    display_name = "WorkBuddy / CodeBuddy"
    vendor = "Tencent"
    description = "腾讯 WorkBuddy / CodeBuddy CLI 智能体，支持 print 模式 stream-json 输出、深度思考与 --resume 续跑。"
    binary_candidates = ("codebuddy", "cbc", "workbuddy")
    env_bin_var = "KIKI_WORKBUDDY_BIN"
    env_model_var = "KIKI_WORKBUDDY_MODEL"
    install_hint = "npm install -g @tencent-ai/codebuddy-code 或 curl -fsSL https://www.codebuddy.cn/cli/install.sh | bash"
    aliases = ("codebuddy", "cbc", "tencent_workbuddy", "work_buddy")
    model_tiers: dict[ThinkingLevel, str] = {
        "low": "hunyuan-turbos",
        "medium": "hunyuan-code",
        "high": "hunyuan-t1",
    }

    @staticmethod
    def _skip_permissions_enabled() -> bool:
        return os.getenv("KIKI_WORKBUDDY_SKIP_PERMISSIONS", "").strip() == "1"

    def build_command(
        self,
        binary: str,
        ctx: AdapterContext,
        profile: ExecutionProfile,
    ) -> list[str]:
        argv = [
            binary,
            "-p",
            ctx.user_content,
            "--output-format",
            "stream-json",
            "--model",
            profile.model,
        ]
        if self._skip_permissions_enabled():
            argv.append("--dangerously-skip-permissions")
        return argv

    def build_resume_command(
        self,
        binary: str,
        ctx: AdapterContext,
        profile: ExecutionProfile,
        session_id: str,
        decision_payload: dict[str, Any],
    ) -> Optional[list[str]]:
        decision = str(decision_payload.get("decision") or "approve").strip()
        comment = str(decision_payload.get("comment") or "").strip()
        followup_prompt = f"用户已确认操作（决定: {decision}）。"
        if comment:
            followup_prompt += f"补充说明: {comment}。"
        followup_prompt += "请继续完成剩余任务。"

        argv = [
            binary,
            "-r",
            session_id,
            "-p",
            followup_prompt,
            "--output-format",
            "stream-json",
            "--model",
            profile.model,
        ]
        if self._skip_permissions_enabled():
            argv.append("--dangerously-skip-permissions")
        return argv

    def build_env(self, ctx: AdapterContext, profile: ExecutionProfile) -> dict[str, str]:
        env = super().build_env(ctx, profile)
        env["CODEBUDDY_THINKING_LEVEL"] = profile.thinking_level
        return env

    def parse_json_event(
        self,
        event: dict[str, Any],
        state: StreamParseState,
        ctx: AdapterContext,
    ) -> None:
        # Capture session_id across CodeBuddy / WorkBuddy stream-json frames
        for key in ("session_id", "sessionId", "conversation_id"):
            val = event.get(key)
            if isinstance(val, str) and val.strip():
                state.session_id = val.strip()
                ctx.session_id = state.session_id
                break

        ev_type = str(event.get("type") or event.get("event") or "").lower()

        if ev_type == "system" and str(event.get("subtype") or "").lower() == "init":
            return

        if ev_type == "assistant":
            message = event.get("message")
            if isinstance(message, dict):
                content = message.get("content")
                if isinstance(content, list):
                    for block in content:
                        if not isinstance(block, dict):
                            continue
                        block_type = str(block.get("type") or "").lower()
                        if block_type in ("tool_use", "tool_call"):
                            tool_name = str(block.get("name") or block.get("tool") or "tool")
                            if state.active_step_id is not None:
                                ctx.finish_step(state.active_step_id, status="success")
                            step = ctx.start_step(
                                "tool_call",
                                f"{self.display_name} 正在调用 {tool_name}...",
                            )
                            state.active_step_id = step["id"]
                        elif block_type in ("thinking", "reasoning"):
                            thought = block.get("thinking") or block.get("text")
                            if isinstance(thought, str) and thought.strip():
                                ctx.update_current_step(
                                    f"{self.display_name} 深度思考中：{thought.strip()[:48]}"
                                )
                        elif block_type == "text":
                            text = block.get("text")
                            if isinstance(text, str) and text.strip():
                                state.final_text_parts.append(text.strip())
                    return

        if ev_type == "result":
            if state.active_step_id is not None:
                is_err = bool(event.get("is_error") or event.get("error"))
                ctx.finish_step(
                    state.active_step_id,
                    status="failed" if is_err else "success",
                )
                state.active_step_id = None

            denials = event.get("permission_denials")
            if isinstance(denials, list) and denials and not state.pending_hitl_prompt:
                first = denials[0] if isinstance(denials[0], dict) else {}
                tool_name = str(first.get("tool_name") or first.get("tool") or "敏感操作")
                state.pending_hitl_prompt = (
                    f"{self.display_name} 请求授权执行 {tool_name}，是否批准继续？"
                )
                state.pending_hitl_options = [
                    {"label": "批准并继续", "value": "approve"},
                    {"label": "拒绝", "value": "reject"},
                ]

            result_text = event.get("result")
            if isinstance(result_text, str) and result_text.strip():
                stripped = result_text.strip()
                if not state.final_text_parts or state.final_text_parts[-1] != stripped:
                    state.final_text_parts.append(stripped)
            return

        if ev_type in ("content_block_delta", "text_delta"):
            delta = event.get("delta")
            if isinstance(delta, dict):
                text = delta.get("text")
                if isinstance(text, str) and text.strip():
                    state.final_text_parts.append(text.strip())
                    return

        super().parse_json_event(event, state, ctx)

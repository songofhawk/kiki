"""Alibaba Qoder CLI Heavy-Path Adapter for Kiki.

Runs Qoder CLI (`qoder` / `qodercli`) in headless print mode with
`--output-format stream-json`, permission mode control, automatic model/thinking
selection, and `--resume` continuation after HITL approval.
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


class QoderAdapter(SubprocessCLIAdapter):
    """Heavy-Path adapter backed by Alibaba Qoder CLI."""

    agent_type = "qoder"
    display_name = "Qoder CLI"
    vendor = "Alibaba"
    description = "阿里 Qoder CLI 后台编程智能体，支持 headless stream-json 实时追踪与权限模式控制。"
    binary_candidates = ("qoder", "qodercli")
    env_bin_var = "KIKI_QODER_BIN"
    env_model_var = "KIKI_QODER_MODEL"
    install_hint = "请从 qoder.com 安装 Qoder CLI (qoder / qodercli)"
    aliases = ("qodercli", "alibaba_qoder")
    model_tiers: dict[ThinkingLevel, str] = {
        "low": "qoder-turbo",
        "medium": "qoder-pro",
        "high": "qoder-max",
    }

    @staticmethod
    def _permission_mode() -> str:
        return os.getenv("KIKI_QODER_PERMISSION_MODE", "dont_ask").strip() or "dont_ask"

    def build_command(
        self,
        binary: str,
        ctx: AdapterContext,
        profile: ExecutionProfile,
    ) -> list[str]:
        perm_mode = self._permission_mode()
        return [
            binary,
            "-p",
            ctx.build_task_prompt(),
            "--output-format",
            "stream-json",
            "--permission-mode",
            perm_mode,
            "--model",
            profile.model,
        ]

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

        perm_mode = self._permission_mode()
        return [
            binary,
            "--resume",
            session_id,
            "-p",
            followup_prompt,
            "--output-format",
            "stream-json",
            "--permission-mode",
            perm_mode,
            "--model",
            profile.model,
        ]

    def build_env(self, ctx: AdapterContext, profile: ExecutionProfile) -> dict[str, str]:
        env = super().build_env(ctx, profile)
        env["QODER_THINKING_LEVEL"] = profile.thinking_level
        return env

    def parse_json_event(
        self,
        event: dict[str, Any],
        state: StreamParseState,
        ctx: AdapterContext,
    ) -> None:
        # Capture session_id early for all Qoder stream-json events
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

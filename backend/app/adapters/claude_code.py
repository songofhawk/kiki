"""Anthropic Claude Code CLI adapter for Kiki Heavy-Path execution."""

from __future__ import annotations

import json
import os
from typing import Any, Optional

from .base import (
    AdapterContext,
    ExecutionProfile,
    StreamParseState,
    SubprocessCLIAdapter,
    ThinkingLevel,
)


def _truncate(text: str, limit: int = 120) -> str:
    cleaned = " ".join(text.split())
    if len(cleaned) <= limit:
        return cleaned
    return f"{cleaned[: limit - 1]}…"


def _format_followup_prompt(decision_payload: dict[str, Any]) -> str:
    decision = str(
        decision_payload.get("decision")
        or ("approved" if decision_payload.get("approved") else "rejected")
    ).strip()
    comment = str(decision_payload.get("comment") or "").strip()
    if comment:
        return f"用户已完成人工确认（决策: {decision}）。补充说明：{comment}。请继续执行任务。"
    return f"用户已完成人工确认（决策: {decision}）。请继续执行任务。"


def _preview_tool_input(tool_input: Any) -> str:
    if tool_input is None:
        return ""
    if isinstance(tool_input, str):
        return _truncate(tool_input)
    if isinstance(tool_input, dict):
        for key in ("command", "file_path", "path", "pattern", "query", "url", "description"):
            val = tool_input.get(key)
            if isinstance(val, str) and val.strip():
                return _truncate(val.strip())
        try:
            return _truncate(json.dumps(tool_input, ensure_ascii=False))
        except TypeError:
            return _truncate(str(tool_input))
    return _truncate(str(tool_input))


class ClaudeCodeAdapter(SubprocessCLIAdapter):
    """Heavy-Path adapter for Anthropic Claude Code CLI (`claude -p --output-format stream-json`)."""

    agent_type = "claude_code"
    display_name = "Claude Code"
    vendor = "Anthropic"
    description = "Anthropic Claude Code CLI，支持 stream-json 实时事件流、工具权限控制、思考强度与 --resume 续跑。"
    binary_candidates = ("claude",)
    extra_binary_paths = (
        "~/.local/bin/claude",
        "~/Library/Application Support/Claude/claude-code-vm/*/claude",
    )
    env_bin_var = "KIKI_CLAUDE_BIN"
    env_model_var = "KIKI_CLAUDE_MODEL"
    install_hint = "npm install -g @anthropic-ai/claude-code"
    aliases = ("cloud_code", "cloud code", "claude", "claude-code")
    model_tiers: dict[ThinkingLevel, str] = {
        "low": "claude-haiku-4-5",
        "medium": "claude-sonnet-4-6",
        "high": "claude-opus-4-6",
    }

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
            "--verbose",
            "--model",
            profile.model,
            "--effort",
            profile.thinking_level,
            "--max-turns",
            str(ctx.max_steps),
        ]
        allowed_tools = os.getenv("KIKI_CLAUDE_ALLOWED_TOOLS", "").strip()
        if allowed_tools:
            argv.extend(["--allowedTools", allowed_tools])
        if os.getenv("KIKI_CLAUDE_SKIP_PERMISSIONS", "").strip() == "1":
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
        followup_prompt = _format_followup_prompt(decision_payload)
        return [
            binary,
            "-r",
            session_id,
            "-p",
            followup_prompt,
            "--output-format",
            "stream-json",
            "--verbose",
            "--model",
            profile.model,
            "--effort",
            profile.thinking_level,
        ]

    def parse_json_event(
        self,
        event: dict[str, Any],
        state: StreamParseState,
        ctx: AdapterContext,
    ) -> None:
        ev_type = str(event.get("type") or "").strip().lower()

        session_id = event.get("session_id")
        if isinstance(session_id, str) and session_id.strip():
            state.session_id = session_id.strip()
            ctx.session_id = state.session_id

        if ev_type in ("system", "init"):
            return

        if ev_type == "assistant":
            message = event.get("message")
            if isinstance(message, dict) and isinstance(message.get("content"), list):
                tool_steps: dict[str, int] = state.extra.setdefault("claude_tool_steps", {})
                for block in message["content"]:
                    if not isinstance(block, dict):
                        continue
                    block_type = str(block.get("type") or "").strip().lower()
                    if block_type == "tool_use":
                        tool_name = str(block.get("name") or "tool_use").strip()
                        preview = _preview_tool_input(block.get("input"))
                        detail = (
                            f"Claude Code 调用 {tool_name}: {preview}"
                            if preview
                            else f"Claude Code 调用 {tool_name}"
                        )
                        step = ctx.start_step("tool_call", detail)
                        state.active_step_id = step["id"]
                        tool_id = str(block.get("id") or "").strip()
                        if tool_id:
                            tool_steps[tool_id] = step["id"]
                    elif block_type == "text":
                        text = block.get("text")
                        if isinstance(text, str) and text.strip():
                            state.final_text_parts.append(text.strip())
                return

        if ev_type == "user":
            message = event.get("message")
            if isinstance(message, dict) and isinstance(message.get("content"), list):
                tool_steps = state.extra.setdefault("claude_tool_steps", {})
                handled = False
                for block in message["content"]:
                    if not isinstance(block, dict):
                        continue
                    block_type = str(block.get("type") or "").strip().lower()
                    if block_type == "tool_result":
                        handled = True
                        is_error = bool(block.get("is_error"))
                        tool_use_id = str(block.get("tool_use_id") or "").strip()
                        step_id = tool_steps.pop(tool_use_id, None) if tool_use_id else None
                        if step_id is None:
                            step_id = state.active_step_id
                        if step_id is not None:
                            ctx.finish_step(
                                step_id,
                                status="failed" if is_error else "success",
                            )
                            if state.active_step_id == step_id:
                                state.active_step_id = None
                if handled:
                    return

        if ev_type == "result":
            is_error = bool(event.get("is_error"))
            if state.active_step_id is not None:
                ctx.finish_step(
                    state.active_step_id,
                    status="failed" if is_error else "success",
                )
                state.active_step_id = None

            result_text = event.get("result")
            if isinstance(result_text, str) and result_text.strip():
                cleaned = result_text.strip()
                if is_error:
                    ctx.emit_raw_log(f"[claude_code error] {cleaned}", stream="stderr")
                    state.stderr_lines.append(cleaned)
                if not state.final_text_parts or state.final_text_parts[-1] != cleaned:
                    state.final_text_parts.append(cleaned)
            return

        super().parse_json_event(event, state, ctx)

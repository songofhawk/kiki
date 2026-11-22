"""Google Antigravity CLI (agy) adapter for Kiki Heavy-Path execution."""

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
        for key in ("command", "CommandLine", "path", "Path", "file_path", "query", "url"):
            val = tool_input.get(key)
            if isinstance(val, str) and val.strip():
                return _truncate(val.strip())
        try:
            return _truncate(json.dumps(tool_input, ensure_ascii=False))
        except TypeError:
            return _truncate(str(tool_input))
    return _truncate(str(tool_input))


class AntigravityAdapter(SubprocessCLIAdapter):
    """Heavy-Path adapter for Google Antigravity CLI (`agy -p --output-format stream-json`)."""

    agent_type = "antigravity"
    display_name = "Antigravity (agy)"
    vendor = "Google"
    description = "Google Antigravity CLI (agy) 后台智能体，支持 stream-json 流式步骤追踪与多模型思考强度调度。"
    binary_candidates = ("agy", "antigravity")
    env_bin_var = "KIKI_ANTIGRAVITY_BIN"
    env_model_var = "KIKI_ANTIGRAVITY_MODEL"
    install_hint = "安装 Google Antigravity CLI (agy) 并执行 agy auth login"
    aliases = ("anti-gravity", "anti_gravity", "agy")
    model_tiers: dict[ThinkingLevel, str] = {
        "low": "gemini-3.5-flash-lite",
        "medium": "gemini-3.8-flash",
        "high": "gemini-3.8-pro",
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
            "--model",
            profile.model,
        ]
        if os.getenv("KIKI_AGY_SKIP_PERMISSIONS", "").strip() == "1":
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
            "--resume",
            session_id,
            "-p",
            followup_prompt,
            "--output-format",
            "stream-json",
            "--model",
            profile.model,
        ]

    def build_env(self, ctx: AdapterContext, profile: ExecutionProfile) -> dict[str, str]:
        env = super().build_env(ctx, profile)
        env["AGY_THINKING_LEVEL"] = profile.thinking_level
        return env

    def parse_json_event(
        self,
        event: dict[str, Any],
        state: StreamParseState,
        ctx: AdapterContext,
    ) -> None:
        for key in ("session_id", "sessionId", "conversation_id"):
            val = event.get(key)
            if isinstance(val, str) and val.strip():
                state.session_id = val.strip()
                ctx.session_id = state.session_id
                break

        ev_type = str(event.get("type") or event.get("event") or "").strip().lower()
        tool_steps: dict[str, int] = state.extra.setdefault("agy_tool_steps", {})

        if ev_type in ("tool_use", "tool_call"):
            state.extra["agy_delta_idx"] = None
            tool_name = str(
                event.get("name")
                or event.get("tool")
                or event.get("tool_name")
                or "tool_call"
            ).strip()
            tool_input = (
                event.get("input")
                if event.get("input") is not None
                else event.get("arguments", event.get("args"))
            )
            preview = _preview_tool_input(tool_input)
            detail = (
                f"Antigravity 调用 {tool_name}: {preview}"
                if preview
                else f"Antigravity 调用 {tool_name}"
            )
            if state.active_step_id is not None:
                ctx.finish_step(state.active_step_id, status="success")
            step = ctx.start_step("tool_call", detail)
            state.active_step_id = step["id"]
            call_id = str(
                event.get("tool_use_id") or event.get("call_id") or event.get("id") or ""
            ).strip()
            if call_id:
                tool_steps[call_id] = step["id"]
            return

        if ev_type == "tool_result":
            state.extra["agy_delta_idx"] = None
            is_error = bool(event.get("is_error") or event.get("error"))
            call_id = str(
                event.get("tool_use_id") or event.get("call_id") or event.get("id") or ""
            ).strip()
            step_id = tool_steps.pop(call_id, None) if call_id else None
            if step_id is None:
                step_id = state.active_step_id
            if step_id is not None:
                ctx.finish_step(step_id, status="failed" if is_error else "success")
                if state.active_step_id == step_id:
                    state.active_step_id = None
            return

        if ev_type == "permission_request":
            state.extra["agy_delta_idx"] = None
            prompt = str(
                event.get("prompt")
                or event.get("message")
                or event.get("reason")
                or f"{self.display_name} 请求人工确认操作。"
            )
            state.pending_hitl_prompt = prompt
            raw_opts = event.get("options")
            if isinstance(raw_opts, list):
                state.pending_hitl_options = raw_opts
            return

        if ev_type == "delta":
            delta_val = event.get("delta")
            chunk = ""
            if isinstance(delta_val, dict):
                raw_chunk = delta_val.get("text") or delta_val.get("content")
                if isinstance(raw_chunk, str):
                    chunk = raw_chunk
            elif isinstance(delta_val, str):
                chunk = delta_val
            else:
                raw_chunk = event.get("text") or event.get("content")
                if isinstance(raw_chunk, str):
                    chunk = raw_chunk

            if chunk:
                delta_idx = state.extra.get("agy_delta_idx")
                if isinstance(delta_idx, int) and 0 <= delta_idx < len(state.final_text_parts):
                    state.final_text_parts[delta_idx] += chunk
                else:
                    state.final_text_parts.append(chunk)
                    state.extra["agy_delta_idx"] = len(state.final_text_parts) - 1
            return

        if ev_type == "assistant":
            state.extra["agy_delta_idx"] = None
            message = event.get("message")
            if isinstance(message, dict) and isinstance(message.get("content"), list):
                for block in message["content"]:
                    if not isinstance(block, dict):
                        continue
                    block_type = str(block.get("type") or "").strip().lower()
                    if block_type in ("tool_use", "tool_call"):
                        tool_name = str(block.get("name") or block.get("tool") or "tool_call").strip()
                        preview = _preview_tool_input(
                            block.get("input", block.get("arguments"))
                        )
                        detail = (
                            f"Antigravity 调用 {tool_name}: {preview}"
                            if preview
                            else f"Antigravity 调用 {tool_name}"
                        )
                        step = ctx.start_step("tool_call", detail)
                        state.active_step_id = step["id"]
                        call_id = str(block.get("id") or "").strip()
                        if call_id:
                            tool_steps[call_id] = step["id"]
                    elif block_type == "text":
                        text = block.get("text")
                        if isinstance(text, str) and text.strip():
                            state.final_text_parts.append(text.strip())
                return

            for field_name in ("text", "content", "message"):
                val = event.get(field_name)
                if isinstance(val, str) and val.strip():
                    state.final_text_parts.append(val.strip())
                    return
            return

        if ev_type == "result":
            state.extra["agy_delta_idx"] = None
            is_error = bool(event.get("is_error") or event.get("error"))
            if state.active_step_id is not None:
                ctx.finish_step(
                    state.active_step_id,
                    status="failed" if is_error else "success",
                )
                state.active_step_id = None

            for field_name in ("result", "output_text", "text", "content"):
                val = event.get(field_name)
                if isinstance(val, str) and val.strip():
                    cleaned = val.strip()
                    if is_error:
                        ctx.emit_raw_log(f"[antigravity error] {cleaned}", stream="stderr")
                        state.stderr_lines.append(cleaned)
                    if not state.final_text_parts or state.final_text_parts[-1].strip() != cleaned:
                        state.final_text_parts.append(cleaned)
                    break
            return

        state.extra["agy_delta_idx"] = None
        super().parse_json_event(event, state, ctx)

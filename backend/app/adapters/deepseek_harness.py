"""DeepSeek Harness (dsh) Heavy-Path CLI adapter for Kiki."""

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


def _extract_text(value: Any) -> str:
    """Extract readable text from a string, content block list, or nested dict."""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, str) and item.strip():
                parts.append(item.strip())
            elif isinstance(item, dict):
                block_type = str(item.get("type") or "").lower()
                if block_type in ("", "text", "output_text", "assistant"):
                    text = item.get("text") or item.get("content")
                    if isinstance(text, str) and text.strip():
                        parts.append(text.strip())
        return "\n".join(parts).strip()
    if isinstance(value, dict):
        for key in ("text", "content", "result", "output"):
            nested = value.get(key)
            extracted = _extract_text(nested)
            if extracted:
                return extracted
    return ""


def _build_followup_prompt(decision_payload: dict[str, Any]) -> str:
    note = str(
        decision_payload.get("followup_prompt")
        or decision_payload.get("note")
        or decision_payload.get("comment")
        or decision_payload.get("decision")
        or ""
    ).strip()
    if note:
        return f"用户已批准继续执行。补充说明：{note}"
    return "用户已批准该操作，请继续完成剩余任务。"


class DeepSeekHarnessAdapter(SubprocessCLIAdapter):
    """Subprocess adapter for DeepSeek's official plugin-based agent runtime (`dsh`)."""

    agent_type = "deepseek_harness"
    display_name = "DeepSeek Harness (dsh)"
    vendor = "DeepSeek"
    description = (
        "DeepSeek 官方插件化 Agent 运行时 (dsh)，支持 headless 自动化执行、"
        "推理模式切换与工具调用。"
    )
    binary_candidates = ("dsh", "deepseek-harness")
    extra_binary_paths = (
        "/Applications/DeepSeek Harness.app/Contents/Resources/runtime/cli/bin/dsh",
        "~/Applications/DeepSeek Harness.app/Contents/Resources/runtime/cli/bin/dsh",
    )
    env_bin_var = "KIKI_DEEPSEEK_HARNESS_BIN"
    env_model_var = "KIKI_DEEPSEEK_HARNESS_MODEL"
    npx_package = "@deepseek-ai/dsh"
    install_hint = "npm install -g @deepseek-ai/dsh 或使用 npx @deepseek-ai/dsh"
    aliases = ("deepseek", "dsh", "deepseek-harness", "deepseek harness")
    model_tiers: dict[ThinkingLevel, str] = {
        "low": "deepseek-chat",
        "medium": "deepseek-v4-flash",
        "high": "deepseek-reasoner",
    }

    @staticmethod
    def _resolve_profile() -> str:
        return os.getenv("KIKI_DSH_PROFILE", "").strip() or "headless"

    def build_command(
        self,
        binary: str,
        ctx: AdapterContext,
        profile: ExecutionProfile,
    ) -> list[str]:
        return [
            binary,
            "--profile",
            self._resolve_profile(),
            "--model",
            profile.model,
            ctx.build_task_prompt(),
        ]

    def build_resume_command(
        self,
        binary: str,
        ctx: AdapterContext,
        profile: ExecutionProfile,
        session_id: str,
        decision_payload: dict[str, Any],
    ) -> Optional[list[str]]:
        followup_prompt = _build_followup_prompt(decision_payload)
        return [
            binary,
            "--profile",
            self._resolve_profile(),
            "--resume",
            session_id,
            "--model",
            profile.model,
            followup_prompt,
        ]

    def build_env(self, ctx: AdapterContext, profile: ExecutionProfile) -> dict[str, str]:
        env = super().build_env(ctx, profile)
        env["DSH_THINKING_LEVEL"] = profile.thinking_level
        env["DSH_MODEL"] = profile.model
        if profile.thinking_level == "high":
            env["DSH_REASONING_EFFORT"] = "high"
        return env

    def parse_json_event(
        self,
        event: dict[str, Any],
        state: StreamParseState,
        ctx: AdapterContext,
    ) -> None:
        # 1. Capture session identifier from top-level or nested session metadata
        for key in ("session_id", "sessionId"):
            val = event.get(key)
            if isinstance(val, str) and val.strip():
                state.session_id = val.strip()
                ctx.session_id = state.session_id
                break
        else:
            session_obj = event.get("session")
            if isinstance(session_obj, dict):
                sid = session_obj.get("id") or session_obj.get("session_id")
                if isinstance(sid, str) and sid.strip():
                    state.session_id = sid.strip()
                    ctx.session_id = state.session_id

        ev_type = str(event.get("type") or event.get("event") or "").lower()

        # 2. Approval / HITL requests emitted by dsh
        if ev_type in ("approval_required", "permission_request", "hitl_required"):
            prompt = str(
                event.get("prompt")
                or event.get("reason")
                or event.get("message")
                or f"{self.display_name} 请求人工确认操作。"
            )
            state.pending_hitl_prompt = prompt
            raw_opts = event.get("options") or event.get("choices")
            if isinstance(raw_opts, list):
                state.pending_hitl_options = raw_opts
            return

        # 3. Step and tool lifecycle events
        if ev_type in (
            "step.started",
            "step_start",
            "step",
            "tool.started",
            "tool_start",
            "tool_call",
            "tool_use",
        ):
            tool_obj = event.get("tool") if isinstance(event.get("tool"), dict) else {}
            step_obj = event.get("step") if isinstance(event.get("step"), dict) else {}
            name = str(
                event.get("tool_name")
                or event.get("name")
                or tool_obj.get("name")
                or (event.get("tool") if isinstance(event.get("tool"), str) else "")
                or step_obj.get("name")
                or step_obj.get("title")
                or event.get("title")
                or "step"
            )
            if state.active_step_id is not None:
                ctx.finish_step(state.active_step_id, status="success")
            step = ctx.start_step("tool_call", f"{self.display_name} 正在执行 {name}...")
            state.active_step_id = step["id"]
            return

        if ev_type in (
            "step.completed",
            "step_end",
            "tool.completed",
            "tool_end",
            "tool_result",
        ):
            if state.active_step_id is not None:
                status_val = str(event.get("status") or "").lower()
                is_err = bool(
                    event.get("is_error")
                    or event.get("error")
                    or status_val in ("error", "failed")
                )
                ctx.finish_step(
                    state.active_step_id,
                    status="failed" if is_err else "success",
                )
                state.active_step_id = None
            return

        # 4. Assistant / result output payloads (including nested message/content blocks)
        if ev_type in ("assistant", "result", "final", "response", "turn.completed", ""):
            for key in ("result", "output", "response", "text", "content", "message"):
                if key in event:
                    extracted = _extract_text(event.get(key))
                    if extracted:
                        state.final_text_parts.append(extracted)
                        return

        # 5. Fallback to base parser
        super().parse_json_event(event, state, ctx)

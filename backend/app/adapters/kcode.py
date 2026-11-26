"""Kimi Code (Kcode) Heavy-Path CLI adapter for Kiki."""

from __future__ import annotations

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
        for key in ("result", "content", "text", "output", "message"):
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


class KCodeAdapter(SubprocessCLIAdapter):
    """Subprocess adapter for Moonshot AI's Kimi Code CLI (`kimi` / `kcode`)."""

    agent_type = "kcode"
    display_name = "Kimi Code (Kcode)"
    vendor = "Moonshot AI"
    description = (
        "月之暗面官方终端编程 Agent (Kimi Code CLI)，"
        "支持全仓库大上下文重构与 stream-json 输出。"
    )
    binary_candidates = ("kimi", "kcode", "kimi-code")
    env_bin_var = "KIKI_KCODE_BIN"
    env_model_var = "KIKI_KCODE_MODEL"
    install_hint = (
        "curl -fsSL https://code.kimi.com/kimi-code/install.sh | bash "
        "或 npm install -g @moonshot-ai/kimi-code"
    )
    aliases = ("kimi", "kimi_code", "kimi-code")
    model_tiers: dict[ThinkingLevel, str] = {
        "low": "kimi-k2-turbo",
        "medium": "kimi-for-coding",
        "high": "kimi-k2.5",
    }

    def build_command(
        self,
        binary: str,
        ctx: AdapterContext,
        profile: ExecutionProfile,
    ) -> list[str]:
        return [
            binary,
            "-p",
            ctx.build_task_prompt(),
            "--output-format",
            "stream-json",
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
        followup_prompt = _build_followup_prompt(decision_payload)
        return [
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

    def build_env(self, ctx: AdapterContext, profile: ExecutionProfile) -> dict[str, str]:
        env = super().build_env(ctx, profile)
        env["KIMI_THINKING_LEVEL"] = profile.thinking_level
        return env

    def parse_json_event(
        self,
        event: dict[str, Any],
        state: StreamParseState,
        ctx: AdapterContext,
    ) -> None:
        # 1. Capture session_id
        for key in ("session_id", "sessionId"):
            val = event.get(key)
            if isinstance(val, str) and val.strip():
                state.session_id = val.strip()
                ctx.session_id = state.session_id
                break

        ev_type = str(event.get("type") or event.get("event") or "").lower()

        # 2. Tool invocation events
        if ev_type in ("tool_call", "tool_use"):
            tc_obj = event.get("tool_call") if isinstance(event.get("tool_call"), dict) else {}
            fn_obj = (
                event.get("function")
                if isinstance(event.get("function"), dict)
                else (tc_obj.get("function") if isinstance(tc_obj.get("function"), dict) else {})
            )
            tool_name = str(
                event.get("tool_name")
                or event.get("name")
                or event.get("tool")
                or tc_obj.get("name")
                or fn_obj.get("name")
                or "tool_call"
            )
            if state.active_step_id is not None:
                ctx.finish_step(state.active_step_id, status="success")
            step = ctx.start_step("tool_call", f"{self.display_name} 正在执行 {tool_name}...")
            state.active_step_id = step["id"]
            return

        # 3. Tool result events
        if ev_type == "tool_result":
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

        # 4. Assistant / content / result events (including structured message content blocks)
        if ev_type in ("assistant", "content", "result", "final", "message"):
            # Inspect message.content blocks for embedded tool_use blocks
            msg = event.get("message")
            content_blocks = (
                msg.get("content")
                if isinstance(msg, dict) and isinstance(msg.get("content"), list)
                else (event.get("content") if isinstance(event.get("content"), list) else None)
            )
            if isinstance(content_blocks, list):
                text_parts: list[str] = []
                for block in content_blocks:
                    if not isinstance(block, dict):
                        continue
                    btype = str(block.get("type") or "").lower()
                    if btype in ("tool_use", "tool_call"):
                        tname = str(block.get("name") or block.get("tool") or "tool_call")
                        if state.active_step_id is not None:
                            ctx.finish_step(state.active_step_id, status="success")
                        step = ctx.start_step(
                            "tool_call", f"{self.display_name} 正在执行 {tname}..."
                        )
                        state.active_step_id = step["id"]
                    elif btype in ("text", "output_text", ""):
                        btext = block.get("text") or block.get("content")
                        if isinstance(btext, str) and btext.strip():
                            text_parts.append(btext.strip())
                if text_parts:
                    state.final_text_parts.append("\n".join(text_parts))
                return

            for key in ("result", "content", "text", "message"):
                if key in event:
                    extracted = _extract_text(event.get(key))
                    if extracted:
                        state.final_text_parts.append(extracted)
                        return

        # 5. Delegate fallback to base parser
        super().parse_json_event(event, state, ctx)

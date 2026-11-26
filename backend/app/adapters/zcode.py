"""ZCode (智谱 Z.ai) Heavy-Path CLI adapter for Kiki."""

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
        for key in ("response", "result", "output", "text", "content", "message"):
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


class ZCodeAdapter(SubprocessCLIAdapter):
    """Subprocess adapter for Zhipu AI / Z.ai ZCode CLI (`zai-org/ZCode`)."""

    agent_type = "zcode"
    display_name = "ZCode (智谱 Z.ai)"
    vendor = "Zhipu AI / Z.ai"
    description = (
        "智谱 AI 官方 Agentic 开发工作台 CLI (zai-org/ZCode)，"
        "深度适配 GLM 系列模型与长程编码任务。"
    )
    binary_candidates = ("zcode", "zcode-cli")
    env_bin_var = "KIKI_ZCODE_BIN"
    env_model_var = "KIKI_ZCODE_MODEL"
    install_hint = "npm install -g zcode-app-cli 或从 github.com/zai-org/ZCode 构建安装"
    aliases = ("z.ai", "zhipu_code", "glm_code", "zcode-cli")
    model_tiers: dict[ThinkingLevel, str] = {
        "low": "glm-5-air",
        "medium": "glm-5.2",
        "high": "glm-5.3",
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
            "--json",
            "--cwd",
            str(ctx.workspace_root),
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
            "--resume",
            session_id,
            "-p",
            followup_prompt,
            "--json",
            "--cwd",
            str(ctx.workspace_root),
            "--model",
            profile.model,
        ]

    def build_env(self, ctx: AdapterContext, profile: ExecutionProfile) -> dict[str, str]:
        env = super().build_env(ctx, profile)
        env["ZCODE_THINKING_LEVEL"] = profile.thinking_level
        return env

    def _record_summary_steps(
        self,
        items: list[Any],
        ctx: AdapterContext,
    ) -> None:
        """Emit step logs for batch `steps` or `tool_calls` arrays in a single JSON summary."""
        for item in items:
            if not isinstance(item, dict):
                continue
            fn_obj = item.get("function") if isinstance(item.get("function"), dict) else {}
            name = str(
                item.get("name")
                or item.get("tool")
                or item.get("tool_name")
                or fn_obj.get("name")
                or item.get("action")
                or item.get("title")
                or "step"
            )
            step = ctx.start_step("tool_call", f"{self.display_name} 执行 {name}")
            status_val = str(item.get("status") or "").lower()
            is_err = bool(
                item.get("is_error")
                or item.get("error")
                or status_val in ("error", "failed")
            )
            ctx.finish_step(step["id"], status="failed" if is_err else "success")

    def parse_json_event(
        self,
        event: dict[str, Any],
        state: StreamParseState,
        ctx: AdapterContext,
    ) -> None:
        # 1. Capture session_id / sessionId
        for key in ("session_id", "sessionId"):
            val = event.get(key)
            if isinstance(val, str) and val.strip():
                state.session_id = val.strip()
                ctx.session_id = state.session_id
                break

        ev_type = str(event.get("type") or event.get("event") or "").lower()

        # 2. Single JSON summary object from `zcode --json` (has response/result/output or steps/tool_calls)
        has_summary_payload = any(
            k in event for k in ("response", "result", "output", "steps", "tool_calls")
        )
        if has_summary_payload and ev_type in ("", "summary", "result", "final", "done", "completed"):
            steps_list = event.get("steps")
            if isinstance(steps_list, list) and steps_list:
                self._record_summary_steps(steps_list, ctx)

            tool_calls = event.get("tool_calls")
            if isinstance(tool_calls, list) and tool_calls:
                self._record_summary_steps(tool_calls, ctx)

            for field_name in ("response", "result", "output"):
                if field_name in event:
                    extracted = _extract_text(event.get(field_name))
                    if extracted:
                        state.final_text_parts.append(extracted)
                        return

            if isinstance(steps_list, list) or isinstance(tool_calls, list):
                return

        # 3. Streaming NDJSON events
        if ev_type in ("tool_call", "tool_use", "step", "step.started", "action"):
            fn_obj = event.get("function") if isinstance(event.get("function"), dict) else {}
            name = str(
                event.get("tool_name")
                or event.get("name")
                or event.get("tool")
                or fn_obj.get("name")
                or event.get("action")
                or "tool_call"
            )
            if state.active_step_id is not None:
                ctx.finish_step(state.active_step_id, status="success")
            step = ctx.start_step("tool_call", f"{self.display_name} 正在执行 {name}...")
            state.active_step_id = step["id"]
            return

        if ev_type in ("tool_result", "step.completed", "action_result"):
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

        if ev_type in ("assistant", "message", "delta", "response"):
            for key in ("response", "content", "text", "delta", "message"):
                if key in event:
                    extracted = _extract_text(event.get(key))
                    if extracted:
                        state.final_text_parts.append(extracted)
                        return

        # 4. Delegate fallback to base parser
        super().parse_json_event(event, state, ctx)

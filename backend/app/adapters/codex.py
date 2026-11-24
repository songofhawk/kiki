"""OpenAI Codex CLI adapter for Kiki Heavy-Path execution."""

from __future__ import annotations

import json
import os
import tomllib
from pathlib import Path
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


def _extract_item_text(item: dict[str, Any]) -> str:
    text = item.get("text")
    if isinstance(text, str) and text.strip():
        return text.strip()

    content = item.get("content")
    if isinstance(content, str) and content.strip():
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict):
                block_text = block.get("text")
                if isinstance(block_text, str) and block_text.strip():
                    parts.append(block_text.strip())
            elif isinstance(block, str) and block.strip():
                parts.append(block.strip())
        if parts:
            return "\n".join(parts)

    return ""


def _describe_codex_item(item_type: str, item: dict[str, Any]) -> tuple[str, str]:
    """Map a Codex JSONL item into (action_type, human_readable_detail)."""
    if item_type == "command_execution":
        cmd = str(item.get("command") or "").strip()
        detail = f"Codex 执行命令: {_truncate(cmd)}" if cmd else "Codex 正在执行终端命令..."
        return "command_execution", detail

    if item_type == "file_change":
        changes = item.get("changes")
        paths: list[str] = []
        if isinstance(changes, list):
            for entry in changes:
                if isinstance(entry, dict) and entry.get("path"):
                    paths.append(str(entry["path"]))
        if paths:
            preview = _truncate(", ".join(paths[:3]) + (" 等" if len(paths) > 3 else ""))
            return "file_change", f"Codex 修改文件: {preview}"
        return "file_change", "Codex 正在修改工作区文件..."

    if item_type == "mcp_tool_call":
        server = str(item.get("server") or "").strip()
        tool = str(item.get("tool") or item.get("name") or "").strip()
        label = f"{server}.{tool}" if server and tool else (tool or server or "MCP")
        return "mcp_tool_call", f"Codex 调用 MCP 工具: {label}"

    if item_type == "reasoning":
        text = _extract_item_text(item)
        detail = f"Codex 思考: {_truncate(text)}" if text else "Codex 正在推理分析..."
        return "reasoning", detail

    if item_type == "agent_message":
        return "agent_message", "Codex 正在整理最终回复..."

    return "tool_call", f"Codex 正在执行 {item_type or 'step'}..."


class CodexAdapter(SubprocessCLIAdapter):
    """Heavy-Path adapter for OpenAI Codex CLI (`codex exec --json`)."""

    agent_type = "codex"
    display_name = "OpenAI Codex CLI"
    vendor = "OpenAI"
    description = "OpenAI Codex CLI 后台编码智能体，支持 headless JSONL 流式执行、沙箱写入与会话恢复。"
    binary_candidates = ("codex",)
    extra_binary_paths = (
        "/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex",
        "~/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex",
        "/Applications/Codex.app/Contents/Resources/codex-cli/bin/codex",
        "/Applications/Codex.app/Contents/Resources/codex",
        "~/Applications/Codex.app/Contents/Resources/codex-cli/bin/codex",
        "~/Applications/Codex.app/Contents/Resources/codex",
    )
    env_bin_var = "KIKI_CODEX_BIN"
    env_model_var = "KIKI_CODEX_MODEL"
    install_hint = "npm install -g @openai/codex（或安装 ChatGPT / Codex Desktop 应用）"
    aliases = ("openai_codex", "codex_cli")
    model_tiers: dict[ThinkingLevel, str] = {
        "low": "o4-mini",
        "medium": "gpt-5-codex",
        "high": "gpt-5.4-codex",
    }

    @staticmethod
    def _read_local_config_model() -> Optional[str]:
        config_path = Path(os.path.expanduser("~/.codex/config.toml"))
        try:
            if not config_path.is_file():
                return None
            with config_path.open("rb") as f:
                data = tomllib.load(f)
            model = data.get("model")
            if isinstance(model, str) and model.strip():
                return model.strip()
        except Exception:
            return None
        return None

    def select_profile(
        self,
        prompt: str,
        model_override: Optional[str] = None,
        thinking_override: Optional[ThinkingLevel] = None,
    ) -> ExecutionProfile:
        effective_override = model_override
        if not (effective_override and effective_override.strip()):
            env_model = os.getenv(self.env_model_var, "").strip() if self.env_model_var else ""
            if not env_model:
                effective_override = self._read_local_config_model()
        return super().select_profile(
            prompt,
            model_override=effective_override,
            thinking_override=thinking_override,
        )

    def probe(self) -> AdapterInfo:
        info = super().probe()
        local_model = self._read_local_config_model()
        if local_model:
            info.default_model = local_model
        return info

    def build_command(
        self,
        binary: str,
        ctx: AdapterContext,
        profile: ExecutionProfile,
    ) -> list[str]:
        sandbox_mode = os.getenv("KIKI_CODEX_SANDBOX", "workspace-write").strip() or "workspace-write"
        cmd = [
            binary,
            "exec",
            "--json",
            "--sandbox",
            sandbox_mode,
            "--skip-git-repo-check",
        ]
        # Respect ~/.codex/config.toml default model unless explicitly overridden via request or env
        env_model = os.getenv(self.env_model_var, "").strip() if self.env_model_var else ""
        if ctx.model_override or env_model:
            cmd.extend(["--model", profile.model])
        cmd.extend([
            "-c",
            f"model_reasoning_effort={profile.thinking_level}",
            ctx.user_content,
        ])
        return cmd

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
            "exec",
            "resume",
            session_id,
            "--json",
            followup_prompt,
        ]

    def parse_json_event(
        self,
        event: dict[str, Any],
        state: StreamParseState,
        ctx: AdapterContext,
    ) -> None:
        ev_type = str(event.get("type") or "").strip().lower()

        if ev_type == "thread.started":
            thread_id = event.get("thread_id") or event.get("session_id")
            if isinstance(thread_id, str) and thread_id.strip():
                state.session_id = thread_id.strip()
                ctx.session_id = state.session_id
            return

        if ev_type in ("item.started", "item.completed"):
            item = event.get("item")
            if not isinstance(item, dict):
                super().parse_json_event(event, state, ctx)
                return

            item_type = str(item.get("type") or "").strip().lower()
            item_id = str(item.get("id") or "").strip()
            item_steps: dict[str, int] = state.extra.setdefault("codex_item_steps", {})

            if ev_type == "item.started":
                if item_type in (
                    "command_execution",
                    "file_change",
                    "mcp_tool_call",
                    "reasoning",
                    "agent_message",
                ):
                    action_type, detail = _describe_codex_item(item_type, item)
                    if state.active_step_id is not None:
                        ctx.finish_step(state.active_step_id, status="success")
                    step = ctx.start_step(action_type, detail)
                    state.active_step_id = step["id"]
                    if item_id:
                        item_steps[item_id] = step["id"]
                    return

            if ev_type == "item.completed":
                if item_type == "agent_message":
                    msg_text = _extract_item_text(item)
                    if msg_text:
                        state.final_text_parts.append(msg_text)

                if item_type in (
                    "command_execution",
                    "file_change",
                    "mcp_tool_call",
                    "reasoning",
                    "agent_message",
                ):
                    step_id = item_steps.pop(item_id, None) if item_id else None
                    if step_id is None:
                        step_id = state.active_step_id

                    exit_code = item.get("exit_code")
                    item_status = str(item.get("status") or "").strip().lower()
                    is_failed = (
                        item_status in ("failed", "error")
                        or bool(item.get("error"))
                        or (isinstance(exit_code, int) and exit_code != 0)
                    )
                    _, completed_detail = _describe_codex_item(item_type, item)

                    if step_id is not None:
                        ctx.finish_step(
                            step_id,
                            status="failed" if is_failed else "success",
                            detail=completed_detail,
                        )
                        if state.active_step_id == step_id:
                            state.active_step_id = None
                    elif item_type != "agent_message":
                        action_type, detail = _describe_codex_item(item_type, item)
                        step = ctx.start_step(action_type, detail)
                        ctx.finish_step(
                            step["id"],
                            status="failed" if is_failed else "success",
                        )
                    return

        if ev_type in ("turn.failed", "error"):
            err_obj = event.get("error")
            if isinstance(err_obj, dict):
                err_msg = str(
                    err_obj.get("message") or json.dumps(err_obj, ensure_ascii=False)
                )
            elif isinstance(err_obj, str) and err_obj.strip():
                err_msg = err_obj.strip()
            else:
                err_msg = str(event.get("message") or json.dumps(event, ensure_ascii=False))

            ctx.emit_raw_log(f"[codex {ev_type}] {err_msg}", stream="stderr")
            state.stderr_lines.append(err_msg)
            if state.active_step_id is not None:
                ctx.finish_step(state.active_step_id, status="failed")
                state.active_step_id = None
            return

        super().parse_json_event(event, state, ctx)

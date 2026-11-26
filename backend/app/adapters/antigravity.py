"""Google Antigravity CLI (agy) adapter for Kiki Heavy-Path execution."""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any, Optional

_IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg")
_LOCAL_IMAGE_PATH_RE = re.compile(
    r"(?:file://)?(/(?:Users|home|tmp|var|private)/[^\s)\"'>]+\.(?:png|jpe?g|webp|gif|svg))",
    flags=re.IGNORECASE,
)


def _clean_local_image_markdown(text: str) -> str:
    """Remove raw local filesystem image/file links that browsers cannot load directly."""
    cleaned = re.sub(
        r"!\[[^\]]*\]\((?:file://)?/[^)]+\.(?:png|jpe?g|webp|gif|svg)\)",
        "",
        text,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(
        r"\n*图片已保存至[：:]\s*\[[^\]]+\]\(file:///[^)]+\)",
        "",
        cleaned,
        flags=re.IGNORECASE,
    )
    return re.sub(r"\n{3,}", "\n\n", cleaned).strip()

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
    install_hint = "curl -fsSL https://antigravity.google/cli/install.sh | bash"
    aliases = ("anti-gravity", "anti_gravity", "agy")
    model_tiers: dict[ThinkingLevel, str] = {
        "low": "gemini-3.8-flash-low",
        "medium": "gemini-4-neon-medium",
        "high": "gemini-4-neon-high",
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
            ctx.build_task_prompt(),
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

        if ev_type == "init":
            return

        if ev_type == "step_update":
            su = event.get("step_update")
            if not isinstance(su, dict):
                return
            conv_id = su.get("conversation_id")
            if isinstance(conv_id, str) and conv_id.strip():
                state.session_id = conv_id.strip()
                ctx.session_id = state.session_id

            step_index = su.get("step_index")
            step_key = f"su_{step_index}" if step_index is not None else ""
            step_state = str(su.get("state") or "").strip().upper()
            step_type = str(su.get("step_type") or "").strip().lower()

            if step_type == "agent_response":
                buffers: dict[str, str] = state.extra.setdefault("agy_resp_buffers", {})
                delta = su.get("text_delta")
                if isinstance(delta, str) and delta:
                    buffers[step_key] = buffers.get(step_key, "") + delta
                if step_state == "DONE":
                    raw_text = (buffers.pop(step_key, "") or "").strip()
                    if raw_text:
                        all_raw: list[str] = state.extra.setdefault("agy_raw_texts", [])
                        all_raw.append(raw_text)
                        display_text = _clean_local_image_markdown(raw_text) or raw_text
                        state.final_text_parts = [display_text]
                        ctx.emit_interim_message(display_text)
                return

            tool_name = str(su.get("tool_name") or "").strip()
            if step_type == "subagent" or tool_name:
                if step_state == "ACTIVE" and step_key not in tool_steps:
                    detail = f"Antigravity 调用 {tool_name or step_type}"
                    sub_info = su.get("subagent_info")
                    if isinstance(sub_info, dict) and isinstance(sub_info.get("subagents"), list) and sub_info["subagents"]:
                        first_sub = sub_info["subagents"][0]
                        if isinstance(first_sub, dict):
                            sub_name = first_sub.get("type_name") or first_sub.get("role") or "subagent"
                            sub_role = first_sub.get("role")
                            detail = (
                                f"Antigravity 调用子代理 {sub_name} ({sub_role})"
                                if sub_role and sub_role != sub_name
                                else f"Antigravity 调用子代理 {sub_name}"
                            )
                    step = ctx.start_step("tool_call", detail)
                    state.active_step_id = step["id"]
                    if step_key:
                        tool_steps[step_key] = step["id"]
                elif step_state in ("DONE", "COMPLETED", "ERROR", "FAILED"):
                    step_id = tool_steps.pop(step_key, None) if step_key else state.active_step_id
                    if step_id is not None:
                        ctx.finish_step(
                            step_id,
                            status="failed" if step_state in ("ERROR", "FAILED") else "success",
                        )
                        if state.active_step_id == step_id:
                            state.active_step_id = None
            return

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
            res_obj = event.get("result")
            is_error = bool(event.get("is_error") or event.get("error"))
            if isinstance(res_obj, dict):
                conv_id = res_obj.get("conversation_id") or res_obj.get("session_id")
                if isinstance(conv_id, str) and conv_id.strip():
                    state.session_id = conv_id.strip()
                    ctx.session_id = state.session_id
                status_str = str(res_obj.get("status") or "").strip().upper()
                if status_str in ("ERROR", "FAILED", "FAILURE"):
                    is_error = True

            if state.active_step_id is not None:
                ctx.finish_step(
                    state.active_step_id,
                    status="failed" if is_error else "success",
                )
                state.active_step_id = None

            extracted_text = ""
            if isinstance(res_obj, dict):
                for key in ("response", "output_text", "text", "content", "message"):
                    val = res_obj.get(key)
                    if isinstance(val, str) and val.strip():
                        extracted_text = val.strip()
                        break
            else:
                for field_name in ("result", "output_text", "text", "content"):
                    val = event.get(field_name)
                    if isinstance(val, str) and val.strip():
                        extracted_text = val.strip()
                        break

            if extracted_text:
                all_raw: list[str] = state.extra.setdefault("agy_raw_texts", [])
                all_raw.append(extracted_text)
                if is_error:
                    ctx.emit_raw_log(f"[antigravity error] {extracted_text}", stream="stderr")
                    state.stderr_lines.append(extracted_text)
                if not state.final_text_parts:
                    display_text = _clean_local_image_markdown(extracted_text) or extracted_text
                    state.final_text_parts.append(display_text)
            return

        state.extra["agy_delta_idx"] = None
        super().parse_json_event(event, state, ctx)

    async def after_process_success(
        self,
        ctx: AdapterContext,
        state: StreamParseState,
    ) -> None:
        seen_hashes: set[str] = set()
        candidate_paths: list[Path] = []

        # 1. Scan Antigravity CLI brain directory for this conversation_id
        if state.session_id:
            brain_roots = [
                Path(os.path.expanduser("~/.gemini/antigravity-cli/brain")) / state.session_id,
                Path(os.path.expanduser("~/.gemini/antigravity/brain")) / state.session_id,
            ]
            for brain_dir in brain_roots:
                if brain_dir.is_dir():
                    for p in sorted(brain_dir.iterdir()):
                        if p.is_file() and p.suffix.lower() in _IMAGE_EXTENSIONS:
                            candidate_paths.append(p)

        # 2. Scan any local image paths referenced in agent response text
        for text in state.extra.get("agy_raw_texts", []):
            for match in _LOCAL_IMAGE_PATH_RE.findall(text):
                p = Path(match)
                if p not in candidate_paths:
                    candidate_paths.append(p)

        for img_path in candidate_paths:
            try:
                resolved = img_path.resolve()
                if not resolved.is_file():
                    continue
                raw_bytes = resolved.read_bytes()
                if len(raw_bytes) < 64:
                    continue
                digest = hashlib.sha256(raw_bytes).hexdigest()
                if digest in seen_hashes:
                    continue
                seen_hashes.add(digest)
                ctx.register_binary_artifact(
                    filename=resolved.name,
                    data=raw_bytes,
                    file_type="image",
                    description=f"Antigravity 生成图片 {resolved.name}",
                )
            except OSError:
                continue

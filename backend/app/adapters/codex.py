"""OpenAI Codex CLI adapter for Kiki Heavy-Path execution."""

from __future__ import annotations

import base64
import glob
import hashlib
import json
import os
import tomllib
from dataclasses import replace
from pathlib import Path
from typing import Any, Optional

_IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg")


def _detect_image_ext(data: bytes, mime_hint: str = "") -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "gif"
    hint = mime_hint.lower()
    if "jpeg" in hint or "jpg" in hint:
        return "jpg"
    if "webp" in hint:
        return "webp"
    if "gif" in hint:
        return "gif"
    return "png"

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
            return replace(info, default_model=local_model)
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
        for extra_dir in ctx.extra_dirs:
            if extra_dir.is_dir():
                cmd.extend(["--add-dir", str(extra_dir)])
        cmd.extend([
            "-c",
            f"model_reasoning_effort={profile.thinking_level}",
            ctx.build_task_prompt(),
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
                        # Keep only the latest agent_message as final summary so pre-tool
                        # commentary is never glued onto the final completion reply.
                        state.final_text_parts = [msg_text]
                        ctx.emit_interim_message(msg_text)

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
                    if item_type == "file_change" and not is_failed:
                        changes = item.get("changes")
                        if isinstance(changes, list):
                            img_files: list[str] = state.extra.setdefault("codex_image_files", [])
                            for entry in changes:
                                if isinstance(entry, dict) and entry.get("path"):
                                    p_str = str(entry["path"]).strip()
                                    if p_str.lower().endswith(_IMAGE_EXTENSIONS) and p_str not in img_files:
                                        img_files.append(p_str)
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

    @staticmethod
    def _find_rollout_files(session_id: str) -> list[Path]:
        if not session_id:
            return []
        codex_home = Path(os.getenv("CODEX_HOME", os.path.expanduser("~/.codex")))
        sessions_dir = codex_home / "sessions"
        if not sessions_dir.is_dir():
            return []
        pattern = str(sessions_dir / "**" / f"*{session_id}*.jsonl")
        return [Path(p) for p in sorted(glob.glob(pattern, recursive=True))]

    async def after_process_success(
        self,
        ctx: AdapterContext,
        state: StreamParseState,
    ) -> None:
        seen_hashes: set[str] = set()
        img_index = 0

        # 1. Extract images emitted by Codex built-in imagegen tool from its rollout JSONL
        for rollout_path in self._find_rollout_files(state.session_id or ""):
            try:
                with rollout_path.open("r", encoding="utf-8", errors="replace") as fh:
                    for raw_line in fh:
                        if "image" not in raw_line.lower():
                            continue
                        try:
                            obj = json.loads(raw_line)
                        except json.JSONDecodeError:
                            continue
                        if not isinstance(obj, dict):
                            continue
                        ev_type = obj.get("type")
                        payload = obj.get("payload")
                        if not isinstance(payload, dict):
                            continue

                        candidates: list[tuple[str, str, str]] = []  # (b64_data, mime_hint, desc)
                        if ev_type == "event_msg" and payload.get("type") == "item_completed":
                            item = payload.get("item")
                            if (
                                isinstance(item, dict)
                                and item.get("type") == "Extension"
                                and "image" in str(item.get("kind") or "").lower()
                                and isinstance(item.get("result"), str)
                                and item["result"].strip()
                            ):
                                desc = str(item.get("revisedPrompt") or "Codex 生成图片").strip()
                                candidates.append((item["result"].strip(), "image/png", desc))
                        elif ev_type == "response_item" and payload.get("type") == "custom_tool_call_output":
                            output = payload.get("output")
                            if isinstance(output, list):
                                for entry in output:
                                    if not isinstance(entry, dict):
                                        continue
                                    image_url = entry.get("image_url")
                                    if (
                                        isinstance(image_url, str)
                                        and image_url.startswith("data:image/")
                                        and ";base64," in image_url
                                    ):
                                        header, b64_part = image_url.split(";base64,", 1)
                                        candidates.append((b64_part.strip(), header, "Codex 生成图片"))

                        for b64_data, mime_hint, desc in candidates:
                            try:
                                raw_bytes = base64.b64decode(b64_data, validate=False)
                            except Exception:
                                continue
                            if len(raw_bytes) < 64:
                                continue
                            digest = hashlib.sha256(raw_bytes).hexdigest()
                            if digest in seen_hashes:
                                continue
                            seen_hashes.add(digest)
                            img_index += 1
                            ext = _detect_image_ext(raw_bytes, mime_hint)
                            ctx.register_binary_artifact(
                                filename=f"codex_image_{img_index}.{ext}",
                                data=raw_bytes,
                                file_type="image",
                                description=_truncate(desc, limit=120),
                            )
            except OSError:
                continue

        # 2. Register any workspace image files written via file_change
        for rel_path in state.extra.get("codex_image_files", []):
            candidate = Path(rel_path)
            full_path = candidate if candidate.is_absolute() else (ctx.workspace_root / candidate)
            try:
                resolved = full_path.resolve()
                if not resolved.is_file():
                    continue
                raw_bytes = resolved.read_bytes()
                digest = hashlib.sha256(raw_bytes).hexdigest()
                if digest in seen_hashes:
                    continue
                seen_hashes.add(digest)
                ctx.register_binary_artifact(
                    filename=resolved.name,
                    data=raw_bytes,
                    file_type="image",
                    description=f"Codex 生成图片 {resolved.name}",
                )
            except OSError:
                continue

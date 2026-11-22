"""ByteDance Doubao Work / Volcengine Ark Hybrid Heavy-Path Adapter for Kiki.

Supports dual-mode execution:
1. Local CLI mode (`doubao-work` / `doubao` / `ark`) via `SubprocessCLIAdapter`.
2. Volcengine Ark OpenAI-compatible HTTP API fallback when `ARK_API_KEY` or
   `DOUBAO_API_KEY` is configured and no CLI binary is installed.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Optional

import httpx

from .base import (
    AdapterCategory,
    AdapterContext,
    AdapterInfo,
    AdapterResult,
    ExecutionProfile,
    StreamParseState,
    SubprocessCLIAdapter,
    ThinkingLevel,
)


class DoubaoWorkAdapter(SubprocessCLIAdapter):
    """Hybrid Heavy-Path adapter for ByteDance Doubao Work / Volcengine Ark."""

    agent_type = "doubao_work"
    display_name = "豆包工作 (Doubao Work)"
    vendor = "ByteDance / Volcengine"
    description = "字节跳动豆包工作 / 火山方舟 Agent 适配器，支持 CLI (doubao-work / ark) 与火山方舟 OpenAI 兼容 API 双模执行。"
    category: AdapterCategory = "hybrid"
    binary_candidates = ("doubao-work", "doubao", "ark")
    env_bin_var = "KIKI_DOUBAO_WORK_BIN"
    env_model_var = "KIKI_DOUBAO_WORK_MODEL"
    install_hint = "npm install -g @volcengine/ark-cli 或在 .env 中配置 ARK_API_KEY / DOUBAO_API_KEY"
    aliases = ("doubao", "doubao-work", "豆包工作", "豆包工作台", "ark", "volcengine")
    model_tiers: dict[ThinkingLevel, str] = {
        "low": "doubao-1-5-lite-32k",
        "medium": "doubao-1-5-pro-32k",
        "high": "doubao-seed-1-6-thinking",
    }

    @staticmethod
    def _get_api_key() -> str:
        return (
            os.getenv("ARK_API_KEY", "").strip()
            or os.getenv("DOUBAO_API_KEY", "").strip()
        )

    def has_api_credentials(self) -> bool:
        """Return True when Volcengine Ark / Doubao API credentials are present."""
        return bool(self._get_api_key())

    def probe(self) -> AdapterInfo:
        bin_path = self.resolve_binary()
        has_api = self.has_api_credentials()
        return AdapterInfo(
            id=self.agent_type,
            name=self.display_name,
            vendor=self.vendor,
            description=self.description,
            category=self.category,
            available=bool(bin_path) or has_api,
            binary_path=bin_path or ("ark-http-api" if has_api else None),
            install_hint=self.install_hint,
            default_model=self.model_tiers["medium"],
            models=dict(self.model_tiers),
            supports_resume=self.supports_resume,
            supports_streaming=self.supports_streaming,
            supports_hitl=self.supports_hitl,
            aliases=self.aliases,
        )

    def build_command(
        self,
        binary: str,
        ctx: AdapterContext,
        profile: ExecutionProfile,
    ) -> list[str]:
        bin_name = Path(binary).name.lower()
        bin_stem = Path(binary).stem.lower()
        if bin_name == "ark" or bin_stem == "ark":
            return [
                binary,
                "chat",
                "--model",
                profile.model,
                "--prompt",
                ctx.user_content,
                "--json",
            ]
        return [
            binary,
            "-p",
            ctx.user_content,
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
        decision = str(decision_payload.get("decision") or "approve").strip()
        comment = str(decision_payload.get("comment") or "").strip()
        followup_prompt = f"用户已确认操作（决定: {decision}）。"
        if comment:
            followup_prompt += f"补充说明: {comment}。"
        followup_prompt += "请继续完成剩余任务。"

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
        env["DOUBAO_THINKING_LEVEL"] = profile.thinking_level
        return env

    def parse_json_event(
        self,
        event: dict[str, Any],
        state: StreamParseState,
        ctx: AdapterContext,
    ) -> None:
        # Handle OpenAI / Volcengine Ark JSON completion shape (`ark chat --json`)
        choices = event.get("choices")
        if isinstance(choices, list) and choices:
            first = choices[0]
            if isinstance(first, dict):
                message = first.get("message") or first.get("delta") or {}
                if isinstance(message, dict):
                    content = message.get("content")
                    if isinstance(content, str) and content.strip():
                        state.final_text_parts.append(content.strip())
                        return

        super().parse_json_event(event, state, ctx)

    async def _execute_via_ark_api(self, ctx: AdapterContext) -> AdapterResult:
        profile = self.select_profile(
            ctx.user_content,
            model_override=ctx.model_override,
            thinking_override=ctx.thinking_override,
        )
        api_key = self._get_api_key()
        base_url = os.getenv(
            "ARK_BASE_URL",
            "https://ark.cn-beijing.volces.com/api/v3",
        ).strip().rstrip("/")
        endpoint_url = f"{base_url}/chat/completions"
        model = os.getenv("ARK_ENDPOINT_ID", "").strip() or profile.model

        init_step = ctx.start_step(
            "adapter_init",
            f"已启动 {self.display_name}（火山方舟 API，模型: {model}，思考强度: {profile.thinking_level}）",
        )
        ctx.emit_event_log(
            {
                "event": "profile_selected",
                "adapter": self.agent_type,
                "mode": "ark_http_api",
                "model": model,
                "thinking_level": profile.thinking_level,
                "reason": profile.reason,
                "endpoint": endpoint_url,
            }
        )

        payload: dict[str, Any] = {
            "model": model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "你是运行在 Kiki 后台的豆包工作 (Doubao Work) 工程智能体。"
                        f"当前工作区路径: {ctx.workspace_root}。"
                        "请结合用户需求给出清晰、结构化、可落地的结果。"
                    ),
                },
                {"role": "user", "content": ctx.user_content},
            ],
        }
        if profile.thinking_level != "low" or "thinking" in model.lower():
            payload["reasoning_effort"] = profile.thinking_level

        api_step = ctx.start_step(
            "llm_call",
            f"正在调用火山方舟 API（模型: {model}）...",
        )

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(ctx.timeout_sec, connect=20.0)
            ) as client:
                response = await client.post(endpoint_url, headers=headers, json=payload)
                response.raise_for_status()
                data = response.json()
        except httpx.HTTPStatusError as exc:
            body_preview = exc.response.text[:400] if exc.response is not None else ""
            ctx.emit_event_log(
                {
                    "event": "ark_api_error",
                    "adapter": self.agent_type,
                    "status_code": exc.response.status_code if exc.response is not None else None,
                    "detail": body_preview,
                }
            )
            ctx.finish_step(api_step["id"], status="failed")
            ctx.finish_step(init_step["id"], status="failed")
            raise RuntimeError(
                f"{self.display_name} 火山方舟 API 调用失败 (HTTP {exc.response.status_code}): {body_preview}"
            ) from exc
        except Exception as exc:
            ctx.emit_event_log(
                {
                    "event": "ark_api_error",
                    "adapter": self.agent_type,
                    "detail": str(exc),
                }
            )
            ctx.finish_step(api_step["id"], status="failed")
            ctx.finish_step(init_step["id"], status="failed")
            raise RuntimeError(f"{self.display_name} 火山方舟 API 请求异常: {exc}") from exc

        choices = data.get("choices") or []
        first_choice = choices[0] if choices and isinstance(choices[0], dict) else {}
        message = first_choice.get("message") if isinstance(first_choice, dict) else {}
        if not isinstance(message, dict):
            message = {}

        content = str(message.get("content") or "").strip()
        reasoning_content = str(message.get("reasoning_content") or "").strip()
        response_id = data.get("id")
        session_id = str(response_id).strip() if isinstance(response_id, str) and response_id.strip() else None
        usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}

        ctx.emit_event_log(
            {
                "event": "ark_api_completed",
                "adapter": self.agent_type,
                "model": model,
                "response_id": session_id,
                "has_reasoning": bool(reasoning_content),
                "usage": usage,
            }
        )
        ctx.finish_step(api_step["id"], status="success")
        ctx.finish_step(init_step["id"], status="success")

        summary = content or f"{self.display_name}（火山方舟 API）已完成任务。"
        metadata: dict[str, Any] = {
            "adapter": self.agent_type,
            "mode": "ark_http_api",
            "endpoint": endpoint_url,
            "usage": usage,
        }
        if reasoning_content:
            metadata["reasoning_content"] = reasoning_content

        return AdapterResult(
            summary=summary,
            raw_output=json.dumps(data, ensure_ascii=False),
            session_id=session_id,
            model_used=model,
            thinking_level_used=profile.thinking_level,
            metadata=metadata,
        )

    async def execute(self, ctx: AdapterContext) -> AdapterResult:
        if self.resolve_binary():
            return await super().execute(ctx)
        if self.has_api_credentials():
            return await self._execute_via_ark_api(ctx)
        raise RuntimeError(
            f"未检测到 {self.display_name} CLI（查找命令: {', '.join(self.binary_candidates)}），"
            f"也未配置 ARK_API_KEY / DOUBAO_API_KEY。请先安装或配置：{self.install_hint}"
        )

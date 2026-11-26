"""Tencent WorkBuddy / CodeBuddy CLI Heavy-Path Adapter for Kiki.

Runs WorkBuddy / CodeBuddy CLI (`codebuddy` / `cbc` / `workbuddy`) in headless
print mode with `--output-format stream-json`, deep-thinking level propagation,
optional permission bypass flag, and `-r` session resume after HITL approval.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from dataclasses import replace
from pathlib import Path
from typing import Any, Optional

from .base import (
    AdapterContext,
    AdapterInfo,
    ExecutionProfile,
    StreamParseState,
    SubprocessCLIAdapter,
    ThinkingLevel,
    estimate_thinking_level,
)

_MODEL_HELP_BLOCK_RE = re.compile(
    r"--model\b.*?(?=\n\s+--|\Z)",
    flags=re.DOTALL,
)
_SUPPORTED_MODELS_RE = re.compile(
    r"Currently\s+supported\s*:\s*\(([^)]+)\)",
    flags=re.IGNORECASE | re.DOTALL,
)


class WorkBuddyAdapter(SubprocessCLIAdapter):
    """Heavy-Path adapter backed by Tencent WorkBuddy / CodeBuddy CLI."""

    agent_type = "workbuddy"
    display_name = "WorkBuddy / CodeBuddy"
    vendor = "Tencent"
    description = "腾讯 WorkBuddy / CodeBuddy CLI 智能体，支持 print 模式 stream-json 输出、深度思考与 --resume 续跑。"
    binary_candidates = ("codebuddy", "cbc", "workbuddy")
    env_bin_var = "KIKI_WORKBUDDY_BIN"
    env_model_var = "KIKI_WORKBUDDY_MODEL"
    install_hint = "npm install -g @tencent-ai/codebuddy-code 或 curl -fsSL https://www.codebuddy.cn/cli/install.sh | bash"
    aliases = ("codebuddy", "cbc", "tencent_workbuddy", "work_buddy")
    fallback_model = "hy4std"
    model_tiers: dict[ThinkingLevel, str] = {
        "low": fallback_model,
        "medium": fallback_model,
        "high": fallback_model,
    }
    _MODEL_DISCOVERY_CACHE: dict[str, tuple[float, list[str]]] = {}
    _MODEL_DISCOVERY_TTL_SEC: float = 300.0

    @staticmethod
    def _parse_supported_models_from_help(help_text: str) -> list[str]:
        """Extract supported model IDs from `codebuddy --help` output."""
        if not help_text:
            return []
        block_match = _MODEL_HELP_BLOCK_RE.search(help_text)
        search_target = block_match.group(0) if block_match else help_text
        match = _SUPPORTED_MODELS_RE.search(search_target)
        if not match and block_match:
            match = _SUPPORTED_MODELS_RE.search(help_text)
        if not match:
            return []

        models: list[str] = []
        seen: set[str] = set()
        for raw in match.group(1).split(","):
            cleaned = raw.strip().strip("\"'")
            if cleaned and cleaned not in seen:
                seen.add(cleaned)
                models.append(cleaned)
        return models

    @staticmethod
    def _read_local_config_model() -> Optional[str]:
        """Read default model configured in ~/.codebuddy/settings.json if present."""
        config_path = Path(os.path.expanduser("~/.codebuddy/settings.json"))
        try:
            if not config_path.is_file():
                return None
            data = json.loads(config_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                model = data.get("model")
                if isinstance(model, str) and model.strip():
                    return model.strip()
        except Exception:
            return None
        return None

    def _discover_supported_models(self, binary: Optional[str] = None) -> list[str]:
        """Query `<binary> --help` (with TTL cache) to discover supported model IDs."""
        resolved = binary or self.resolve_binary()
        if not resolved:
            return []

        now = time.monotonic()
        cached = self._MODEL_DISCOVERY_CACHE.get(resolved)
        if cached is not None and (now - cached[0]) < self._MODEL_DISCOVERY_TTL_SEC:
            return list(cached[1])

        env = os.environ.copy()
        path_parts = [p for p in env.get("PATH", "").split(os.pathsep) if p]
        for extra_dir in self._common_bin_dirs():
            if extra_dir not in path_parts and os.path.isdir(extra_dir):
                path_parts.append(extra_dir)
        env["PATH"] = os.pathsep.join(path_parts)

        models: list[str] = []
        try:
            proc = subprocess.run(
                [resolved, "--help"],
                capture_output=True,
                text=True,
                timeout=5.0,
                check=False,
                env=env,
            )
            combined = f"{proc.stdout or ''}\n{proc.stderr or ''}"
            models = self._parse_supported_models_from_help(combined)
        except Exception:
            models = []

        self._MODEL_DISCOVERY_CACHE[resolved] = (now, list(models))
        return models

    @classmethod
    def _select_model_tiers(cls, supported_models: list[str]) -> dict[ThinkingLevel, str]:
        """Map dynamically discovered models onto low/medium/high tiers, falling back to hy4std."""
        if not supported_models:
            return {
                "low": cls.fallback_model,
                "medium": cls.fallback_model,
                "high": cls.fallback_model,
            }

        lower_map = {m.lower(): m for m in supported_models}

        medium = lower_map.get(cls.fallback_model.lower())
        if not medium:
            medium = next(
                (m for m in supported_models if "flash" not in m.lower() and "free" not in m.lower()),
                supported_models[0],
            )

        low = (
            lower_map.get("hy4flash")
            or lower_map.get("hy4flash-free")
            or next((m for m in supported_models if "flash" in m.lower()), None)
            or medium
        )

        high = (
            next(
                (
                    m
                    for m in supported_models
                    if m.lower().startswith("hy4")
                    and any(tag in m.lower() for tag in ("pro", "max", "t1"))
                ),
                None,
            )
            or lower_map.get(cls.fallback_model.lower())
            or next((m for m in supported_models if "pro" in m.lower()), None)
            or medium
        )

        return {
            "low": low,
            "medium": medium,
            "high": high,
        }

    def resolve_model_tiers(self, binary: Optional[str] = None) -> dict[ThinkingLevel, str]:
        """Return active model tiers resolved from CLI output with hy4std fallback."""
        supported = self._discover_supported_models(binary=binary)
        return self._select_model_tiers(supported)

    def select_profile(
        self,
        prompt: str,
        model_override: Optional[str] = None,
        thinking_override: Optional[ThinkingLevel] = None,
    ) -> ExecutionProfile:
        if thinking_override in ("low", "medium", "high"):
            level: ThinkingLevel = thinking_override
            reason = f"用户指定思考强度: {level}"
        else:
            level, reason = estimate_thinking_level(prompt)

        env_model = os.getenv(self.env_model_var, "").strip() if self.env_model_var else ""
        if model_override and model_override.strip():
            model = model_override.strip()
        elif env_model:
            model = env_model
        else:
            supported = self._discover_supported_models()
            local_model = self._read_local_config_model()
            if local_model and (not supported or local_model in supported):
                model = local_model
            else:
                tiers = self._select_model_tiers(supported)
                model = tiers.get(level) or tiers["medium"]

        return ExecutionProfile(model=model, thinking_level=level, reason=reason)

    def probe(self) -> AdapterInfo:
        info = super().probe()
        supported = self._discover_supported_models(binary=info.binary_path)
        tiers = self._select_model_tiers(supported)
        local_model = self._read_local_config_model()
        default_model = (
            local_model
            if local_model and (not supported or local_model in supported)
            else tiers["medium"]
        )
        return replace(info, default_model=default_model, models=dict(tiers))

    @staticmethod
    def _skip_permissions_enabled() -> bool:
        return os.getenv("KIKI_WORKBUDDY_SKIP_PERMISSIONS", "").strip() == "1"

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
        if self._skip_permissions_enabled():
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
        decision = str(decision_payload.get("decision") or "approve").strip()
        comment = str(decision_payload.get("comment") or "").strip()
        followup_prompt = f"用户已确认操作（决定: {decision}）。"
        if comment:
            followup_prompt += f"补充说明: {comment}。"
        followup_prompt += "请继续完成剩余任务。"

        argv = [
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
        if self._skip_permissions_enabled():
            argv.append("--dangerously-skip-permissions")
        return argv

    def build_env(self, ctx: AdapterContext, profile: ExecutionProfile) -> dict[str, str]:
        env = super().build_env(ctx, profile)
        env["CODEBUDDY_THINKING_LEVEL"] = profile.thinking_level
        return env

    def parse_json_event(
        self,
        event: dict[str, Any],
        state: StreamParseState,
        ctx: AdapterContext,
    ) -> None:
        # Capture session_id across CodeBuddy / WorkBuddy stream-json frames
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
                        elif block_type in ("thinking", "reasoning"):
                            thought = block.get("thinking") or block.get("text")
                            if isinstance(thought, str) and thought.strip():
                                ctx.update_current_step(
                                    f"{self.display_name} 深度思考中：{thought.strip()[:48]}"
                                )
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

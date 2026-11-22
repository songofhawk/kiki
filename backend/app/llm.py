"""Unified Gemini API client wrapping google-genai >= 3.2.0 Interactions API."""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, AsyncIterator, Optional

from google import genai

from .config import Settings
from .schemas import RouteDecision

logger = logging.getLogger(__name__)

DECISION_SYSTEM_PROMPT_TEMPLATE = """你是一个智能意图分流与 Topic 判定引擎。
你的职责：分析用户的新消息，决定它是属于现有的某个 Topic、需要新建一个 Topic，还是仅为前台轻量闲聊。

现有活跃 Topic 列表：
{active_topics_json}

请输出以下格式的 JSON，切勿包含其他多余字符：
{{
  "path": "fast" | "heavy",
  "topic_action": "reuse" | "create" | "none",
  "topic_id": "Existing Topic ID if reuse, else null",
  "new_topic_title": "Short title if create, else null",
  "confidence": 0.0 - 1.0,
  "reasoning": "简短判定理由"
}}

判定规则：
1. 若输入为简单的问候、感叹、前台对话交互，path 为 'fast'，topic_action 为 'none'。
2. 若输入涉及代码重构、长文生成、数据分析、多步骤操作、文件读写或命令行排查，path 为 'heavy'。
3. 置信度 confidence < 0.65 时，前端将展示 Kiki 思考并提供无感纠错胶囊。
"""

KIKI_FAST_SYSTEM_PROMPT = """你是 Kiki，一只聪明、温暖、略带一点傲娇小性格的手绘风蓝灰虎斑小猫个人智能体（Personal Agent）。
- 回答简洁、有温度、自然口语化，偶尔可以带一点点猫咪的可爱语气（但不要过度卖萌或啰嗦）。
- 优先使用与用户相同的语言（默认中文）。
- 当前所处话题：{topic_title}
"""


def _extract_json_block(text: str) -> str:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start != -1 and end != -1 and end > start:
        return cleaned[start : end + 1]
    return cleaned


class KikiLLMClient:
    """Async Gemini Interactions API wrapper designed for easy mocking in tests."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.model = settings.gemini_model
        self.fallback_model = settings.gemini_fallback_model
        self._client: Optional[genai.Client] = None
        self._primary_cooldown_until: float = 0.0

    def _get_client(self) -> genai.Client:
        if self._client is None:
            if not self.settings.api_key_configured:
                raise RuntimeError("GEMINI_API_KEY is not configured in .env")
            self._client = genai.Client(api_key=self.settings.gemini_api_key)
        return self._client

    def _candidate_models(self) -> list[str]:
        now = time.monotonic()
        models: list[str] = []
        if now >= self._primary_cooldown_until:
            models.append(self.model)
        if self.fallback_model and self.fallback_model != self.model:
            models.append(self.fallback_model)
        if not models:
            models.append(self.model)
        return models

    def _mark_primary_cooldown(self, model_name: str, exc: Exception) -> None:
        err_text = str(exc).lower()
        if model_name == self.model and (
            "503" in err_text
            or "service_unavailable" in err_text
            or "high demand" in err_text
            or "disconnected" in err_text
        ):
            self._primary_cooldown_until = time.monotonic() + 120.0
            logger.warning(
                "Primary model %s unavailable (%s); cooling down 120s and falling back to %s",
                self.model,
                type(exc).__name__,
                self.fallback_model,
            )

    async def route_message(
        self,
        content: str,
        active_topics: list[dict[str, Any]],
    ) -> RouteDecision:
        compact_topics = [
            {
                "id": t["id"],
                "title": t["title"],
                "summary": t.get("summary") or "",
            }
            for t in active_topics
        ]
        system_instruction = DECISION_SYSTEM_PROMPT_TEMPLATE.format(
            active_topics_json=json.dumps(compact_topics, ensure_ascii=False, indent=2)
        )
        last_exc: Optional[Exception] = None
        for model_name in self._candidate_models():
            try:
                client = self._get_client()
                interaction = await client.aio.interactions.create(
                    model=model_name,
                    input=content,
                    system_instruction=system_instruction,
                    response_format={
                        "type": "text",
                        "mime_type": "application/json",
                        "schema": RouteDecision.model_json_schema(),
                    },
                    generation_config={"thinking_level": "low"},
                    store=False,
                )
                raw_text = interaction.output_text or ""
                return RouteDecision.model_validate_json(_extract_json_block(raw_text))
            except Exception as exc:
                last_exc = exc
                self._mark_primary_cooldown(model_name, exc)
                logger.warning("Decision Model (%s) call failed: %s", model_name, exc)

        return RouteDecision(
            path="fast",
            topic_action="none",
            topic_id=None,
            new_topic_title=None,
            confidence=0.5,
            reasoning=f"路由模型暂不可用，降级为前台轻聊 ({type(last_exc).__name__ if last_exc else 'Error'})",
        )

    async def stream_fast_reply(
        self,
        content: str,
        history: list[dict[str, str]],
        topic_title: str,
    ) -> AsyncIterator[str]:
        system_instruction = KIKI_FAST_SYSTEM_PROMPT.format(topic_title=topic_title)
        # Format recent conversation context cleanly for stateless Interactions call
        context_lines: list[str] = []
        for msg in history[-10:]:
            role_label = "用户" if msg["role"] == "user" else "Kiki"
            if msg["content"].strip():
                context_lines.append(f"{role_label}: {msg['content']}")

        if context_lines:
            prompt_input = (
                "以下是本话题最近的对话记录：\n"
                + "\n".join(context_lines)
                + f"\n\n用户最新消息：\n{content}"
            )
        else:
            prompt_input = content

        last_exc: Optional[Exception] = None
        for model_name in self._candidate_models():
            yielded_any = False
            try:
                client = self._get_client()
                stream = await client.aio.interactions.create(
                    model=model_name,
                    input=prompt_input,
                    system_instruction=system_instruction,
                    generation_config={"thinking_level": "low"},
                    stream=True,
                    store=False,
                )
                async for event in stream:
                    ev_type = getattr(event, "event_type", None)
                    if ev_type == "error":
                        err_obj = getattr(event, "error", None)
                        raise RuntimeError(f"Stream ErrorEvent: {err_obj}")
                    if ev_type == "step.delta":
                        delta = getattr(event, "delta", None)
                        if delta is not None and getattr(delta, "type", None) == "text":
                            text_chunk = getattr(delta, "text", None)
                            if text_chunk:
                                yielded_any = True
                                yield text_chunk
                if yielded_any:
                    return
            except Exception as exc:
                last_exc = exc
                self._mark_primary_cooldown(model_name, exc)
                logger.warning("Fast-Path streaming (%s) failed: %s", model_name, exc)
                if yielded_any:
                    return

        err_str = str(last_exc) if last_exc else ""
        if "429" in err_str or "RESOURCE_EXHAUSTED" in err_str:
            yield "喵呜～Gemini API 刚刚触发了速率限制（429），稍等几秒再喊我吧！"
        elif not self.settings.api_key_configured:
            yield "喵～还没在 `.env` 里配置 `GEMINI_API_KEY` 呢，配好之后我就能陪你聊啦！"
        elif last_exc is not None:
            yield f"喵呜，刚才开小差出错了（{type(last_exc).__name__}），请稍后再试一次。"
        else:
            yield "喵？我刚刚走神了一下，能再说一遍吗？"

    async def run_agent_turn(
        self,
        input_data: Any,
        tools: list[dict[str, Any]],
        system_instruction: str,
        previous_interaction_id: Optional[str] = None,
        thinking_level: str = "medium",
    ) -> Any:
        client = self._get_client()
        last_exc: Optional[Exception] = None
        for model_name in self._candidate_models():
            kwargs: dict[str, Any] = {
                "model": model_name,
                "input": input_data,
                "tools": tools,
                "system_instruction": system_instruction,
                "generation_config": {"thinking_level": thinking_level},
                "store": True,
            }
            if previous_interaction_id:
                kwargs["previous_interaction_id"] = previous_interaction_id
            try:
                return await client.aio.interactions.create(**kwargs)
            except Exception as exc:
                last_exc = exc
                self._mark_primary_cooldown(model_name, exc)
                logger.warning("Agent turn (%s) failed: %s", model_name, exc)
                if previous_interaction_id:
                    # Cannot switch model mid-chain with previous_interaction_id
                    raise
        assert last_exc is not None
        raise last_exc

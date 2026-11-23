"""Unified Gemini API client wrapping google-genai >= 3.2.0 Interactions API."""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, AsyncIterator, Optional

from google import genai

from .config import Settings
from .schemas import BacktrackDecision, DreamConsolidationPlan, RouteDecision

logger = logging.getLogger(__name__)

DECISION_SYSTEM_PROMPT_TEMPLATE = """你是一个智能意图分流与 Topic 判定引擎。
你的职责：分析用户的新消息（结合最近对话上下文），决定它是属于现有的某个 Topic、需要新建一个 Topic，还是仅为前台轻量闲聊。

现有活跃 Topic 列表：
{active_topics_json}

最近对话上下文（按时间从早到晚）：
{recent_context_text}

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
1. 若输入仅为简单的问候、感叹或毫无延续主题的一次性闲聊，path 为 'fast'，topic_action 为 'none'。
2. 注意：`topic-general`（日常闲聊）仅用于 `topic_action='none'` 的兜底，切勿对 `topic-general` 返回 `topic_action='reuse'`。若结合最近上下文发现对话已经围绕某个具体主题展开（例如社保、就医、理财、旅行、技术排查等），应选择 `create` 新建简短话题（若已有对应非闲聊话题则 `reuse`）。
3. 若输入涉及代码重构、长文生成、数据分析、多步骤操作、文件读写、命令行排查，或需要联网搜索/实时信息查询/网页资料检索，path 为 'heavy'。
4. 置信度 confidence < 0.65 时，前端将展示 Kiki 思考并提供无感纠错胶囊。
"""

BACKTRACK_SYSTEM_PROMPT_TEMPLATE = """你是一个话题历史回溯整理助手。
刚刚从全局对话流中新识别/确立了目标话题，请从“日常闲聊（topic-general）”的候选历史消息中，找出实际上属于该目标话题的前置铺垫、提问或对应的回复。

目标话题：
{target_topic_json}

当前触发该话题的最新消息：
{trigger_content}

已确认归属该话题的较新消息（若为空表示刚开始回溯）：
{already_matched_text}

待检查的“日常闲聊”历史候选消息（按时间从早到晚排列）：
{candidates_text}

请输出 JSON，切勿包含其他多余字符：
{{
  "matched_message_ids": ["属于目标话题的候选消息 ID"],
  "reasoning": "简短说明判断依据"
}}

判定规则：
1. 仔细判断候选消息中哪些与目标话题属于同一件事/同一主题的延续（包括前面铺垫背景、追问以及紧随其后的 Kiki 回复）。
2. 纯粹无关的寒暄问候（如“早安”“哈哈”）或明显属于其他无关事件的闲聊不要选入。
3. 只能返回上述候选消息列表中真实存在的 `id`；若本批候选消息均不属于目标话题，请返回空数组 `[]`。
"""

DREAM_SYSTEM_PROMPT_TEMPLATE = """你是 Kiki 在后台空闲打盹时的“做梦记忆整理”引擎。
你的职责：在用户不知不觉中，安静地整理“日常闲聊（topic-general）”里被错放或逐渐成形的历史对话，并为缺少/过时摘要的活跃话题补全一句话摘要。

现有活跃话题（含已有摘要）：
{active_topics_json}

待整理的“日常闲聊”历史消息（按时间从早到晚排列）：
{general_messages_text}

缺少摘要或需要刷新摘要的话题近期内容：
{topics_to_summarize_text}

请输出 JSON，切勿包含其他多余字符：
{{
  "migrations": [
    {{
      "action": "move_to_existing" | "create_topic",
      "target_topic_id": "Existing Topic ID if move_to_existing, else null",
      "new_topic_title": "Short title if create_topic, else null",
      "new_topic_summary": "One-sentence summary if create_topic, else null",
      "message_ids": ["msg-..."],
      "confidence": 0.0 - 1.0,
      "reasoning": "简短说明归并或建话题理由"
    }}
  ],
  "topic_summaries": [
    {{
      "topic_id": "topic-...",
      "summary": "一句简明扼要的话题摘要（不超过40字）"
    }}
  ],
  "reasoning": "本次做梦整理的整体说明"
}}

整理原则（宁缺毋滥）：
1. 仅当确信“日常闲聊”里的某些消息明显属于某个已有非闲聊话题时，使用 `move_to_existing`（建议 `confidence >= 0.78`）。切勿把消息迁移到 `topic-general`。
2. 仅当“日常闲聊”里存在连续多条（至少 2 轮/3 条以上）围绕同一个具体主题展开的实质性对话、且现有活跃话题都无法覆盖时，才使用 `create_topic` 新建简短话题（建议 `confidence >= 0.82`）。
3. 纯寒暄（如“早安”“哈哈”“好的”）、一次性随口吐槽不要强行归类或建话题，保持在“日常闲聊”即可。
4. `message_ids` 必须严格来自上述待整理消息列表，并将对应的用户提问与 Kiki 回复一并选入。
5. 若暂无需要整理的内容，返回空的 `migrations: []`。
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

    async def _run_structured_json(
        self,
        *,
        input_text: str,
        system_instruction: str,
        schema_cls: type[Any],
        call_label: str,
    ) -> tuple[Optional[Any], Optional[Exception]]:
        last_exc: Optional[Exception] = None
        for model_name in self._candidate_models():
            try:
                client = self._get_client()
                interaction = await client.aio.interactions.create(
                    model=model_name,
                    input=input_text,
                    system_instruction=system_instruction,
                    response_format={
                        "type": "text",
                        "mime_type": "application/json",
                        "schema": schema_cls.model_json_schema(),
                    },
                    generation_config={"thinking_level": "low"},
                    store=False,
                )
                raw_text = interaction.output_text or ""
                return schema_cls.model_validate_json(_extract_json_block(raw_text)), None
            except Exception as exc:
                last_exc = exc
                self._mark_primary_cooldown(model_name, exc)
                logger.warning("%s (%s) call failed: %s", call_label, model_name, exc)
        return None, last_exc

    @staticmethod
    def _format_context_lines(messages: Optional[list[dict[str, Any]]], *, include_id: bool = False) -> str:
        if not messages:
            return "（无）"
        lines: list[str] = []
        for msg in messages:
            role_label = "用户" if msg.get("role") == "user" else "Kiki"
            topic_title = msg.get("topic_title")
            prefix = f"[{msg['id']}] " if include_id and msg.get("id") else ""
            topic_tag = f"(#{topic_title}) " if topic_title else ""
            content = " ".join(str(msg.get("content") or "").split())
            if len(content) > 240:
                content = content[:239] + "…"
            lines.append(f"- {prefix}{topic_tag}{role_label}: {content}")
        return "\n".join(lines) if lines else "（无）"

    async def route_message(
        self,
        content: str,
        active_topics: list[dict[str, Any]],
        recent_messages: Optional[list[dict[str, Any]]] = None,
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
            active_topics_json=json.dumps(compact_topics, ensure_ascii=False, indent=2),
            recent_context_text=self._format_context_lines(recent_messages),
        )
        parsed, last_exc = await self._run_structured_json(
            input_text=content,
            system_instruction=system_instruction,
            schema_cls=RouteDecision,
            call_label="Decision Model",
        )
        if parsed is not None:
            return parsed

        return RouteDecision(
            path="fast",
            topic_action="none",
            topic_id=None,
            new_topic_title=None,
            confidence=0.5,
            reasoning=f"路由模型暂不可用，降级为前台轻聊 ({type(last_exc).__name__ if last_exc else 'Error'})",
        )

    async def select_backtrack_messages(
        self,
        *,
        target_topic: dict[str, Any],
        trigger_content: str,
        candidate_messages: list[dict[str, Any]],
        already_matched_messages: Optional[list[dict[str, Any]]] = None,
    ) -> BacktrackDecision:
        if not candidate_messages:
            return BacktrackDecision(matched_message_ids=[], reasoning="无候选历史消息")

        compact_target = {
            "id": target_topic["id"],
            "title": target_topic["title"],
            "summary": target_topic.get("summary") or "",
        }
        system_instruction = BACKTRACK_SYSTEM_PROMPT_TEMPLATE.format(
            target_topic_json=json.dumps(compact_target, ensure_ascii=False, indent=2),
            trigger_content=trigger_content,
            already_matched_text=self._format_context_lines(
                already_matched_messages[-6:] if already_matched_messages else None
            ),
            candidates_text=self._format_context_lines(candidate_messages, include_id=True),
        )
        parsed, last_exc = await self._run_structured_json(
            input_text="请选出候选消息中属于目标话题的消息 ID。",
            system_instruction=system_instruction,
            schema_cls=BacktrackDecision,
            call_label="Backtrack Model",
        )
        if parsed is not None:
            return parsed

        return BacktrackDecision(
            matched_message_ids=[],
            reasoning=f"回溯模型暂不可用 ({type(last_exc).__name__ if last_exc else 'Error'})",
        )

    async def consolidate_dream(
        self,
        *,
        active_topics: list[dict[str, Any]],
        general_messages: list[dict[str, Any]],
        topics_to_summarize: Optional[list[dict[str, Any]]] = None,
    ) -> DreamConsolidationPlan:
        if not general_messages and not topics_to_summarize:
            return DreamConsolidationPlan(reasoning="暂无需要做梦整理的内容")

        compact_topics = [
            {
                "id": t["id"],
                "title": t["title"],
                "summary": t.get("summary") or "",
            }
            for t in active_topics
            if t.get("id") != "topic-general"
        ]
        summary_blocks: list[str] = []
        for t in topics_to_summarize or []:
            msgs = t.get("recent_messages") or []
            if not msgs:
                continue
            summary_blocks.append(
                f"话题 [{t['id']}] #{t['title']}（当前摘要：{t.get('summary') or '无'}）：\n"
                + self._format_context_lines(msgs)
            )

        system_instruction = DREAM_SYSTEM_PROMPT_TEMPLATE.format(
            active_topics_json=json.dumps(compact_topics, ensure_ascii=False, indent=2),
            general_messages_text=self._format_context_lines(general_messages, include_id=True),
            topics_to_summarize_text="\n\n".join(summary_blocks) if summary_blocks else "（无）",
        )
        parsed, last_exc = await self._run_structured_json(
            input_text="请整理待处理的日常闲聊记录并生成必要的话题摘要。",
            system_instruction=system_instruction,
            schema_cls=DreamConsolidationPlan,
            call_label="Dream Model",
        )
        if parsed is not None:
            return parsed

        return DreamConsolidationPlan(
            migrations=[],
            topic_summaries=[],
            reasoning=f"做梦模型暂不可用 ({type(last_exc).__name__ if last_exc else 'Error'})",
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

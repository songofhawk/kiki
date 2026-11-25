"""Unified Gemini API client wrapping google-genai >= 3.2.0 Interactions API."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid
from types import SimpleNamespace
from typing import Any, AsyncIterator, Optional

import httpx
from google import genai
from google.genai.types import HttpOptions, HttpRetryOptions

from .config import Settings
from .schemas import BacktrackDecision, DreamConsolidationPlan, RouteDecision

logger = logging.getLogger(__name__)

DECISION_SYSTEM_PROMPT_TEMPLATE = """你是一个智能意图分流与 Topic 判定引擎。
你的职责：分析用户的新消息（结合最近对话上下文），决定它是属于现有的某个 Topic、需要新建一个 Topic，还是仅为前台轻量闲聊。

现有活跃 Topic 列表：
{active_topics_json}

可用后台执行引擎（Heavy-Path Agents）列表：
{available_adapters_json}

最近对话上下文（按时间从早到晚）：
{recent_context_text}

请输出以下格式的 JSON，切勿包含其他多余字符：
{{
  "path": "fast" | "heavy",
  "topic_action": "reuse" | "create" | "none",
  "topic_id": "Existing Topic ID if reuse, else null",
  "new_topic_title": "Short title if create, else null",
  "agent_type": "Explicitly requested agent ID (e.g. codex, claude_code, gemini_agent, antigravity, deepseek_harness, zcode, kcode, qoder, workbuddy) if specified by user or continuing that agent's task, else null",
  "confidence": 0.0 - 1.0,
  "reasoning": "简短判定理由"
}}

判定规则：
1. 若输入仅为简单的问候、感叹或毫无延续主题的一次性闲聊，path 为 'fast'，topic_action 为 'none'。
2. 注意：`topic-general`（日常闲聊）仅用于 `topic_action='none'` 的兜底，切勿对 `topic-general` 返回 `topic_action='reuse'`。若结合最近上下文发现对话已经围绕某个具体主题展开（例如社保、就医、理财、旅行、技术排查等），应选择 `create` 新建简短话题（若已有对应非闲聊话题则 `reuse`）。
3. 若输入涉及代码重构、长文生成、图片/文件生成、数据分析、多步骤操作、文件读写、命令行排查、调用外部 Agent/工具，或需要联网搜索/实时信息查询/网页资料检索，path 为 'heavy'。
4. 置信度 confidence < 0.65 时，前端将展示 Kiki 思考并提供无感纠错胶囊。
5. 若最近上下文显示某条用户消息带有 `[后台任务执行失败...]`、`[后台任务已取消...]` 或 `[后台任务等待人工确认...]` 等状态，而用户随后发送“再试一次”“再生成一次”“重试”“继续”等追问，应 `reuse` 该原话题，并在需要重新执行任务时选择 `heavy`。
6. `agent_type` 判别规则：{agent_selection_rule}
7. 若用户正在纠正执行引擎并要求完成/重跑任务（例如“不是，我是让你用豆包工作来生成图，你为什么用 Codex？”），**必须**判为 `path: "heavy"`、`topic_action: "reuse"`，并将 `agent_type` 设为**用户期望使用的目标引擎**，绝不能判为 `fast` 闲聊。
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
- 回答简洁、有温度、自然口语化，偶尔可以带一点点猫咪的可爱语气（但不要过度卖萌或啰嗦，也不要发脾气、指责或甩锅给用户）。
- 优先使用与用户相同的语言（默认中文）。
- 当前所处话题：{topic_title}
- 重要约束：
  1. 绝不要向用户提及“前台纯对话模式 / Fast-Path / Heavy-Path / 无法切换后台引擎”等内部系统实现细节，也不要让用户自己去点输入框下方切换。
  2. 你当前这轮对话没有挂载任何外部工具或生图能力；如果用户要求生成图片、运行代码或调用外部工具，严禁假装正在执行或说“稍等马上就好”，只需简洁、礼貌地表示刚才没能成功拉起后台任务，请用户再直接发一句简洁的指令即可。
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
    """Async Gemini Interactions API wrapper with automatic DeepSeek paid fallback."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.model = settings.gemini_model
        all_models = settings.resolved_gemini_models
        self.fallback_models: list[str] = [m for m in all_models if m != self.model]
        self.fallback_model: str = self.fallback_models[0] if self.fallback_models else ""
        self.deepseek_model: str = settings.deepseek_model
        self.deepseek_base_url: str = settings.deepseek_base_url.rstrip("/")
        self.zhipu_model: str = settings.zhipu_model
        self.zhipu_base_url: str = settings.zhipu_base_url.rstrip("/")
        self._zhipu_cooldown_until: float = 0.0
        self.jev_model: str = settings.jev_model
        self.jev_base_url: str = settings.jev_base_url.rstrip("/")
        self._jev_cooldown_until: float = 0.0
        self._http_client: Optional[httpx.AsyncClient] = None
        self._client: Optional[genai.Client] = None
        self._cooldown_seconds: float = max(0.0, float(settings.gemini_cooldown_seconds))
        self._model_cooldowns: dict[str, float] = {}
        self._interaction_models: dict[str, str] = {}
        self._deepseek_sessions: dict[str, list[dict[str, Any]]] = {}
        self.last_used_provider: Optional[str] = None
        self.last_used_model: Optional[str] = None

    def _all_models(self) -> list[str]:
        models: list[str] = []
        seen: set[str] = set()
        for name in [self.model, *self.fallback_models]:
            cleaned = (name or "").strip()
            if cleaned and cleaned not in seen:
                seen.add(cleaned)
                models.append(cleaned)
        return models

    def _get_client(self) -> genai.Client:
        if self._client is None:
            if not self.settings.api_key_configured:
                raise RuntimeError("GEMINI_API_KEY is not configured in .env")
            # Fail fast on 503/429 when multiple fallback models (or OpenAI-compatible fallback)
            # are configured, avoiding google-genai's default 5-attempt (~30s+) backoff.
            has_failover = (
                len(self._all_models()) > 1
                or self.settings.deepseek_configured
                or self.settings.zhipu_configured
            )
            attempts = 1 if has_failover else 2
            self._client = genai.Client(
                api_key=self.settings.gemini_api_key,
                http_options=HttpOptions(
                    retry_options=HttpRetryOptions(
                        attempts=attempts,
                        initial_delay=0.5,
                        max_delay=2.0,
                    )
                ),
            )
        return self._client

    def _candidate_models(self, *, pinned_model: Optional[str] = None) -> list[str]:
        if pinned_model:
            return [pinned_model]
        if not self.settings.api_key_configured:
            return []
        now = time.monotonic()
        all_models = self._all_models()
        ready = [m for m in all_models if now >= self._model_cooldowns.get(m, 0.0)]
        if ready:
            return ready
        # If DeepSeek or Zhipu fallback is configured and all Gemini Free-Tier models are
        # cooling down, skip Gemini immediately (0ms overhead) until a cooldown expires
        # and automatically re-probes Gemini.
        if self.settings.deepseek_configured or (
            self.settings.zhipu_configured and now >= self._zhipu_cooldown_until
        ):
            return []
        # No DeepSeek fallback: try Gemini models in order of earliest cooldown expiry.
        return sorted(all_models, key=lambda m: self._model_cooldowns.get(m, 0.0))

    def _mark_model_cooldown(self, model_name: str, exc: Exception) -> None:
        err_text = str(exc).lower()
        is_unavailable_on_tier = (
            "404" in err_text
            or "not found" in err_text
            or "no longer available" in err_text
            or "limit: 0" in err_text
        )
        is_transient_overload = (
            "503" in err_text
            or "service_unavailable" in err_text
            or "high demand" in err_text
            or "overloaded" in err_text
            or "disconnected" in err_text
            or "timeout" in err_text
            or "timed out" in err_text
            or "429" in err_text
            or "too_many_requests" in err_text
            or "resource_exhausted" in err_text
            or isinstance(exc, TimeoutError)
        )
        if not (is_unavailable_on_tier or is_transient_overload):
            return

        duration = max(self._cooldown_seconds, 600.0) if is_unavailable_on_tier else self._cooldown_seconds
        self._model_cooldowns[model_name] = time.monotonic() + duration
        remaining = [
            m for m in self._all_models()
            if m != model_name and time.monotonic() >= self._model_cooldowns.get(m, 0.0)
        ]
        fallback_note = (
            remaining
            if remaining
            else (
                [f"deepseek:{self.deepseek_model}"]
                if self.settings.deepseek_configured
                else ["(all cooling down)"]
            )
        )
        logger.warning(
            "Model %s unavailable (%s); cooling down %.0fs; next fallbacks=%s",
            model_name,
            type(exc).__name__,
            duration,
            fallback_note,
        )

    def _remember_interaction_model(self, interaction_id: Optional[str], model_name: str) -> None:
        if not interaction_id:
            return
        self._interaction_models[interaction_id] = model_name
        if len(self._interaction_models) > 64:
            oldest_key = next(iter(self._interaction_models))
            self._interaction_models.pop(oldest_key, None)

    def _remember_deepseek_session(
        self,
        interaction_id: str,
        messages: list[dict[str, Any]],
    ) -> None:
        self._deepseek_sessions[interaction_id] = messages
        if len(self._deepseek_sessions) > 64:
            oldest_key = next(iter(self._deepseek_sessions))
            self._deepseek_sessions.pop(oldest_key, None)

    def status(self) -> dict[str, Any]:
        now = time.monotonic()
        gemini_models_status: list[dict[str, Any]] = []
        first_ready_gemini: Optional[str] = None
        for m in self._all_models():
            rem = max(0.0, self._model_cooldowns.get(m, 0.0) - now)
            ready = self.settings.api_key_configured and rem <= 0.0
            if ready and first_ready_gemini is None:
                first_ready_gemini = m
            gemini_models_status.append(
                {
                    "model": m,
                    "ready": ready,
                    "cooldown_remaining_seconds": round(rem, 1),
                }
            )

        if first_ready_gemini is not None:
            active_provider = "gemini"
            active_model = first_ready_gemini
        elif self.settings.zhipu_configured and now >= self._zhipu_cooldown_until:
            active_provider = "zhipu"
            active_model = self.zhipu_model
        elif self.settings.deepseek_configured:
            active_provider = "deepseek"
            active_model = self.deepseek_model
        elif self.settings.api_key_configured:
            active_provider = "gemini"
            active_model = self.model
        elif self.settings.zhipu_configured:
            active_provider = "zhipu"
            active_model = self.zhipu_model
        else:
            active_provider = "none"
            active_model = ""

        return {
            "active_provider": active_provider,
            "active_model": active_model,
            "gemini_configured": self.settings.api_key_configured,
            "deepseek_configured": self.settings.deepseek_configured,
            "deepseek_model": self.deepseek_model,
            "zhipu_configured": self.settings.zhipu_configured,
            "zhipu_model": self.zhipu_model,
            "jev_configured": self.settings.jev_configured,
            "jev_model": self.jev_model,
            "last_used_provider": self.last_used_provider,
            "last_used_model": self.last_used_model,
            "gemini_models": gemini_models_status,
        }

    async def probe_gemini_free_tier(self, *, timeout_seconds: float = 10.0) -> dict[str, Any]:
        """Actively probe Gemini Free Tier models in priority order and update cooldowns."""
        probe_results: list[dict[str, Any]] = []
        gemini_available = False

        if self.settings.api_key_configured:
            for model_name in self._all_models():
                t0 = time.perf_counter()
                try:
                    client = self._get_client()
                    await asyncio.wait_for(
                        client.aio.interactions.create(
                            model=model_name,
                            input='Reply with strictly JSON: {"ok": true}',
                            response_format={
                                "type": "text",
                                "mime_type": "application/json",
                                "schema": {
                                    "type": "object",
                                    "properties": {"ok": {"type": "boolean"}},
                                    "required": ["ok"],
                                },
                            },
                            generation_config={"thinking_level": "low"},
                            store=False,
                        ),
                        timeout=timeout_seconds,
                    )
                    latency_ms = round((time.perf_counter() - t0) * 1000.0, 1)
                    self._model_cooldowns.pop(model_name, None)
                    gemini_available = True
                    probe_results.append(
                        {"model": model_name, "ok": True, "latency_ms": latency_ms}
                    )
                    break
                except Exception as exc:
                    latency_ms = round((time.perf_counter() - t0) * 1000.0, 1)
                    self._mark_model_cooldown(model_name, exc)
                    probe_results.append(
                        {
                            "model": model_name,
                            "ok": False,
                            "latency_ms": latency_ms,
                            "error": f"{type(exc).__name__}: {str(exc)[:180]}",
                        }
                    )

        return {
            "gemini_available": gemini_available,
            "probe_results": probe_results,
            **self.status(),
        }

    # ------------------------------------------------------------------
    # DeepSeek OpenAI-compatible paid fallback helpers
    # ------------------------------------------------------------------

    def _deepseek_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.settings.deepseek_api_key.strip()}",
            "Content-Type": "application/json",
        }

    async def _openai_post_chat(
        self,
        *,
        base_url: str,
        api_key: str,
        payload: dict[str, Any],
        timeout: float = 60.0,
    ) -> dict[str, Any]:
        url = f"{base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {api_key.strip()}",
            "Content-Type": "application/json",
        }
        async with httpx.AsyncClient(timeout=timeout) as http_client:
            resp = await http_client.post(url, headers=headers, json=payload)
            resp.raise_for_status()
            return resp.json()

    async def _deepseek_post_chat(
        self,
        payload: dict[str, Any],
        *,
        timeout: float = 60.0,
    ) -> dict[str, Any]:
        return await self._openai_post_chat(
            base_url=self.deepseek_base_url,
            api_key=self.settings.deepseek_api_key,
            payload=payload,
            timeout=timeout,
        )

    async def _openai_stream_chat(
        self,
        *,
        base_url: str,
        api_key: str,
        payload: dict[str, Any],
        timeout: float = 90.0,
    ) -> AsyncIterator[str]:
        url = f"{base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {api_key.strip()}",
            "Content-Type": "application/json",
        }
        async with httpx.AsyncClient(timeout=timeout) as http_client:
            async with http_client.stream(
                "POST",
                url,
                headers=headers,
                json=payload,
            ) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    stripped = line.strip()
                    if not stripped or not stripped.startswith("data:"):
                        continue
                    data_str = stripped[5:].strip()
                    if data_str == "[DONE]":
                        break
                    try:
                        chunk_obj = json.loads(data_str)
                    except json.JSONDecodeError:
                        continue
                    choices = chunk_obj.get("choices") or []
                    if not choices:
                        continue
                    delta = choices[0].get("delta") or {}
                    text = delta.get("content")
                    if text:
                        yield str(text)

    async def _deepseek_stream_chat(
        self,
        payload: dict[str, Any],
        *,
        timeout: float = 90.0,
    ) -> AsyncIterator[str]:
        async for chunk in self._openai_stream_chat(
            base_url=self.deepseek_base_url,
            api_key=self.settings.deepseek_api_key,
            payload=payload,
            timeout=timeout,
        ):
            yield chunk

    async def _zhipu_stream_chat(
        self,
        payload: dict[str, Any],
        *,
        timeout: float = 90.0,
    ) -> AsyncIterator[str]:
        async for chunk in self._openai_stream_chat(
            base_url=self.zhipu_base_url,
            api_key=self.settings.zhipu_api_key,
            payload=payload,
            timeout=timeout,
        ):
            yield chunk

    @staticmethod
    def _convert_tools_to_openai(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
        converted: list[dict[str, Any]] = []
        for t in tools:
            if t.get("type") == "function" and "function" in t:
                converted.append(t)
            else:
                converted.append(
                    {
                        "type": "function",
                        "function": {
                            "name": t.get("name", ""),
                            "description": t.get("description", ""),
                            "parameters": t.get("parameters", {"type": "object", "properties": {}}),
                        },
                    }
                )
        return converted

    async def _run_openai_structured_json(
        self,
        *,
        provider_name: str,
        base_url: str,
        api_key: str,
        model_name: str,
        input_text: str,
        system_instruction: str,
        schema_cls: type[Any],
        timeout: float = 12.0,
        post_fn: Optional[Any] = None,
    ) -> Any:
        sys_prompt = system_instruction
        if "json" not in sys_prompt.lower():
            sys_prompt = f"{sys_prompt}\n\nReturn strictly valid JSON."
        payload: dict[str, Any] = {
            "model": model_name,
            "messages": [
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": input_text},
            ],
            "response_format": {"type": "json_object"},
            "max_tokens": 4096,
        }
        if post_fn is not None:
            data = await post_fn(payload, timeout=timeout)
        else:
            data = await self._openai_post_chat(
                base_url=base_url,
                api_key=api_key,
                payload=payload,
                timeout=timeout,
            )
        choices = data.get("choices") or []
        raw_text = ""
        if choices:
            raw_text = str((choices[0].get("message") or {}).get("content") or "")
        self.last_used_provider = provider_name
        self.last_used_model = model_name
        return schema_cls.model_validate_json(_extract_json_block(raw_text))

    async def _run_deepseek_structured_json(
        self,
        *,
        input_text: str,
        system_instruction: str,
        schema_cls: type[Any],
    ) -> Any:
        return await self._run_openai_structured_json(
            provider_name="deepseek",
            base_url=self.deepseek_base_url,
            api_key=self.settings.deepseek_api_key,
            model_name=self.deepseek_model,
            input_text=input_text,
            system_instruction=system_instruction,
            schema_cls=schema_cls,
            timeout=60.0,
            post_fn=self._deepseek_post_chat,
        )

    async def _run_zhipu_structured_json(
        self,
        *,
        input_text: str,
        system_instruction: str,
        schema_cls: type[Any],
        timeout: float = 12.0,
    ) -> Any:
        return await self._run_openai_structured_json(
            provider_name="zhipu",
            base_url=self.zhipu_base_url,
            api_key=self.settings.zhipu_api_key,
            model_name=self.zhipu_model,
            input_text=input_text,
            system_instruction=system_instruction,
            schema_cls=schema_cls,
            timeout=timeout,
        )

    async def _run_openai_agent_turn(
        self,
        *,
        provider_name: str,
        session_prefix: str,
        base_url: str,
        api_key: str,
        model_name: str,
        input_data: Any,
        tools: list[dict[str, Any]],
        system_instruction: str,
        previous_interaction_id: Optional[str] = None,
        post_fn: Optional[Any] = None,
    ) -> Any:
        if previous_interaction_id and previous_interaction_id in self._deepseek_sessions:
            messages = list(self._deepseek_sessions[previous_interaction_id])
        else:
            messages = [{"role": "system", "content": system_instruction}]

        if isinstance(input_data, list):
            for item in input_data:
                if isinstance(item, dict) and item.get("type") == "function_result":
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": str(item.get("call_id") or ""),
                            "content": str(item.get("result") or ""),
                        }
                    )
                else:
                    messages.append({"role": "user", "content": json.dumps(item, ensure_ascii=False)})
        else:
            messages.append({"role": "user", "content": str(input_data)})

        payload: dict[str, Any] = {
            "model": model_name,
            "messages": messages,
            "max_tokens": 8192,
        }
        if tools:
            payload["tools"] = self._convert_tools_to_openai(tools)
            payload["tool_choice"] = "auto"

        if post_fn is not None:
            data = await post_fn(payload, timeout=90.0)
        else:
            data = await self._openai_post_chat(
                base_url=base_url,
                api_key=api_key,
                payload=payload,
                timeout=90.0,
            )
        choices = data.get("choices") or []
        assistant_msg = (choices[0].get("message") or {}) if choices else {}
        output_text = str(assistant_msg.get("content") or "")
        raw_tool_calls = assistant_msg.get("tool_calls") or []

        steps: list[Any] = []
        for idx, tc in enumerate(raw_tool_calls):
            fn = tc.get("function") or {}
            raw_args = fn.get("arguments") or "{}"
            if isinstance(raw_args, str):
                try:
                    parsed_args = json.loads(raw_args)
                except json.JSONDecodeError:
                    parsed_args = {}
            elif isinstance(raw_args, dict):
                parsed_args = raw_args
            else:
                parsed_args = {}
            steps.append(
                SimpleNamespace(
                    type="function_call",
                    id=str(tc.get("id") or f"{session_prefix}call_{idx + 1}"),
                    name=str(fn.get("name") or "unknown"),
                    arguments=parsed_args,
                )
            )

        stored_assistant: dict[str, Any] = {"role": "assistant", "content": output_text}
        if raw_tool_calls:
            stored_assistant["tool_calls"] = raw_tool_calls
        messages.append(stored_assistant)

        interaction_id = f"{session_prefix}int_{uuid.uuid4().hex[:16]}"
        self._remember_deepseek_session(interaction_id, messages)
        self.last_used_provider = provider_name
        self.last_used_model = model_name
        return SimpleNamespace(
            id=interaction_id,
            status="completed",
            steps=steps,
            output_text=output_text,
        )

    async def _run_deepseek_agent_turn(
        self,
        *,
        input_data: Any,
        tools: list[dict[str, Any]],
        system_instruction: str,
        previous_interaction_id: Optional[str] = None,
    ) -> Any:
        return await self._run_openai_agent_turn(
            provider_name="deepseek",
            session_prefix="ds_",
            base_url=self.deepseek_base_url,
            api_key=self.settings.deepseek_api_key,
            model_name=self.deepseek_model,
            input_data=input_data,
            tools=tools,
            system_instruction=system_instruction,
            previous_interaction_id=previous_interaction_id,
            post_fn=self._deepseek_post_chat,
        )

    async def _run_zhipu_agent_turn(
        self,
        *,
        input_data: Any,
        tools: list[dict[str, Any]],
        system_instruction: str,
        previous_interaction_id: Optional[str] = None,
    ) -> Any:
        return await self._run_openai_agent_turn(
            provider_name="zhipu",
            session_prefix="zp_",
            base_url=self.zhipu_base_url,
            api_key=self.settings.zhipu_api_key,
            model_name=self.zhipu_model,
            input_data=input_data,
            tools=tools,
            system_instruction=system_instruction,
            previous_interaction_id=previous_interaction_id,
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

        if self.settings.zhipu_configured and time.monotonic() >= self._zhipu_cooldown_until:
            try:
                parsed = await self._run_zhipu_structured_json(
                    input_text=input_text,
                    system_instruction=system_instruction,
                    schema_cls=schema_cls,
                    timeout=12.0,
                )
                return parsed, None
            except Exception as exc:
                last_exc = exc
                self._zhipu_cooldown_until = time.monotonic() + min(self._cooldown_seconds, 60.0)
                logger.warning(
                    "%s (zhipu:%s) call failed, falling back: %s",
                    call_label,
                    self.zhipu_model,
                    exc,
                )

        for model_name in self._candidate_models():
            try:
                client = self._get_client()
                interaction = await asyncio.wait_for(
                    client.aio.interactions.create(
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
                    ),
                    timeout=12.0,
                )
                raw_text = interaction.output_text or ""
                parsed = schema_cls.model_validate_json(_extract_json_block(raw_text))
                self.last_used_provider = "gemini"
                self.last_used_model = model_name
                return parsed, None
            except Exception as exc:
                last_exc = exc
                self._mark_model_cooldown(model_name, exc)
                logger.warning("%s (%s) call failed: %s", call_label, model_name, exc)

        if self.settings.deepseek_configured:
            try:
                logger.info(
                    "%s falling back to paid DeepSeek (%s)",
                    call_label,
                    self.deepseek_model,
                )
                parsed = await self._run_deepseek_structured_json(
                    input_text=input_text,
                    system_instruction=system_instruction,
                    schema_cls=schema_cls,
                )
                return parsed, None
            except Exception as exc:
                last_exc = exc
                logger.warning(
                    "%s (deepseek:%s) fallback failed: %s",
                    call_label,
                    self.deepseek_model,
                    exc,
                )

        return None, last_exc

    @staticmethod
    def _format_session_run_note(
        session_run: Optional[dict[str, Any]],
        *,
        role: Optional[str] = None,
    ) -> str:
        if not isinstance(session_run, dict):
            return ""
        status = str(session_run.get("status") or "").strip().lower()
        if not status:
            return ""
        agent_type = str(session_run.get("agent_type") or "agent").strip()
        if status == "failed":
            err = " ".join(str(session_run.get("error_message") or "").split())
            if len(err) > 100:
                err = err[:99] + "…"
            return f" [后台任务执行失败({agent_type}): {err}]" if err else f" [后台任务执行失败({agent_type})]"
        if status == "cancelled":
            return f" [后台任务已取消({agent_type})]"
        if status == "waiting_input":
            return f" [后台任务等待人工确认({agent_type})]"
        if status in ("running", "queued"):
            return f" [后台任务进行中({agent_type})]"
        if status == "completed" and role == "user":
            return f" [已触发后台任务({agent_type}): 已完成]"
        return ""

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
            run_note = KikiLLMClient._format_session_run_note(
                msg.get("session_run"), role=msg.get("role")
            )
            lines.append(f"- {prefix}{topic_tag}{role_label}: {content}{run_note}")
        return "\n".join(lines) if lines else "（无）"

    def _get_http_client(self) -> httpx.AsyncClient:
        if self._http_client is None or self._http_client.is_closed:
            self._http_client = httpx.AsyncClient(timeout=60.0)
        return self._http_client

    async def _jev_post_systemone(
        self,
        payload: dict[str, Any],
        *,
        timeout: float = 10.0,
    ) -> dict[str, Any]:
        url = f"{self.jev_base_url}/systemone"
        headers = {
            "Authorization": f"Bearer {self.settings.jev_api_key.strip()}",
            "Content-Type": "application/json",
        }
        client = self._get_http_client()
        resp = await client.post(url, headers=headers, json=payload, timeout=timeout)
        resp.raise_for_status()
        return resp.json()

    @staticmethod
    def _derive_topic_title_from_text(content: str) -> str:
        cleaned = " ".join((content or "").strip().split())
        for prefix in ("请帮我", "帮我", "麻烦", "请问", "我想", "我要", "哎，", "哎,", "那个"):
            if cleaned.startswith(prefix):
                cleaned = cleaned[len(prefix) :].lstrip("，,：: ")
        cleaned = cleaned.strip("？?！!。.,，、\"'“”")
        if len(cleaned) > 14:
            return cleaned[:13] + "…"
        return cleaned or "新话题"

    async def _route_with_jev(
        self,
        content: str,
        active_topics: list[dict[str, Any]],
        recent_messages: Optional[list[dict[str, Any]]] = None,
        available_adapters: Optional[list[dict[str, Any]]] = None,
        forced_agent_type: Optional[str] = None,
    ) -> RouteDecision:
        non_general_topics = [
            {
                "id": t["id"],
                "title": t["title"],
                "summary": t.get("summary") or "",
            }
            for t in active_topics
            if t.get("id") != "topic-general"
        ]
        state_obj = {
            "user_message": content,
            "recent_context": self._format_context_lines(recent_messages),
            "active_topics": non_general_topics,
        }
        questions: dict[str, Any] = {
            "path": {
                "type": "choice",
                "instructions": (
                    "Decide whether the user message should be handled by 'fast' (casual chat, simple greeting) "
                    "or 'heavy' (coding, refactoring, file ops, CLI, image/file generation, web search, "
                    "external agents like codex/claude_code/kcode/workbuddy, or retrying a failed background task)."
                ),
                "criteria": {
                    "fast": "Simple greeting, casual chitchat, or pure Q&A without tools/search/agents",
                    "heavy": "Requires tools, coding, file ops, web search, external agent execution, or task retry",
                },
            },
            "topic_action": {
                "type": "choice",
                "instructions": (
                    "Decide how to assign the message to a conversation topic. "
                    "Never reuse topic-general; use 'none' for pure casual chitchat."
                ),
                "criteria": {
                    "none": "One-off greeting, exclamation, or casual chitchat with no specific ongoing theme",
                    "reuse": "Clearly continues or relates to one of the existing non-general active_topics",
                    "create": "Starts a concrete new subject/task not covered by existing active_topics",
                },
            },
        }
        if not forced_agent_type:
            agent_criteria: dict[str, str] = {
                "none": "No specific agent explicitly requested",
            }
            if available_adapters:
                for adapter in available_adapters:
                    aid = str(adapter.get("id") or "").strip()
                    if not aid:
                        continue
                    aname = str(adapter.get("name") or aid).strip()
                    aliases = ", ".join(adapter.get("aliases") or [])
                    avail_tag = "available" if adapter.get("available", True) else "unavailable"
                    agent_criteria[aid] = (
                        f"{aname} [{avail_tag}]" + (f" (aliases: {aliases})" if aliases else "")
                    )
            else:
                agent_criteria.update(
                    {
                        "codex": "OpenAI Codex",
                        "claude_code": "Claude Code",
                        "gemini_agent": "Gemini Agent",
                        "antigravity": "Antigravity",
                        "deepseek_harness": "DeepSeek Harness",
                        "zcode": "ZCode",
                        "kcode": "KCode",
                        "qoder": "Qoder",
                        "workbuddy": "WorkBuddy",
                    }
                )
            questions["agent_type"] = {
                "type": "choice",
                "instructions": (
                    "Identify the execution agent the user wants to use (handling corrections like "
                    "'I asked for A, why did you use B' by choosing A), or continuing a prior topic run, "
                    "or 'none' if not specified."
                ),
                "criteria": agent_criteria,
            }
        if non_general_topics:
            topic_criteria = {
                t["id"]: f"{t['title']} ({t['summary']})" if t["summary"] else t["title"]
                for t in non_general_topics
            }
            topic_criteria["__none__"] = "None of the existing topics match"
            questions["topic_id"] = {
                "type": "choice",
                "instructions": "If the message belongs to an existing topic in active_topics, select its ID; otherwise '__none__'.",
                "criteria": topic_criteria,
            }

        data = await self._jev_post_systemone(
            {"model": self.jev_model, "state": state_obj, "questions": questions},
            timeout=10.0,
        )
        answers = data.get("answers") or {}

        path_ans = answers.get("path") or {}
        path_val = "heavy" if path_ans.get("choice") == "heavy" else "fast"

        action_ans = answers.get("topic_action") or {}
        action_val = str(action_ans.get("choice") or "none")
        if action_val not in ("none", "reuse", "create"):
            action_val = "none"

        topic_ans = answers.get("topic_id") or {}
        selected_topic_id = topic_ans.get("choice")
        valid_topic_ids = {t["id"] for t in non_general_topics}

        resolved_topic_id: Optional[str] = None
        new_topic_title: Optional[str] = None

        if action_val == "reuse":
            if selected_topic_id in valid_topic_ids:
                resolved_topic_id = str(selected_topic_id)
            else:
                action_val = "create"
                new_topic_title = self._derive_topic_title_from_text(content)
        elif action_val == "create":
            new_topic_title = self._derive_topic_title_from_text(content)

        if forced_agent_type:
            agent_type: Optional[str] = forced_agent_type
        else:
            agent_ans = answers.get("agent_type") or {}
            agent_choice = agent_ans.get("choice")
            agent_type = str(agent_choice) if agent_choice and agent_choice != "none" else None

        # Use calibrated probability of the chosen topic_action (or path) as confidence
        probs = action_ans.get("probabilities") or {}
        raw_prob = probs.get(action_val)
        if isinstance(raw_prob, (int, float)):
            confidence = max(0.0, min(1.0, float(raw_prob)))
        else:
            path_probs = path_ans.get("probabilities") or {}
            confidence = float(path_probs.get(path_val, 0.85))

        used_model = str(data.get("model") or self.jev_model)
        self.last_used_provider = "jev"
        self.last_used_model = used_model

        return RouteDecision(
            path=path_val,
            topic_action=action_val,  # type: ignore[arg-type]
            topic_id=resolved_topic_id,
            new_topic_title=new_topic_title,
            agent_type=agent_type,
            confidence=round(confidence, 3),
            reasoning=f"Jev ({used_model}) 判定: path={path_val}, topic={action_val} (p={confidence:.2f})",
        )

    async def route_message(
        self,
        content: str,
        active_topics: list[dict[str, Any]],
        recent_messages: Optional[list[dict[str, Any]]] = None,
        available_adapters: Optional[list[dict[str, Any]]] = None,
        forced_agent_type: Optional[str] = None,
    ) -> RouteDecision:
        if self.settings.jev_configured and time.monotonic() >= self._jev_cooldown_until:
            try:
                return await self._route_with_jev(
                    content,
                    active_topics,
                    recent_messages=recent_messages,
                    available_adapters=available_adapters,
                    forced_agent_type=forced_agent_type,
                )
            except Exception as exc:
                self._jev_cooldown_until = time.monotonic() + min(self._cooldown_seconds, 60.0)
                logger.warning("Decision Model (jev:%s) call failed, falling back: %s", self.jev_model, exc)

        compact_topics = [
            {
                "id": t["id"],
                "title": t["title"],
                "summary": t.get("summary") or "",
            }
            for t in active_topics
        ]
        compact_adapters = [
            {
                "id": a.get("id"),
                "name": a.get("name"),
                "aliases": a.get("aliases") or [],
                "available": bool(a.get("available", True)),
                "description": a.get("description") or "",
            }
            for a in (available_adapters or [])
        ]
        if forced_agent_type:
            agent_selection_rule = (
                f"用户已显式指定执行引擎 `{forced_agent_type}`，无需再判别引擎，`agent_type` 直接填 `{forced_agent_type}`（fast 路径填 null）。"
            )
        else:
            agent_selection_rule = (
                "当 path='heavy' 时，请结合用户语义、历史任务上下文以及上述可用引擎列表（含 aliases 与 description）"
                "综合判别最合适的 `agent_type`：若用户明确点名或纠正为某引擎（或延续该话题先前使用的引擎），输出对应引擎 `id`；"
                "若无特定引擎偏好，可输出 `gemini_agent`（或 `null`）。当 path='fast' 时输出 `null`。"
            )
        system_instruction = DECISION_SYSTEM_PROMPT_TEMPLATE.format(
            active_topics_json=json.dumps(compact_topics, ensure_ascii=False, indent=2),
            available_adapters_json=(
                json.dumps(compact_adapters, ensure_ascii=False, indent=2)
                if compact_adapters
                else "（默认使用 gemini_agent）"
            ),
            recent_context_text=self._format_context_lines(recent_messages),
            agent_selection_rule=agent_selection_rule,
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
            content_text = str(msg.get("content") or "").strip()
            run_note = self._format_session_run_note(
                msg.get("session_run"), role=msg.get("role")
            )
            if content_text or run_note:
                context_lines.append(f"{role_label}: {content_text}{run_note}")

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
                    self.last_used_provider = "gemini"
                    self.last_used_model = model_name
                    return
            except Exception as exc:
                last_exc = exc
                self._mark_model_cooldown(model_name, exc)
                logger.warning("Fast-Path streaming (%s) failed: %s", model_name, exc)
                if yielded_any:
                    return

        if self.settings.zhipu_configured and time.monotonic() >= self._zhipu_cooldown_until:
            yielded_any = False
            try:
                logger.info(
                    "Fast-Path streaming falling back to Zhipu (%s)",
                    self.zhipu_model,
                )
                payload = {
                    "model": self.zhipu_model,
                    "messages": [
                        {"role": "system", "content": system_instruction},
                        {"role": "user", "content": prompt_input},
                    ],
                    "stream": True,
                    "max_tokens": 4096,
                }
                async for text_chunk in self._zhipu_stream_chat(payload):
                    yielded_any = True
                    yield text_chunk
                if yielded_any:
                    self.last_used_provider = "zhipu"
                    self.last_used_model = self.zhipu_model
                    return
            except Exception as exc:
                last_exc = exc
                self._zhipu_cooldown_until = time.monotonic() + min(self._cooldown_seconds, 60.0)
                logger.warning(
                    "Fast-Path streaming (zhipu:%s) fallback failed: %s",
                    self.zhipu_model,
                    exc,
                )
                if yielded_any:
                    return

        if self.settings.deepseek_configured:
            yielded_any = False
            try:
                logger.info(
                    "Fast-Path streaming falling back to paid DeepSeek (%s)",
                    self.deepseek_model,
                )
                payload = {
                    "model": self.deepseek_model,
                    "messages": [
                        {"role": "system", "content": system_instruction},
                        {"role": "user", "content": prompt_input},
                    ],
                    "stream": True,
                    "max_tokens": 4096,
                }
                async for text_chunk in self._deepseek_stream_chat(payload):
                    yielded_any = True
                    yield text_chunk
                if yielded_any:
                    self.last_used_provider = "deepseek"
                    self.last_used_model = self.deepseek_model
                    return
            except Exception as exc:
                last_exc = exc
                logger.warning(
                    "Fast-Path streaming (deepseek:%s) fallback failed: %s",
                    self.deepseek_model,
                    exc,
                )
                if yielded_any:
                    return

        err_str = str(last_exc) if last_exc else ""
        if "429" in err_str or "RESOURCE_EXHAUSTED" in err_str:
            yield "喵呜～API 刚刚触发了速率限制（429），稍等几秒再喊我吧！"
        elif "503" in err_str or "SERVICE_UNAVAILABLE" in err_str or "high demand" in err_str.lower():
            yield "喵呜～Gemini API 多个备选模型目前都处于高负载（503），稍等几秒再试试吧！"
        elif not self.settings.any_llm_configured:
            yield "喵～还没在 `.env` 里配置 `GEMINI_API_KEY` 或 `DEEPSEEK_API_KEY` 呢，配好之后我就能陪你聊啦！"
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
        if previous_interaction_id and previous_interaction_id.startswith("ds_int_"):
            return await self._run_deepseek_agent_turn(
                input_data=input_data,
                tools=tools,
                system_instruction=system_instruction,
                previous_interaction_id=previous_interaction_id,
            )
        if previous_interaction_id and previous_interaction_id.startswith("zp_int_"):
            return await self._run_zhipu_agent_turn(
                input_data=input_data,
                tools=tools,
                system_instruction=system_instruction,
                previous_interaction_id=previous_interaction_id,
            )

        pinned_model = (
            self._interaction_models.get(previous_interaction_id)
            if previous_interaction_id
            else None
        )
        last_exc: Optional[Exception] = None
        candidates = self._candidate_models(pinned_model=pinned_model)
        if candidates:
            client = self._get_client()
            for model_name in candidates:
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
                    interaction = await client.aio.interactions.create(**kwargs)
                    self._remember_interaction_model(getattr(interaction, "id", None), model_name)
                    self.last_used_provider = "gemini"
                    self.last_used_model = model_name
                    return interaction
                except Exception as exc:
                    last_exc = exc
                    self._mark_model_cooldown(model_name, exc)
                    logger.warning("Agent turn (%s) failed: %s", model_name, exc)
                    if previous_interaction_id:
                        # Cannot switch model mid-chain with previous_interaction_id
                        raise

        if (
            not previous_interaction_id
            and self.settings.zhipu_configured
            and time.monotonic() >= self._zhipu_cooldown_until
        ):
            try:
                logger.info(
                    "Agent turn falling back to Zhipu (%s)",
                    self.zhipu_model,
                )
                return await self._run_zhipu_agent_turn(
                    input_data=input_data,
                    tools=tools,
                    system_instruction=system_instruction,
                    previous_interaction_id=None,
                )
            except Exception as exc:
                last_exc = exc
                self._zhipu_cooldown_until = time.monotonic() + min(self._cooldown_seconds, 60.0)
                logger.warning("Agent turn (zhipu:%s) fallback failed: %s", self.zhipu_model, exc)

        if not previous_interaction_id and self.settings.deepseek_configured:
            logger.info(
                "Agent turn falling back to paid DeepSeek (%s)",
                self.deepseek_model,
            )
            return await self._run_deepseek_agent_turn(
                input_data=input_data,
                tools=tools,
                system_instruction=system_instruction,
                previous_interaction_id=None,
            )

        if last_exc is not None:
            raise last_exc
        raise RuntimeError("No LLM provider configured (set GEMINI_API_KEY or DEEPSEEK_API_KEY)")

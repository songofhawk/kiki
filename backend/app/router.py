"""Decision Router and Fast-Path streaming reply service for Kiki V1."""

from __future__ import annotations

import asyncio
import inspect
import logging
from typing import Any, Callable, Literal, Optional

from .adapters import AdapterRegistry
from .db import GENERAL_TOPIC_ID, Database
from .llm import KikiLLMClient
from .schemas import RouteDecision
from .sse import EventBus

logger = logging.getLogger(__name__)


class DecisionRouter:
    """Routes incoming messages to Fast-Path or Heavy-Path and resolves Topic assignment."""

    def __init__(
        self,
        db: Database,
        llm: KikiLLMClient,
        event_bus: EventBus,
        *,
        adapter_registry: Optional[AdapterRegistry] = None,
        status_provider: Optional[Callable[[], str]] = None,
        backtrack_batch_size: int = 6,
        max_backtrack_batches: int = 6,
        recent_context_limit: int = 10,
    ) -> None:
        self.db = db
        self.llm = llm
        self.event_bus = event_bus
        self.adapter_registry = adapter_registry
        self._status_provider = status_provider
        self.backtrack_batch_size = backtrack_batch_size
        self.max_backtrack_batches = max_backtrack_batches
        self.recent_context_limit = recent_context_limit
        self._active_routes: int = 0

    @property
    def is_busy(self) -> bool:
        return self._active_routes > 0

    def set_status_provider(self, provider: Optional[Callable[[], str]]) -> None:
        self._status_provider = provider

    def _available_adapters_payload(self) -> list[dict[str, Any]]:
        if self.adapter_registry is None:
            return []
        return [info.to_dict() for info in self.adapter_registry.list_infos()]

    async def _call_route_message(
        self,
        content: str,
        active_topics: list[dict[str, Any]],
        recent_messages: list[dict[str, Any]],
        *,
        forced_agent_type: Optional[str] = None,
    ) -> RouteDecision:
        sig = inspect.signature(self.llm.route_message)
        has_varkw = any(
            p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
        )
        kwargs: dict[str, Any] = {}
        if has_varkw or "recent_messages" in sig.parameters:
            kwargs["recent_messages"] = recent_messages
        if has_varkw or "available_adapters" in sig.parameters:
            kwargs["available_adapters"] = self._available_adapters_payload()
        if has_varkw or "forced_agent_type" in sig.parameters:
            kwargs["forced_agent_type"] = forced_agent_type
        return await self.llm.route_message(content, active_topics, **kwargs)

    async def _backtrack_general_messages(
        self,
        *,
        target_topic: dict[str, Any],
        trigger_content: str,
    ) -> list[dict[str, Any]]:
        """Iteratively scan 日常闲聊 upwards and pull older messages belonging to target_topic."""
        if not hasattr(self.llm, "select_backtrack_messages"):
            return []

        cursor_rowid: Optional[int] = None
        reassigned_all: list[dict[str, Any]] = []
        seen_ids: set[str] = set()

        for _batch_idx in range(self.max_backtrack_batches):
            candidates, min_candidate_rowid = self.db.list_topic_messages_page_before(
                GENERAL_TOPIC_ID,
                before_rowid=cursor_rowid,
                limit=self.backtrack_batch_size,
            )
            if not candidates or min_candidate_rowid is None:
                break

            decision = await self.llm.select_backtrack_messages(
                target_topic=target_topic,
                trigger_content=trigger_content,
                candidate_messages=candidates,
                already_matched_messages=reassigned_all,
            )
            candidate_ids = {m["id"] for m in candidates}
            matched_ids = [
                mid
                for mid in dict.fromkeys(decision.matched_message_ids)
                if mid in candidate_ids and mid not in seen_ids
            ]
            if not matched_ids:
                break

            expanded_ids, min_expanded_rowid = self.db.expand_paired_topic_messages(
                GENERAL_TOPIC_ID, matched_ids
            )
            to_reassign = [mid for mid in expanded_ids if mid not in seen_ids]
            if not to_reassign:
                break

            updated_batch = self.db.reassign_messages_topic(
                to_reassign, target_topic["id"], backtracked=True
            )
            if not updated_batch:
                break

            for msg in updated_batch:
                seen_ids.add(msg["id"])
            # Prepend older batch before newer batches to maintain chronological order
            reassigned_all = updated_batch + reassigned_all

            # Continue backtracking upwards strictly older than this batch (and any expanded pair)
            next_cursor = min_candidate_rowid
            if min_expanded_rowid is not None and min_expanded_rowid < next_cursor:
                next_cursor = min_expanded_rowid
            cursor_rowid = next_cursor

        if reassigned_all:
            enriched_out: list[dict[str, Any]] = []
            for msg in reassigned_all:
                if msg.get("session_run"):
                    msg = dict(msg)
                    msg["session_run"] = self.event_bus.enrich_run(msg["session_run"])
                enriched_out.append(msg)
                self.event_bus.publish("message_updated", {"message": msg})

            general_topic = self.db.get_topic(GENERAL_TOPIC_ID)
            if general_topic:
                self.event_bus.publish("topic_updated", {"topic": general_topic})
            refreshed_target = self.db.get_topic(target_topic["id"])
            if refreshed_target:
                self.event_bus.publish("topic_updated", {"topic": refreshed_target})
            return enriched_out

        return []

    async def route(
        self,
        content: str,
        focus_topic_id: Optional[str] = None,
        force_path: Optional[Literal["fast", "heavy"]] = None,
        force_agent_type: Optional[str] = None,
    ) -> tuple[RouteDecision, dict[str, Any], bool, list[dict[str, Any]]]:
        """Return (RouteDecision, resolved_topic_dict, topic_created, reassigned_messages)."""
        self._active_routes += 1
        published_thinking = False
        had_error = False
        explicit_agent = (
            self.adapter_registry.try_resolve_type(force_agent_type)
            if self.adapter_registry is not None
            else (force_agent_type.strip() if force_agent_type else None)
        )
        try:
            # 1. Topic Focus Mode locks topic assignment, while still classifying path when force_path is auto (None)
            if focus_topic_id:
                topic = self.db.get_topic(focus_topic_id)
                if topic is None:
                    raise KeyError(f"Topic not found: {focus_topic_id}")
                if force_path is not None:
                    decision = RouteDecision(
                        path=force_path,
                        topic_action="reuse",
                        topic_id=topic["id"],
                        new_topic_title=None,
                        agent_type=explicit_agent if force_path == "heavy" else None,
                        confidence=1.0,
                        reasoning="Topic Focus Mode 锁定当前话题（手动指定路径）",
                    )
                    return decision, topic, False, []

                published_thinking = True
                self.event_bus.publish_kiki_status("thinking", reason="Kiki 正在判断当前话题内的任务意图...")
                topic_recent_messages = self.db.get_recent_messages_with_topic(
                    limit=self.recent_context_limit,
                    topic_id=topic["id"],
                )
                raw_decision = await self._call_route_message(
                    content,
                    [topic],
                    topic_recent_messages,
                    forced_agent_type=explicit_agent,
                )
                resolved_agent = explicit_agent or (
                    self.adapter_registry.try_resolve_type(raw_decision.agent_type)
                    if self.adapter_registry is not None
                    else raw_decision.agent_type
                )
                promoted_path = (
                    "heavy"
                    if raw_decision.path == "fast" and raw_decision.agent_type and not explicit_agent
                    else raw_decision.path
                )
                decision = raw_decision.model_copy(
                    update={
                        "path": promoted_path,
                        "topic_action": "reuse",
                        "topic_id": topic["id"],
                        "new_topic_title": None,
                        "agent_type": resolved_agent if promoted_path == "heavy" else None,
                        "confidence": 1.0,
                    }
                )
                return decision, topic, False, []

            # 2. All Stream mode: invoke Decision Model with recent context
            published_thinking = True
            self.event_bus.publish_kiki_status("thinking", reason="Kiki 正在判断意图与话题...")
            active_topics = self.db.list_topics(include_archived=False)
            recent_messages = self.db.get_recent_messages_with_topic(
                limit=self.recent_context_limit
            )
            latest_msg = self.db.get_latest_message()
            decision = await self._call_route_message(
                content,
                active_topics,
                recent_messages,
                forced_agent_type=explicit_agent,
            )

            if force_path is not None:
                decision = decision.model_copy(update={"path": force_path})
            elif decision.path == "fast" and decision.agent_type and not explicit_agent:
                decision = decision.model_copy(update={"path": "heavy"})

            resolved_agent = explicit_agent or (
                self.adapter_registry.try_resolve_type(decision.agent_type)
                if self.adapter_registry is not None
                else decision.agent_type
            )
            decision = decision.model_copy(
                update={"agent_type": resolved_agent if decision.path == "heavy" else None}
            )

            # 3. Resolve topic_action
            topic_created = False
            topic: Optional[dict[str, Any]] = None

            if decision.topic_action == "reuse" and decision.topic_id:
                topic = self.db.get_topic(decision.topic_id)
                if topic is None:
                    # Model hallucinated a non-existent topic ID; fallback gracefully
                    title = decision.new_topic_title or self._fallback_title(content)
                    topic = self.db.create_topic(title=title)
                    topic_created = True
                    decision = decision.model_copy(
                        update={"topic_action": "create", "topic_id": topic["id"], "new_topic_title": title}
                    )
            elif decision.topic_action == "create":
                title = (decision.new_topic_title or "").strip() or self._fallback_title(content)
                topic = self.db.create_topic(title=title)
                topic_created = True
                decision = decision.model_copy(update={"topic_id": topic["id"], "new_topic_title": title})
            else:
                # topic_action == "none": bind to default general lounge topic to satisfy FK
                topic = self.db.ensure_general_topic()
                decision = decision.model_copy(update={"topic_id": topic["id"]})

            if topic_created:
                self.event_bus.publish("topic_created", {"topic": topic})

            # 4. Backtrack 日常闲聊 upwards if a real topic was created or resumed right after casual chat
            reassigned_messages: list[dict[str, Any]] = []
            should_backtrack = topic["id"] != GENERAL_TOPIC_ID and (
                topic_created
                or (
                    decision.topic_action == "reuse"
                    and latest_msg is not None
                    and latest_msg["topic_id"] == GENERAL_TOPIC_ID
                )
            )
            if should_backtrack:
                self.event_bus.publish_kiki_status("thinking", reason="Kiki 正在整理话题上下文...")
                reassigned_messages = await self._backtrack_general_messages(
                    target_topic=topic,
                    trigger_content=content,
                )
                if reassigned_messages:
                    topic = self.db.get_topic(topic["id"]) or topic

            return decision, topic, topic_created, reassigned_messages
        except Exception:
            had_error = True
            raise
        finally:
            self._active_routes = max(0, self._active_routes - 1)
            if had_error and published_thinking:
                next_status = self._status_provider() if self._status_provider else (
                    "thinking" if self._active_routes > 0 else "idle"
                )
                self.event_bus.publish_kiki_status(next_status)  # type: ignore[arg-type]

    @staticmethod
    def _fallback_title(content: str) -> str:
        clean = " ".join(content.strip().split())
        if len(clean) <= 18:
            return clean or "新话题"
        return clean[:18] + "…"


class FastPathService:
    """Streams lightweight foreground replies via SSE and persists final content."""

    def __init__(
        self,
        db: Database,
        llm: KikiLLMClient,
        event_bus: EventBus,
        idle_status_provider: Optional[Callable[[], str]] = None,
    ) -> None:
        self.db = db
        self.llm = llm
        self.event_bus = event_bus
        self._idle_status_provider = idle_status_provider
        self._tasks: set[asyncio.Task[Any]] = set()
        self._active_message_ids: set[str] = set()

    @property
    def is_busy(self) -> bool:
        return bool(self._active_message_ids)

    def set_idle_status_provider(self, provider: Optional[Callable[[], str]]) -> None:
        self._idle_status_provider = provider

    def start_reply(
        self,
        topic: dict[str, Any],
        user_content: str,
        user_message_id: str,
    ) -> dict[str, Any]:
        """Create placeholder assistant message and spawn background streaming task."""
        topic_id = topic["id"]
        # Capture history (including session_run status) before creating the empty placeholder
        history = self.db.get_recent_messages_with_topic(limit=12, topic_id=topic_id)
        # Remove the current user message from history so it isn't duplicated (safe under concurrency)
        history = [m for m in history if m.get("id") != user_message_id]

        placeholder = self.db.create_message(
            topic_id=topic_id,
            role="assistant",
            content="",
            metadata={"streaming": True, "reply_to": user_message_id, "path": "fast"},
        )
        self._active_message_ids.add(placeholder["id"])
        self.event_bus.publish("message_created", {"message": placeholder})

        task = asyncio.create_task(
            self._stream_reply_task(
                message_id=placeholder["id"],
                topic_id=topic_id,
                topic_title=topic["title"],
                user_content=user_content,
                history=history,
            )
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return placeholder

    async def _stream_reply_task(
        self,
        message_id: str,
        topic_id: str,
        topic_title: str,
        user_content: str,
        history: list[dict[str, str]],
    ) -> None:
        self.event_bus.publish_kiki_status("thinking", reason="Kiki 正在回复...")
        chunks: list[str] = []
        try:
            async for delta in self.llm.stream_fast_reply(
                content=user_content,
                history=history,
                topic_title=topic_title,
            ):
                chunks.append(delta)
                self.event_bus.publish(
                    "message_delta",
                    {
                        "message_id": message_id,
                        "topic_id": topic_id,
                        "delta": delta,
                    },
                )
        except Exception as exc:
            logger.exception("Unexpected error during fast reply stream: %s", exc)
            err_msg = "喵呜，回复时遇到了一点小问题，请再试一次。"
            chunks.append(err_msg)
            self.event_bus.publish(
                "message_delta",
                {"message_id": message_id, "topic_id": topic_id, "delta": err_msg},
            )
        finally:
            self._active_message_ids.discard(message_id)
            final_text = "".join(chunks).strip() or "喵？我刚刚没听清，能再说一遍吗？"
            completed = self.db.update_message(
                message_id,
                content=final_text,
                metadata={"streaming": False},
            )
            if completed:
                self.event_bus.publish("message_completed", {"message": completed})
            if self._idle_status_provider:
                next_status = self._idle_status_provider()
            else:
                next_status = "thinking" if self.is_busy else "idle"
            self.event_bus.publish_kiki_status(next_status)  # type: ignore[arg-type]

    async def shutdown(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)

"""Decision Router and Fast-Path streaming reply service for Kiki V1."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Literal, Optional

from .db import Database
from .llm import KikiLLMClient
from .schemas import RouteDecision
from .sse import EventBus

logger = logging.getLogger(__name__)


class DecisionRouter:
    """Routes incoming messages to Fast-Path or Heavy-Path and resolves Topic assignment."""

    def __init__(self, db: Database, llm: KikiLLMClient, event_bus: EventBus) -> None:
        self.db = db
        self.llm = llm
        self.event_bus = event_bus

    async def route(
        self,
        content: str,
        focus_topic_id: Optional[str] = None,
        force_path: Optional[Literal["fast", "heavy"]] = None,
    ) -> tuple[RouteDecision, dict[str, Any], bool]:
        """Return (RouteDecision, resolved_topic_dict, topic_created)."""
        # 1. Topic Focus Mode bypasses Decision Model
        if focus_topic_id:
            topic = self.db.get_topic(focus_topic_id)
            if topic is None:
                raise KeyError(f"Topic not found: {focus_topic_id}")
            decision = RouteDecision(
                path=force_path or "fast",
                topic_action="reuse",
                topic_id=topic["id"],
                new_topic_title=None,
                confidence=1.0,
                reasoning="Topic Focus Mode 锁定当前话题",
            )
            return decision, topic, False

        # 2. All Stream mode: invoke Decision Model
        self.event_bus.publish_kiki_status("thinking", reason="Kiki 正在判断意图与话题...")
        active_topics = self.db.list_topics(include_archived=False)
        decision = await self.llm.route_message(content, active_topics)

        if force_path is not None:
            decision = decision.model_copy(update={"path": force_path})

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

        return decision, topic, topic_created

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

    def start_reply(
        self,
        topic: dict[str, Any],
        user_content: str,
        user_message_id: str,
    ) -> dict[str, Any]:
        """Create placeholder assistant message and spawn background streaming task."""
        topic_id = topic["id"]
        # Capture history before creating the empty placeholder
        history = self.db.get_topic_history(topic_id, limit=12)
        # Remove the current user message from history so it isn't duplicated
        if history and history[-1]["role"] == "user" and history[-1]["content"] == user_content:
            history = history[:-1]

        placeholder = self.db.create_message(
            topic_id=topic_id,
            role="assistant",
            content="",
            metadata={"streaming": True, "reply_to": user_message_id, "path": "fast"},
        )
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
            final_text = "".join(chunks).strip() or "喵？我刚刚没听清，能再说一遍吗？"
            completed = self.db.update_message(
                message_id,
                content=final_text,
                metadata={"streaming": False},
            )
            if completed:
                self.event_bus.publish("message_completed", {"message": completed})
            next_status = self._idle_status_provider() if self._idle_status_provider else "idle"
            self.event_bus.publish_kiki_status(next_status)  # type: ignore[arg-type]

    async def shutdown(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)

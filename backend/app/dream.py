"""Background Idle Dream Consolidation ("做梦整理") for Kiki V1."""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from typing import Any, Callable, Optional

from .db import GENERAL_TOPIC_ID, MEMORY_CATEGORIES, Database, utc_now_iso
from .llm import KikiLLMClient
from .sse import EventBus

logger = logging.getLogger(__name__)


class DreamConsolidator:
    """Quietly consolidates 日常闲聊 messages, rolling topic summaries, and long-term memories while Kiki is idle."""

    def __init__(
        self,
        db: Database,
        llm: KikiLLMClient,
        event_bus: EventBus,
        *,
        is_busy_provider: Optional[Callable[[], bool]] = None,
        idle_seconds: float = 180.0,
        cooldown_seconds: float = 900.0,
        poll_interval_seconds: float = 30.0,
        batch_size: int = 24,
        min_move_confidence: float = 0.78,
        min_create_confidence: float = 0.82,
        min_create_messages: int = 3,
        min_memory_confidence: float = 0.78,
        summary_refresh_delta: int = 10,
    ) -> None:
        self.db = db
        self.llm = llm
        self.event_bus = event_bus
        self._is_busy_provider = is_busy_provider
        self.idle_seconds = idle_seconds
        self.cooldown_seconds = cooldown_seconds
        self.poll_interval_seconds = poll_interval_seconds
        self.batch_size = batch_size
        self.min_move_confidence = min_move_confidence
        self.min_create_confidence = min_create_confidence
        self.min_create_messages = min_create_messages
        self.min_memory_confidence = min_memory_confidence
        self.summary_refresh_delta = summary_refresh_delta

        now = time.monotonic()
        self._last_activity_at: float = now
        self._last_dream_at: float = 0.0
        self._activity_epoch: int = 0
        self._topics_changed_since_dream: bool = False
        self._lock = asyncio.Lock()
        self._task: Optional[asyncio.Task[Any]] = None
        self._stopped = False

    def notify_activity(self, *, topic_created: bool = False) -> None:
        self._activity_epoch += 1
        self._last_activity_at = time.monotonic()
        if topic_created:
            self._topics_changed_since_dream = True

    def is_busy(self) -> bool:
        return bool(self._is_busy_provider and self._is_busy_provider())

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stopped = False
        self._task = asyncio.create_task(self._idle_loop())

    async def shutdown(self) -> None:
        self._stopped = True
        if self._task is not None and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None

    async def _idle_loop(self) -> None:
        while not self._stopped:
            try:
                await asyncio.sleep(self.poll_interval_seconds)
                if self._stopped:
                    break
                if self.is_busy():
                    self._last_activity_at = time.monotonic()
                    continue
                await self.run_once(force=False)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("Dream consolidation loop error: %s", exc)

    def _can_call_llm(self) -> bool:
        if not hasattr(self.llm, "consolidate_dream"):
            return False
        settings = getattr(self.llm, "settings", None)
        if settings is not None:
            configured = getattr(
                settings,
                "any_llm_configured",
                getattr(settings, "api_key_configured", True),
            )
            if not configured:
                return False
        return True

    async def run_once(
        self,
        *,
        force: bool = False,
        ignore_busy: bool = False,
    ) -> dict[str, Any]:
        """Run one quiet consolidation pass if idle/cooldown gates pass (or force=True)."""
        if not self._can_call_llm():
            return {"ran": False, "reason": "llm_not_configured"}

        async with self._lock:
            now = time.monotonic()
            if not ignore_busy and self.is_busy():
                self._last_activity_at = now
                return {"ran": False, "reason": "foreground_busy"}

            if not force:
                if (now - self._last_activity_at) < self.idle_seconds:
                    return {"ran": False, "reason": "not_idle_enough"}
                if self._last_dream_at > 0.0 and (now - self._last_dream_at) < self.cooldown_seconds:
                    return {"ran": False, "reason": "cooldown"}

            start_epoch = self._activity_epoch
            active_topics = self.db.list_topics(include_archived=False)
            general_messages = self.db.list_general_messages_for_dream(
                limit=self.batch_size,
                include_checked=self._topics_changed_since_dream,
            )

            topics_to_summarize: list[dict[str, Any]] = []
            for t in active_topics:
                if t["id"] == GENERAL_TOPIC_ID:
                    continue
                msg_count = int(t.get("message_count") or 0)
                summarized_count = int(t.get("summary_msg_count") or 0)
                has_summary = bool((t.get("summary") or "").strip())
                needs_initial = (not has_summary) and msg_count >= 2
                needs_rolling_refresh = (
                    has_summary
                    and self.summary_refresh_delta > 0
                    and (msg_count - summarized_count) >= self.summary_refresh_delta
                )
                if needs_initial or needs_rolling_refresh:
                    recent = self.db.get_topic_history(t["id"], limit=10)
                    if recent:
                        topics_to_summarize.append({**t, "recent_messages": recent})
                if len(topics_to_summarize) >= 5:
                    break

            existing_memories = (
                self.db.list_memories(status="active", limit=40)
                if hasattr(self.db, "list_memories")
                else []
            )
            memory_candidates = (
                self.db.list_messages_for_memory_extraction(limit=self.batch_size)
                if hasattr(self.db, "list_messages_for_memory_extraction")
                else []
            )

            if not general_messages and not topics_to_summarize and not memory_candidates:
                self._last_dream_at = time.monotonic()
                self._topics_changed_since_dream = False
                return {"ran": False, "reason": "nothing_to_consolidate"}

            consolidate_sig = inspect.signature(self.llm.consolidate_dream)
            has_varkw = any(
                p.kind == inspect.Parameter.VAR_KEYWORD
                for p in consolidate_sig.parameters.values()
            )
            dream_kwargs: dict[str, Any] = {
                "active_topics": active_topics,
                "general_messages": general_messages,
                "topics_to_summarize": topics_to_summarize,
            }
            if has_varkw or "existing_memories" in consolidate_sig.parameters:
                dream_kwargs["existing_memories"] = existing_memories
            if has_varkw or "memory_candidates" in consolidate_sig.parameters:
                dream_kwargs["memory_candidates"] = memory_candidates

            plan = await self.llm.consolidate_dream(**dream_kwargs)

            # Yield immediately if foreground user activity happened while waiting on LLM
            if not force and self._activity_epoch != start_epoch:
                return {"ran": False, "reason": "interrupted_by_activity"}
            if not ignore_busy and self.is_busy():
                return {"ran": False, "reason": "interrupted_by_busy"}

            candidate_ids = {m["id"] for m in general_messages}
            active_non_general = {
                t["id"]: t for t in active_topics if t["id"] != GENERAL_TOPIC_ID
            }
            migrated_ids: set[str] = set()
            migrated_messages_out: list[dict[str, Any]] = []
            created_topics_out: list[dict[str, Any]] = []
            touched_topic_ids: set[str] = set()

            for group in plan.migrations:
                seed_ids = [
                    mid
                    for mid in dict.fromkeys(group.message_ids)
                    if mid in candidate_ids and mid not in migrated_ids
                ]
                if not seed_ids:
                    continue

                expanded_ids, _ = self.db.expand_paired_topic_messages(
                    GENERAL_TOPIC_ID, seed_ids
                )
                to_move = [mid for mid in expanded_ids if mid not in migrated_ids]
                if not to_move:
                    continue

                target_topic_id: Optional[str] = None
                if group.action == "move_to_existing":
                    if (
                        group.confidence >= self.min_move_confidence
                        and group.target_topic_id in active_non_general
                    ):
                        target_topic_id = group.target_topic_id
                elif group.action == "create_topic":
                    title = (group.new_topic_title or "").strip()
                    if (
                        group.confidence >= self.min_create_confidence
                        and len(to_move) >= self.min_create_messages
                        and title
                    ):
                        created = self.db.create_topic(
                            title=title,
                            summary=(group.new_topic_summary or "").strip() or None,
                        )
                        target_topic_id = created["id"]
                        active_non_general[target_topic_id] = created
                        created_topics_out.append(created)
                        self.event_bus.publish("topic_created", {"topic": created})

                if not target_topic_id:
                    continue

                updated_msgs = self.db.reassign_messages_topic(
                    to_move,
                    target_topic_id,
                    backtracked=True,
                    extra_metadata={"dream_consolidated": True},
                )
                if not updated_msgs:
                    continue

                touched_topic_ids.add(target_topic_id)
                touched_topic_ids.add(GENERAL_TOPIC_ID)
                for msg in updated_msgs:
                    migrated_ids.add(msg["id"])
                    if msg.get("session_run"):
                        msg = dict(msg)
                        msg["session_run"] = self.event_bus.enrich_run(msg["session_run"])
                    migrated_messages_out.append(msg)
                    self.event_bus.publish("message_updated", {"message": msg})

            updated_summaries: list[str] = []
            for item in plan.topic_summaries:
                summary_text = (item.summary or "").strip()
                if (
                    item.topic_id in active_non_general
                    and summary_text
                ):
                    current_topic = self.db.get_topic(item.topic_id) or active_non_general[item.topic_id]
                    updated_topic = self.db.update_topic(
                        item.topic_id,
                        summary=summary_text,
                        summary_msg_count=int(current_topic.get("message_count") or 0),
                    )
                    if updated_topic:
                        updated_summaries.append(item.topic_id)
                        touched_topic_ids.add(item.topic_id)

            existing_mem_ids = {m["id"] for m in existing_memories}
            memories_added: list[str] = []
            memories_updated: list[str] = []
            memories_invalidated: list[str] = []
            for op in getattr(plan, "memory_ops", None) or []:
                if float(op.confidence) < self.min_memory_confidence:
                    continue
                if op.action == "add":
                    content_text = (op.content or "").strip()
                    if not content_text:
                        continue
                    cat = op.category if op.category in MEMORY_CATEGORIES else "preference"
                    src_tid = op.source_topic_id if op.source_topic_id in active_non_general else None
                    created_mem = self.db.create_memory(
                        content=content_text,
                        category=cat,
                        source_topic_id=src_tid,
                        confidence=float(op.confidence),
                    )
                    memories_added.append(created_mem["id"])
                    self.event_bus.publish("memory_updated", {"action": "created", "memory": created_mem})
                elif op.action == "update":
                    content_text = (op.content or "").strip()
                    if not op.memory_id or op.memory_id not in existing_mem_ids or not content_text:
                        continue
                    cat = op.category if op.category in MEMORY_CATEGORIES else None
                    updated_mem = self.db.update_memory(
                        op.memory_id,
                        content=content_text,
                        category=cat,
                        confidence=float(op.confidence),
                    )
                    if updated_mem:
                        memories_updated.append(updated_mem["id"])
                        self.event_bus.publish("memory_updated", {"action": "updated", "memory": updated_mem})
                elif op.action == "invalidate":
                    if not op.memory_id or op.memory_id not in existing_mem_ids:
                        continue
                    inv_mem = self.db.update_memory(op.memory_id, status="superseded")
                    if inv_mem:
                        memories_invalidated.append(inv_mem["id"])
                        self.event_bus.publish("memory_updated", {"action": "deleted", "memory_id": inv_mem["id"]})

            now_iso = utc_now_iso()
            remaining_checked_ids = [
                m["id"] for m in general_messages if m["id"] not in migrated_ids
            ]
            if remaining_checked_ids:
                self.db.mark_messages_dream_checked(remaining_checked_ids, now_iso)
            if memory_candidates and hasattr(self.db, "mark_messages_memory_extracted"):
                self.db.mark_messages_memory_extracted(
                    [m["id"] for m in memory_candidates],
                    now_iso,
                )

            for tid in touched_topic_ids:
                refreshed = self.db.get_topic(tid)
                if refreshed:
                    self.event_bus.publish("topic_updated", {"topic": refreshed})

            self._last_dream_at = time.monotonic()
            self._topics_changed_since_dream = False

            return {
                "ran": True,
                "migrated_count": len(migrated_messages_out),
                "created_topics": [t["id"] for t in created_topics_out],
                "updated_summaries": updated_summaries,
                "memories_added": memories_added,
                "memories_updated": memories_updated,
                "memories_invalidated": memories_invalidated,
            }

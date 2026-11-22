"""Background TaskManager for Heavy-Path SessionRuns with pluggable Agent Adapters & HITL."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Literal, Optional

from .adapters import (
    AdapterContext,
    AdapterRegistry,
    ThinkingLevel,
    create_default_registry,
)
from .config import Settings
from .db import Database
from .llm import KikiLLMClient
from .sse import EventBus
from .tools import WorkspaceToolExecutor

logger = logging.getLogger(__name__)


def _truncate_summary(text: str, max_chars: int = 200) -> str:
    cleaned = " ".join(text.strip().split())
    if not cleaned:
        return "后台任务已完成。"
    if len(cleaned) <= max_chars:
        return cleaned
    return cleaned[: max_chars - 1] + "…"


def _normalize_hitl_options(raw_options: Any) -> list[dict[str, Any]]:
    if isinstance(raw_options, list) and raw_options:
        normalized: list[dict[str, Any]] = []
        for idx, opt in enumerate(raw_options):
            if isinstance(opt, dict):
                approved = bool(opt.get("approved", idx == 0))
                style = opt.get("style")
                if style not in ("primary", "danger", "secondary"):
                    style = "primary" if approved else "danger"
                normalized.append(
                    {
                        "label": str(opt.get("label") or opt.get("value") or f"选项 {idx + 1}"),
                        "value": str(opt.get("value") or opt.get("label") or f"opt_{idx + 1}"),
                        "approved": approved,
                        "style": style,
                    }
                )
        if normalized:
            return normalized
    return [
        {"label": "同意继续", "value": "approve", "approved": True, "style": "primary"},
        {"label": "拒绝并终止", "value": "reject", "approved": False, "style": "danger"},
    ]


class TaskManager:
    """Schedules and executes Heavy-Path SessionRuns asynchronously via HeavyTaskAdapters."""

    def __init__(
        self,
        db: Database,
        llm: KikiLLMClient,
        event_bus: EventBus,
        tool_executor: WorkspaceToolExecutor,
        adapter_registry: Optional[AdapterRegistry] = None,
        settings: Optional[Settings] = None,
        max_steps: int = 10,
    ) -> None:
        self.db = db
        self.llm = llm
        self.event_bus = event_bus
        self.tool_executor = tool_executor
        self.settings = settings
        self.adapter_registry = adapter_registry or create_default_registry(
            gemini_model=settings.gemini_model if settings else None,
            gemini_api_key_configured=settings.api_key_configured if settings else True,
        )
        self.max_steps = max_steps

        self._tasks: dict[str, asyncio.Task[Any]] = {}
        self._run_adapters: dict[str, str] = {}
        self._run_states: dict[str, str] = {}
        self._hitl_events: dict[str, asyncio.Event] = {}
        self._hitl_decisions: dict[str, dict[str, Any]] = {}

    def compute_kiki_status(self) -> Literal["idle", "thinking", "running", "waiting_input"]:
        states = set(self._run_states.values())
        if "waiting_input" in states:
            return "waiting_input"
        if "running" in states or "queued" in states:
            return "running"
        return "idle"

    def enqueue_run(
        self,
        topic_id: str,
        trigger_message_id: str,
        user_content: str,
        agent_type: Optional[str] = None,
        model_override: Optional[str] = None,
        thinking_override: Optional[ThinkingLevel] = None,
    ) -> dict[str, Any]:
        default_agent = (
            self.settings.default_heavy_agent if self.settings else "gemini_agent"
        )
        canonical_type = self.adapter_registry.resolve_type(agent_type, default=default_agent)
        adapter = self.adapter_registry.get(canonical_type)

        run = self.db.create_session_run(
            topic_id=topic_id,
            trigger_message_id=trigger_message_id,
            agent_type=canonical_type,
            status="queued",
            current_step=f"任务已入队（{adapter.display_name}），Kiki 准备出发...",
        )
        run_id = run["id"]
        # Link run_id back onto trigger message metadata
        self.db.update_message(trigger_message_id, metadata={"session_run_id": run_id})

        self._run_states[run_id] = "queued"
        self._run_adapters[run_id] = canonical_type
        enriched = self.event_bus.enrich_run(run)
        self.event_bus.publish("run_created", {"run": enriched})
        self.event_bus.publish_kiki_status(self.compute_kiki_status(), reason="后台任务已启动")

        task = asyncio.create_task(
            self._execute_run(
                run_id=run_id,
                topic_id=topic_id,
                user_content=user_content,
                agent_type=canonical_type,
                model_override=model_override,
                thinking_override=thinking_override,
            )
        )
        self._tasks[run_id] = task
        task.add_done_callback(lambda _t, rid=run_id: self._cleanup_task(rid))
        return enriched

    def _cleanup_task(self, run_id: str) -> None:
        self._tasks.pop(run_id, None)
        self._run_adapters.pop(run_id, None)
        self._run_states.pop(run_id, None)
        self._hitl_events.pop(run_id, None)
        self._hitl_decisions.pop(run_id, None)

    def _publish_run_update(self, run_id: str) -> Optional[dict[str, Any]]:
        run = self.db.get_session_run(run_id)
        if not run:
            return None
        enriched = self.event_bus.enrich_run(run)
        self.event_bus.publish("run_updated", {"run": enriched})
        return enriched

    async def _await_hitl_decision(
        self,
        run_id: str,
        topic_id: str,
        prompt: str,
        raw_options: list[dict[str, Any]],
    ) -> dict[str, Any]:
        options = _normalize_hitl_options(raw_options)
        hitl_req = {
            "run_id": run_id,
            "topic_id": topic_id,
            "prompt": prompt,
            "options": options,
        }
        self.event_bus.set_hitl_request(run_id, hitl_req)
        self._run_states[run_id] = "waiting_input"
        self.db.update_session_run(
            run_id,
            status="waiting_input",
            current_step=f"等待主人拍板：{prompt[:60]}",
        )
        self.event_bus.publish("hitl_required", hitl_req)
        self._publish_run_update(run_id)
        self.event_bus.publish_kiki_status(self.compute_kiki_status(), reason=prompt)

        wait_event = asyncio.Event()
        self._hitl_events[run_id] = wait_event
        await wait_event.wait()

        decision_payload = self._hitl_decisions.pop(run_id, {})
        self.event_bus.clear_hitl_request(run_id)
        self._run_states[run_id] = "running"

        self.db.update_session_run(
            run_id,
            status="running",
            current_step="收到主人决策，继续执行中...",
        )
        self._publish_run_update(run_id)
        self.event_bus.publish_kiki_status(self.compute_kiki_status())
        self.event_bus.append_raw_log(
            run_id,
            json.dumps(
                {"event": "hitl_resumed", "decision": decision_payload},
                ensure_ascii=False,
            ),
            stream="ndjson",
        )
        return decision_payload

    async def _execute_run(
        self,
        run_id: str,
        topic_id: str,
        user_content: str,
        agent_type: str = "gemini_agent",
        model_override: Optional[str] = None,
        thinking_override: Optional[ThinkingLevel] = None,
    ) -> None:
        adapter = self.adapter_registry.get(agent_type)
        try:
            self._run_states[run_id] = "running"
            self.db.update_session_run(
                run_id,
                status="running",
                current_step=f"{adapter.display_name} 正在拆解任务并规划步骤...",
            )
            self._publish_run_update(run_id)
            self.event_bus.publish_kiki_status(self.compute_kiki_status(), reason="后台任务运行中")
            self.event_bus.append_raw_log(
                run_id,
                json.dumps(
                    {
                        "event": "run_started",
                        "run_id": run_id,
                        "agent_type": adapter.agent_type,
                        "adapter_name": adapter.display_name,
                    },
                    ensure_ascii=False,
                ),
                stream="ndjson",
            )

            topic = self.db.get_topic(topic_id)
            topic_title = topic["title"] if topic else "后台任务"

            ctx = AdapterContext(
                run_id=run_id,
                topic_id=topic_id,
                topic_title=topic_title,
                user_content=user_content,
                workspace_root=self.tool_executor.workspace_root,
                artifacts_dir=self.tool_executor.artifacts_dir,
                db=self.db,
                event_bus=self.event_bus,
                tool_executor=self.tool_executor,
                llm=self.llm,
                settings=self.settings,
                model_override=model_override,
                thinking_override=thinking_override,
                max_steps=self.max_steps,
                _hitl_handler=lambda prompt, opts: self._await_hitl_decision(
                    run_id, topic_id, prompt, opts
                ),
                _publish_run_update=self._publish_run_update,
            )

            result = await adapter.execute(ctx)
            summary = _truncate_summary(result.summary, max_chars=200)

            self._run_states.pop(run_id, None)
            self.event_bus.clear_hitl_request(run_id)
            self.db.update_session_run(
                run_id,
                status="completed",
                current_step="任务已完成",
            )
            self._publish_run_update(run_id)

            assistant_msg = self.db.create_message(
                topic_id=topic_id,
                role="assistant",
                content=summary,
                metadata={
                    "session_run_id": run_id,
                    "source": "heavy_agent",
                    "path": "heavy",
                    "agent_type": adapter.agent_type,
                    "model_used": result.model_used,
                    "thinking_level_used": result.thinking_level_used,
                    "session_id": result.session_id,
                },
            )
            self.event_bus.publish("message_created", {"message": assistant_msg})

            updated_topic = self.db.update_topic(topic_id, unread=True)
            if updated_topic:
                self.event_bus.publish("topic_updated", {"topic": updated_topic})

            self.event_bus.publish(
                "toast_notification",
                {
                    "topic_id": topic_id,
                    "topic_title": updated_topic["title"] if updated_topic else topic_title,
                    "run_id": run_id,
                    "summary": summary,
                },
            )
            self.event_bus.append_raw_log(
                run_id,
                json.dumps(
                    {
                        "event": "run_completed",
                        "agent_type": adapter.agent_type,
                        "model_used": result.model_used,
                        "thinking_level_used": result.thinking_level_used,
                        "summary": summary,
                    },
                    ensure_ascii=False,
                ),
                stream="ndjson",
            )
            self.event_bus.publish_kiki_status(self.compute_kiki_status())

        except asyncio.CancelledError:
            await adapter.cancel(run_id)
            self._run_states.pop(run_id, None)
            self.event_bus.clear_hitl_request(run_id)
            self.db.update_session_run(
                run_id,
                status="cancelled",
                current_step="任务已取消",
            )
            self._publish_run_update(run_id)
            self.event_bus.append_raw_log(
                run_id,
                json.dumps({"event": "run_cancelled", "run_id": run_id}, ensure_ascii=False),
                stream="ndjson",
            )
            self.event_bus.publish_kiki_status(self.compute_kiki_status())
            raise
        except Exception as exc:
            logger.exception("SessionRun %s (%s) failed: %s", run_id, agent_type, exc)
            self._run_states.pop(run_id, None)
            self.event_bus.clear_hitl_request(run_id)
            err_msg = str(exc) or type(exc).__name__
            self.db.update_session_run(
                run_id,
                status="failed",
                current_step="任务执行失败",
                error_message=err_msg,
            )
            self._publish_run_update(run_id)
            self.event_bus.append_raw_log(
                run_id,
                f"[ERROR] {err_msg}",
                stream="stderr",
            )
            self.event_bus.publish_kiki_status(self.compute_kiki_status())

    def resume_run(
        self,
        run_id: str,
        decision: str,
        approved: bool,
        comment: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        run = self.db.get_session_run(run_id)
        if not run:
            return None
        if run["status"] != "waiting_input":
            raise ValueError(f"Run {run_id} is not waiting for input (status={run['status']})")

        self._hitl_decisions[run_id] = {
            "decision": decision,
            "approved": approved,
            "comment": comment,
        }
        event = self._hitl_events.get(run_id)
        if event is not None:
            event.set()
        else:
            # Handle orphaned waiting_input record (e.g., after server restart)
            self.event_bus.clear_hitl_request(run_id)
            self.db.update_session_run(
                run_id,
                status="completed" if approved else "cancelled",
                current_step=f"已记录决策：{decision}",
            )
            self._publish_run_update(run_id)

        updated = self.db.get_session_run(run_id)
        return self.event_bus.enrich_run(updated) if updated else None

    async def cancel_run(self, run_id: str) -> Optional[dict[str, Any]]:
        run = self.db.get_session_run(run_id)
        if not run:
            return None

        adapter_type = self._run_adapters.get(run_id) or run.get("agent_type")
        if adapter_type:
            try:
                adapter = self.adapter_registry.get(adapter_type)
                await adapter.cancel(run_id)
            except KeyError:
                pass

        task = self._tasks.get(run_id)
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        else:
            self._run_states.pop(run_id, None)
            self.event_bus.clear_hitl_request(run_id)
            if run["status"] in ("queued", "running", "waiting_input"):
                self.db.update_session_run(
                    run_id,
                    status="cancelled",
                    current_step="任务已取消",
                )
                self._publish_run_update(run_id)
                self.event_bus.publish_kiki_status(self.compute_kiki_status())

        updated = self.db.get_session_run(run_id)
        return self.event_bus.enrich_run(updated) if updated else None

    async def shutdown(self) -> None:
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

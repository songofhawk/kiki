"""Background TaskManager for Heavy-Path SessionRuns with pluggable Agent Adapters & HITL."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import TYPE_CHECKING, Any, Callable, Literal, Optional

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
from .workspace import WorkspaceManager

if TYPE_CHECKING:
    from .desktop.manager import DesktopTakeoverManager

logger = logging.getLogger(__name__)


def _truncate_summary(text: str, max_chars: int = 200) -> str:
    cleaned = " ".join(text.strip().split())
    if not cleaned:
        return "后台任务已完成。"
    if len(cleaned) <= max_chars:
        return cleaned
    return cleaned[: max_chars - 1] + "…"


def _format_human_failure_reply(agent_name: str, err_msg: str) -> str:
    """Translate raw Heavy-Path exception text into a readable Kiki assistant message."""
    raw = " ".join((err_msg or "").split())
    lower = raw.lower()

    # Check missing binary / not-installed errors FIRST so install hints mentioning
    # "login" or "API_KEY" (e.g. "执行 agy auth login") are never misclassified as auth failures.
    if (
        "未检测到" in raw
        or "command not found" in lower
        or "no such file or directory" in lower
        or "enoent" in lower
    ):
        return f"喵呜，**{agent_name}** 还没准备好：{_truncate_summary(raw, max_chars=220)}"

    if any(
        kw in lower
        for kw in (
            "usage limit",
            "rate limit",
            "insufficient_quota",
            "quota",
            "429",
            "too many requests",
        )
    ):
        retry_match = re.search(
            r"try again (?:at|in)\s+([A-Za-z0-9:.,\s]+?)(?:\.|\n|$)",
            raw,
            flags=re.IGNORECASE,
        )
        retry_hint = (
            f"（预计恢复时间：{retry_match.group(1).strip()}）"
            if retry_match
            else ""
        )
        return (
            f"喵呜，调用 **{agent_name}** 失败了：当前账户的额度或调用频率已达上限{retry_hint}。\n\n"
            f"建议等额度恢复后再试，或者在底部切换其他可用引擎 / 账号来执行这个任务。"
        )

    if any(
        kw in lower
        for kw in ("unauthorized", "invalid api key", "api key", "authentication", "401", "login")
    ):
        return (
            f"喵呜，**{agent_name}** 鉴权失败了，好像是登录态失效或 API Key 未配置正确。\n\n"
            f"请检查对应应用的登录状态或 `.env` 配置后再试一次~"
        )

    if any(
        kw in lower
        for kw in (
            "service_unavailable",
            "503",
            "high demand",
            "server disconnected",
            "timed out",
            "timeout",
            "connection",
        )
    ):
        return (
            f"喵呜，**{agent_name}** 上游服务暂时繁忙或网络波动中断了（{ _truncate_summary(raw, max_chars=90) }）。\n\n"
            f"稍等一下再让我重跑一次就好啦。"
        )

    if "not found" in lower:
        return f"喵呜，**{agent_name}** 还没准备好：{_truncate_summary(raw, max_chars=220)}"

    detail = _truncate_summary(raw, max_chars=160) if raw else "未知错误"
    return (
        f"喵呜，**{agent_name}** 执行任务时出错了：{detail}\n\n"
        f"你可以点开上方失败卡片的「日志」查看完整报错详情，或者换个指令让我再试一次。"
    )


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
        workspace_manager: Optional[WorkspaceManager] = None,
        desktop_manager: Optional[DesktopTakeoverManager] = None,
        max_steps: int = 10,
        status_provider: Optional[Callable[[], Literal["idle", "thinking", "running", "waiting_input"]]] = None,
    ) -> None:
        self.db = db
        self.llm = llm
        self.event_bus = event_bus
        self.tool_executor = tool_executor
        self.settings = settings
        self.desktop_manager = desktop_manager
        self.workspace_manager = workspace_manager or WorkspaceManager(
            workspace_root=tool_executor.workspace_root,
            legacy_artifacts_dir=tool_executor.artifacts_dir,
        )
        self.adapter_registry = adapter_registry or create_default_registry(
            gemini_model=settings.gemini_model if settings else None,
            gemini_api_key_configured=settings.any_llm_configured if settings else True,
        )
        self.max_steps = max_steps
        self._status_provider = status_provider

        self._tasks: dict[str, asyncio.Task[Any]] = {}
        self._run_adapters: dict[str, str] = {}
        self._run_states: dict[str, str] = {}
        self._hitl_events: dict[str, asyncio.Event] = {}
        self._hitl_decisions: dict[str, dict[str, Any]] = {}
        self._shutting_down: bool = False

    def set_status_provider(
        self,
        provider: Optional[Callable[[], Literal["idle", "thinking", "running", "waiting_input"]]],
    ) -> None:
        self._status_provider = provider

    def compute_kiki_status(self) -> Literal["idle", "thinking", "running", "waiting_input"]:
        states = set(self._run_states.values())
        if "waiting_input" in states:
            return "waiting_input"
        if "running" in states or "queued" in states:
            return "running"
        return "idle"

    def _publish_kiki_status(self, reason: Optional[str] = None) -> None:
        status = self._status_provider() if self._status_provider else self.compute_kiki_status()
        self.event_bus.publish_kiki_status(status, reason=reason)

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
            self.settings.default_heavy_agent if self.settings else "internal_agent"
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
        self._publish_kiki_status(reason="后台任务已启动")

        task = asyncio.create_task(
            self._execute_run(
                run_id=run_id,
                topic_id=topic_id,
                trigger_message_id=trigger_message_id,
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
        meta = self.event_bus.pop_hitl_meta(run_id)
        kind = "desktop_takeover" if meta.get("kind") == "desktop_takeover" else "approval"
        hitl_req: dict[str, Any] = {
            "run_id": run_id,
            "topic_id": topic_id,
            "prompt": prompt,
            "kind": kind,
            "options": options,
        }
        if meta.get("instructions"):
            hitl_req["instructions"] = str(meta["instructions"])
        if meta.get("url"):
            hitl_req["url"] = str(meta["url"])
        self.event_bus.set_hitl_request(run_id, hitl_req)
        self._run_states[run_id] = "waiting_input"
        step_prefix = "需要主人接管桌面" if kind == "desktop_takeover" else "等待主人拍板"
        self.db.update_session_run(
            run_id,
            status="waiting_input",
            current_step=f"{step_prefix}：{prompt[:60]}",
        )
        self.event_bus.publish("hitl_required", hitl_req)
        self._publish_run_update(run_id)
        self._publish_kiki_status(reason=prompt)

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
        self._publish_kiki_status()
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
        trigger_message_id: str,
        user_content: str,
        agent_type: str = "internal_agent",
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
            self._publish_kiki_status(reason="后台任务运行中")
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
            topic_summary = topic.get("summary") if topic else None
            raw_history = self.db.list_messages(topic_id=topic_id, limit=16)
            topic_history = [
                m
                for m in raw_history
                if m.get("id") != trigger_message_id
                and not (m.get("session_run") and m["session_run"].get("id") == run_id)
            ]

            def _emit_interim_message(text: str) -> dict[str, Any]:
                interim_msg = self.db.create_message(
                    topic_id=topic_id,
                    role="assistant",
                    content=text.strip() or "后台任务正在推进中…",
                    metadata={
                        "reply_to": trigger_message_id,
                        "session_run_id": run_id,
                        "source": "heavy_agent_interim",
                        "path": "heavy",
                        "agent_type": adapter.agent_type,
                    },
                )
                self.event_bus.publish("message_created", {"message": interim_msg})
                return interim_msg

            topic_mounts = self.db.list_workspace_mounts(scope="topic", topic_id=topic_id)
            global_mounts = self.db.list_workspace_mounts(scope="global")
            resolved_ws = self.workspace_manager.resolve_for_topic(
                topic_id,
                topic_mounts=topic_mounts,
                global_mounts=global_mounts,
                topic_workspace_dir=topic.get("workspace_dir") if topic else None,
                ensure_dirs=True,
            )
            scoped_executor = self.tool_executor.for_workspace(resolved_ws)
            rule_files = self.workspace_manager.list_rule_files(resolved_ws.topic_dir)

            ctx = AdapterContext(
                run_id=run_id,
                topic_id=topic_id,
                topic_title=topic_title,
                user_content=user_content,
                workspace_root=resolved_ws.primary_dir,
                artifacts_dir=resolved_ws.topic_artifacts_dir,
                db=self.db,
                event_bus=self.event_bus,
                tool_executor=scoped_executor,
                llm=self.llm,
                settings=self.settings,
                desktop_manager=self.desktop_manager,
                model_override=model_override,
                thinking_override=thinking_override,
                history=topic_history,
                topic_summary=topic_summary,
                kiki_workspace_root=resolved_ws.workspace_root,
                topic_dir=resolved_ws.topic_dir,
                shared_dir=resolved_ws.shared_dir,
                rules_dir=resolved_ws.rules_dir,
                extra_dirs=list(resolved_ws.extra_dirs),
                rule_files=rule_files,
                max_steps=self.max_steps,
                _hitl_handler=lambda prompt, opts: self._await_hitl_decision(
                    run_id, topic_id, prompt, opts
                ),
                _publish_run_update=self._publish_run_update,
                _emit_interim_message=_emit_interim_message,
            )

            result = await adapter.execute(ctx)
            summary = result.summary.strip() or "后台任务已完成。"

            self._run_states.pop(run_id, None)
            self.event_bus.clear_hitl_request(run_id)
            self.db.update_session_run(
                run_id,
                status="completed",
                current_step="任务已完成",
            )
            self._publish_run_update(run_id)

            final_metadata = {
                "reply_to": trigger_message_id,
                "session_run_id": run_id,
                "source": "heavy_agent",
                "path": "heavy",
                "agent_type": adapter.agent_type,
                "model_used": result.model_used,
                "thinking_level_used": result.thinking_level_used,
                "session_id": result.session_id,
            }
            already_emitted_same = bool(
                ctx.last_emitted_message_id
                and ctx.last_emitted_text
                and ctx.last_emitted_text.strip() == summary
            )
            if already_emitted_same and ctx.last_emitted_message_id:
                updated_msg = self.db.update_message(
                    ctx.last_emitted_message_id,
                    content=summary,
                    metadata=final_metadata,
                )
                if updated_msg:
                    if updated_msg.get("session_run"):
                        updated_msg = dict(updated_msg)
                        updated_msg["session_run"] = self.event_bus.enrich_run(
                            updated_msg["session_run"]
                        )
                    self.event_bus.publish("message_updated", {"message": updated_msg})
            else:
                assistant_msg = self.db.create_message(
                    topic_id=topic_id,
                    role="assistant",
                    content=summary,
                    metadata=final_metadata,
                )
                if assistant_msg.get("session_run"):
                    assistant_msg = dict(assistant_msg)
                    assistant_msg["session_run"] = self.event_bus.enrich_run(
                        assistant_msg["session_run"]
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
            self._publish_kiki_status()

        except asyncio.CancelledError:
            await adapter.cancel(run_id)
            self._run_states.pop(run_id, None)
            self.event_bus.clear_hitl_request(run_id)
            is_shutdown = self._shutting_down
            cancel_step = "服务重启/热重载中断" if is_shutdown else "任务已取消"
            self.db.update_session_run(
                run_id,
                status="cancelled",
                current_step=cancel_step,
                error_message=cancel_step if is_shutdown else None,
            )
            self._publish_run_update(run_id)
            self.event_bus.append_raw_log(
                run_id,
                json.dumps(
                    {
                        "event": "run_cancelled",
                        "run_id": run_id,
                        "reason": "shutdown" if is_shutdown else "cancelled",
                        "detail": cancel_step,
                    },
                    ensure_ascii=False,
                ),
                stream="ndjson",
            )
            self._publish_kiki_status()
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

            failure_reply = await self._build_failure_reply(
                agent_name=adapter.display_name,
                user_content=user_content,
                err_msg=err_msg,
            )
            failure_msg = self.db.create_message(
                topic_id=topic_id,
                role="assistant",
                content=failure_reply,
                metadata={
                    "reply_to": trigger_message_id,
                    "session_run_id": run_id,
                    "source": "heavy_agent_error",
                    "path": "heavy",
                    "agent_type": adapter.agent_type,
                    "error": True,
                },
            )
            self.event_bus.publish("message_created", {"message": failure_msg})
            updated_topic = self.db.update_topic(topic_id, unread=True)
            if updated_topic:
                self.event_bus.publish("topic_updated", {"topic": updated_topic})

            self._publish_kiki_status()

    async def _build_failure_reply(
        self,
        *,
        agent_name: str,
        user_content: str,
        err_msg: str,
    ) -> str:
        """Use the LLM to understand and explain a run failure, with template fallback."""
        explain_fn = getattr(self.llm, "explain_run_failure", None)
        if callable(explain_fn):
            try:
                explained = await asyncio.wait_for(
                    explain_fn(
                        agent_name=agent_name,
                        user_content=user_content,
                        error_message=err_msg,
                    ),
                    timeout=9.0,
                )
                if isinstance(explained, str) and explained.strip():
                    return explained.strip()
            except Exception as exc:
                logger.warning("LLM failure explanation fallback triggered: %s", exc)
        return _format_human_failure_reply(agent_name, err_msg)

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

    def retry_run(self, run_id: str) -> Optional[dict[str, Any]]:
        """Create and enqueue a new SessionRun re-running a failed or cancelled run."""
        run = self.db.get_session_run(run_id)
        if not run:
            return None
        if run["status"] not in ("failed", "cancelled"):
            raise ValueError(
                f"Run {run_id} cannot be retried in status '{run['status']}'"
            )
        trigger_msg = self.db.get_message(run["trigger_message_id"])
        if not trigger_msg:
            raise ValueError(
                f"Trigger message {run['trigger_message_id']} not found for run {run_id}"
            )
        return self.enqueue_run(
            topic_id=run["topic_id"],
            trigger_message_id=run["trigger_message_id"],
            user_content=str(trigger_msg.get("content") or ""),
            agent_type=run.get("agent_type"),
        )

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
                self._publish_kiki_status()

        updated = self.db.get_session_run(run_id)
        return self.event_bus.enrich_run(updated) if updated else None

    async def shutdown(self) -> None:
        self._shutting_down = True
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

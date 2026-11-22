"""Background TaskManager for Heavy-Path SessionRuns with multi-step tool loop & HITL."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Literal, Optional

from .db import Database
from .llm import KikiLLMClient
from .sse import EventBus
from .tools import AGENT_TOOL_DECLARATIONS, WorkspaceToolExecutor

logger = logging.getLogger(__name__)

AGENT_SYSTEM_PROMPT = """你是 Kiki 的后台重型执行引擎 (gemini_agent)。
你可以使用工作区工具（list_files, read_file, write_artifact, run_shell, request_human_approval）来完成用户的复杂任务。

执行规范：
1. 先探索再操作，合理调用工具获取真实信息。
2. 若需要产出报告、分析文档或代码，使用 `write_artifact` 保存为产物文件。
3. 若任务涉及高风险操作、破坏性变更或需要用户明确拍板确认，必须调用 `request_human_approval` 挂起等待用户决策。
4. 任务完成时，用简洁、信息密度高的中文给出最终总结（**严格控制在 200 字以内**），直接说明结论与产出物。
"""


def _truncate_summary(text: str, max_chars: int = 200) -> str:
    cleaned = " ".join(text.strip().split())
    if not cleaned:
        return "后台任务已完成。"
    if len(cleaned) <= max_chars:
        return cleaned
    return cleaned[: max_chars - 1] + "…"


def _action_type_for_tool(tool_name: str) -> str:
    if tool_name == "run_shell":
        return "shell_exec"
    if tool_name == "write_artifact":
        return "file_edit"
    if tool_name == "request_human_approval":
        return "hitl_wait"
    return "tool_call"


def _step_label_for_tool(tool_name: str, args: dict[str, Any]) -> str:
    if tool_name == "list_files":
        return f"正在浏览目录 {args.get('path', '.')}..."
    if tool_name == "read_file":
        return f"正在读取文件 {args.get('path', '')}..."
    if tool_name == "write_artifact":
        return f"正在生成产物 {args.get('filename', 'artifact')}..."
    if tool_name == "run_shell":
        cmd = str(args.get("command", ""))
        short_cmd = cmd if len(cmd) <= 45 else cmd[:45] + "…"
        return f"正在执行命令 `{short_cmd}`..."
    if tool_name == "request_human_approval":
        prompt = str(args.get("prompt", "等待人工确认"))
        return f"等待主人拍板：{prompt[:50]}"
    return f"正在执行工具 {tool_name}..."


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
    """Schedules and executes Heavy-Path SessionRuns asynchronously."""

    def __init__(
        self,
        db: Database,
        llm: KikiLLMClient,
        event_bus: EventBus,
        tool_executor: WorkspaceToolExecutor,
        max_steps: int = 10,
    ) -> None:
        self.db = db
        self.llm = llm
        self.event_bus = event_bus
        self.tool_executor = tool_executor
        self.max_steps = max_steps

        self._tasks: dict[str, asyncio.Task[Any]] = {}
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
        agent_type: str = "gemini_agent",
    ) -> dict[str, Any]:
        run = self.db.create_session_run(
            topic_id=topic_id,
            trigger_message_id=trigger_message_id,
            agent_type=agent_type,
            status="queued",
            current_step="任务已入队，Kiki 戴好邮差帽准备出发...",
        )
        run_id = run["id"]
        # Link run_id back onto trigger message metadata
        self.db.update_message(trigger_message_id, metadata={"session_run_id": run_id})

        self._run_states[run_id] = "queued"
        enriched = self.event_bus.enrich_run(run)
        self.event_bus.publish("run_created", {"run": enriched})
        self.event_bus.publish_kiki_status(self.compute_kiki_status(), reason="后台任务已启动")

        task = asyncio.create_task(self._execute_run(run_id, topic_id, user_content))
        self._tasks[run_id] = task
        task.add_done_callback(lambda _t, rid=run_id: self._cleanup_task(rid))
        return enriched

    def _cleanup_task(self, run_id: str) -> None:
        self._tasks.pop(run_id, None)
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

    async def _execute_run(self, run_id: str, topic_id: str, user_content: str) -> None:
        step_counter = 0
        try:
            self._run_states[run_id] = "running"
            self.db.update_session_run(
                run_id,
                status="running",
                current_step="Kiki 正在拆解任务并规划步骤...",
            )
            self._publish_run_update(run_id)
            self.event_bus.publish_kiki_status(self.compute_kiki_status(), reason="后台任务运行中")
            self.event_bus.append_raw_log(
                run_id,
                json.dumps(
                    {"event": "run_started", "run_id": run_id, "agent_type": "gemini_agent"},
                    ensure_ascii=False,
                ),
                stream="ndjson",
            )

            topic = self.db.get_topic(topic_id)
            topic_title = topic["title"] if topic else "后台任务"

            input_data: Any = f"当前话题：{topic_title}\n用户任务：{user_content}"
            previous_interaction_id: Optional[str] = None
            final_text: str = ""

            for turn in range(1, self.max_steps + 1):
                self.event_bus.append_raw_log(
                    run_id,
                    json.dumps({"event": "llm_turn", "turn": turn}, ensure_ascii=False),
                    stream="ndjson",
                )
                interaction = await self.llm.run_agent_turn(
                    input_data=input_data,
                    tools=AGENT_TOOL_DECLARATIONS,
                    system_instruction=AGENT_SYSTEM_PROMPT,
                    previous_interaction_id=previous_interaction_id,
                    thinking_level="medium",
                )
                previous_interaction_id = getattr(interaction, "id", None)

                steps = getattr(interaction, "steps", None) or []
                function_calls = [s for s in steps if getattr(s, "type", None) == "function_call"]

                if not function_calls:
                    final_text = getattr(interaction, "output_text", None) or ""
                    break

                function_results: list[dict[str, Any]] = []
                for call in function_calls:
                    step_counter += 1
                    call_id = getattr(call, "id", f"call-{step_counter}")
                    tool_name = getattr(call, "name", "unknown")
                    raw_args = getattr(call, "arguments", None)
                    args = raw_args if isinstance(raw_args, dict) else {}

                    step_label = _step_label_for_tool(tool_name, args)
                    action_type = _action_type_for_tool(tool_name)

                    step_log = self.db.add_step_log(
                        run_id=run_id,
                        step_number=step_counter,
                        action_type=action_type,
                        detail=step_label,
                        status="in_progress",
                    )
                    self.event_bus.publish("step_log", {"run_id": run_id, "step_log": step_log})
                    self.db.update_session_run(run_id, current_step=step_label)
                    self._publish_run_update(run_id)
                    self.event_bus.append_raw_log(
                        run_id,
                        json.dumps(
                            {"event": "tool_call", "tool": tool_name, "arguments": args},
                            ensure_ascii=False,
                        ),
                        stream="ndjson",
                    )

                    if tool_name == "request_human_approval":
                        prompt = str(args.get("prompt") or "Kiki 需要你拍板确认下一步操作。")
                        options = _normalize_hitl_options(args.get("options"))
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
                        self.event_bus.publish_kiki_status(
                            self.compute_kiki_status(), reason=prompt
                        )

                        # Wait for POST /api/runs/{run_id}/resume
                        wait_event = asyncio.Event()
                        self._hitl_events[run_id] = wait_event
                        await wait_event.wait()

                        decision_payload = self._hitl_decisions.pop(run_id, {})
                        self.event_bus.clear_hitl_request(run_id)
                        self._run_states[run_id] = "running"

                        approved = bool(decision_payload.get("approved", False))
                        decision_val = str(decision_payload.get("decision", ""))
                        comment = decision_payload.get("comment") or ""
                        result_str = json.dumps(
                            {
                                "approved": approved,
                                "decision": decision_val,
                                "comment": comment,
                            },
                            ensure_ascii=False,
                        )
                        updated_step = self.db.update_step_log(
                            step_log["id"],
                            status="success",
                            detail=f"主人已拍板：{'同意' if approved else '拒绝'} ({decision_val})",
                        )
                        if updated_step:
                            self.event_bus.publish(
                                "step_log", {"run_id": run_id, "step_log": updated_step}
                            )
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
                        function_results.append(
                            {
                                "type": "function_result",
                                "call_id": call_id,
                                "name": tool_name,
                                "result": result_str,
                                "is_error": False,
                            }
                        )
                    else:
                        result_str, is_error = await self.tool_executor.execute(
                            run_id=run_id,
                            name=tool_name,
                            arguments=args,
                        )
                        updated_step = self.db.update_step_log(
                            step_log["id"],
                            status="failed" if is_error else "success",
                        )
                        if updated_step:
                            self.event_bus.publish(
                                "step_log", {"run_id": run_id, "step_log": updated_step}
                            )
                        self.event_bus.append_raw_log(
                            run_id,
                            json.dumps(
                                {
                                    "event": "tool_result",
                                    "tool": tool_name,
                                    "is_error": is_error,
                                    "preview": result_str[:240],
                                },
                                ensure_ascii=False,
                            ),
                            stream="ndjson",
                        )
                        function_results.append(
                            {
                                "type": "function_result",
                                "call_id": call_id,
                                "name": tool_name,
                                "result": result_str,
                                "is_error": is_error,
                            }
                        )

                input_data = function_results

            summary = _truncate_summary(final_text, max_chars=200)
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
                metadata={"session_run_id": run_id, "source": "heavy_agent", "path": "heavy"},
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
                json.dumps({"event": "run_completed", "summary": summary}, ensure_ascii=False),
                stream="ndjson",
            )
            self.event_bus.publish_kiki_status(self.compute_kiki_status())

        except asyncio.CancelledError:
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
            logger.exception("SessionRun %s failed: %s", run_id, exc)
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

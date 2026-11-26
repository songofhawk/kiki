"""End-to-end tests for agent HITL request_desktop_takeover tool flow."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.config import Settings
from app.db import Database
from app.sse import EventBus
from app.task_manager import TaskManager
from app.tools import AGENT_TOOL_DECLARATIONS, WorkspaceToolExecutor


class _FakeTakeoverLLM:
    """First turn requests desktop takeover; second turn returns final summary."""

    def __init__(self) -> None:
        self.calls: list[Any] = []
        self.last_used_model = "gemini-3.8-flash"

    async def run_agent_turn(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if len(self.calls) == 1:
            return SimpleNamespace(
                id="inter-1",
                steps=[
                    SimpleNamespace(
                        type="function_call",
                        id="call-takeover-1",
                        name="request_desktop_takeover",
                        arguments={
                            "reason": "目标网站出现人机图形验证码，需要主人接管桌面完成验证。",
                            "instructions": "请打开 Chrome 窗口完成滑块验证后点击交还给 Kiki。",
                            "url": "https://example.com/login",
                        },
                    )
                ],
                output_text="",
            )
        return SimpleNamespace(
            id="inter-2",
            steps=[],
            output_text="主人已完成桌面登录验证，任务已顺利收尾！",
        )


def test_request_desktop_takeover_tool_declared() -> None:
    names = {t["name"] for t in AGENT_TOOL_DECLARATIONS}
    assert "request_desktop_takeover" in names


@pytest.mark.asyncio
async def test_internal_agent_desktop_takeover_hitl_roundtrip(tmp_path: Path) -> None:
    cfg = Settings(
        gemini_api_key="test-key",
        gemini_model="gemini-3.8-flash",
        workspace_root=tmp_path / "workspace",
        database_path=tmp_path / "kiki.db",
        artifacts_dir=tmp_path / "workspace" / "artifacts",
    )
    db = Database(cfg.database_path)
    db.init_db()
    bus = EventBus()
    llm = _FakeTakeoverLLM()
    executor = WorkspaceToolExecutor(
        workspace_root=cfg.workspace_root,
        artifacts_dir=cfg.artifacts_dir,
        db=db,
        event_bus=bus,
    )
    tm = TaskManager(db=db, llm=llm, event_bus=bus, tool_executor=executor, settings=cfg)

    topic = db.create_topic(title="登录排查")
    msg = db.create_message(topic_id=topic["id"], role="user", content="帮我登录后台导出报表")
    run = tm.enqueue_run(
        topic_id=topic["id"],
        trigger_message_id=msg["id"],
        user_content="帮我登录后台导出报表",
        agent_type="internal_agent",
    )
    run_id = run["id"]

    # Wait for run to enter waiting_input with desktop_takeover metadata
    for _ in range(50):
        hitl = bus.get_hitl_request(run_id)
        if hitl is not None:
            break
        await asyncio.sleep(0.02)

    hitl = bus.get_hitl_request(run_id)
    assert hitl is not None
    assert hitl["kind"] == "desktop_takeover"
    assert hitl["url"] == "https://example.com/login"
    assert "滑块验证" in (hitl.get("instructions") or "")

    # Resume run after user completes takeover
    tm.resume_run(run_id, decision="takeover_done", approved=True, comment="已完成验证码登录")

    # Wait for completion
    for _ in range(50):
        current = db.get_session_run(run_id)
        if current and current["status"] == "completed":
            break
        await asyncio.sleep(0.02)

    final_run = db.get_session_run(run_id)
    assert final_run is not None
    assert final_run["status"] == "completed"
    assert len(llm.calls) == 2
    second_input = llm.calls[1]["input_data"]
    assert second_input[0]["name"] == "request_desktop_takeover"
    assert "已完成验证码登录" in second_input[0]["result"]

    db.close()

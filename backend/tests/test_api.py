"""Comprehensive API & Integration tests for Kiki V1 backend."""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from typing import Any, AsyncIterator, Optional

import httpx
import pytest

from app.config import Settings
from app.db import Database
from app.main import create_app
from app.schemas import RouteDecision
from app.sse import EventBus


class FakeLLMClient:
    """Deterministic fake LLM client for fast, repeatable integration tests."""

    def __init__(self) -> None:
        self.route_calls = 0
        self.next_decision: Optional[RouteDecision] = None
        self.agent_script: list[Any] = []
        self._agent_turn = 0

    async def route_message(
        self,
        content: str,
        active_topics: list[dict[str, Any]],
    ) -> RouteDecision:
        self.route_calls += 1
        if self.next_decision is not None:
            decision = self.next_decision
            self.next_decision = None
            return decision
        return RouteDecision(
            path="fast",
            topic_action="none",
            topic_id=None,
            new_topic_title=None,
            confidence=0.92,
            reasoning="测试默认轻量闲聊",
        )

    async def stream_fast_reply(
        self,
        content: str,
        history: list[dict[str, Any]],
        topic_title: str,
    ) -> AsyncIterator[str]:
        for chunk in ["喵~ ", "收到你的消息：", content[:20]]:
            await asyncio.sleep(0.01)
            yield chunk

    async def run_agent_turn(
        self,
        input_data: Any,
        tools: list[dict[str, Any]],
        system_instruction: str,
        previous_interaction_id: Optional[str] = None,
        thinking_level: str = "medium",
    ) -> Any:
        idx = self._agent_turn
        self._agent_turn += 1
        if idx < len(self.agent_script):
            return self.agent_script[idx]
        return SimpleNamespace(
            id=f"int_{idx}",
            status="completed",
            steps=[],
            output_text="任务已完成！已检查项目目录并生成了摘要报告。",
        )


def _build_test_app(tmp_path: Path):
    db_path = tmp_path / "kiki_test.db"
    artifacts_dir = tmp_path / "artifacts"
    settings = Settings(
        gemini_api_key="test-key",
        gemini_model="gemini-3.5-flash-lite",
        workspace_root=Path("/Users/harry/gitrepo/kiki"),
        database_path=db_path,
        artifacts_dir=artifacts_dir,
    )
    db = Database(db_path)
    db.init_db()
    bus = EventBus()
    fake_llm = FakeLLMClient()
    app = create_app(settings=settings, db=db, llm=fake_llm, event_bus=bus)
    return app, db, bus, fake_llm, db_path


async def _wait_for_run_status(
    client: httpx.AsyncClient,
    run_id: str,
    target_statuses: set[str],
    timeout: float = 5.0,
) -> dict[str, Any]:
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        resp = await client.get(f"/api/runs/{run_id}")
        assert resp.status_code == 200
        data = resp.json()
        if data["status"] in target_statuses:
            return data
        await asyncio.sleep(0.05)
    raise AssertionError(f"Run {run_id} did not reach {target_statuses} in {timeout}s")


@pytest.mark.asyncio
async def test_health_and_five_core_tables(tmp_path: Path):
    app, _db, _bus, _llm, db_path = _build_test_app(tmp_path)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        res = await client.get("/api/health")
        assert res.status_code == 200
        body = res.json()
        assert body["status"] == "ok"
        assert body["model"] == "gemini-3.5-flash-lite"
        assert body["api_key_configured"] is True

    conn = sqlite3.connect(db_path)
    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    conn.close()
    assert {"topics", "messages", "session_runs", "step_logs", "artifacts"}.issubset(
        tables
    )


@pytest.mark.asyncio
async def test_fast_path_streaming_and_topic_focus_bypass(tmp_path: Path):
    app, _db, bus, fake_llm, _db_path = _build_test_app(tmp_path)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        # 1. Fast-Path in All Stream
        res = await client.post("/api/chat", json={"content": "早安 Kiki！"})
        assert res.status_code == 200
        data = res.json()
        assert fake_llm.route_calls == 1
        assert data["route_decision"]["path"] == "fast"
        assert data["topic"]["id"] == "topic-general"
        assert data["assistant_message"] is not None

        # Wait briefly for background streaming to finish
        await asyncio.sleep(0.15)
        msgs = (await client.get("/api/messages")).json()
        assistant_msgs = [m for m in msgs if m["role"] == "assistant"]
        assert len(assistant_msgs) == 1
        assert "喵~" in assistant_msgs[0]["content"]

        # 2. Create a custom topic and test Topic Focus Mode bypass
        t_res = await client.post("/api/topics", json={"title": "排查502"})
        assert t_res.status_code == 201
        topic_id = t_res.json()["id"]

        res2 = await client.post(
            "/api/chat",
            json={"content": "继续看这个报错", "focus_topic_id": topic_id},
        )
        assert res2.status_code == 200
        data2 = res2.json()
        # Decision Model must have been bypassed!
        assert fake_llm.route_calls == 1
        assert data2["topic"]["id"] == topic_id
        assert data2["route_decision"]["confidence"] == 1.0


@pytest.mark.asyncio
async def test_zero_interrupt_correction_split_and_move(tmp_path: Path):
    app, _db, _bus, fake_llm, _db_path = _build_test_app(tmp_path)
    fake_llm.next_decision = RouteDecision(
        path="fast",
        topic_action="create",
        topic_id=None,
        new_topic_title="模糊话题",
        confidence=0.42,
        reasoning="低置信度触发纠错胶囊",
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        res = await client.post("/api/chat", json={"content": "顺便帮我看个东西"})
        assert res.status_code == 200
        user_msg_id = res.json()["user_message"]["id"]
        assert res.json()["user_message"]["metadata"]["confidence"] == pytest.approx(0.42)

        # Split into a new topic
        split_res = await client.patch(
            f"/api/messages/{user_msg_id}/topic",
            json={"action": "split", "new_topic_title": "独立新话题"},
        )
        assert split_res.status_code == 200
        new_topic_id = split_res.json()["topic"]["id"]
        assert split_res.json()["topic"]["title"] == "独立新话题"
        assert split_res.json()["message"]["topic_id"] == new_topic_id

        # Move to another topic (simulating drag-and-drop)
        target = (await client.post("/api/topics", json={"title": "目标话题"})).json()
        move_res = await client.patch(
            f"/api/messages/{user_msg_id}/topic",
            json={"action": "move", "target_topic_id": target["id"]},
        )
        assert move_res.status_code == 200
        assert move_res.json()["message"]["topic_id"] == target["id"]


@pytest.mark.asyncio
async def test_heavy_path_tools_artifacts_and_toast(tmp_path: Path):
    app, _db, bus, fake_llm, _db_path = _build_test_app(tmp_path)
    fake_llm.next_decision = RouteDecision(
        path="heavy",
        topic_action="create",
        topic_id=None,
        new_topic_title="代码巡检",
        confidence=0.95,
        reasoning="涉及文件检查与报告生成",
    )
    fake_llm.agent_script = [
        SimpleNamespace(
            id="int_1",
            status="requires_action",
            steps=[
                SimpleNamespace(
                    type="function_call",
                    id="fc_1",
                    name="list_files",
                    arguments={"path": "docs"},
                )
            ],
            output_text=None,
        ),
        SimpleNamespace(
            id="int_2",
            status="requires_action",
            steps=[
                SimpleNamespace(
                    type="function_call",
                    id="fc_2",
                    name="write_artifact",
                    arguments={
                        "filename": "audit.md",
                        "content": "# 巡检报告\n一切正常。",
                        "file_type": "markdown",
                        "description": "项目巡检报告",
                    },
                )
            ],
            output_text=None,
        ),
        SimpleNamespace(
            id="int_3",
            status="completed",
            steps=[],
            output_text="巡检完成：docs 目录结构完整，已生成 audit.md 报告。",
        ),
    ]

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        res = await client.post("/api/chat", json={"content": "帮我巡检 docs 并生成报告"})
        assert res.status_code == 200
        body = res.json()
        assert body["session_run"] is not None
        run_id = body["session_run"]["id"]
        topic_id = body["topic"]["id"]

        done_run = await _wait_for_run_status(client, run_id, {"completed"})
        assert len(done_run["step_logs"]) == 2
        assert len(done_run["artifacts"]) == 1
        assert done_run["artifacts"][0]["file_type"] == "markdown"

        # Topic should be marked unread & have <= 200-char writeback assistant message
        topics = (await client.get("/api/topics")).json()
        topic = next(t for t in topics if t["id"] == topic_id)
        assert topic["unread"] is True

        msgs = (await client.get(f"/api/messages?topic_id={topic_id}")).json()
        assistant_msgs = [m for m in msgs if m["role"] == "assistant"]
        assert len(assistant_msgs) == 1
        assert len(assistant_msgs[0]["content"]) <= 200

        # Mark read
        patch_res = await client.patch(f"/api/topics/{topic_id}", json={"unread": False})
        assert patch_res.status_code == 200
        assert patch_res.json()["unread"] is False


@pytest.mark.asyncio
async def test_heavy_path_hitl_pause_resume_and_cancel(tmp_path: Path):
    app, _db, _bus, fake_llm, _db_path = _build_test_app(tmp_path)
    fake_llm.next_decision = RouteDecision(
        path="heavy",
        topic_action="create",
        topic_id=None,
        new_topic_title="容器重启",
        confidence=0.96,
        reasoning="需要高危操作确认",
    )
    fake_llm.agent_script = [
        SimpleNamespace(
            id="int_hitl_1",
            status="requires_action",
            steps=[
                SimpleNamespace(
                    type="function_call",
                    id="fc_hitl",
                    name="request_human_approval",
                    arguments={
                        "prompt": "Kiki 需要你拍板：是否允许重启 3 个异常容器？",
                        "options": [
                            {
                                "label": "同意重启",
                                "value": "approve_restart",
                                "approved": True,
                                "style": "primary",
                            },
                            {
                                "label": "拒绝并终止",
                                "value": "reject_restart",
                                "approved": False,
                                "style": "danger",
                            },
                        ],
                    },
                )
            ],
            output_text=None,
        ),
        SimpleNamespace(
            id="int_hitl_2",
            status="completed",
            steps=[],
            output_text="已按你的授权完成 3 个异常容器重启，服务恢复正常。",
        ),
    ]

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        res = await client.post("/api/chat", json={"content": "帮我重启异常容器"})
        run_id = res.json()["session_run"]["id"]

        waiting = await _wait_for_run_status(client, run_id, {"waiting_input"})
        assert waiting["hitl_request"] is not None
        assert "3 个异常容器" in waiting["hitl_request"]["prompt"]

        resume_res = await client.post(
            f"/api/runs/{run_id}/resume",
            json={"decision": "approve_restart", "approved": True},
        )
        assert resume_res.status_code == 200

        done = await _wait_for_run_status(client, run_id, {"completed"})
        assert done["status"] == "completed"

        # Now test cancel on a second HITL run
        fake_llm._agent_turn = 0
        fake_llm.next_decision = RouteDecision(
            path="heavy",
            topic_action="create",
            topic_id=None,
            new_topic_title="取消测试",
            confidence=0.9,
            reasoning="测试取消",
        )
        res2 = await client.post("/api/chat", json={"content": "再来一次高危操作"})
        run2_id = res2.json()["session_run"]["id"]
        await _wait_for_run_status(client, run2_id, {"waiting_input"})

        cancel_res = await client.post(f"/api/runs/{run2_id}/cancel")
        assert cancel_res.status_code == 200
        assert cancel_res.json()["run"]["status"] == "cancelled"

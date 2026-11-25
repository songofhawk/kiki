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
from app.schemas import (
    BacktrackDecision,
    DreamConsolidationPlan,
    DreamMigrationGroup,
    DreamTopicSummaryUpdate,
    RouteDecision,
)
from app.sse import EventBus


class FakeLLMClient:
    """Deterministic fake LLM client for fast, repeatable integration tests."""

    def __init__(self) -> None:
        self.route_calls = 0
        self.next_decision: Optional[RouteDecision] = None
        self.last_recent_messages: list[dict[str, Any]] = []
        self.last_fast_history: list[dict[str, Any]] = []
        self.backtrack_calls: list[dict[str, Any]] = []
        self.backtrack_handler: Optional[Any] = None
        self.dream_calls: list[dict[str, Any]] = []
        self.dream_handler: Optional[Any] = None
        self.agent_script: list[Any] = []
        self._agent_turn = 0

    async def route_message(
        self,
        content: str,
        active_topics: list[dict[str, Any]],
        recent_messages: Optional[list[dict[str, Any]]] = None,
    ) -> RouteDecision:
        self.route_calls += 1
        self.last_recent_messages = list(recent_messages or [])
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

    async def select_backtrack_messages(
        self,
        *,
        target_topic: dict[str, Any],
        trigger_content: str,
        candidate_messages: list[dict[str, Any]],
        already_matched_messages: Optional[list[dict[str, Any]]] = None,
    ) -> BacktrackDecision:
        self.backtrack_calls.append(
            {
                "target_topic": target_topic,
                "trigger_content": trigger_content,
                "candidate_ids": [m["id"] for m in candidate_messages],
                "already_matched_ids": [m["id"] for m in (already_matched_messages or [])],
            }
        )
        if self.backtrack_handler is not None:
            return self.backtrack_handler(
                target_topic=target_topic,
                trigger_content=trigger_content,
                candidate_messages=candidate_messages,
                already_matched_messages=already_matched_messages or [],
            )
        return BacktrackDecision(matched_message_ids=[], reasoning="测试默认不回溯")

    async def consolidate_dream(
        self,
        *,
        active_topics: list[dict[str, Any]],
        general_messages: list[dict[str, Any]],
        topics_to_summarize: Optional[list[dict[str, Any]]] = None,
    ) -> DreamConsolidationPlan:
        self.dream_calls.append(
            {
                "active_topic_ids": [t["id"] for t in active_topics],
                "general_message_ids": [m["id"] for m in general_messages],
                "summarize_topic_ids": [t["id"] for t in (topics_to_summarize or [])],
            }
        )
        if self.dream_handler is not None:
            result = self.dream_handler(
                active_topics=active_topics,
                general_messages=general_messages,
                topics_to_summarize=topics_to_summarize or [],
            )
            if asyncio.iscoroutine(result):
                return await result
            return result
        return DreamConsolidationPlan(reasoning="测试默认做梦无变更")

    async def stream_fast_reply(
        self,
        content: str,
        history: list[dict[str, Any]],
        topic_title: str,
    ) -> AsyncIterator[str]:
        self.last_fast_history = list(history)
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


def _build_test_app(tmp_path: Path, *, llm: Optional[Any] = None):
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
    fake_llm = llm or FakeLLMClient()
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
async def test_fast_path_streaming_and_topic_focus_bypass(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from app.adapters.codex import CodexAdapter

    monkeypatch.setattr(CodexAdapter, "resolve_binary", lambda self: None)
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
        assert data["assistant_message"]["streaming"] is True

        # Wait briefly for background streaming to finish
        await asyncio.sleep(0.15)
        msgs = (await client.get("/api/messages")).json()["messages"]
        assistant_msgs = [m for m in msgs if m["role"] == "assistant"]
        assert len(assistant_msgs) == 1
        assert "喵~" in assistant_msgs[0]["content"]
        assert assistant_msgs[0]["streaming"] is False

        # 2. Create a custom topic and test Topic Focus Mode:
        # 2a. Explicit force_path bypasses Decision Model
        t_res = await client.post("/api/topics", json={"title": "排查502"})
        assert t_res.status_code == 201
        topic_id = t_res.json()["id"]

        res2 = await client.post(
            "/api/chat",
            json={
                "content": "继续看这个报错",
                "focus_topic_id": topic_id,
                "force_path": "fast",
            },
        )
        assert res2.status_code == 200
        data2 = res2.json()
        assert fake_llm.route_calls == 1
        assert data2["topic"]["id"] == topic_id
        assert data2["route_decision"]["path"] == "fast"
        assert data2["route_decision"]["confidence"] == 1.0

        # 2b. Default auto mode in Topic Focus Mode still classifies path (e.g. heavy) while locking topic
        fake_llm.next_decision = RouteDecision(
            path="heavy",
            topic_action="create",
            topic_id=None,
            new_topic_title="不应被新建的话题",
            agent_type="codex",
            confidence=0.88,
            reasoning="用户要求重新执行后台任务",
        )
        res3 = await client.post(
            "/api/chat",
            json={
                "content": "来，再生成一次",
                "focus_topic_id": topic_id,
                "agent_type": None,
            },
        )
        assert res3.status_code == 200
        data3 = res3.json()
        assert fake_llm.route_calls == 2
        assert data3["topic"]["id"] == topic_id
        assert data3["route_decision"]["path"] == "heavy"
        assert data3["route_decision"]["topic_action"] == "reuse"
        assert data3["route_decision"]["confidence"] == 1.0
        assert data3["session_run"] is not None
        assert data3["session_run"]["agent_type"] == "codex"


@pytest.mark.asyncio
async def test_explicit_agent_skips_model_selection_while_auto_uses_decision_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from app.adapters.codex import CodexAdapter

    monkeypatch.setattr(CodexAdapter, "resolve_binary", lambda self: None)
    app, _db, _bus, fake_llm, _db_path = _build_test_app(tmp_path)

    # 1. Auto mode (agent_type omitted / None): Decision Model chooses agent_type
    fake_llm.next_decision = RouteDecision(
        path="heavy",
        topic_action="create",
        topic_id=None,
        new_topic_title="生成猫猫 lulu 图片",
        agent_type="codex",
        confidence=0.95,
        reasoning="用户请求调用 Codex 生图",
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        res = await client.post(
            "/api/chat",
            json={"content": "调用 Codex 帮我生成一张猫猫的图吧，名字叫 lulu"},
        )
        assert res.status_code == 200
        data = res.json()
        assert data["route_decision"]["path"] == "heavy"
        assert data["route_decision"]["agent_type"] == "codex"
        assert data["session_run"] is not None
        assert data["session_run"]["agent_type"] == "codex"

        # 2. Explicit agent_type in request overrides Decision Model (skips model agent selection)
        topic_id = data["topic"]["id"]
        fake_llm.next_decision = RouteDecision(
            path="heavy",
            topic_action="reuse",
            topic_id=topic_id,
            new_topic_title=None,
            agent_type="codex",
            confidence=0.9,
            reasoning="默认判断",
        )
        res2 = await client.post(
            "/api/chat",
            json={
                "content": "这次用内置引擎再试一次",
                "focus_topic_id": topic_id,
                "agent_type": "gemini_agent",
            },
        )
        assert res2.status_code == 200
        data2 = res2.json()
        assert data2["route_decision"]["agent_type"] == "gemini_agent"
        assert data2["session_run"]["agent_type"] == "gemini_agent"

        # 3. Literal "auto" is treated identically to None (falls back to Decision Model)
        fake_llm.next_decision = RouteDecision(
            path="heavy",
            topic_action="reuse",
            topic_id=topic_id,
            new_topic_title=None,
            agent_type="codex",
            confidence=0.9,
            reasoning="auto 模式采用路由结果",
        )
        res3 = await client.post(
            "/api/chat",
            json={
                "content": "自动选择引擎重跑",
                "focus_topic_id": topic_id,
                "agent_type": "auto",
            },
        )
        assert res3.status_code == 200
        assert res3.json()["session_run"]["agent_type"] == "codex"


@pytest.mark.asyncio
async def test_failed_heavy_run_creates_readable_kiki_reply(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from app.adapters.codex import CodexAdapter

    async def fake_execute_usage_limit(self, ctx):
        raise RuntimeError(
            "OpenAI Codex CLI 退出码 1: You’ve hit your usage limit. "
            "Upgrade to Pro (https://chatgpt.com/explore/pro) or try again at 9:42 PM."
        )

    monkeypatch.setattr(CodexAdapter, "execute", fake_execute_usage_limit)
    app, _db, _bus, fake_llm, _db_path = _build_test_app(tmp_path)
    fake_llm.next_decision = RouteDecision(
        path="heavy",
        topic_action="create",
        topic_id=None,
        new_topic_title="Codex 额度测试",
        agent_type="codex",
        confidence=0.95,
        reasoning="调用 Codex 执行任务",
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        res = await client.post(
            "/api/chat",
            json={"content": "调用 Codex 生成一张猫猫图"},
        )
        assert res.status_code == 200
        run_id = res.json()["session_run"]["id"]
        topic_id = res.json()["topic"]["id"]

        run_detail = await _wait_for_run_status(client, run_id, {"failed"})
        assert run_detail["status"] == "failed"

        msgs = (await client.get(f"/api/messages?topic_id={topic_id}")).json()["messages"]
        assistant_msgs = [m for m in msgs if m["role"] == "assistant"]
        assert len(assistant_msgs) == 1
        reply_text = assistant_msgs[0]["content"]
        assert "OpenAI Codex CLI" in reply_text
        assert "额度" in reply_text
        assert "9:42 PM" in reply_text


@pytest.mark.asyncio
async def test_corrective_agent_switch_prefers_desired_agent_over_complained_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from app.adapters.workbuddy import WorkBuddyAdapter

    monkeypatch.setattr(WorkBuddyAdapter, "resolve_binary", lambda self: None)
    app, _db, _bus, fake_llm, _db_path = _build_test_app(tmp_path)
    fake_llm.next_decision = RouteDecision(
        path="fast",
        topic_action="create",
        topic_id=None,
        new_topic_title="生成猫猫图",
        agent_type="workbuddy",
        confidence=0.9,
        reasoning="用户要求用 WorkBuddy 执行任务",
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        res = await client.post(
            "/api/chat",
            json={
                "content": "不是，我是让你用 WorkBuddy 来执行，你为什么不停地用 Codex？",
                "agent_type": None,
            },
        )
        assert res.status_code == 200
        data = res.json()
        assert data["route_decision"]["path"] == "heavy"
        assert data["route_decision"]["agent_type"] == "workbuddy"
        assert data["session_run"] is not None
        assert data["session_run"]["agent_type"] == "workbuddy"


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

        msgs = (await client.get(f"/api/messages?topic_id={topic_id}")).json()["messages"]
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


@pytest.mark.asyncio
async def test_recursive_upward_backtrack_on_topic_create_and_pair_integrity(tmp_path: Path):
    app, db, bus, fake_llm, _db_path = _build_test_app(tmp_path)
    # Use small batch size (4 messages = 2 turns) to verify multi-round upward backtracking
    app.state.decision_router.backtrack_batch_size = 4
    app.state.decision_router.max_backtrack_batches = 6

    db.ensure_general_topic()
    # Unrelated casual messages (oldest, should stay in topic-general)
    u0 = db.create_message("topic-general", "user", "早安 Kiki")
    a0 = db.create_message("topic-general", "assistant", "早安喵~", metadata={"reply_to": u0["id"]})

    # Older 社保 messages across 2+ batches (should all be pulled into new topic)
    u1 = db.create_message("topic-general", "user", "下个月准备离职了")
    a1 = db.create_message("topic-general", "assistant", "离职要注意社保衔接哦", metadata={"reply_to": u1["id"]})
    u2 = db.create_message("topic-general", "user", "担心五险一金断缴影响医保")
    a2 = db.create_message("topic-general", "assistant", "断缴次月医保就不能报销了", metadata={"reply_to": u2["id"]})
    u3 = db.create_message("topic-general", "user", "北京可以自己交灵活就业吗？")
    a3 = db.create_message("topic-general", "assistant", "可以的，线上就能办理", metadata={"reply_to": u3["id"]})

    # Extra unrelated middle/boundary test: notice batch 1 sees [u2, a2, u3, a3],
    # batch 2 sees [u0, a0, u1, a1] -> matches u1/a1 -> continues to batch 3? No more msgs -> stops!
    # Also test pair expansion: suppose model only returns assistant IDs [a2, a3] in batch 1 and [a1] in batch 2!
    def _select_backtrack(**kwargs: Any) -> BacktrackDecision:
        candidates = kwargs["candidate_messages"]
        matched = [
            m["id"]
            for m in candidates
            if m["id"] in {a1["id"], a2["id"], a3["id"]}
        ]
        # Verify batch 2 receives already_matched_messages from batch 1
        if any(m["id"] == a1["id"] for m in candidates):
            already_ids = {m["id"] for m in kwargs["already_matched_messages"]}
            assert {u2["id"], a2["id"], u3["id"], a3["id"]}.issubset(already_ids)
        return BacktrackDecision(matched_message_ids=matched, reasoning="社保前置上下文")

    fake_llm.backtrack_handler = _select_backtrack
    fake_llm.next_decision = RouteDecision(
        path="fast",
        topic_action="create",
        topic_id=None,
        new_topic_title="北京离职社保指南",
        confidence=0.94,
        reasoning="明确讨论北京离职社保",
    )

    sse_events: list[tuple[str, dict[str, Any]]] = []
    original_publish = bus.publish

    def _capture_publish(event_type: str, payload: dict[str, Any]) -> None:
        sse_events.append((event_type, payload))
        original_publish(event_type, payload)

    bus.publish = _capture_publish  # type: ignore[method-assign]

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        res = await client.post("/api/chat", json={"content": "帮我总结北京社保不断缴步骤"})
        assert res.status_code == 200
        body = res.json()
        new_topic_id = body["topic"]["id"]
        assert body["topic"]["title"] == "北京离职社保指南"

        # 1. Decision model received recent context from topic-general
        assert len(fake_llm.last_recent_messages) == 8
        assert fake_llm.last_recent_messages[-1]["id"] == a3["id"]

        # 2. Upward backtracking ran 2 batches (since batch 2 hit the top of topic-general, batch 3 had 0 rows)
        assert len(fake_llm.backtrack_calls) == 2

        # 3. Pair expansion pulled in u1, u2, u3 alongside a1, a2, a3; u0/a0 stayed in topic-general
        reassigned_ids = [m["id"] for m in body["reassigned_messages"]]
        assert reassigned_ids == [u1["id"], a1["id"], u2["id"], a2["id"], u3["id"], a3["id"]]
        for m in body["reassigned_messages"]:
            assert m["topic_id"] == new_topic_id
            assert m["metadata"]["backtracked"] is True
            assert m["metadata"]["reassigned_from"] == "topic-general"

        general_msgs = (await client.get("/api/messages?topic_id=topic-general")).json()["messages"]
        assert [m["id"] for m in general_msgs] == [u0["id"], a0["id"]]

        # Wait briefly for fast reply streaming to complete and verify FastPath received backtracked history
        await asyncio.sleep(0.15)
        history_contents = [m["content"] for m in fake_llm.last_fast_history]
        assert "下个月准备离职了" in history_contents
        assert "北京可以自己交灵活就业吗？" in history_contents

        topic_msgs = (await client.get(f"/api/messages?topic_id={new_topic_id}")).json()["messages"]
        # 6 backtracked + 1 new user + 1 new assistant = 8
        assert len(topic_msgs) == 8

        # 4. SSE emitted message_updated for all 6 reassigned messages and topic_updated for both topics
        updated_msg_events = [p["message"]["id"] for et, p in sse_events if et == "message_updated"]
        assert updated_msg_events == reassigned_ids
        updated_topic_ids = {p["topic"]["id"] for et, p in sse_events if et == "topic_updated"}
        assert {"topic-general", new_topic_id}.issubset(updated_topic_ids)


@pytest.mark.asyncio
async def test_upward_backtrack_stops_when_older_batch_unrelated_and_handles_reuse(tmp_path: Path):
    app, db, _bus, fake_llm, _db_path = _build_test_app(tmp_path)
    app.state.decision_router.backtrack_batch_size = 2
    app.state.decision_router.max_backtrack_batches = 6

    db.ensure_general_topic()
    # Oldest batch (should never be reached once middle batch returns empty)
    u_old = db.create_message("topic-general", "user", "上个月聊过一次社保")
    a_old = db.create_message("topic-general", "assistant", "好的喵", metadata={"reply_to": u_old["id"]})
    # Middle unrelated batch -> stops upward traversal here!
    u_mid = db.create_message("topic-general", "user", "今天中午吃什么")
    a_mid = db.create_message("topic-general", "assistant", "吃牛肉面吧", metadata={"reply_to": u_mid["id"]})
    # Latest batch in general (misclassified start of existing topic)
    u_new = db.create_message("topic-general", "user", "对了帮我看下报销进度")
    a_new = db.create_message("topic-general", "assistant", "哪个报销呀？", metadata={"reply_to": u_new["id"]})

    # Pre-existing topic
    existing_topic = db.create_topic(title="差旅报销")

    def _select_only_reimbursement(**kwargs: Any) -> BacktrackDecision:
        matched = [
            m["id"]
            for m in kwargs["candidate_messages"]
            if "报销" in m["content"]
        ]
        return BacktrackDecision(matched_message_ids=matched, reasoning="仅匹配报销相关")

    fake_llm.backtrack_handler = _select_only_reimbursement
    fake_llm.next_decision = RouteDecision(
        path="fast",
        topic_action="reuse",
        topic_id=existing_topic["id"],
        new_topic_title=None,
        confidence=0.95,
        reasoning="延续差旅报销话题",
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        res = await client.post("/api/chat", json={"content": "就是上周上海出差的差旅报销"})
        assert res.status_code == 200
        body = res.json()
        assert body["topic"]["id"] == existing_topic["id"]
        assert [m["id"] for m in body["reassigned_messages"]] == [u_new["id"], a_new["id"]]
        # Batch 1 matched [u_new, a_new] -> continued upwards to Batch 2 [u_mid, a_mid] -> returned [] -> stopped before Batch 3!
        assert len(fake_llm.backtrack_calls) == 2

        await asyncio.sleep(0.15)
        # Now latest message is in existing_topic (NOT topic-general), so another reuse must NOT trigger backtrack
        fake_llm.next_decision = RouteDecision(
            path="fast",
            topic_action="reuse",
            topic_id=existing_topic["id"],
            new_topic_title=None,
            confidence=0.98,
            reasoning="继续差旅报销",
        )
        res2 = await client.post("/api/chat", json={"content": "发票我待会发你"})
        assert res2.status_code == 200
        assert res2.json()["reassigned_messages"] == []
        assert len(fake_llm.backtrack_calls) == 2


@pytest.mark.asyncio
async def test_dream_consolidation_quietly_organizes_general_and_summaries(tmp_path: Path):
    app, db, bus, fake_llm, _db_path = _build_test_app(tmp_path)
    dream = app.state.dream_consolidator

    db.ensure_general_topic()
    existing_topic = db.create_topic(title="社保办理")
    db.create_message(existing_topic["id"], "user", "北京社保怎么查缴费记录？")
    db.create_message(existing_topic["id"], "assistant", "可以在北京人社 App 查询。")

    # Pure chitchat (should stay in topic-general and get marked dream_checked_at)
    u_chat = db.create_message("topic-general", "user", "早安 Kiki")
    a_chat = db.create_message("topic-general", "assistant", "早安喵~", metadata={"reply_to": u_chat["id"]})

    # Stray messages belonging to existing_topic
    u_stray = db.create_message("topic-general", "user", "对了，社保基数每年几月调整来着？")
    a_stray = db.create_message("topic-general", "assistant", "通常在每年 7 月调整。", metadata={"reply_to": u_stray["id"]})

    # Cohesive 2-turn cluster for a brand-new topic in general
    u_trip1 = db.create_message("topic-general", "user", "下个月想去成都玩三天")
    a_trip1 = db.create_message("topic-general", "assistant", "成都三天很合适！", metadata={"reply_to": u_trip1["id"]})
    u_trip2 = db.create_message("topic-general", "user", "帮我推荐下宽窄巷子和熊猫基地的路线")
    a_trip2 = db.create_message("topic-general", "assistant", "可以上午先去熊猫基地...", metadata={"reply_to": u_trip2["id"]})

    def _dream_plan(**_kwargs: Any) -> DreamConsolidationPlan:
        return DreamConsolidationPlan(
            migrations=[
                DreamMigrationGroup(
                    action="move_to_existing",
                    target_topic_id=existing_topic["id"],
                    message_ids=[u_stray["id"]],  # pair expansion should pull a_stray too
                    confidence=0.91,
                    reasoning="属于已有社保话题",
                ),
                DreamMigrationGroup(
                    action="create_topic",
                    new_topic_title="成都三日游规划",
                    new_topic_summary="成都三日游行程与景点路线安排",
                    message_ids=[u_trip1["id"], a_trip1["id"], u_trip2["id"], a_trip2["id"]],
                    confidence=0.89,
                    reasoning="连续两轮聚焦成都旅游路线",
                ),
            ],
            topic_summaries=[
                DreamTopicSummaryUpdate(
                    topic_id=existing_topic["id"],
                    summary="北京社保缴费查询与基数调整咨询",
                )
            ],
            reasoning="整理日常闲聊并补全话题摘要",
        )

    fake_llm.dream_handler = _dream_plan
    sse_events: list[tuple[str, dict[str, Any]]] = []
    original_publish = bus.publish
    bus.publish = lambda et, payload: (sse_events.append((et, payload)), original_publish(et, payload))  # type: ignore[method-assign]

    res = await dream.run_once(force=True)
    assert res["ran"] is True
    assert res["migrated_count"] == 6
    assert len(res["created_topics"]) == 1
    assert existing_topic["id"] in res["updated_summaries"]

    # Pure chitchat remains in topic-general and is stamped dream_checked_at
    general_msgs = db.list_messages(topic_id="topic-general")
    assert [m["id"] for m in general_msgs] == [u_chat["id"], a_chat["id"]]
    assert all(m["metadata"].get("dream_checked_at") for m in general_msgs)

    # Summary updated on existing_topic and stray pair moved in
    refreshed_existing = db.get_topic(existing_topic["id"])
    assert refreshed_existing is not None
    assert refreshed_existing["summary"] == "北京社保缴费查询与基数调整咨询"
    assert refreshed_existing["message_count"] == 4
    # Quietly consolidated without unread/toast
    assert refreshed_existing["unread"] is False
    assert not any(et == "toast_notification" for et, _ in sse_events)

    # Running again without new messages or new topics sees nothing left to scan
    res_again = await dream.run_once(force=True)
    assert res_again["ran"] is False
    assert res_again["reason"] == "nothing_to_consolidate"
    assert len(fake_llm.dream_calls) == 1

    # Activity interruption check (non-forced run aborts if user interrupts mid-dream)
    u_new = db.create_message("topic-general", "user", "再问个新问题")
    dream._last_activity_at -= 1000.0
    dream._last_dream_at = 0.0

    async def _interrupted_plan(**_kwargs: Any) -> DreamConsolidationPlan:
        dream.notify_activity()
        return DreamConsolidationPlan(
            migrations=[
                DreamMigrationGroup(
                    action="move_to_existing",
                    target_topic_id=existing_topic["id"],
                    message_ids=[u_new["id"]],
                    confidence=0.95,
                )
            ]
        )

    fake_llm.dream_handler = _interrupted_plan
    aborted = await dream.run_once(force=False)
    assert aborted["ran"] is False
    assert aborted["reason"] == "interrupted_by_activity"
    assert db.get_message(u_new["id"])["topic_id"] == "topic-general"


@pytest.mark.asyncio
async def test_batch_multi_select_message_topic_split_and_move(tmp_path: Path):
    app, db, bus, _fake_llm, _db_path = _build_test_app(tmp_path)
    db.ensure_general_topic()
    m1 = db.create_message("topic-general", "user", "先聊聊基金定投")
    m2 = db.create_message("topic-general", "assistant", "定投适合长期坚持喵", metadata={"reply_to": m1["id"]})
    m3 = db.create_message("topic-general", "user", "每月投多少合适？")
    m4 = db.create_message("topic-general", "assistant", "建议按闲钱比例安排", metadata={"reply_to": m3["id"]})

    sse_events: list[tuple[str, dict[str, Any]]] = []
    original_publish = bus.publish
    bus.publish = lambda et, payload: (sse_events.append((et, payload)), original_publish(et, payload))  # type: ignore[method-assign]

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        # 1. Batch split selected messages into a new topic
        split_res = await client.post(
            "/api/messages/batch-topic",
            json={
                "message_ids": [m1["id"], m2["id"], m3["id"]],
                "action": "split",
                "new_topic_title": "基金定投计划",
            },
        )
        assert split_res.status_code == 200
        split_body = split_res.json()
        new_tid = split_body["topic"]["id"]
        assert split_body["topic"]["title"] == "基金定投计划"
        assert [m["id"] for m in split_body["messages"]] == [m1["id"], m2["id"], m3["id"]]
        assert all(m["topic_id"] == new_tid for m in split_body["messages"])
        assert db.get_message(m4["id"])["topic_id"] == "topic-general"

        # 2. Batch move remaining m4 into that topic
        move_res = await client.post(
            "/api/messages/batch-topic",
            json={
                "message_ids": [m4["id"]],
                "action": "move",
                "target_topic_id": new_tid,
            },
        )
        assert move_res.status_code == 200
        assert move_res.json()["messages"][0]["topic_id"] == new_tid
        assert move_res.json()["topic"]["message_count"] == 4

        updated_msg_ids = [p["message"]["id"] for et, p in sse_events if et == "message_updated"]
        assert updated_msg_ids == [m1["id"], m2["id"], m3["id"], m4["id"]]


@pytest.mark.asyncio
async def test_messages_cursor_pagination_and_active_runs(tmp_path: Path):
    app, db, _bus, _fake_llm, _db_path = _build_test_app(tmp_path)
    db.ensure_general_topic()
    topic_a = db.create_topic(title="话题A")
    topic_b = db.create_topic(title="话题B")

    # Insert 10 messages across two topics so global stream & topic stream both paginate cleanly
    created_all: list[dict[str, Any]] = []
    created_a: list[dict[str, Any]] = []
    for i in range(1, 11):
        tid = topic_a["id"] if i % 2 == 1 else topic_b["id"]
        msg = db.create_message(tid, "user", f"msg-{i}")
        created_all.append(msg)
        if tid == topic_a["id"]:
            created_a.append(msg)

    # Create an active run attached to the OLDEST message (outside latest page!)
    old_active_run = db.create_session_run(
        topic_id=topic_a["id"],
        trigger_message_id=created_all[0]["id"],
        agent_type="gemini_agent",
        status="running",
        current_step="正在处理旧任务",
    )
    # Create a completed run (should NOT be returned when active_only=true)
    db.create_session_run(
        topic_id=topic_b["id"],
        trigger_message_id=created_all[-1]["id"],
        agent_type="gemini_agent",
        status="completed",
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        # 1. Global stream first page returns LATEST 4 messages in chronological order
        p1 = (await client.get("/api/messages?limit=4")).json()
        assert [m["content"] for m in p1["messages"]] == ["msg-7", "msg-8", "msg-9", "msg-10"]
        assert p1["has_more"] is True
        assert p1["next_cursor"] == created_all[6]["id"]

        # Simulate a brand-new message arriving while user is scrolling up
        db.create_message(topic_b["id"], "assistant", "msg-11-live")

        # 2. Global stream second page using before_id must NOT duplicate or skip due to msg-11-live
        p2 = (
            await client.get(f"/api/messages?limit=4&before_id={p1['next_cursor']}")
        ).json()
        assert [m["content"] for m in p2["messages"]] == ["msg-3", "msg-4", "msg-5", "msg-6"]
        assert p2["has_more"] is True
        assert p2["next_cursor"] == created_all[2]["id"]

        # 3. Global stream final page reaches beginning
        p3 = (
            await client.get(f"/api/messages?limit=4&before_id={p2['next_cursor']}")
        ).json()
        assert [m["content"] for m in p3["messages"]] == ["msg-1", "msg-2"]
        assert p3["has_more"] is False
        assert p3["next_cursor"] is None

        # 4. Topic-scoped pagination independently pages only topic_a messages (msg-1,3,5,7,9)
        tp1 = (await client.get(f"/api/messages?topic_id={topic_a['id']}&limit=3")).json()
        assert [m["content"] for m in tp1["messages"]] == ["msg-5", "msg-7", "msg-9"]
        assert tp1["has_more"] is True
        assert tp1["next_cursor"] == created_a[2]["id"]

        tp2 = (
            await client.get(
                f"/api/messages?topic_id={topic_a['id']}&limit=3&before_id={tp1['next_cursor']}"
            )
        ).json()
        assert [m["content"] for m in tp2["messages"]] == ["msg-1", "msg-3"]
        assert tp2["has_more"] is False
        assert tp2["next_cursor"] is None

        # 5. Active runs endpoint surfaces active runs even when their trigger message is on an older page
        active_runs = (await client.get("/api/runs?active_only=true")).json()
        assert [r["id"] for r in active_runs] == [old_active_run["id"]]

        all_runs = (await client.get("/api/runs?active_only=false")).json()
        assert len(all_runs) == 2


@pytest.mark.asyncio
async def test_parallel_tasks_and_chat_while_running(tmp_path: Path):
    """While Heavy-Path tasks are running, Kiki accepts parallel Heavy and Fast inputs without blocking."""
    gate_a = asyncio.Event()
    gate_b = asyncio.Event()
    started_a = asyncio.Event()
    started_b = asyncio.Event()

    class ParallelFakeLLM(FakeLLMClient):
        async def run_agent_turn(
            self,
            input_data: Any,
            tools: list[dict[str, Any]],
            system_instruction: str,
            previous_interaction_id: Optional[str] = None,
            thinking_level: str = "medium",
        ) -> Any:
            text = str(input_data)
            if "任务A" in text:
                started_a.set()
                await gate_a.wait()
                return SimpleNamespace(id="int-a", status="completed", steps=[], output_text="任务A完成")
            if "任务B" in text:
                started_b.set()
                await gate_b.wait()
                return SimpleNamespace(id="int-b", status="completed", steps=[], output_text="任务B完成")
            return SimpleNamespace(id="int-x", status="completed", steps=[], output_text="完成")

        async def stream_fast_reply(
            self,
            content: str,
            history: list[dict[str, str]],
            topic_title: str,
        ):
            yield f"并行轻聊回复:{content}"

    app, db, bus, _fake_llm, _db_path = _build_test_app(tmp_path, llm=ParallelFakeLLM())
    topic_a = db.create_topic(title="并行话题A")
    topic_b = db.create_topic(title="并行话题B")
    topic_c = db.create_topic(title="并行轻聊C")

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        # 1. Start Heavy-Path Task A
        res_a = await client.post(
            "/api/chat",
            json={
                "content": "执行任务A",
                "focus_topic_id": topic_a["id"],
                "force_path": "heavy",
            },
        )
        assert res_a.status_code == 200
        run_a_id = res_a.json()["session_run"]["id"]
        await asyncio.wait_for(started_a.wait(), timeout=3.0)

        # 2. While Task A is still running, launch Heavy-Path Task B AND Fast-Path Chat C concurrently
        res_b, res_c = await asyncio.gather(
            client.post(
                "/api/chat",
                json={
                    "content": "执行任务B",
                    "focus_topic_id": topic_b["id"],
                    "force_path": "heavy",
                },
            ),
            client.post(
                "/api/chat",
                json={
                    "content": "顺便问一句天气",
                    "focus_topic_id": topic_c["id"],
                    "force_path": "fast",
                },
            ),
        )
        assert res_b.status_code == 200
        assert res_c.status_code == 200
        run_b_id = res_b.json()["session_run"]["id"]
        await asyncio.wait_for(started_b.wait(), timeout=3.0)

        # Both runs are simultaneously active
        active_runs = (await client.get("/api/runs?active_only=true")).json()
        assert {r["id"] for r in active_runs} == {run_a_id, run_b_id}

        # Wait briefly for Fast-Path C to finish streaming; status must stay 'running' while A & B run
        await asyncio.sleep(0.05)
        msgs_c = (await client.get(f"/api/messages?topic_id={topic_c['id']}")).json()["messages"]
        assert any("并行轻聊回复:顺便问一句天气" in m["content"] for m in msgs_c)
        assert bus.current_kiki_status["status"] == "running"

        # 3. Release Task B first -> Task A is still running so status remains 'running'
        gate_b.set()
        for _ in range(50):
            r_b = (await client.get(f"/api/runs/{run_b_id}")).json()
            if r_b["status"] == "completed":
                break
            await asyncio.sleep(0.02)
        assert r_b["status"] == "completed"
        assert bus.current_kiki_status["status"] == "running"

        # 4. Release Task A -> now everything is done and status returns to 'idle'
        gate_a.set()
        for _ in range(50):
            r_a = (await client.get(f"/api/runs/{run_a_id}")).json()
            if r_a["status"] == "completed":
                break
            await asyncio.sleep(0.02)
        assert r_a["status"] == "completed"
        assert bus.current_kiki_status["status"] == "idle"


@pytest.mark.asyncio
async def test_cors_restricts_origins_and_optional_api_token(tmp_path: Path):
    db_path = tmp_path / "kiki_sec.db"
    settings = Settings(
        gemini_api_key="test-key",
        gemini_model="gemini-3.5-flash-lite",
        workspace_root=tmp_path,
        database_path=db_path,
        artifacts_dir=tmp_path / "artifacts",
        api_token="secret-local-token",
    )
    db = Database(db_path)
    db.init_db()
    app = create_app(settings=settings, db=db, llm=FakeLLMClient(), event_bus=EventBus())

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        # Allowed localhost origin receives CORS header
        res_ok = await client.get(
            "/api/health", headers={"Origin": "http://localhost:5173"}
        )
        assert res_ok.status_code == 200
        assert res_ok.headers.get("access-control-allow-origin") == "http://localhost:5173"

        # Untrusted external origin is NOT reflected
        res_evil = await client.get(
            "/api/health", headers={"Origin": "https://evil.example"}
        )
        assert res_evil.status_code == 200
        assert res_evil.headers.get("access-control-allow-origin") is None

        # Protected endpoint requires Bearer token when api_token is configured
        res_unauth = await client.get("/api/topics")
        assert res_unauth.status_code == 401

        res_bad = await client.get(
            "/api/topics", headers={"Authorization": "Bearer wrong-token"}
        )
        assert res_bad.status_code == 401

        res_auth = await client.get(
            "/api/topics", headers={"Authorization": "Bearer secret-local-token"}
        )
        assert res_auth.status_code == 200


@pytest.mark.asyncio
async def test_workspace_tools_block_sensitive_files_and_scrub_shell_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from app.tools import WorkspaceToolExecutor

    (tmp_path / ".env").write_text("GEMINI_API_KEY=super-secret-value\n", encoding="utf-8")
    (tmp_path / ".env.example").write_text("GEMINI_API_KEY=your_key\n", encoding="utf-8")
    (tmp_path / "private.pem").write_text("PRIVATE KEY", encoding="utf-8")
    monkeypatch.setenv("GEMINI_API_KEY", "should-not-leak-to-shell")
    monkeypatch.setenv("KIKI_API_TOKEN", "token-should-not-leak")

    db = Database(tmp_path / "tools.db")
    db.init_db()
    executor = WorkspaceToolExecutor(
        workspace_root=tmp_path,
        artifacts_dir=tmp_path / "artifacts",
        db=db,
        event_bus=EventBus(),
    )

    # list_files hides .env and private.pem, keeps .env.example
    listing = executor.list_files(".")
    entries = listing.splitlines()
    assert ".env.example" in entries
    assert ".env" not in entries
    assert "private.pem" not in entries

    # read_file blocks .env and .pem, allows .env.example
    out_env, err_env = await executor.execute("run-1", "read_file", {"path": ".env"})
    assert err_env is True
    assert "sensitive" in out_env.lower()

    out_pem, err_pem = await executor.execute("run-1", "read_file", {"path": "private.pem"})
    assert err_pem is True

    out_ex, err_ex = await executor.execute("run-1", "read_file", {"path": ".env.example"})
    assert err_ex is False
    assert "your_key" in out_ex

    # run_shell blocks split rm -r -f and scrubs secret env vars
    blocked = await executor.run_shell("run-1", "rm -r -f ./tmp_dir")
    assert "Blocked unsafe command" in blocked

    env_out = await executor.run_shell("run-1", "echo $GEMINI_API_KEY:$KIKI_API_TOKEN")
    assert "should-not-leak-to-shell" not in env_out
    assert "token-should-not-leak" not in env_out

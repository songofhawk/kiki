"""Tests for mid-run steering (TaskManager + InternalAgentAdapter + SubprocessCLIAdapter)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

import pytest

from app.adapters.base import (
    AdapterContext,
    ExecutionProfile,
    StreamParseState,
    SubprocessCLIAdapter,
)
from app.adapters.registry import AdapterRegistry
from app.adapters.internal_agent import InternalAgentAdapter
from app.db import Database
from app.sse import EventBus
from app.task_manager import TaskManager
from app.tools import WorkspaceToolExecutor
from app.workspace import WorkspaceManager


class _SteeringMockLLM:
    """Mock LLM that pauses during turn 1 so the test can inject a steering message."""

    def __init__(self, turn1_started: asyncio.Event, allow_turn1_finish: asyncio.Event) -> None:
        self.turn1_started = turn1_started
        self.allow_turn1_finish = allow_turn1_finish
        self.received_inputs: list[Any] = []
        self.last_used_model = "mock-gemini"

    async def run_agent_turn(
        self,
        *,
        input_data: Any,
        tools: list[dict[str, Any]],
        system_instruction: str,
        previous_interaction_id: Optional[str] = None,
        thinking_level: str = "medium",
        model: Optional[str] = None,
    ) -> Any:
        self.received_inputs.append(input_data)
        turn_idx = len(self.received_inputs)
        if turn_idx == 1:
            self.turn1_started.set()
            await self.allow_turn1_finish.wait()
            return SimpleNamespace(
                id="int_turn_1",
                steps=[
                    SimpleNamespace(
                        type="function_call",
                        id="call_1",
                        name="list_files",
                        arguments={"path": "."},
                    )
                ],
                output_text="",
            )
        return SimpleNamespace(
            id=f"int_turn_{turn_idx}",
            steps=[],
            output_text="已结合补充要求一并完成任务。",
        )


@pytest.mark.asyncio
async def test_internal_agent_receives_mid_run_steering(tmp_path: Path) -> None:
    db = Database(tmp_path / "kiki.db")
    db.init_db()
    bus = EventBus()
    ws_mgr = WorkspaceManager(workspace_root=tmp_path / "ws", legacy_artifacts_dir=tmp_path / "art")
    ws_mgr.ensure_base_layout()
    tool_exec = WorkspaceToolExecutor(
        workspace_root=tmp_path / "ws",
        artifacts_dir=tmp_path / "art",
        db=db,
        event_bus=bus,
    )
    turn1_started = asyncio.Event()
    allow_turn1_finish = asyncio.Event()
    mock_llm = _SteeringMockLLM(turn1_started, allow_turn1_finish)

    registry = AdapterRegistry([InternalAgentAdapter(default_model="mock-gemini", api_key_configured=True)])
    tm = TaskManager(
        db=db,
        llm=mock_llm,  # type: ignore[arg-type]
        event_bus=bus,
        tool_executor=tool_exec,
        adapter_registry=registry,
        workspace_manager=ws_mgr,
    )

    topic = db.create_topic(title="实现登录页")
    msg1 = db.create_message(topic_id=topic["id"], role="user", content="帮我做一个登录页面")
    run = tm.enqueue_run(
        topic_id=topic["id"],
        trigger_message_id=msg1["id"],
        user_content=msg1["content"],
        agent_type="internal_agent",
    )

    await asyncio.wait_for(turn1_started.wait(), timeout=3.0)
    active = tm.get_active_run_for_topic(topic["id"])
    assert active is not None
    assert active["id"] == run["id"]

    msg2 = db.create_message(topic_id=topic["id"], role="user", content="顺便加上记住密码功能")
    steered = tm.steer_run(run["id"], message_id=msg2["id"], content=msg2["content"])
    assert steered is not None
    assert steered["id"] == run["id"]

    allow_turn1_finish.set()
    task = tm._tasks.get(run["id"])
    if task is not None:
        await asyncio.wait_for(task, timeout=5.0)

    final_run = db.get_session_run(run["id"])
    assert final_run is not None
    assert final_run["status"] == "completed"
    # Verify only one run exists for this topic
    all_runs = db.list_session_runs(limit=10)
    assert len(all_runs) == 1

    # Turn 2 input should include both the tool result and the injected steering text
    assert len(mock_llm.received_inputs) == 2
    turn2_input = mock_llm.received_inputs[1]
    assert isinstance(turn2_input, list)
    text_items = [item for item in turn2_input if isinstance(item, dict) and item.get("type") == "text"]
    assert len(text_items) == 1
    assert "顺便加上记住密码功能" in text_items[0]["text"]


class _LateSteeringMockLLM:
    """Mock LLM that finishes turn 1 without tool calls, while steering arrived during turn 1."""

    def __init__(self, turn1_started: asyncio.Event, allow_turn1_finish: asyncio.Event) -> None:
        self.turn1_started = turn1_started
        self.allow_turn1_finish = allow_turn1_finish
        self.received_inputs: list[Any] = []
        self.last_used_model = "mock-gemini"

    async def run_agent_turn(self, *, input_data: Any, **kwargs: Any) -> Any:
        self.received_inputs.append(input_data)
        if len(self.received_inputs) == 1:
            self.turn1_started.set()
            await self.allow_turn1_finish.wait()
            return SimpleNamespace(id="int_1", steps=[], output_text="初版完成")
        return SimpleNamespace(id="int_2", steps=[], output_text="已包含补充要求的最终版")


@pytest.mark.asyncio
async def test_internal_agent_handles_late_steering_before_exit(tmp_path: Path) -> None:
    db = Database(tmp_path / "kiki.db")
    db.init_db()
    bus = EventBus()
    ws_mgr = WorkspaceManager(workspace_root=tmp_path / "ws", legacy_artifacts_dir=tmp_path / "art")
    ws_mgr.ensure_base_layout()
    tool_exec = WorkspaceToolExecutor(
        workspace_root=tmp_path / "ws",
        artifacts_dir=tmp_path / "art",
        db=db,
        event_bus=bus,
    )
    turn1_started = asyncio.Event()
    allow_turn1_finish = asyncio.Event()
    mock_llm = _LateSteeringMockLLM(turn1_started, allow_turn1_finish)

    registry = AdapterRegistry([InternalAgentAdapter(default_model="mock-gemini", api_key_configured=True)])
    tm = TaskManager(
        db=db,
        llm=mock_llm,  # type: ignore[arg-type]
        event_bus=bus,
        tool_executor=tool_exec,
        adapter_registry=registry,
        workspace_manager=ws_mgr,
    )

    topic = db.create_topic(title="写周报")
    msg1 = db.create_message(topic_id=topic["id"], role="user", content="帮我整理本周周报")
    run = tm.enqueue_run(
        topic_id=topic["id"],
        trigger_message_id=msg1["id"],
        user_content=msg1["content"],
    )

    await asyncio.wait_for(turn1_started.wait(), timeout=3.0)
    msg2 = db.create_message(topic_id=topic["id"], role="user", content="补充一条：上线了新路由")
    tm.steer_run(run["id"], message_id=msg2["id"], content=msg2["content"])
    allow_turn1_finish.set()

    task = tm._tasks.get(run["id"])
    if task is not None:
        await asyncio.wait_for(task, timeout=5.0)

    assert len(mock_llm.received_inputs) == 2
    assert "上线了新路由" in str(mock_llm.received_inputs[1])
    msgs = db.list_messages(topic_id=topic["id"], limit=10)
    assistant_msgs = [m for m in msgs if m["role"] == "assistant"]
    assert len(assistant_msgs) == 1
    assert "最终版" in assistant_msgs[0]["content"]


class _DummyCLIAdapter(SubprocessCLIAdapter):
    agent_type = "dummy_cli"
    display_name = "Dummy CLI"
    vendor = "Test"
    description = "Dummy CLI for steering tests"
    binary_candidates = ("dummy",)

    def __init__(self, first_call_started: asyncio.Event, allow_first_finish: asyncio.Event) -> None:
        super().__init__()
        self.first_call_started = first_call_started
        self.allow_first_finish = allow_first_finish
        self.invocations: list[list[str]] = []

    def resolve_binary(self) -> Optional[str]:
        return "/usr/bin/dummy"

    def build_command(self, binary: str, ctx: AdapterContext, profile: ExecutionProfile) -> list[str]:
        return [binary, "run", ctx.user_content]

    def build_resume_command(
        self,
        binary: str,
        ctx: AdapterContext,
        profile: ExecutionProfile,
        session_id: str,
        decision_payload: dict[str, Any],
    ) -> Optional[list[str]]:
        return [binary, "resume", session_id, str(decision_payload.get("comment") or "")]

    async def _run_process(
        self,
        argv: list[str],
        ctx: AdapterContext,
        profile: ExecutionProfile,
        state: StreamParseState,
    ) -> int:
        self.invocations.append(list(argv))
        if len(self.invocations) == 1:
            state.session_id = "sess-abc-123"
            ctx.session_id = "sess-abc-123"
            state.final_text_parts.append("第一阶段完成")
            self.first_call_started.set()
            await self.allow_first_finish.wait()
            return 0
        state.final_text_parts.append("已按补充要求续跑完成")
        return 0


@pytest.mark.asyncio
async def test_subprocess_cli_adapter_resumes_same_session_on_steering(tmp_path: Path) -> None:
    db = Database(tmp_path / "kiki.db")
    db.init_db()
    bus = EventBus()
    ws_mgr = WorkspaceManager(workspace_root=tmp_path / "ws", legacy_artifacts_dir=tmp_path / "art")
    ws_mgr.ensure_base_layout()
    tool_exec = WorkspaceToolExecutor(
        workspace_root=tmp_path / "ws",
        artifacts_dir=tmp_path / "art",
        db=db,
        event_bus=bus,
    )

    first_started = asyncio.Event()
    allow_first_finish = asyncio.Event()
    cli_adapter = _DummyCLIAdapter(first_started, allow_first_finish)
    registry = AdapterRegistry([cli_adapter])
    tm = TaskManager(
        db=db,
        llm=None,  # type: ignore[arg-type]
        event_bus=bus,
        tool_executor=tool_exec,
        adapter_registry=registry,
        workspace_manager=ws_mgr,
    )

    topic = db.create_topic(title="重构模块")
    msg1 = db.create_message(topic_id=topic["id"], role="user", content="重构 router.py")
    run = tm.enqueue_run(
        topic_id=topic["id"],
        trigger_message_id=msg1["id"],
        user_content=msg1["content"],
        agent_type="dummy_cli",
    )

    await asyncio.wait_for(first_started.wait(), timeout=3.0)
    msg2 = db.create_message(topic_id=topic["id"], role="user", content="记得保持向后兼容")
    tm.steer_run(run["id"], message_id=msg2["id"], content=msg2["content"])
    allow_first_finish.set()

    task = tm._tasks.get(run["id"])
    if task is not None:
        await asyncio.wait_for(task, timeout=5.0)

    assert len(cli_adapter.invocations) == 2
    assert cli_adapter.invocations[1][:3] == ["/usr/bin/dummy", "resume", "sess-abc-123"]
    assert "记得保持向后兼容" in cli_adapter.invocations[1][3]
    final_run = db.get_session_run(run["id"])
    assert final_run is not None
    assert final_run["status"] == "completed"


import httpx

from app.config import Settings
from app.main import create_app
from app.schemas import RouteDecision


class _APISteeringMockLLM:
    """Mock LLM supporting both routing and agent turns for /api/chat steering tests."""

    def __init__(self, turn1_started: asyncio.Event, allow_turn1_finish: asyncio.Event) -> None:
        self.turn1_started = turn1_started
        self.allow_turn1_finish = allow_turn1_finish
        self.received_agent_inputs: list[Any] = []
        self.received_active_runs: list[list[dict[str, Any]]] = []
        self.next_route_action: str = "new_run"
        self.last_used_model = "mock-gemini"

    async def route_message(
        self,
        content: str,
        active_topics: list[dict[str, Any]],
        recent_messages: Optional[list[dict[str, Any]]] = None,
        available_adapters: Optional[list[dict[str, Any]]] = None,
        forced_agent_type: Optional[str] = None,
        active_runs: Optional[list[dict[str, Any]]] = None,
    ) -> RouteDecision:
        self.received_active_runs.append(list(active_runs or []))
        if active_runs:
            run0 = active_runs[0]
            if self.next_route_action == "cancel":
                return RouteDecision(
                    path="fast",
                    topic_action="reuse",
                    topic_id=run0["topic_id"],
                    run_action="cancel",
                    target_run_id=run0["id"],
                    confidence=0.95,
                    reasoning="用户要求取消当前任务",
                )
            # Even if router returns new_run on the same topic, guardrail should steer it!
            return RouteDecision(
                path="heavy",
                topic_action="reuse",
                topic_id=run0["topic_id"],
                run_action=self.next_route_action,  # type: ignore[arg-type]
                target_run_id=run0["id"] if self.next_route_action == "steer" else None,
                confidence=0.9,
                reasoning="同话题补充说明",
            )
        return RouteDecision(
            path="heavy",
            topic_action="create",
            new_topic_title="登录页改造",
            run_action="new_run",
            confidence=0.92,
            reasoning="新建任务",
        )

    async def stream_fast_reply(self, **kwargs: Any):
        yield "好的，已帮你取消刚才的任务。"

    async def run_agent_turn(self, *, input_data: Any, **kwargs: Any) -> Any:
        self.received_agent_inputs.append(input_data)
        if len(self.received_agent_inputs) == 1:
            self.turn1_started.set()
            await self.allow_turn1_finish.wait()
            return SimpleNamespace(
                id="int_api_1",
                steps=[
                    SimpleNamespace(
                        type="function_call",
                        id="call_api_1",
                        name="list_files",
                        arguments={"path": "."},
                    )
                ],
                output_text="",
            )
        return SimpleNamespace(
            id=f"int_api_{len(self.received_agent_inputs)}",
            steps=[],
            output_text="已合并补充需求并完成。",
        )


def _make_test_settings(tmp_path: Path) -> Settings:
    return Settings(
        gemini_api_key="test-key",
        gemini_model="mock-gemini",
        database_path=tmp_path / "api.db",
        artifacts_dir=tmp_path / "artifacts",
        workspace_root=tmp_path / "ws",
        api_token="",
        auth_username="",
        auth_password_hash="",
        auth_users="",
        desktop_driver="mock",
    )


@pytest.mark.asyncio
async def test_chat_endpoint_auto_steers_same_topic_active_run(tmp_path: Path) -> None:
    cfg = _make_test_settings(tmp_path)
    db = Database(cfg.database_path)
    db.init_db()
    bus = EventBus()
    turn1_started = asyncio.Event()
    allow_turn1_finish = asyncio.Event()
    mock_llm = _APISteeringMockLLM(turn1_started, allow_turn1_finish)
    registry = AdapterRegistry([InternalAgentAdapter(default_model="mock-gemini", api_key_configured=True)])
    app = create_app(settings=cfg, db=db, llm=mock_llm, event_bus=bus, adapter_registry=registry)  # type: ignore[arg-type]

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        res1 = await client.post("/api/chat", json={"content": "帮我实现一个登录页面"})
        assert res1.status_code == 200
        body1 = res1.json()
        run1_id = body1["session_run"]["id"]
        topic_id = body1["topic"]["id"]

        await asyncio.wait_for(turn1_started.wait(), timeout=3.0)

        # Follow-up while run1 is executing (even when router says new_run, same-topic guardrail steers it)
        mock_llm.next_route_action = "new_run"
        res2 = await client.post("/api/chat", json={"content": "补充一句：支持手机号验证码登录"})
        assert res2.status_code == 200
        body2 = res2.json()
        assert body2["topic"]["id"] == topic_id
        assert body2["steered_run_id"] == run1_id
        assert body2["session_run"]["id"] == run1_id
        assert body2["user_message"]["metadata"]["steered_run_id"] == run1_id
        assert body2["user_message"]["metadata"].get("session_run_id") is None

        # Router should have received the active run in its context
        assert len(mock_llm.received_active_runs) == 2
        assert len(mock_llm.received_active_runs[1]) == 1
        assert mock_llm.received_active_runs[1][0]["id"] == run1_id

        # Only 1 SessionRun exists in DB
        assert len(db.list_session_runs(limit=10)) == 1

        allow_turn1_finish.set()
        task = app.state.task_manager._tasks.get(run1_id)
        if task is not None:
            await asyncio.wait_for(task, timeout=5.0)

        # Agent received the follow-up in turn 2
        assert len(mock_llm.received_agent_inputs) == 2
        turn2_items = mock_llm.received_agent_inputs[1]
        assert any(
            isinstance(it, dict) and it.get("type") == "text" and "手机号验证码登录" in it.get("text", "")
            for it in turn2_items
        )


@pytest.mark.asyncio
async def test_chat_endpoint_force_new_run_bypasses_steering(tmp_path: Path) -> None:
    cfg = _make_test_settings(tmp_path)
    db = Database(cfg.database_path)
    db.init_db()
    bus = EventBus()
    turn1_started = asyncio.Event()
    allow_turn1_finish = asyncio.Event()
    mock_llm = _APISteeringMockLLM(turn1_started, allow_turn1_finish)
    registry = AdapterRegistry([InternalAgentAdapter(default_model="mock-gemini", api_key_configured=True)])
    app = create_app(settings=cfg, db=db, llm=mock_llm, event_bus=bus, adapter_registry=registry)  # type: ignore[arg-type]

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        res1 = await client.post("/api/chat", json={"content": "帮我实现登录页"})
        assert res1.status_code == 200
        run1_id = res1.json()["session_run"]["id"]
        await asyncio.wait_for(turn1_started.wait(), timeout=3.0)

        res2 = await client.post(
            "/api/chat",
            json={"content": "同时并行帮我跑一下 lint 检查", "force_new_run": True},
        )
        assert res2.status_code == 200
        body2 = res2.json()
        assert body2["steered_run_id"] is None
        assert body2["session_run"]["id"] != run1_id
        assert len(db.list_session_runs(limit=10)) == 2

        allow_turn1_finish.set()
        await app.state.task_manager.shutdown()


@pytest.mark.asyncio
async def test_chat_endpoint_cancel_action_stops_active_run(tmp_path: Path) -> None:
    cfg = _make_test_settings(tmp_path)
    db = Database(cfg.database_path)
    db.init_db()
    bus = EventBus()
    turn1_started = asyncio.Event()
    allow_turn1_finish = asyncio.Event()
    mock_llm = _APISteeringMockLLM(turn1_started, allow_turn1_finish)
    registry = AdapterRegistry([InternalAgentAdapter(default_model="mock-gemini", api_key_configured=True)])
    app = create_app(settings=cfg, db=db, llm=mock_llm, event_bus=bus, adapter_registry=registry)  # type: ignore[arg-type]

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        res1 = await client.post("/api/chat", json={"content": "帮我分析全仓代码"})
        assert res1.status_code == 200
        run1_id = res1.json()["session_run"]["id"]
        await asyncio.wait_for(turn1_started.wait(), timeout=3.0)

        mock_llm.next_route_action = "cancel"
        res2 = await client.post("/api/chat", json={"content": "算了，先停一下"})
        assert res2.status_code == 200
        body2 = res2.json()
        assert body2["session_run"]["id"] == run1_id
        assert body2["session_run"]["status"] == "cancelled"
        assert body2["assistant_message"] is not None
        assert len(db.list_session_runs(limit=10)) == 1

        await app.state.fast_path.shutdown()
        await app.state.task_manager.shutdown()

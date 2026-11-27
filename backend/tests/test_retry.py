"""Tests for Heavy-Path transient rate-limit wait-and-retry and manual run retry."""

from __future__ import annotations

import asyncio
from pathlib import Path
import time
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from app.adapters import AdapterContext, InternalAgentAdapter
from app.adapters.internal_agent import _classify_retryable_llm_error
from app.config import Settings
from app.db import Database
from app.llm import KikiLLMClient, parse_retry_after_seconds
from app.main import create_app
from app.schemas import RouteDecision
from app.sse import EventBus
from app.tools import WorkspaceToolExecutor


def test_parse_retry_after_seconds_variants():
    assert parse_retry_after_seconds("429 RESOURCE_EXHAUSTED. Please retry in 43.25s.") == pytest.approx(43.25)
    assert parse_retry_after_seconds("{'retryDelay': '36s'}") == pytest.approx(36.0)
    assert parse_retry_after_seconds("Retry-After: 18") == pytest.approx(18.0)
    assert parse_retry_after_seconds("Please retry in 2 minutes") == pytest.approx(120.0)
    assert parse_retry_after_seconds("Please retry in 500ms") == pytest.approx(0.5)
    assert parse_retry_after_seconds("Unknown error without delay") is None


def test_classify_retryable_llm_error_filters_permanent_and_long_waits():
    # Standard Gemini RPM 429 -> retryable with parsed delay + buffer
    res = _classify_retryable_llm_error(
        RuntimeError("429 RESOURCE_EXHAUSTED: Quota exceeded. Please retry in 36s."),
        retry_index=0,
        max_wait_seconds=90.0,
    )
    assert res is not None
    wait_sec, reason = res
    assert wait_sec == pytest.approx(37.5)
    assert "限流" in reason

    # Daily quota / limit: 0 / auth error -> NOT retryable
    assert _classify_retryable_llm_error(RuntimeError("429 quota exceeded per day (RPD)"), 0) is None
    assert _classify_retryable_llm_error(RuntimeError("429 limit: 0 for this model"), 0) is None
    assert _classify_retryable_llm_error(RuntimeError("401 Unauthorized invalid api key"), 0) is None

    # Wait longer than cap -> NOT retryable
    assert _classify_retryable_llm_error(
        RuntimeError("429 RESOURCE_EXHAUSTED. Please retry in 180s."),
        0,
        max_wait_seconds=90.0,
    ) is None


def test_llm_cooldown_respects_parsed_retry_delay(tmp_path: Path):
    settings = Settings(
        gemini_api_key="test-key",
        gemini_model="gemini-3.8-flash",
        gemini_cooldown_seconds=120.0,
        database_path=tmp_path / "kiki.db",
        workspace_root=tmp_path / "ws",
        artifacts_dir=tmp_path / "ws" / "artifacts",
    )
    client = KikiLLMClient(settings)
    before = time.monotonic()
    client._mark_model_cooldown(
        "gemini-3.8-flash",
        RuntimeError("429 RESOURCE_EXHAUSTED. Please retry in 36s."),
    )
    rem = client._model_cooldowns["gemini-3.8-flash"] - before
    assert 37.0 <= rem <= 40.0


@pytest.mark.asyncio
async def test_internal_agent_waits_and_retries_mid_chain_on_429(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    db = Database(tmp_path / "test.db")
    db.init_db()
    bus = EventBus()
    topic = db.create_topic(title="小红书发布")
    msg = db.create_message(topic_id=topic["id"], role="user", content="帮我发一篇小红书")
    run = db.create_session_run(
        topic_id=topic["id"],
        trigger_message_id=msg["id"],
        agent_type="internal_agent",
        status="running",
    )
    tool_exec = WorkspaceToolExecutor(
        workspace_root=tmp_path,
        artifacts_dir=tmp_path / "artifacts",
        db=db,
        event_bus=bus,
    )

    calls: list[dict[str, Any]] = []

    class _FlakyChainLLM:
        last_used_model = "gemini-3.8-flash"

        async def run_agent_turn(self, **kwargs: Any) -> Any:
            calls.append(kwargs)
            if len(calls) == 1:
                # Turn 1: returns a tool call and establishes previous_interaction_id
                return SimpleNamespace(
                    id="int_chain_1",
                    status="completed",
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
            if len(calls) == 2:
                # Turn 2 first attempt: 429 rate limit mid-chain
                raise RuntimeError(
                    "429 RESOURCE_EXHAUSTED: You exceeded your current quota (15 RPM). Please retry in 36s."
                )
            # Turn 2 retry attempt: succeeds with the SAME previous_interaction_id!
            return SimpleNamespace(
                id="int_chain_2",
                status="completed",
                steps=[],
                output_text="已顺利完成小红书发布准备。",
            )

    slept: list[float] = []

    async def _fast_sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", _fast_sleep)

    ctx = AdapterContext(
        run_id=run["id"],
        topic_id=topic["id"],
        topic_title=topic["title"],
        user_content="帮我发一篇小红书",
        workspace_root=tmp_path,
        artifacts_dir=tmp_path / "artifacts",
        db=db,
        event_bus=bus,
        tool_executor=tool_exec,
        llm=_FlakyChainLLM(),  # type: ignore[arg-type]
    )

    adapter = InternalAgentAdapter()
    result = await adapter.execute(ctx)

    assert "顺利完成" in result.summary
    assert len(calls) == 3
    # Crucially, the retried turn preserved previous_interaction_id!
    assert calls[1]["previous_interaction_id"] == "int_chain_1"
    assert calls[2]["previous_interaction_id"] == "int_chain_1"
    assert sum(slept) == pytest.approx(37.5)

    run_row = db.get_session_run(run["id"])
    assert run_row is not None
    step_logs = run_row["step_logs"]
    wait_steps = [s for s in step_logs if s["action_type"] == "rate_limit_wait"]
    assert len(wait_steps) == 1
    assert wait_steps[0]["status"] == "success"


@pytest.mark.asyncio
async def test_internal_agent_rate_limit_wait_is_cancellable(tmp_path: Path):
    db = Database(tmp_path / "test.db")
    db.init_db()
    bus = EventBus()
    topic = db.create_topic(title="取消测试")
    msg = db.create_message(topic_id=topic["id"], role="user", content="测试取消")
    run = db.create_session_run(
        topic_id=topic["id"],
        trigger_message_id=msg["id"],
        agent_type="internal_agent",
        status="running",
    )
    tool_exec = WorkspaceToolExecutor(
        workspace_root=tmp_path,
        artifacts_dir=tmp_path / "artifacts",
        db=db,
        event_bus=bus,
    )

    class _Always429LLM:
        async def run_agent_turn(self, **kwargs: Any) -> Any:
            raise RuntimeError("429 RESOURCE_EXHAUSTED. Please retry in 30s.")

    ctx = AdapterContext(
        run_id=run["id"],
        topic_id=topic["id"],
        topic_title=topic["title"],
        user_content="测试取消",
        workspace_root=tmp_path,
        artifacts_dir=tmp_path / "artifacts",
        db=db,
        event_bus=bus,
        tool_executor=tool_exec,
        llm=_Always429LLM(),  # type: ignore[arg-type]
    )

    adapter = InternalAgentAdapter()
    task = asyncio.create_task(adapter.execute(ctx))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    run_row = db.get_session_run(run["id"])
    assert run_row is not None
    step_logs = run_row["step_logs"]
    wait_steps = [s for s in step_logs if s["action_type"] == "rate_limit_wait"]
    assert len(wait_steps) == 1
    assert wait_steps[0]["status"] == "failed"


@pytest.mark.asyncio
async def test_retry_run_api_endpoint(tmp_path: Path):
    class _RetryTestLLM:
        def __init__(self) -> None:
            self.attempts = 0

        async def route_message(self, content: str, active_topics: list[dict[str, Any]], **kwargs: Any) -> RouteDecision:
            return RouteDecision(
                path="heavy",
                topic_action="create",
                topic_id=None,
                new_topic_title="重试测试",
                confidence=0.95,
                reasoning="heavy",
            )

        async def run_agent_turn(self, **kwargs: Any) -> Any:
            self.attempts += 1
            if self.attempts == 1:
                raise RuntimeError("401 Unauthorized: simulated failure")
            return SimpleNamespace(
                id="int_ok",
                status="completed",
                steps=[],
                output_text="重试后执行成功！",
            )

    settings = Settings(
        gemini_api_key="test-key",
        gemini_model="gemini-3.8-flash",
        database_path=tmp_path / "kiki.db",
        workspace_root=tmp_path / "ws",
        artifacts_dir=tmp_path / "ws" / "artifacts",
    )
    db = Database(settings.database_path)
    db.init_db()
    app = create_app(settings=settings, db=db, llm=_RetryTestLLM())  # type: ignore[arg-type]
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/api/chat", json={"content": "跑一个后台任务"})
        assert resp.status_code == 200
        first_run_id = resp.json()["session_run"]["id"]

        # Wait for first run to fail
        for _ in range(40):
            r = await client.get(f"/api/runs/{first_run_id}")
            if r.json()["status"] == "failed":
                break
            await asyncio.sleep(0.05)
        assert r.json()["status"] == "failed"

        # Retry the failed run
        retry_resp = await client.post(f"/api/runs/{first_run_id}/retry")
        assert retry_resp.status_code == 200
        second_run = retry_resp.json()["run"]
        assert second_run["id"] != first_run_id
        assert second_run["trigger_message_id"] == r.json()["trigger_message_id"]

        # Wait for retried run to complete
        for _ in range(40):
            r2 = await client.get(f"/api/runs/{second_run['id']}")
            if r2.json()["status"] == "completed":
                break
            await asyncio.sleep(0.05)
        assert r2.json()["status"] == "completed"

        # Retrying a completed run should be rejected with 409
        conflict_resp = await client.post(f"/api/runs/{second_run['id']}/retry")
        assert conflict_resp.status_code == 409

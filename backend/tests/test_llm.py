"""Unit tests for KikiLLMClient multi-model fallback chain and per-model cooldowns."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.config import Settings, load_settings
from app.llm import KikiLLMClient


def _make_settings(
    tmp_path: Path,
    *,
    gemini_api_key: str = "test-key",
    gemini_model: str = "gemini-3.5-flash-lite",
    gemini_fallback_model: str = "gemini-3.1-flash-lite",
    gemini_fallback_models: tuple[str, ...] = (
        "gemini-3.1-flash-lite",
        "gemini-3.6-flash",
        "gemini-3.8-flash",
        "gemini-4-hydrogen",
    ),
    gemini_cooldown_seconds: float = 120.0,
    deepseek_api_key: str = "",
    deepseek_model: str = "deepseek-flash",
) -> Settings:
    return Settings(
        gemini_api_key=gemini_api_key,
        gemini_model=gemini_model,
        gemini_fallback_model=gemini_fallback_model,
        gemini_fallback_models=gemini_fallback_models,
        gemini_cooldown_seconds=gemini_cooldown_seconds,
        deepseek_api_key=deepseek_api_key,
        deepseek_model=deepseek_model,
        workspace_root=tmp_path,
        database_path=tmp_path / "kiki.db",
        artifacts_dir=tmp_path / "artifacts",
    )


class _StubInteractions:
    def __init__(self, handler: Any) -> None:
        self._handler = handler
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return await self._handler(kwargs)


def _attach_stub_client(llm: KikiLLMClient, handler: Any) -> _StubInteractions:
    interactions = _StubInteractions(handler)
    llm._client = SimpleNamespace(aio=SimpleNamespace(interactions=interactions))
    return interactions


@pytest.mark.asyncio
async def test_multi_model_failover_and_independent_cooldowns(tmp_path: Path) -> None:
    settings = _make_settings(tmp_path)
    llm = KikiLLMClient(settings)

    async def handler(kwargs: dict[str, Any]) -> Any:
        model = kwargs["model"]
        if model in {"gemini-3.5-flash-lite", "gemini-3.1-flash-lite"}:
            raise RuntimeError(
                f"Error code: 503 - {model} is currently experiencing high demand"
            )
        return SimpleNamespace(
            output_text=(
                '{"path":"fast","topic_action":"none","topic_id":null,'
                '"new_topic_title":null,"confidence":0.95,"reasoning":"ok"}'
            )
        )

    stub = _attach_stub_client(llm, handler)

    # First call: tries 3.5-flash-lite (503) -> 3.1-flash-lite (503) -> succeeds on 3.6-flash
    decision = await llm.route_message("你好", active_topics=[])
    assert decision.path == "fast"
    assert decision.confidence == 0.95
    assert [c["model"] for c in stub.calls] == [
        "gemini-3.5-flash-lite",
        "gemini-3.1-flash-lite",
        "gemini-3.6-flash",
    ]

    # Second call: both 503 models are cooling down, so it goes straight to 3.6-flash
    stub.calls.clear()
    decision2 = await llm.route_message("再聊一句", active_topics=[])
    assert decision2.confidence == 0.95
    assert [c["model"] for c in stub.calls] == ["gemini-3.6-flash"]


@pytest.mark.asyncio
async def test_all_models_cooling_down_orders_by_earliest_expiry(tmp_path: Path) -> None:
    settings = _make_settings(
        tmp_path,
        gemini_model="m1",
        gemini_fallback_model="m2",
        gemini_fallback_models=("m2", "m3"),
    )
    llm = KikiLLMClient(settings)
    llm._model_cooldowns = {
        "m1": 1e12 + 300.0,
        "m2": 1e12 + 50.0,
        "m3": 1e12 + 150.0,
    }
    assert llm._candidate_models() == ["m2", "m3", "m1"]


@pytest.mark.asyncio
async def test_tier_unavailable_gets_longer_cooldown_than_transient_503(tmp_path: Path) -> None:
    settings = _make_settings(
        tmp_path,
        gemini_model="m-503",
        gemini_fallback_model="m-free-zero",
        gemini_fallback_models=("m-free-zero", "m-ok"),
        gemini_cooldown_seconds=60.0,
    )
    llm = KikiLLMClient(settings)

    async def handler(kwargs: dict[str, Any]) -> Any:
        model = kwargs["model"]
        if model == "m-503":
            raise RuntimeError("Error code: 503 - high demand")
        if model == "m-free-zero":
            raise RuntimeError("Error code: 429 - limit: 0 input tokens per minute on Free Tier")
        return SimpleNamespace(
            output_text=(
                '{"path":"fast","topic_action":"none","topic_id":null,'
                '"new_topic_title":null,"confidence":0.9,"reasoning":"ok"}'
            )
        )

    _attach_stub_client(llm, handler)
    await llm.route_message("hi", active_topics=[])

    assert llm._model_cooldowns["m-free-zero"] - llm._model_cooldowns["m-503"] >= 500.0


@pytest.mark.asyncio
async def test_stream_fast_reply_fails_over_across_models(tmp_path: Path) -> None:
    settings = _make_settings(
        tmp_path,
        gemini_model="m1",
        gemini_fallback_model="m2",
        gemini_fallback_models=("m2", "m3"),
    )
    llm = KikiLLMClient(settings)

    async def _ok_stream():
        yield SimpleNamespace(
            event_type="step.delta",
            delta=SimpleNamespace(type="text", text="喵～"),
        )
        yield SimpleNamespace(
            event_type="step.delta",
            delta=SimpleNamespace(type="text", text="切到备用模型啦"),
        )

    async def handler(kwargs: dict[str, Any]) -> Any:
        if kwargs["model"] == "m1":
            raise RuntimeError("503 service_unavailable high demand")
        return _ok_stream()

    stub = _attach_stub_client(llm, handler)
    chunks = [
        chunk
        async for chunk in llm.stream_fast_reply("hi", history=[], topic_title="日常闲聊")
    ]
    assert "".join(chunks) == "喵～切到备用模型啦"
    assert [c["model"] for c in stub.calls] == ["m1", "m2"]


@pytest.mark.asyncio
async def test_run_agent_turn_pins_model_for_previous_interaction_id(tmp_path: Path) -> None:
    settings = _make_settings(
        tmp_path,
        gemini_model="m1",
        gemini_fallback_model="m2",
        gemini_fallback_models=("m2", "m3"),
    )
    llm = KikiLLMClient(settings)

    async def handler(kwargs: dict[str, Any]) -> Any:
        if kwargs["model"] == "m1":
            raise RuntimeError("503 high demand")
        return SimpleNamespace(id="int_fallback_1", steps=[], output_text="done")

    stub = _attach_stub_client(llm, handler)

    turn1 = await llm.run_agent_turn(
        input_data="task",
        tools=[],
        system_instruction="sys",
    )
    assert turn1.id == "int_fallback_1"

    # Even if m1's cooldown expires before turn 2, previous_interaction_id stays pinned to m2
    llm._model_cooldowns.clear()
    await llm.run_agent_turn(
        input_data="tool_result",
        tools=[],
        system_instruction="sys",
        previous_interaction_id="int_fallback_1",
    )
    assert [c["model"] for c in stub.calls] == ["m1", "m2", "m2"]


def test_load_settings_parses_fallback_models_and_legacy_single(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import app.config as config_mod

    monkeypatch.setattr(config_mod, "ENV_FILE", tmp_path / ".env.missing")
    monkeypatch.setenv("GEMINI_MODEL", "gemini-3.5-flash-lite")
    monkeypatch.setenv(
        "GEMINI_FALLBACK_MODELS",
        "gemini-3.6-flash, gemini-3.1-flash-lite, gemini-3.5-flash-lite, gemini-3.6-flash",
    )
    monkeypatch.delenv("GEMINI_FALLBACK_MODEL", raising=False)
    s = load_settings()
    assert s.resolved_gemini_models == [
        "gemini-3.5-flash-lite",
        "gemini-3.6-flash",
        "gemini-3.1-flash-lite",
    ]
    assert s.gemini_fallback_model == "gemini-3.6-flash"

    # Legacy single GEMINI_FALLBACK_MODEL still works when GEMINI_FALLBACK_MODELS is unset
    monkeypatch.delenv("GEMINI_FALLBACK_MODELS", raising=False)
    monkeypatch.setenv("GEMINI_FALLBACK_MODEL", "gemini-3.8-flash")
    s2 = load_settings()
    assert s2.resolved_gemini_models == ["gemini-3.5-flash-lite", "gemini-3.8-flash"]


@pytest.mark.asyncio
async def test_gemini_free_first_then_deepseek_paid_fallback_and_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _make_settings(
        tmp_path,
        gemini_model="gemini-3.5-flash-lite",
        gemini_fallback_model="gemini-3.1-flash-lite",
        gemini_fallback_models=("gemini-3.1-flash-lite",),
        deepseek_api_key="sk-deepseek-test",
        deepseek_model="deepseek-flash",
    )
    llm = KikiLLMClient(settings)

    gemini_up = False

    async def gemini_handler(kwargs: dict[str, Any]) -> Any:
        if not gemini_up:
            raise RuntimeError(f"503 high demand on {kwargs['model']}")
        return SimpleNamespace(
            output_text=(
                '{"path":"fast","topic_action":"none","topic_id":null,'
                '"new_topic_title":null,"confidence":0.91,"reasoning":"from-gemini"}'
            )
        )

    gemini_stub = _attach_stub_client(llm, gemini_handler)
    deepseek_calls: list[dict[str, Any]] = []

    async def fake_deepseek_post(payload: dict[str, Any], *, timeout: float = 60.0) -> dict[str, Any]:
        deepseek_calls.append(payload)
        return {
            "choices": [
                {
                    "message": {
                        "content": (
                            '{"path":"fast","topic_action":"none","topic_id":null,'
                            '"new_topic_title":null,"confidence":0.88,"reasoning":"from-deepseek"}'
                        )
                    }
                }
            ]
        }

    monkeypatch.setattr(llm, "_deepseek_post_chat", fake_deepseek_post)

    # 1) All Gemini models 503 -> falls back to paid deepseek-flash
    d1 = await llm.route_message("你好", active_topics=[])
    assert d1.reasoning == "from-deepseek"
    assert llm.last_used_provider == "deepseek"
    assert llm.last_used_model == "deepseek-flash"
    assert [c["model"] for c in gemini_stub.calls] == [
        "gemini-3.5-flash-lite",
        "gemini-3.1-flash-lite",
    ]
    assert len(deepseek_calls) == 1
    assert deepseek_calls[0]["model"] == "deepseek-flash"
    assert llm.status()["active_provider"] == "deepseek"

    # 2) While Gemini models are cooling down -> skips Gemini completely (0ms wasted)
    gemini_stub.calls.clear()
    d2 = await llm.route_message("第二句", active_topics=[])
    assert d2.reasoning == "from-deepseek"
    assert gemini_stub.calls == []
    assert len(deepseek_calls) == 2

    # 3) Cooldown expires & Gemini recovers -> automatically switches back to free Gemini
    llm._model_cooldowns.clear()
    gemini_up = True
    d3 = await llm.route_message("第三句", active_topics=[])
    assert d3.reasoning == "from-gemini"
    assert llm.last_used_provider == "gemini"
    assert llm.last_used_model == "gemini-3.5-flash-lite"
    assert len(deepseek_calls) == 2  # DeepSeek not called!


@pytest.mark.asyncio
async def test_stream_fast_reply_falls_back_to_deepseek(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _make_settings(
        tmp_path,
        gemini_model="gemini-3.5-flash-lite",
        gemini_fallback_model="",
        gemini_fallback_models=(),
        deepseek_api_key="sk-deepseek-test",
    )
    llm = KikiLLMClient(settings)

    async def gemini_handler(kwargs: dict[str, Any]) -> Any:
        raise RuntimeError("503 service_unavailable high demand")

    _attach_stub_client(llm, gemini_handler)

    async def fake_ds_stream(payload: dict[str, Any], *, timeout: float = 90.0):
        assert payload["model"] == "deepseek-flash"
        assert payload["stream"] is True
        yield "DeepSeek "
        yield "兜底回复"

    monkeypatch.setattr(llm, "_deepseek_stream_chat", fake_ds_stream)
    chunks = [
        chunk
        async for chunk in llm.stream_fast_reply("你好", history=[], topic_title="日常闲聊")
    ]
    assert "".join(chunks) == "DeepSeek 兜底回复"
    assert llm.last_used_provider == "deepseek"


@pytest.mark.asyncio
async def test_agent_turn_tool_calling_falls_back_to_deepseek(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _make_settings(
        tmp_path,
        gemini_model="gemini-3.5-flash-lite",
        gemini_fallback_model="",
        gemini_fallback_models=(),
        deepseek_api_key="sk-deepseek-test",
    )
    llm = KikiLLMClient(settings)

    async def gemini_handler(kwargs: dict[str, Any]) -> Any:
        raise RuntimeError("503 high demand")

    _attach_stub_client(llm, gemini_handler)

    responses = [
        {
            "choices": [
                {
                    "message": {
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "type": "function",
                                "function": {
                                    "name": "list_files",
                                    "arguments": '{"path": "."}',
                                },
                            }
                        ],
                    }
                }
            ]
        },
        {
            "choices": [
                {
                    "message": {
                        "content": "目录检查完成！",
                    }
                }
            ]
        },
    ]
    seen_payloads: list[dict[str, Any]] = []

    async def fake_ds_post(payload: dict[str, Any], *, timeout: float = 90.0) -> dict[str, Any]:
        seen_payloads.append(payload)
        return responses.pop(0)

    monkeypatch.setattr(llm, "_deepseek_post_chat", fake_ds_post)

    tools = [
        {
            "type": "function",
            "name": "list_files",
            "description": "List files",
            "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
        }
    ]
    turn1 = await llm.run_agent_turn(
        input_data="看看根目录",
        tools=tools,
        system_instruction="sys",
    )
    assert turn1.id.startswith("ds_int_")
    assert len(turn1.steps) == 1
    assert turn1.steps[0].name == "list_files"
    assert turn1.steps[0].arguments == {"path": "."}

    turn2 = await llm.run_agent_turn(
        input_data=[{"type": "function_result", "call_id": "call_1", "result": "README.md"}],
        tools=tools,
        system_instruction="sys",
        previous_interaction_id=turn1.id,
    )
    assert turn2.output_text == "目录检查完成！"
    assert any(m.get("role") == "tool" and m.get("tool_call_id") == "call_1" for m in seen_payloads[1]["messages"])


@pytest.mark.asyncio
async def test_probe_gemini_free_tier_reports_status(tmp_path: Path) -> None:
    settings = _make_settings(
        tmp_path,
        gemini_model="gemini-3.5-flash-lite",
        gemini_fallback_model="gemini-3.1-flash-lite",
        gemini_fallback_models=("gemini-3.1-flash-lite",),
        deepseek_api_key="sk-deepseek-test",
    )
    llm = KikiLLMClient(settings)

    async def fail_all(kwargs: dict[str, Any]) -> Any:
        raise RuntimeError("503 high demand")

    _attach_stub_client(llm, fail_all)
    report = await llm.probe_gemini_free_tier()
    assert report["gemini_available"] is False
    assert report["active_provider"] == "deepseek"
    assert report["active_model"] == "deepseek-flash"
    assert len(report["probe_results"]) == 2


def test_recent_messages_with_topic_includes_failed_session_run_in_context(tmp_path: Path) -> None:
    from app.db import Database

    db = Database(tmp_path / "test.db")
    db.init_db()
    topic = db.create_topic(title="深圳实时天气查询")
    user_msg = db.create_message(
        topic_id=topic["id"],
        role="user",
        content="再查一下深圳现在的天气",
        metadata={"path": "heavy"},
    )
    run = db.create_session_run(
        topic_id=topic["id"],
        trigger_message_id=user_msg["id"],
        agent_type="gemini_agent",
        status="queued",
    )
    db.update_message(user_msg["id"], metadata={"session_run_id": run["id"]})
    db.update_session_run(
        run["id"],
        status="failed",
        current_step="任务执行失败",
        error_message="503 Service Unavailable",
    )

    recent = db.get_recent_messages_with_topic(limit=10)
    assert len(recent) == 1
    assert recent[0]["topic_title"] == "深圳实时天气查询"
    assert recent[0]["session_run"]["status"] == "failed"

    formatted = KikiLLMClient._format_context_lines(recent)
    assert "(#深圳实时天气查询)" in formatted
    assert "再查一下深圳现在的天气" in formatted
    assert "[后台任务执行失败(gemini_agent): 503 Service Unavailable]" in formatted

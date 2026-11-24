"""Unit and integration tests for all 10 Kiki Heavy-Path Agent Adapters."""

from __future__ import annotations

import asyncio
import json
import os
import stat
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.adapters import (
    AdapterContext,
    AntigravityAdapter,
    ClaudeCodeAdapter,
    CodexAdapter,
    DeepSeekHarnessAdapter,
    DoubaoWorkAdapter,
    GeminiAgentAdapter,
    KCodeAdapter,
    QoderAdapter,
    StreamParseState,
    WorkBuddyAdapter,
    ZCodeAdapter,
    create_default_registry,
    estimate_thinking_level,
)
from app.config import Settings
from app.db import Database
from app.main import create_app
from app.schemas import RouteDecision
from app.sse import EventBus
from app.tools import WorkspaceToolExecutor

ALL_ADAPTER_IDS = [
    "gemini_agent",
    "codex",
    "claude_code",
    "antigravity",
    "deepseek_harness",
    "zcode",
    "kcode",
    "qoder",
    "workbuddy",
    "doubao_work",
]


class _FakeLLM:
    def __init__(self) -> None:
        self.next_decision: RouteDecision | None = None

    async def route_message(self, content: str, active_topics: list[dict[str, Any]]) -> RouteDecision:
        if self.next_decision is not None:
            return self.next_decision
        return RouteDecision(
            path="heavy",
            topic_action="create",
            topic_id=None,
            new_topic_title="重型任务测试",
            confidence=0.95,
            reasoning="测试路由",
        )

    async def stream_fast_reply(self, content: str, history: list[dict[str, Any]], topic_title: str):
        yield "ok"

    async def run_agent_turn(self, **kwargs: Any) -> Any:
        return SimpleNamespace(id="int_0", status="completed", steps=[], output_text="完成")


def _make_context(tmp_path: Path, prompt: str = "分析并重构后端模块") -> AdapterContext:
    db = Database(tmp_path / "test.db")
    db.init_db()
    bus = EventBus()
    topic = db.create_topic(title="测试话题")
    msg = db.create_message(topic_id=topic["id"], role="user", content=prompt)
    run = db.create_session_run(
        topic_id=topic["id"],
        trigger_message_id=msg["id"],
        agent_type="codex",
        status="running",
    )
    tool_exec = WorkspaceToolExecutor(
        workspace_root=tmp_path,
        artifacts_dir=tmp_path / "artifacts",
        db=db,
        event_bus=bus,
    )
    return AdapterContext(
        run_id=run["id"],
        topic_id=topic["id"],
        topic_title=topic["title"],
        user_content=prompt,
        workspace_root=tmp_path,
        artifacts_dir=tmp_path / "artifacts",
        db=db,
        event_bus=bus,
        tool_executor=tool_exec,
        llm=_FakeLLM(),  # type: ignore[arg-type]
    )


def test_registry_contains_all_ten_adapters_and_aliases():
    registry = create_default_registry()
    assert registry.list_types() == ALL_ADAPTER_IDS

    # Alias normalization checks (including user-spoken variants)
    assert registry.resolve_type("Cloud Code") == "claude_code"
    assert registry.resolve_type("claude") == "claude_code"
    assert registry.resolve_type("Anti-Gravity") == "antigravity"
    assert registry.resolve_type("agy") == "antigravity"
    assert registry.resolve_type("DeepSeek Harness") == "deepseek_harness"
    assert registry.resolve_type("dsh") == "deepseek_harness"
    assert registry.resolve_type("z.ai") == "zcode"
    assert registry.resolve_type("kimi") == "kcode"
    assert registry.resolve_type("qodercli") == "qoder"
    assert registry.resolve_type("codebuddy") == "workbuddy"
    assert registry.resolve_type("豆包工作") == "doubao_work"
    assert registry.resolve_type("ark") == "doubao_work"

    with pytest.raises(KeyError):
        registry.resolve_type("nonexistent_agent_xyz")


def test_auto_thinking_level_and_model_selection():
    low_level, _ = estimate_thinking_level("列出 README 文件")
    assert low_level == "low"

    med_level, _ = estimate_thinking_level("帮我给登录接口加一个单元测试")
    assert med_level == "medium"

    high_level, _ = estimate_thinking_level("请对整个项目做一次全面安全审计并给出架构重构方案")
    assert high_level == "high"

    registry = create_default_registry()
    for adapter_id in ALL_ADAPTER_IDS:
        adapter = registry.get(adapter_id)
        p_low = adapter.select_profile("列出 README")
        p_med = adapter.select_profile("实现用户分页查询接口")
        p_high = adapter.select_profile("全仓架构重构与并发死锁根因分析")
        assert p_low.thinking_level == "low"
        assert p_med.thinking_level == "medium"
        assert p_high.thinking_level == "high"
        assert p_low.model
        assert p_med.model
        assert p_high.model

        # Explicit override must win
        p_override = adapter.select_profile(
            "列出 README",
            model_override="custom-model-x",
            thinking_override="high",
        )
        assert p_override.model == "custom-model-x"
        assert p_override.thinking_level == "high"


def test_command_and_resume_building_for_all_cli_adapters(tmp_path: Path):
    ctx = _make_context(tmp_path, "修复 auth.py 中的竞态条件")
    decision = {"decision": "approve", "approved": True, "comment": "可以继续"}

    adapters = [
        CodexAdapter(),
        ClaudeCodeAdapter(),
        AntigravityAdapter(),
        DeepSeekHarnessAdapter(),
        ZCodeAdapter(),
        KCodeAdapter(),
        QoderAdapter(),
        WorkBuddyAdapter(),
        DoubaoWorkAdapter(),
    ]

    for adapter in adapters:
        profile = adapter.select_profile(ctx.user_content)
        bin_name = adapter.binary_candidates[0]
        cmd = adapter.build_command(bin_name, ctx, profile)
        assert cmd[0] == bin_name
        assert any(ctx.user_content in arg for arg in cmd)

        resume_cmd = adapter.build_resume_command(bin_name, ctx, profile, "sess_123", decision)
        assert resume_cmd is not None
        assert "sess_123" in resume_cmd


def test_stream_json_parsing_for_codex_claude_and_chinese_adapters(tmp_path: Path):
    ctx = _make_context(tmp_path, "检查项目")

    # 1. Codex JSONL events
    codex = CodexAdapter()
    st_codex = StreamParseState()
    codex.parse_stdout_line(json.dumps({"type": "thread.started", "thread_id": "th_01"}), st_codex, ctx)
    codex.parse_stdout_line(
        json.dumps({"type": "item.started", "item": {"type": "command_execution", "command": "ls"}}),
        st_codex,
        ctx,
    )
    codex.parse_stdout_line(
        json.dumps(
            {
                "type": "item.completed",
                "item": {"type": "agent_message", "text": "Codex 巡检完成，一切正常。"},
            }
        ),
        st_codex,
        ctx,
    )
    assert st_codex.session_id == "th_01"
    assert "Codex 巡检完成" in st_codex.best_summary()

    # 2. Claude Code stream-json events
    claude = ClaudeCodeAdapter()
    st_claude = StreamParseState()
    claude.parse_stdout_line(
        json.dumps({"type": "system", "subtype": "init", "session_id": "cl_99"}),
        st_claude,
        ctx,
    )
    claude.parse_stdout_line(
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "tool_use", "name": "Read", "input": {"file_path": "README.md"}}
                    ]
                },
            }
        ),
        st_claude,
        ctx,
    )
    claude.parse_stdout_line(
        json.dumps({"type": "result", "session_id": "cl_99", "result": "Claude Code 已完成修复。"}),
        st_claude,
        ctx,
    )
    assert st_claude.session_id == "cl_99"
    assert "Claude Code 已完成修复" in st_claude.best_summary()

    # 3. ZCode single JSON object summary
    zcode = ZCodeAdapter()
    st_zcode = StreamParseState()
    zcode.parse_stdout_line(
        json.dumps({"session_id": "zc_7", "response": "ZCode 已生成重构方案。"}),
        st_zcode,
        ctx,
    )
    assert st_zcode.session_id == "zc_7"
    assert "ZCode 已生成重构方案" in st_zcode.best_summary()

    # 4. KCode / Qoder / WorkBuddy / DeepSeek Harness / Antigravity generic stream-json
    for adapter in (
        AntigravityAdapter(),
        DeepSeekHarnessAdapter(),
        KCodeAdapter(),
        QoderAdapter(),
        WorkBuddyAdapter(),
        DoubaoWorkAdapter(),
    ):
        st = StreamParseState()
        adapter.parse_stdout_line(
            json.dumps({"type": "result", "session_id": f"s_{adapter.agent_type}", "result": f"{adapter.agent_type} ok"}),
            st,
            ctx,
        )
        assert st.session_id == f"s_{adapter.agent_type}"
        assert f"{adapter.agent_type} ok" in st.best_summary()


@pytest.mark.asyncio
async def test_api_adapters_endpoint_and_end_to_end_fake_cli_subprocess(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    # Create a tiny executable fake Claude CLI script that emits stream-json
    fake_cli = tmp_path / "fake_claude.py"
    fake_cli.write_text(
        f"""#!{sys.executable}
import json, sys
print(json.dumps({{"type": "system", "subtype": "init", "session_id": "sess_e2e_1"}}), flush=True)
print(json.dumps({{"type": "assistant", "message": {{"content": [{{"type": "tool_use", "name": "Bash", "input": {{"command": "git status"}}}}]}}}}), flush=True)
print(json.dumps({{"type": "user", "message": {{"content": [{{"type": "tool_result", "is_error": False}}]}}}}), flush=True)
print(json.dumps({{"type": "result", "session_id": "sess_e2e_1", "result": "Cloud Code E2E 执行成功，所有检查通过。"}}), flush=True)
""",
        encoding="utf-8",
    )
    fake_cli.chmod(fake_cli.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("KIKI_CLAUDE_BIN", str(fake_cli))

    db_path = tmp_path / "kiki_e2e.db"
    settings = Settings(
        gemini_api_key="test-key",
        gemini_model="gemini-3.5-flash-lite",
        workspace_root=tmp_path,
        database_path=db_path,
        artifacts_dir=tmp_path / "artifacts",
    )
    db = Database(db_path)
    db.init_db()
    bus = EventBus()
    app = create_app(settings=settings, db=db, llm=_FakeLLM(), event_bus=bus)  # type: ignore[arg-type]

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        # 1. GET /api/adapters lists all 10 adapters
        res_adapters = await client.get("/api/adapters")
        assert res_adapters.status_code == 200
        items = res_adapters.json()
        assert [i["id"] for i in items] == ALL_ADAPTER_IDS
        claude_item = next(i for i in items if i["id"] == "claude_code")
        assert claude_item["available"] is True

        # 2. POST /api/chat with alias "Cloud Code" triggers claude_code adapter end-to-end
        res_chat = await client.post(
            "/api/chat",
            json={
                "content": "帮我检查仓库状态并重构配置模块",
                "force_path": "heavy",
                "agent_type": "Cloud Code",
            },
        )
        assert res_chat.status_code == 200
        run_id = res_chat.json()["session_run"]["id"]
        assert res_chat.json()["session_run"]["agent_type"] == "claude_code"

        # Poll until completed
        for _ in range(60):
            r = (await client.get(f"/api/runs/{run_id}")).json()
            if r["status"] in ("completed", "failed"):
                break
            await asyncio.sleep(0.05)

        assert r["status"] == "completed", f"Unexpected run failure: {r.get('error_message')}"
        assert len(r["step_logs"]) >= 2

        # Unknown adapter returns 400
        bad_res = await client.post(
            "/api/chat",
            json={"content": "hello", "force_path": "heavy", "agent_type": "unknown_xyz"},
        )
        assert bad_res.status_code == 400


@pytest.mark.asyncio
async def test_doubao_work_hybrid_ark_api_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.delenv("KIKI_DOUBAO_WORK_BIN", raising=False)
    monkeypatch.setenv("ARK_API_KEY", "test-ark-secret")
    monkeypatch.setenv("ARK_ENDPOINT_ID", "ep-test-doubao")

    adapter = DoubaoWorkAdapter()
    ctx = _make_context(tmp_path, "帮我整理一份架构优化建议")

    fake_response = MagicMock()
    fake_response.status_code = 200
    fake_response.raise_for_status = MagicMock()
    fake_response.json.return_value = {
        "id": "chatcmpl_ark_01",
        "choices": [
            {"message": {"role": "assistant", "content": "豆包工作（火山方舟）已完成架构优化建议整理。"}}
        ],
    }

    fake_client = MagicMock()
    fake_client.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client.__aexit__ = AsyncMock(return_value=None)
    fake_client.post = AsyncMock(return_value=fake_response)

    with patch.object(adapter, "resolve_binary", return_value=None), patch(
        "httpx.AsyncClient", return_value=fake_client
    ):
        info = adapter.probe()
        assert info.available is True
        assert info.binary_path == "ark-http-api"

        result = await adapter.execute(ctx)
        assert "豆包工作" in result.summary
        assert result.session_id == "chatcmpl_ark_01"


@pytest.mark.asyncio
async def test_gemini_agent_includes_topic_history_when_present(tmp_path: Path):
    adapter = GeminiAgentAdapter()
    ctx = _make_context(tmp_path, "再试一次")
    ctx.topic_title = "深圳实时天气查询"
    ctx.history = [
        {
            "role": "user",
            "content": "再查一下深圳现在的天气",
            "session_run": {
                "status": "failed",
                "agent_type": "gemini_agent",
                "error_message": "503 high demand",
            },
        }
    ]

    captured: list[dict[str, Any]] = []

    async def fake_run_agent_turn(**kwargs: Any) -> Any:
        captured.append(kwargs)
        return MagicMock(id="int-1", steps=[], output_text="深圳当前多云 17°C。")

    ctx.llm = MagicMock()
    ctx.llm.run_agent_turn = AsyncMock(side_effect=fake_run_agent_turn)

    result = await adapter.execute(ctx)
    assert result.summary == "深圳当前多云 17°C。"
    assert len(captured) == 1
    sent_input = captured[0]["input_data"]
    assert "当前话题：深圳实时天气查询" in sent_input
    assert "再查一下深圳现在的天气" in sent_input
    assert "[后台任务执行失败(gemini_agent): 503 high demand]" in sent_input
    assert "用户当前任务：再试一次" in sent_input

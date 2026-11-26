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


def test_codex_probe_uses_local_config_model(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(CodexAdapter, "_read_local_config_model", staticmethod(lambda: "gpt-6.1-terra"))
    codex = CodexAdapter()
    info = codex.probe()
    assert info.default_model == "gpt-6.1-terra"
    profile = codex.select_profile("帮我生成一张猫猫图")
    assert profile.model == "gpt-6.1-terra"


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


@pytest.mark.asyncio
async def test_gemini_agent_probe_model_override_and_max_steps_synthesis(tmp_path: Path):
    assert GeminiAgentAdapter(api_key_configured=False).probe().available is False
    assert GeminiAgentAdapter(api_key_configured=True).probe().available is True

    adapter = GeminiAgentAdapter()
    ctx = _make_context(tmp_path, "循环查目录")
    ctx.max_steps = 1
    ctx.model_override = "gemini-3.8-flash"

    calls: list[dict[str, Any]] = []

    async def fake_turn(**kwargs: Any) -> Any:
        calls.append(kwargs)
        if kwargs.get("tools"):
            return SimpleNamespace(
                id="int-loop",
                steps=[
                    SimpleNamespace(
                        type="function_call",
                        id="fc-1",
                        name="list_files",
                        arguments={"path": "."},
                    )
                ],
                output_text="",
            )
        return SimpleNamespace(id="int-synth", steps=[], output_text="已达步数上限，当前已完成根目录扫描。")

    ctx.llm = MagicMock()
    ctx.llm.last_used_model = "gemini-3.8-flash"
    ctx.llm.run_agent_turn = AsyncMock(side_effect=fake_turn)

    result = await adapter.execute(ctx)
    assert len(calls) == 2
    assert calls[0]["model"] == "gemini-3.8-flash"
    assert calls[1]["tools"] == []
    assert result.model_used == "gemini-3.8-flash"
    assert result.metadata["max_steps_reached"] is True
    assert "已完成根目录扫描" in result.summary


@pytest.mark.asyncio
async def test_cli_adapter_hitl_rejection_cancels_instead_of_completing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    fake_cli = tmp_path / "fake_qoder.py"
    fake_cli.write_text(
        f"""#!{sys.executable}
import json
print(json.dumps({{
    "type": "result",
    "session_id": "q_sess_1",
    "permission_denials": [{{"tool_name": "Bash"}}],
    "result": "等待授权执行 Bash"
}}), flush=True)
""",
        encoding="utf-8",
    )
    fake_cli.chmod(fake_cli.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("KIKI_QODER_BIN", str(fake_cli))

    adapter = QoderAdapter()
    ctx = _make_context(tmp_path, "清理临时文件")

    async def reject_hitl(_prompt: str, _opts: list[dict[str, Any]]) -> dict[str, Any]:
        return {"decision": "reject", "approved": False}

    ctx._hitl_handler = reject_hitl
    with pytest.raises(asyncio.CancelledError):
        await adapter.execute(ctx)


def test_cli_adapters_include_topic_history_and_run_status_in_prompt(tmp_path: Path):
    ctx = _make_context(tmp_path, "嗯，算了，那就还是用 Codex 生成吧")
    ctx.topic_title = "生成猫猫 lulu 图片"
    ctx.topic_summary = "围绕生成名为 lulu 的猫猫图片展开的对话。"
    ctx.history = [
        {
            "role": "user",
            "content": "调用 Codex 帮我生成一张猫猫的图吧，名字叫 lulu",
            "session_run": {"agent_type": "codex", "status": "cancelled", "current_step": "任务已取消"},
        },
        {
            "role": "assistant",
            "content": "喵呜，豆包工作还没准备好。",
        },
    ]

    adapters = [
        CodexAdapter(),
        ClaudeCodeAdapter(),
        AntigravityAdapter(),
        DeepSeekHarnessAdapter(),
        ZCodeAdapter(),
        KCodeAdapter(),
        QoderAdapter(),
        WorkBuddyAdapter(),
    ]
    for adapter in adapters:
        profile = adapter.select_profile(ctx.user_content)
        cmd = adapter.build_command(adapter.binary_candidates[0], ctx, profile)
        joined = "\n".join(cmd)
        assert "生成猫猫 lulu 图片" in joined
        assert "围绕生成名为 lulu 的猫猫图片展开的对话" in joined
        assert "名字叫 lulu" in joined
        assert "后台任务已取消(codex)" in joined
        assert ctx.user_content in joined


@pytest.mark.asyncio
async def test_codex_interim_messages_and_rollout_image_artifact_extraction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import base64

    codex_home = tmp_path / ".codex"
    sess_dir = codex_home / "sessions" / "2026" / "11" / "25"
    sess_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    # Minimal valid PNG bytes (>= 64 bytes)
    png_bytes = b"\x89PNG\r\n\x1a\n" + b"\x00" * 80
    b64_png = base64.b64encode(png_bytes).decode("ascii")
    rollout = sess_dir / "rollout-2026-11-25T15-00-00-th_img_01.jsonl"
    rollout.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "type": "event_msg",
                        "payload": {
                            "type": "item_completed",
                            "item": {
                                "type": "Extension",
                                "kind": "image_gen.generation",
                                "revisedPrompt": "Cute cat Lulu portrait",
                                "result": b64_png,
                            },
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "custom_tool_call_output",
                            "output": [
                                {
                                    "type": "input_image",
                                    "image_url": f"data:image/png;base64,{b64_png}",
                                }
                            ],
                        },
                    }
                ),
            ]
        ),
        encoding="utf-8",
    )

    ctx = _make_context(tmp_path, "再试一次")
    emitted: list[str] = []
    ctx._emit_interim_message = lambda text: (emitted.append(text), {"id": f"m-{len(emitted)}"})[1]

    codex = CodexAdapter()
    st = StreamParseState()
    codex.parse_stdout_line(
        json.dumps({"type": "thread.started", "thread_id": "th_img_01"}), st, ctx
    )
    codex.parse_stdout_line(
        json.dumps(
            {
                "type": "item.completed",
                "item": {"id": "item_0", "type": "agent_message", "text": "我再试一次生成 Lulu 的图片。"},
            }
        ),
        st,
        ctx,
    )
    codex.parse_stdout_line(
        json.dumps(
            {
                "type": "item.completed",
                "item": {"id": "item_2", "type": "agent_message", "text": "这次生成成功啦！这就是猫猫 Lulu 🐱"},
            }
        ),
        st,
        ctx,
    )

    assert emitted == ["我再试一次生成 Lulu 的图片。", "这次生成成功啦！这就是猫猫 Lulu 🐱"]
    assert st.best_summary() == "这次生成成功啦！这就是猫猫 Lulu 🐱"

    await codex.after_process_success(ctx, st)
    run = ctx.db.get_session_run(ctx.run_id)
    assert run is not None
    assert len(run["artifacts"]) == 1
    art = run["artifacts"][0]
    assert art["file_type"] == "image"
    assert art["file_path"].endswith("codex_image_1.png")
    assert (tmp_path / art["file_path"]).read_bytes() == png_bytes


def test_format_human_failure_reply_prioritizes_missing_cli_over_login_hint():
    from app.task_manager import _format_human_failure_reply

    msg = _format_human_failure_reply(
        "Antigravity (agy)",
        "未检测到 Antigravity (agy) CLI（查找命令: agy, antigravity）。请先安装：安装 Google Antigravity CLI (agy) 并执行 agy auth login",
    )
    assert "还没准备好" in msg
    assert "未检测到 Antigravity (agy) CLI" in msg
    assert "鉴权失败" not in msg


@pytest.mark.asyncio
async def test_antigravity_step_update_result_dict_and_image_artifact_extraction(
    tmp_path: Path,
):
    img_file = tmp_path / "lulu_portrait_123.jpg"
    jpg_bytes = b"\xff\xd8\xff\xe0" + b"\x00" * 80
    img_file.write_bytes(jpg_bytes)

    ctx = _make_context(tmp_path, "这回再试试，应该有 agy 了")
    emitted: list[str] = []
    ctx._emit_interim_message = lambda text: (emitted.append(text), {"id": f"m-{len(emitted)}"})[1]

    agy = AntigravityAdapter()
    st = StreamParseState()

    agy.parse_stdout_line(
        json.dumps({"event": "init", "conversation_id": "conv_agy_1"}), st, ctx
    )
    agy.parse_stdout_line(
        json.dumps(
            {
                "event": "step_update",
                "step_update": {
                    "conversation_id": "conv_agy_1",
                    "step_index": 2,
                    "state": "ACTIVE",
                    "step_type": "subagent",
                    "tool_name": "invoke_subagent",
                    "subagent_info": {
                        "subagents": [{"type_name": "image-generator", "role": "Cat Image Generator"}]
                    },
                },
            }
        ),
        st,
        ctx,
    )
    agy.parse_stdout_line(
        json.dumps(
            {
                "event": "step_update",
                "step_update": {
                    "conversation_id": "conv_agy_1",
                    "step_index": 2,
                    "state": "DONE",
                    "step_type": "subagent",
                    "tool_name": "invoke_subagent",
                },
            }
        ),
        st,
        ctx,
    )
    agy.parse_stdout_line(
        json.dumps(
            {
                "event": "step_update",
                "step_update": {
                    "conversation_id": "conv_agy_1",
                    "step_index": 3,
                    "state": "ACTIVE",
                    "step_type": "agent_response",
                    "text_delta": "正在为你生成猫猫 Lulu 的图片，请稍候~",
                },
            }
        ),
        st,
        ctx,
    )
    agy.parse_stdout_line(
        json.dumps(
            {
                "event": "step_update",
                "step_update": {
                    "conversation_id": "conv_agy_1",
                    "step_index": 3,
                    "state": "DONE",
                    "step_type": "agent_response",
                    "text_delta": "\n",
                },
            }
        ),
        st,
        ctx,
    )
    final_raw = (
        f"Antigravity 帮你生成好猫猫 Lulu 的图片啦！🐱\n\n"
        f"图片已保存至：[lulu_portrait_123.jpg](file://{img_file})\n\n"
        f"![Lulu]({img_file})"
    )
    agy.parse_stdout_line(
        json.dumps(
            {
                "event": "step_update",
                "step_update": {
                    "conversation_id": "conv_agy_1",
                    "step_index": 5,
                    "state": "DONE",
                    "step_type": "agent_response",
                    "text_delta": final_raw,
                },
            }
        ),
        st,
        ctx,
    )
    agy.parse_stdout_line(
        json.dumps(
            {
                "event": "result",
                "result": {
                    "conversation_id": "conv_agy_1",
                    "status": "SUCCESS",
                    "response": f"正在为你生成猫猫 Lulu 的图片，请稍候~\n{final_raw}",
                },
            }
        ),
        st,
        ctx,
    )

    assert st.session_id == "conv_agy_1"
    assert len(emitted) == 2
    assert emitted[0] == "正在为你生成猫猫 Lulu 的图片，请稍候~"
    assert emitted[1] == "Antigravity 帮你生成好猫猫 Lulu 的图片啦！🐱"
    assert st.best_summary() == "Antigravity 帮你生成好猫猫 Lulu 的图片啦！🐱"
    assert '{"event"' not in st.best_summary()

    await agy.after_process_success(ctx, st)
    run = ctx.db.get_session_run(ctx.run_id)
    assert run is not None
    assert len(run["artifacts"]) == 1
    assert run["artifacts"][0]["file_type"] == "image"
    assert run["artifacts"][0]["file_path"].endswith("lulu_portrait_123.jpg")





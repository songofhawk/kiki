"""Tests for Kiki workspace directory layout, per-topic subdirs, mounts, and agent passthrough."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any, Optional

import httpx
import pytest

from app.adapters import (
    AdapterContext,
    AdapterInfo,
    AdapterRegistry,
    AdapterResult,
    ClaudeCodeAdapter,
    CodexAdapter,
    ExecutionProfile,
    HeavyTaskAdapter,
)
from app.config import Settings
from app.db import Database
from app.main import create_app
from app.schemas import RouteDecision
from app.sse import EventBus
from app.tools import WorkspaceToolExecutor
from app.workspace import WorkspaceManager


class _FakeLLM:
    async def route_message(self, content: str, active_topics: list[dict[str, Any]], recent_messages=None) -> RouteDecision:
        return RouteDecision(
            path="heavy",
            topic_action="create",
            topic_id=None,
            new_topic_title="工作空间测试话题",
            confidence=0.95,
            reasoning="测试重型任务路由",
        )


class _RecordingAdapter(HeavyTaskAdapter):
    agent_type = "recording_agent"
    display_name = "Recording Agent"
    vendor = "Test"
    description = "Captures AdapterContext workspace fields"
    category = "builtin"

    def __init__(self) -> None:
        self.captured: Optional[AdapterContext] = None

    def probe(self) -> AdapterInfo:
        return AdapterInfo(
            id=self.agent_type,
            name=self.display_name,
            vendor=self.vendor,
            description=self.description,
            category="builtin",
            available=True,
            binary_path="in-process",
            install_hint="",
            default_model="test-model",
            models={"low": "test-model", "medium": "test-model", "high": "test-model"},
        )

    async def execute(self, ctx: AdapterContext) -> AdapterResult:
        self.captured = ctx
        ctx.register_artifact(
            filename="result.md",
            content="# Workspace Artifact\nOK",
            file_type="markdown",
            description="测试话题产物",
        )
        return AdapterResult(summary="已完成目录协同测试。")


@pytest.mark.asyncio
async def test_workspace_layout_mounts_and_api(tmp_path: Path) -> None:
    ws_root = tmp_path / "kiki-ws"
    custom_global = tmp_path / "shared-docs"
    custom_topic_repo = tmp_path / "my-project"
    custom_global.mkdir()
    custom_topic_repo.mkdir()

    cfg = Settings(
        gemini_api_key="test-key",
        gemini_model="gemini-3.8-flash",
        workspace_root=ws_root,
        database_path=tmp_path / "kiki.db",
        artifacts_dir=ws_root / "artifacts",
        default_heavy_agent="recording_agent",
    )
    recording = _RecordingAdapter()
    registry = AdapterRegistry()
    registry.register(recording)

    app = create_app(settings=cfg, llm=_FakeLLM(), adapter_registry=registry)  # type: ignore[arg-type]

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            # 1. Global workspace base layout auto-created
            res_ws = await client.get("/api/workspace")
            assert res_ws.status_code == 200
            ws_data = res_ws.json()
            assert Path(ws_data["workspace_root"]) == ws_root.resolve()
            assert (ws_root / "rules").is_dir()
            assert (ws_root / "shared").is_dir()
            assert (ws_root / "topics").is_dir()

            # 2. Create a topic -> auto-creates dedicated topic subdir
            res_topic = await client.post("/api/topics", json={"title": "开发新功能"})
            assert res_topic.status_code == 201
            topic = res_topic.json()
            topic_id = topic["id"]
            expected_topic_dir = (ws_root / "topics" / topic_id).resolve()
            assert Path(topic["workspace_dir"]) == expected_topic_dir
            assert (expected_topic_dir / "artifacts").is_dir()
            assert (expected_topic_dir / "files").is_dir()

            # 3. Default topic workspace primary_dir is topic_dir
            res_tws = await client.get(f"/api/topics/{topic_id}/workspace")
            assert res_tws.status_code == 200
            tws = res_tws.json()
            assert Path(tws["primary_dir"]) == expected_topic_dir

            # 4. Invalid mount path rejected with 400
            bad_mount = await client.post(
                "/api/workspace/mounts",
                json={"scope": "global", "path": str(tmp_path / "does-not-exist")},
            )
            assert bad_mount.status_code == 400

            # 5. Add global mount (main stream) and topic mount (custom repo)
            res_gm = await client.post(
                "/api/workspace/mounts",
                json={"scope": "global", "path": str(custom_global), "label": "公共资料"},
            )
            assert res_gm.status_code == 201
            gm = res_gm.json()
            assert gm["scope"] == "global"
            assert gm["is_primary"] is False

            res_tm = await client.post(
                "/api/workspace/mounts",
                json={
                    "scope": "topic",
                    "topic_id": topic_id,
                    "path": str(custom_topic_repo),
                    "label": "业务仓库",
                    "is_primary": True,
                },
            )
            assert res_tm.status_code == 201
            tm = res_tm.json()
            assert tm["is_primary"] is True

            # 6. Topic workspace now resolves primary_dir to custom_topic_repo (Option A)
            # and includes topic_dir, shared, rules, global mount in extra_dirs
            res_tws2 = await client.get(f"/api/topics/{topic_id}/workspace")
            assert res_tws2.status_code == 200
            tws2 = res_tws2.json()
            assert Path(tws2["primary_dir"]) == custom_topic_repo.resolve()
            extra_paths = {Path(p) for p in tws2["extra_dirs"]}
            assert expected_topic_dir in extra_paths
            assert (ws_root / "shared").resolve() in extra_paths
            assert (ws_root / "rules").resolve() in extra_paths
            assert custom_global.resolve() in extra_paths

            # 7. Trigger a Heavy-Path run on this topic -> adapter receives resolved directories
            (ws_root / "rules" / "global.md").write_text("Always be concise.", encoding="utf-8")
            res_chat = await client.post(
                "/api/chat",
                json={
                    "content": "检查项目并生成报告",
                    "focus_topic_id": topic_id,
                    "force_path": "heavy",
                    "agent_type": "recording_agent",
                },
            )
            assert res_chat.status_code == 200
            run_id = res_chat.json()["session_run"]["id"]

            # Wait for background task completion
            for _ in range(50):
                run_res = await client.get(f"/api/runs/{run_id}")
                if run_res.json()["status"] in ("completed", "failed"):
                    break
                await asyncio.sleep(0.05)

            run_detail = (await client.get(f"/api/runs/{run_id}")).json()
            assert run_detail["status"] == "completed"
            assert recording.captured is not None
            assert recording.captured.workspace_root == custom_topic_repo.resolve()
            assert recording.captured.topic_dir == expected_topic_dir
            assert recording.captured.artifacts_dir == (expected_topic_dir / "artifacts").resolve()
            assert recording.captured.shared_dir == (ws_root / "shared").resolve()
            assert recording.captured.rules_dir == (ws_root / "rules").resolve()
            assert custom_global.resolve() in recording.captured.extra_dirs

            # Prompt includes workspace guidance and rule file path
            prompt = recording.captured.build_task_prompt()
            assert str(custom_topic_repo.resolve()) in prompt
            assert str(expected_topic_dir) in prompt
            assert "global.md" in prompt

            # Artifact saved inside topic_dir/artifacts/<run_id>/result.md and downloadable via API
            assert len(run_detail["artifacts"]) == 1
            art = run_detail["artifacts"][0]
            art_file = expected_topic_dir / "artifacts" / run_id / "result.md"
            assert art_file.is_file()
            dl_res = await client.get(f"/api/runs/{run_id}/artifacts/{art['id']}")
            assert dl_res.status_code == 200
            assert "Workspace Artifact" in dl_res.text

            # 8. Delete topic mount -> primary_dir falls back to topic_dir
            del_res = await client.delete(f"/api/workspace/mounts/{tm['id']}")
            assert del_res.status_code == 204
            tws3 = (await client.get(f"/api/topics/{topic_id}/workspace")).json()
            assert Path(tws3["primary_dir"]) == expected_topic_dir


def test_cli_adapters_env_and_add_dir_passthrough(tmp_path: Path) -> None:
    ws_root = tmp_path / "kiki-ws"
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    wm = WorkspaceManager(ws_root)
    resolved = wm.resolve_for_topic(
        "topic-abc",
        topic_mounts=[{"path": str(project_dir), "is_primary": True}],
        ensure_dirs=True,
    )

    db = Database(tmp_path / "test.db")
    db.init_db()
    bus = EventBus()
    executor = WorkspaceToolExecutor(
        workspace_root=resolved.primary_dir,
        artifacts_dir=resolved.topic_artifacts_dir,
        db=db,
        event_bus=bus,
        extra_roots=resolved.allowed_roots,
    )

    # Scoped tool executor can read files in both primary_dir and shared/rules/topic_dir
    (project_dir / "main.py").write_text("print('hi')\n", encoding="utf-8")
    (resolved.rules_dir / "style.md").write_text("# Style\n", encoding="utf-8")
    assert "print('hi')" in executor.read_file("main.py")
    assert "# Style" in executor.read_file(str(resolved.rules_dir / "style.md"))

    ctx = AdapterContext(
        run_id="run-1",
        topic_id="topic-abc",
        topic_title="测试话题",
        user_content="分析代码",
        workspace_root=resolved.primary_dir,
        artifacts_dir=resolved.topic_artifacts_dir,
        db=db,
        event_bus=bus,
        tool_executor=executor,
        llm=_FakeLLM(),  # type: ignore[arg-type]
        kiki_workspace_root=resolved.workspace_root,
        topic_dir=resolved.topic_dir,
        shared_dir=resolved.shared_dir,
        rules_dir=resolved.rules_dir,
        extra_dirs=list(resolved.extra_dirs),
    )

    profile = ExecutionProfile(model="test-model", thinking_level="medium")

    claude = ClaudeCodeAdapter()
    claude_cmd = claude.build_command("claude", ctx, profile)
    assert "--add-dir" in claude_cmd
    assert str(resolved.topic_dir) in claude_cmd
    assert str(resolved.shared_dir) in claude_cmd
    assert str(resolved.rules_dir) in claude_cmd

    claude_env = claude.build_env(ctx, profile)
    assert claude_env["KIKI_WORKSPACE_ROOT"] == str(resolved.workspace_root)
    assert claude_env["KIKI_TOPIC_DIR"] == str(resolved.topic_dir)
    assert claude_env["KIKI_ARTIFACTS_DIR"] == str(resolved.topic_artifacts_dir)
    assert claude_env["KIKI_SHARED_DIR"] == str(resolved.shared_dir)
    assert claude_env["KIKI_RULES_DIR"] == str(resolved.rules_dir)
    assert claude_env["KIKI_PRIMARY_DIR"] == str(resolved.primary_dir)
    assert str(resolved.topic_dir) in claude_env["KIKI_EXTRA_DIRS"].split(os.pathsep)

    codex = CodexAdapter()
    codex_cmd = codex.build_command("codex", ctx, profile)
    assert "--add-dir" in codex_cmd
    assert str(resolved.topic_dir) in codex_cmd
    assert str(resolved.rules_dir) in codex_cmd

"""Tests for Kiki's redesigned memory system (SQLite memories, FTS5, rolling summaries, dream distillation, tools & API)."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

import httpx
import pytest

from app.adapters.base import AdapterContext
from app.config import Settings
from app.db import Database
from app.dream import DreamConsolidator
from app.llm import KikiLLMClient
from app.main import create_app
from app.schemas import (
    DreamConsolidationPlan,
    DreamMemoryOperation,
    DreamTopicSummaryUpdate,
)
from app.sse import EventBus
from app.tools import WorkspaceToolExecutor
from app.workspace import WorkspaceManager


class _FakeDreamLLM:
    """Stub LLM that returns a scripted DreamConsolidationPlan."""

    def __init__(self, plan: DreamConsolidationPlan) -> None:
        self.plan = plan
        self.calls: list[dict[str, Any]] = []

    async def consolidate_dream(
        self,
        *,
        active_topics: list[dict[str, Any]],
        general_messages: list[dict[str, Any]],
        topics_to_summarize: Optional[list[dict[str, Any]]] = None,
        existing_memories: Optional[list[dict[str, Any]]] = None,
        memory_candidates: Optional[list[dict[str, Any]]] = None,
    ) -> DreamConsolidationPlan:
        self.calls.append(
            {
                "active_topics": active_topics,
                "general_messages": general_messages,
                "topics_to_summarize": topics_to_summarize or [],
                "existing_memories": existing_memories or [],
                "memory_candidates": memory_candidates or [],
            }
        )
        return self.plan


def _make_settings(tmp_path: Path) -> Settings:
    return Settings(
        gemini_api_key="",
        gemini_model="gemini-3.8-flash",
        database_path=tmp_path / "kiki.db",
        artifacts_dir=tmp_path / "artifacts",
        workspace_root=tmp_path / "workspace",
        auth_username="",
        auth_password_hash="",
        auth_users="",
        api_token="",
    )


def test_memory_crud_fts5_and_markdown_sync(tmp_path: Path) -> None:
    ws = WorkspaceManager(tmp_path / "workspace")
    ws.ensure_base_layout()
    db = Database(tmp_path / "kiki.db")
    db.set_workspace_manager(ws)
    db.init_db()

    topic = db.create_topic(title="前端工程")
    m1 = db.create_memory(
        content="前端项目默认使用 pnpm 而不是 npm",
        category="preference",
        source_topic_id=topic["id"],
        pinned=True,
    )
    m2 = db.create_memory(
        content="主人正在开发 Kiki 个人智能体",
        category="profile",
    )
    m3 = db.create_memory(
        content="部署前必须先跑 pytest 全量单测",
        category="lesson",
    )

    # Deduplication on identical content
    m1_dup = db.create_memory(content="前端项目默认使用 pnpm 而不是 npm", category="preference")
    assert m1_dup["id"] == m1["id"]

    # Core memories include pinned + profile/preference, excluding unpinned lesson
    core = db.list_core_memories(limit=10)
    core_ids = [m["id"] for m in core]
    assert m1["id"] in core_ids
    assert m2["id"] in core_ids
    assert m3["id"] not in core_ids

    # FTS5 / LIKE search for English & Chinese queries (including <3-char short query)
    assert any(m["id"] == m1["id"] for m in db.search_memories("pnpm"))
    assert any(m["id"] == m2["id"] for m in db.search_memories("个人智能体"))
    assert any(m["id"] == m3["id"] for m in db.search_memories("单测"))

    # Cross-topic message search
    db.create_message(topic["id"], "user", "我们决定把状态管理迁移到 Zustand")
    hits = db.search_history("Zustand")
    assert len(hits) == 1
    assert hits[0]["topic_title"] == "前端工程"

    # MEMORY.md mirror synced automatically
    mem_md = ws.memory_file.read_text(encoding="utf-8")
    assert "前端项目默认使用 pnpm 而不是 npm" in mem_md
    assert "主人正在开发 Kiki 个人智能体" in mem_md

    # Update & delete sync FTS + MEMORY.md
    updated = db.update_memory(m3["id"], content="发布前先跑 pytest 和 vite build", pinned=True)
    assert updated is not None and updated["pinned"] is True
    assert any(m["id"] == m3["id"] for m in db.search_memories("vite build"))

    assert db.delete_memory(m2["id"]) is True
    assert not any(m["id"] == m2["id"] for m in db.search_memories("个人智能体"))
    assert "主人正在开发 Kiki 个人智能体" not in ws.memory_file.read_text(encoding="utf-8")
    db.close()


@pytest.mark.asyncio
async def test_dream_rolling_summary_and_memory_distillation(tmp_path: Path) -> None:
    ws = WorkspaceManager(tmp_path / "workspace")
    ws.ensure_base_layout()
    db = Database(tmp_path / "kiki.db")
    db.set_workspace_manager(ws)
    db.init_db()
    bus = EventBus()

    topic = db.create_topic(title="记忆系统重构", summary="初步讨论方案")
    old_mem = db.create_memory(content="前端包管理器使用 yarn", category="preference")

    # Add 4 messages so delta >= summary_refresh_delta (set to 3 for test)
    for idx in range(4):
        db.create_message(topic["id"], "user" if idx % 2 == 0 else "assistant", f"第 {idx + 1} 轮讨论：改用 pnpm 与 SQLite FTS5")

    plan = DreamConsolidationPlan(
        topic_summaries=[
            DreamTopicSummaryUpdate(
                topic_id=topic["id"],
                summary="已确定使用 SQLite FTS5 + 滚动摘要 + 常驻核心记忆方案",
            )
        ],
        memory_ops=[
            DreamMemoryOperation(
                action="update",
                memory_id=old_mem["id"],
                category="preference",
                content="前端包管理器已改为使用 pnpm",
                confidence=0.92,
            ),
            DreamMemoryOperation(
                action="add",
                category="project",
                content="Kiki 记忆系统基于 SQLite FTS5 与做梦整理实现",
                source_topic_id=topic["id"],
                confidence=0.9,
            ),
        ],
    )
    fake_llm = _FakeDreamLLM(plan)
    consolidator = DreamConsolidator(
        db=db,
        llm=fake_llm,  # type: ignore[arg-type]
        event_bus=bus,
        summary_refresh_delta=3,
    )

    result = await consolidator.run_once(force=True, ignore_busy=True)
    assert result["ran"] is True
    assert topic["id"] in result["updated_summaries"]
    assert len(result["memories_added"]) == 1
    assert old_mem["id"] in result["memories_updated"]

    refreshed_topic = db.get_topic(topic["id"])
    assert refreshed_topic is not None
    assert "SQLite FTS5" in (refreshed_topic["summary"] or "")
    assert refreshed_topic["summary_msg_count"] == 4

    updated_old = db.get_memory(old_mem["id"])
    assert updated_old is not None
    assert "pnpm" in updated_old["content"]

    # Second run immediately should have nothing left to consolidate
    result2 = await consolidator.run_once(force=True, ignore_busy=True)
    assert result2["ran"] is False
    assert result2["reason"] == "nothing_to_consolidate"
    db.close()


@pytest.mark.asyncio
async def test_memory_tools_and_prompt_injection(tmp_path: Path) -> None:
    ws = WorkspaceManager(tmp_path / "workspace")
    ws.ensure_base_layout()
    db = Database(tmp_path / "kiki.db")
    db.set_workspace_manager(ws)
    db.init_db()
    bus = EventBus()

    topic = db.create_topic(title="后端架构", summary="使用 FastAPI + SQLite")
    msg = db.create_message(topic["id"], "user", "我们决定端口固定在 8000")
    run = db.create_session_run(topic["id"], msg["id"], "internal_agent")

    executor = WorkspaceToolExecutor(
        workspace_root=tmp_path / "workspace",
        artifacts_dir=tmp_path / "artifacts",
        db=db,
        event_bus=bus,
    )

    out, is_err = await executor.execute(
        run["id"],
        "save_memory",
        {"content": "后端服务端口固定使用 8000", "category": "project", "pinned": True},
    )
    assert is_err is False
    assert "已记住长期记忆" in out

    recall_out, recall_err = await executor.execute(
        run["id"],
        "recall_memory",
        {"query": "8000", "include_history": True},
    )
    assert recall_err is False
    assert "后端服务端口固定使用 8000" in recall_out
    assert "端口固定在 8000" in recall_out

    # Prompt injection check for AdapterContext
    core_memories = db.list_core_memories(limit=10)
    ctx = AdapterContext(
        run_id=run["id"],
        topic_id=topic["id"],
        topic_title=topic["title"],
        user_content="帮我检查配置",
        workspace_root=tmp_path / "workspace",
        artifacts_dir=tmp_path / "artifacts",
        db=db,
        event_bus=bus,
        tool_executor=executor,
        llm=KikiLLMClient(_make_settings(tmp_path)),
        topic_summary=topic["summary"],
        core_memories=core_memories,
        kiki_workspace_root=ws.workspace_root,
    )
    prompt = ctx.build_task_prompt()
    assert "后端服务端口固定使用 8000" in prompt
    assert "MEMORY.md" in prompt

    forget_out, forget_err = await executor.execute(
        run["id"],
        "forget_memory",
        {"query": "8000"},
    )
    assert forget_err is False
    assert "已删除匹配记忆" in forget_out
    assert db.list_memories(status="active") == []
    db.close()


@pytest.mark.asyncio
async def test_memory_rest_api(tmp_path: Path) -> None:
    cfg = _make_settings(tmp_path)
    db = Database(cfg.database_path)
    db.init_db()
    app = create_app(settings=cfg, db=db)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        # Create
        res = await client.post(
            "/api/memories",
            json={"content": "回复尽量简洁直接", "category": "preference", "pinned": True},
        )
        assert res.status_code == 201
        created = res.json()
        assert created["pinned"] is True

        # List
        list_res = await client.get("/api/memories")
        assert list_res.status_code == 200
        assert len(list_res.json()) == 1

        # Search
        search_res = await client.get("/api/memories/search", params={"q": "简洁"})
        assert search_res.status_code == 200
        assert len(search_res.json()["memories"]) == 1

        # Patch
        patch_res = await client.patch(
            f"/api/memories/{created['id']}",
            json={"content": "回复保持简洁直接并附代码链接", "pinned": False},
        )
        assert patch_res.status_code == 200
        assert patch_res.json()["pinned"] is False
        assert "代码链接" in patch_res.json()["content"]

        # Delete
        del_res = await client.delete(f"/api/memories/{created['id']}")
        assert del_res.status_code == 204
        list_after = await client.get("/api/memories")
        assert list_after.json() == []

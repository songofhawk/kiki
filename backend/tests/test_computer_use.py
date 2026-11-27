"""Unit and end-to-end tests for built-in internal_agent ComputerUse capabilities."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.config import Settings
from app.db import Database
from app.desktop import (
    COMPUTER_USE_TOOL_DECLARATIONS,
    ComputerUseExecutor,
    DesktopTakeoverManager,
    MockDesktopDriver,
)
from app.llm import _stringify_tool_result_for_openai
from app.sse import EventBus
from app.task_manager import TaskManager
from app.tools import WorkspaceToolExecutor


def _make_env(tmp_path: Path, *, accessibility: bool = True, screen_recording: bool = True):
    cfg = Settings(
        gemini_api_key="test-key",
        gemini_model="gemini-3.8-flash",
        workspace_root=tmp_path / "workspace",
        database_path=tmp_path / "kiki.db",
        artifacts_dir=tmp_path / "workspace" / "artifacts",
        computer_use_enabled=True,
        computer_use_max_steps=16,
    )
    cfg.workspace_root.mkdir(parents=True, exist_ok=True)
    db = Database(cfg.database_path)
    db.init_db()
    bus = EventBus()
    driver = MockDesktopDriver(
        accessibility=accessibility,
        screen_recording=screen_recording,
        spaces_per_display=3,
    )
    desktop_manager = DesktopTakeoverManager(driver=driver, event_bus=bus)
    tool_executor = WorkspaceToolExecutor(
        workspace_root=cfg.workspace_root,
        artifacts_dir=cfg.artifacts_dir,
        db=db,
        event_bus=bus,
    )
    return cfg, db, bus, driver, desktop_manager, tool_executor


def test_computer_use_tool_declarations_and_openai_flattening() -> None:
    names = {t["name"] for t in COMPUTER_USE_TOOL_DECLARATIONS}
    assert names == {"computer_screenshot", "computer_action"}

    multimodal_result = [
        {"type": "text", "text": '{"ok": true, "action": "click"}'},
        {"type": "image", "data": "iVBORw0KGgoAAAANSUhEUg==", "mime_type": "image/png"},
    ]
    flattened = _stringify_tool_result_for_openai(multimodal_result)
    assert '"action": "click"' in flattened
    assert "screenshot image attached" in flattened
    assert "iVBORw0KGgo" not in flattened


def test_mock_driver_open_url_and_open_app() -> None:
    driver = MockDesktopDriver()
    driver.open_url("https://creator.xiaohongshu.com/publish/publish")
    driver.open_app("Google Chrome")
    assert driver.opened_urls == ["https://creator.xiaohongshu.com/publish/publish"]
    assert driver.opened_apps == ["Google Chrome"]

    with pytest.raises(ValueError):
        driver.open_url("file:///etc/passwd")
    with pytest.raises(ValueError):
        driver.open_app("-bad-flag")
    with pytest.raises(ValueError):
        driver.open_app("../evil")


@pytest.mark.asyncio
async def test_computer_use_executor_core_actions(tmp_path: Path) -> None:
    cfg, db, bus, driver, desktop_manager, tool_executor = _make_env(tmp_path)
    topic = db.create_topic(title="自媒体发布测试")
    msg = db.create_message(topic_id=topic["id"], role="user", content="发一篇小红书")
    run = db.create_session_run(topic_id=topic["id"], trigger_message_id=msg["id"], agent_type="internal_agent")
    run_id = run["id"]

    cu = ComputerUseExecutor(desktop_manager, tool_executor)

    # 1. Screenshot + save artifact
    shot = await cu.execute(
        run_id,
        "computer_screenshot",
        {"save_artifact": True, "artifact_label": "初始桌面"},
    )
    assert not shot.is_error
    assert shot.image_bytes is not None
    assert shot.artifact is not None
    payload = shot.to_function_result_value()
    assert isinstance(payload, list) and len(payload) == 2
    assert payload[0]["type"] == "text"
    assert payload[1]["type"] == "image"
    assert payload[1]["mime_type"] == "image/png"

    # 2. Click (0..1000 -> 0..1.0)
    res_click = await cu.execute(
        run_id,
        "computer_action",
        {"action": "click", "x": 500, "y": 250, "wait_after": 0},
    )
    assert not res_click.is_error
    assert driver.recorded_events[-1].action == "mouse_click"
    assert driver.recorded_events[-1].x == pytest.approx(0.5)
    assert driver.recorded_events[-1].y == pytest.approx(0.25)

    # 3. Type short ASCII -> type_text; type Chinese -> clipboard paste & restore
    driver.set_clipboard("previous-clip")
    res_ascii = await cu.execute(
        run_id,
        "computer_action",
        {"action": "type", "text": "hello", "wait_after": 0, "return_screenshot": False},
    )
    assert not res_ascii.is_error
    assert json.loads(res_ascii.text)["method"] == "type_text"
    assert driver.recorded_events[-1].action == "type_text"

    res_zh = await cu.execute(
        run_id,
        "computer_action",
        {"action": "type", "text": "今天分享一个 AI Agent 实战教程！", "wait_after": 0, "return_screenshot": False},
    )
    assert not res_zh.is_error
    assert json.loads(res_zh.text)["method"] == "paste"
    assert driver.recorded_events[-1].action == "key_combo"
    assert driver.recorded_events[-1].key == "v"
    assert "cmd" in driver.recorded_events[-1].modifiers
    assert driver.get_clipboard() == "previous-clip"

    # 3b. Type with replace=True sends Cmd+A, Backspace, then pastes
    before_replace = len(driver.recorded_events)
    res_replace = await cu.execute(
        run_id,
        "computer_action",
        {"action": "type", "text": "替换后的中文正文", "replace": True, "wait_after": 0, "return_screenshot": False},
    )
    assert not res_replace.is_error
    assert json.loads(res_replace.text)["replace"] is True
    replace_evts = driver.recorded_events[before_replace:]
    assert replace_evts[0].action == "key_combo" and replace_evts[0].key == "a"
    assert replace_evts[1].action == "key_tap" and replace_evts[1].key == "backspace"
    assert replace_evts[2].action == "key_combo" and replace_evts[2].key == "v"

    # 4. Drag action
    before_len = len(driver.recorded_events)
    res_drag = await cu.execute(
        run_id,
        "computer_action",
        {"action": "drag", "x": 100, "y": 200, "end_x": 600, "end_y": 700, "wait_after": 0, "return_screenshot": False},
    )
    assert not res_drag.is_error
    drag_actions = [e.action for e in driver.recorded_events[before_len:]]
    assert drag_actions[0] == "mouse_move"
    assert drag_actions[1] == "mouse_down"
    assert drag_actions[-1] == "mouse_up"

    # 5. select_file_in_dialog: valid workspace file vs sensitive .env file
    video_file = cfg.workspace_root / "demo_video.mp4"
    video_file.write_bytes(b"fake-mp4-data")
    res_file = await cu.execute(
        run_id,
        "computer_action",
        {"action": "select_file_in_dialog", "file_path": "demo_video.mp4", "wait_after": 0, "return_screenshot": False},
    )
    assert not res_file.is_error
    assert str(video_file.resolve()) in res_file.text

    env_file = cfg.workspace_root / ".env"
    env_file.write_text("SECRET=123", encoding="utf-8")
    res_secret = await cu.execute(
        run_id,
        "computer_action",
        {"action": "select_file_in_dialog", "file_path": ".env", "wait_after": 0, "return_screenshot": False},
    )
    assert res_secret.is_error
    assert "sensitive" in res_secret.text.lower()

    # 6. Human takeover conflict lock blocks mutating actions in control mode
    session = desktop_manager.start_session(control_mode="control")
    res_blocked = await cu.execute(
        run_id,
        "computer_action",
        {"action": "click", "x": 100, "y": 100, "wait_after": 0},
    )
    assert res_blocked.is_error
    assert "接管控制" in res_blocked.text

    # View-only takeover session does NOT block ComputerUse
    desktop_manager.update_session(session.id, control_mode="view")
    res_allowed = await cu.execute(
        run_id,
        "computer_action",
        {"action": "click", "x": 100, "y": 100, "wait_after": 0, "return_screenshot": False},
    )
    assert not res_allowed.is_error
    desktop_manager.end_session(session.id)

    db.close()


@pytest.mark.asyncio
async def test_computer_use_permission_checks(tmp_path: Path) -> None:
    cfg, db, bus, driver, desktop_manager, tool_executor = _make_env(
        tmp_path, accessibility=False, screen_recording=False
    )
    cu = ComputerUseExecutor(desktop_manager, tool_executor)

    res_shot = await cu.execute("run-perm", "computer_screenshot", {})
    assert res_shot.is_error
    assert "屏幕录制" in res_shot.text

    res_click = await cu.execute(
        "run-perm",
        "computer_action",
        {"action": "click", "x": 100, "y": 100, "wait_after": 0},
    )
    assert res_click.is_error
    assert "辅助功能" in res_click.text

    db.close()


class _FakeSocialPublishLLM:
    """Simulates a multi-step social media video publishing workflow with HITL confirmation."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.last_used_model = "gemini-3.8-flash"

    async def run_agent_turn(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        step = len(self.calls)
        if step == 1:
            return SimpleNamespace(
                id="cu-1",
                steps=[
                    SimpleNamespace(
                        type="function_call",
                        id="c1",
                        name="computer_action",
                        arguments={
                            "action": "open_url",
                            "url": "https://creator.xiaohongshu.com/publish/publish",
                            "wait_after": 0,
                        },
                    )
                ],
                output_text="",
            )
        if step == 2:
            return SimpleNamespace(
                id="cu-2",
                steps=[
                    SimpleNamespace(
                        type="function_call",
                        id="c2",
                        name="computer_action",
                        arguments={
                            "action": "select_file_in_dialog",
                            "file_path": "vlog.mp4",
                            "wait_after": 0,
                        },
                    )
                ],
                output_text="",
            )
        if step == 3:
            return SimpleNamespace(
                id="cu-3",
                steps=[
                    SimpleNamespace(
                        type="function_call",
                        id="c3",
                        name="computer_action",
                        arguments={
                            "action": "type",
                            "text": "Kiki 自动发布的视频标题 #AI效率",
                            "wait_after": 0,
                            "save_artifact": True,
                            "artifact_label": "发布草稿预览",
                        },
                    )
                ],
                output_text="",
            )
        if step == 4:
            return SimpleNamespace(
                id="cu-4",
                steps=[
                    SimpleNamespace(
                        type="function_call",
                        id="c4",
                        name="request_human_approval",
                        arguments={
                            "prompt": "小红书视频和文案已填好（见预览截图），确认点击发布吗？",
                            "options": [
                                {"label": "确认发布", "value": "publish", "approved": True, "style": "primary"},
                                {"label": "先存草稿", "value": "draft", "approved": False, "style": "secondary"},
                            ],
                        },
                    )
                ],
                output_text="",
            )
        if step == 5:
            return SimpleNamespace(
                id="cu-5",
                steps=[
                    SimpleNamespace(
                        type="function_call",
                        id="c5",
                        name="computer_action",
                        arguments={
                            "action": "click",
                            "x": 820,
                            "y": 900,
                            "wait_after": 0,
                        },
                    )
                ],
                output_text="",
            )
        return SimpleNamespace(
            id="cu-6",
            steps=[],
            output_text="已将视频 vlog.mp4 与标题成功发布至小红书创作者后台，并保存了发布前预览截图。",
        )


@pytest.mark.asyncio
async def test_internal_agent_end_to_end_social_media_publish(tmp_path: Path) -> None:
    cfg, db, bus, driver, desktop_manager, tool_executor = _make_env(tmp_path)
    llm = _FakeSocialPublishLLM()
    tm = TaskManager(
        db=db,
        llm=llm,
        event_bus=bus,
        tool_executor=tool_executor,
        settings=cfg,
        desktop_manager=desktop_manager,
        max_steps=4,  # intentionally smaller than 6 to verify dynamic ComputerUse step budget expansion
    )

    topic = db.create_topic(title="小红书视频发布")
    # Put vlog.mp4 in the topic's workspace primary dir (workspace_root/topics/<id> is topic_dir; primary_dir defaults to topic_dir)
    resolved_ws = tm.workspace_manager.resolve_for_topic(topic["id"], ensure_dirs=True)
    (resolved_ws.primary_dir / "vlog.mp4").write_bytes(b"video-content")

    msg = db.create_message(topic_id=topic["id"], role="user", content="帮我把 vlog.mp4 发到小红书")
    run = tm.enqueue_run(
        topic_id=topic["id"],
        trigger_message_id=msg["id"],
        user_content="帮我把 vlog.mp4 发到小红书",
        agent_type="internal_agent",
    )
    run_id = run["id"]

    # Wait for HITL approval pause before clicking publish
    for _ in range(100):
        hitl = bus.get_hitl_request(run_id)
        if hitl is not None:
            break
        await asyncio.sleep(0.02)

    hitl = bus.get_hitl_request(run_id)
    assert hitl is not None
    assert "确认点击发布" in hitl["prompt"]

    # Approve publishing
    tm.resume_run(run_id, decision="publish", approved=True)

    for _ in range(100):
        cur = db.get_session_run(run_id)
        if cur and cur["status"] == "completed":
            break
        await asyncio.sleep(0.02)

    final_run = db.get_session_run(run_id)
    assert final_run is not None
    assert final_run["status"] == "completed"
    assert len(llm.calls) == 6
    # Verify ComputerUse tools were declared to the LLM
    declared_names = {t["name"] for t in llm.calls[0]["tools"]}
    assert {"computer_screenshot", "computer_action"}.issubset(declared_names)
    # Verify multimodal screenshot was passed back in turn 2 input
    turn2_result = llm.calls[1]["input_data"][0]["result"]
    assert isinstance(turn2_result, list)
    assert turn2_result[1]["type"] == "image"
    # Verify browser URL was opened and preview screenshot artifact was saved
    assert driver.opened_urls == ["https://creator.xiaohongshu.com/publish/publish"]
    assert len(final_run["artifacts"]) == 1
    assert any(s["action_type"] == "computer_use" for s in final_run["step_logs"])

    db.close()


@pytest.mark.asyncio
async def test_computer_use_multi_display_auto_follow_and_default_step_limits(tmp_path: Path) -> None:
    cfg = Settings(
        gemini_api_key="test-key",
        gemini_model="gemini-3.8-flash",
        workspace_root=tmp_path / "workspace",
        database_path=tmp_path / "kiki.db",
        artifacts_dir=tmp_path / "workspace" / "artifacts",
    )
    assert cfg.agent_max_steps == 25
    assert cfg.computer_use_max_steps == 80

    db = Database(cfg.database_path)
    db.init_db()
    bus = EventBus()
    driver = MockDesktopDriver(displays_count=2)
    driver.frontmost_display_index = 1
    driver.frontmost_app = "Google Chrome"
    driver.windows_by_display = {
        0: ["微信"],
        1: ["Google Chrome", "Safari浏览器"],
    }
    desktop_manager = DesktopTakeoverManager(driver=driver, event_bus=bus)
    tool_executor = WorkspaceToolExecutor(
        workspace_root=cfg.workspace_root,
        artifacts_dir=cfg.artifacts_dir,
        db=db,
        event_bus=bus,
    )
    cu = ComputerUseExecutor(desktop_manager, tool_executor)

    # open_url without display_index should auto-follow Google Chrome onto display 1
    res_open = await cu.execute(
        "run-multi",
        "computer_action",
        {
            "action": "open_url",
            "url": "https://creator.xiaohongshu.com/publish/publish",
            "wait_after": 0,
        },
    )
    assert not res_open.is_error
    meta_open = json.loads(res_open.text)
    assert meta_open["display_index"] == 1
    assert meta_open["auto_switched_display"] is True
    assert meta_open["frontmost_app"] == "Google Chrome"
    assert len(meta_open["displays"]) == 2
    assert "Google Chrome" in meta_open["displays"][1]["windows"]

    # Subsequent click without display_index should stay on display 1 (remembered active display)
    res_click = await cu.execute(
        "run-multi",
        "computer_action",
        {"action": "click", "x": 500, "y": 500, "wait_after": 0, "return_screenshot": False},
    )
    assert not res_click.is_error
    assert driver.recorded_events[-1].display_index == 1

    # Screenshot on display 0 explicitly should report hint pointing to frontmost window on display 1
    res_shot0 = await cu.execute(
        "run-multi",
        "computer_screenshot",
        {"display_index": 0},
    )
    meta_shot0 = json.loads(res_shot0.text)
    assert meta_shot0["display_index"] == 0
    assert "显示器 1" in meta_shot0.get("hint", "")

    db.close()

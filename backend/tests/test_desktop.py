"""Unit tests for Kiki Server Desktop Takeover core (drivers, models, session manager)."""

from __future__ import annotations

import platform
import time

import pytest

from app.config import load_settings
from app.desktop import (
    DesktopConflictError,
    DesktopDisabledError,
    DesktopInputEvent,
    DesktopNotFoundError,
    DesktopTakeoverError,
    DesktopTakeoverManager,
    DesktopUnauthorizedError,
    MacOSDesktopDriver,
    MockDesktopDriver,
    create_desktop_driver,
)
from app.sse import EventBus


def test_desktop_config_env_parsing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KIKI_DESKTOP_TAKEOVER_ENABLED", "false")
    monkeypatch.setenv("KIKI_DESKTOP_DRIVER", "mock")
    monkeypatch.setenv("KIKI_DESKTOP_IDLE_TIMEOUT_SECONDS", "120")
    monkeypatch.setenv("KIKI_DESKTOP_DEFAULT_FPS", "8")
    monkeypatch.setenv("KIKI_DESKTOP_MAX_WIDTH", "1600")

    cfg = load_settings()
    assert cfg.desktop_takeover_enabled is False
    assert cfg.desktop_driver == "mock"
    assert cfg.desktop_idle_timeout_seconds == 120
    assert cfg.desktop_default_fps == 8
    assert cfg.desktop_max_width == 1600


def test_input_event_normalization_and_validation() -> None:
    ev = DesktopInputEvent.from_payload(
        {"action": "mouse_click", "x": 1.5, "y": -0.2, "button": "RIGHT", "modifiers": ["Cmd", "Shift"]}
    )
    assert ev.x == 1.0
    assert ev.y == 0.0
    assert ev.button == "right"
    assert ev.modifiers == ("cmd", "shift")

    with pytest.raises(ValueError):
        DesktopInputEvent.from_payload({"action": "invalid_action"})


def test_mock_driver_capture_input_and_clipboard() -> None:
    driver = MockDesktopDriver(width=1440, height=900)
    caps = driver.probe()
    assert caps.available is True
    assert caps.supports_control is True
    assert len(caps.displays) == 1

    frame = driver.capture_frame()
    assert frame.mime_type == "image/png"
    assert frame.data.startswith(b"\x89PNG\r\n\x1a\n")
    assert frame.sha1

    ev = DesktopInputEvent.from_payload({"action": "type_text", "text": "hello kiki"})
    driver.send_input(ev)
    assert len(driver.recorded_events) == 1
    assert driver.recorded_events[0].text == "hello kiki"

    driver.set_clipboard("copied text")
    assert driver.get_clipboard() == "copied text"


def test_macos_driver_probe_on_darwin() -> None:
    driver = create_desktop_driver("macos")
    assert isinstance(driver, MacOSDesktopDriver)
    caps = driver.probe()
    if platform.system() == "Darwin":
        assert caps.driver_name == "macos"
        assert caps.platform == "Darwin"
        assert caps.available is True
        assert len(caps.displays) >= 1


@pytest.mark.asyncio
async def test_manager_session_lifecycle_exclusive_lock_and_tokens() -> None:
    bus = EventBus()
    driver = MockDesktopDriver()
    mgr = DesktopTakeoverManager(driver, bus, idle_timeout_seconds=60, default_fps=5)

    status = mgr.get_status()
    assert status["available"] is True
    assert status["active_session"] is None

    sess = mgr.start_session(run_id="run-123", topic_id="topic-1", reason="需要登录验证码")
    assert sess.control_mode == "control"
    assert sess.fps == 5
    assert sess.run_id == "run-123"
    assert mgr.get_active_session() is sess
    assert driver.wake_calls == 1
    assert driver.display_awake_held is True

    # Exclusive lock blocks second session unless force=True
    with pytest.raises(DesktopConflictError):
        mgr.start_session()

    # Token verification
    assert mgr.require_session(sess.id, token=sess.session_token) is sess
    with pytest.raises(DesktopUnauthorizedError):
        mgr.require_session(sess.id, token="wrong-token")

    # Input injection increments action_count
    await mgr.handle_input(sess.id, {"action": "mouse_click", "x": 0.5, "y": 0.5})
    assert sess.action_count == 1
    assert len(driver.recorded_events) == 1

    # Capture frame & clipboard
    frame = await mgr.capture_frame(sess.id)
    assert frame.data.startswith(b"\x89PNG")
    await mgr.set_clipboard(sess.id, "otp-123456")
    assert await mgr.get_clipboard(sess.id) == "otp-123456"

    # Raw log audit written to bound run_id
    logs = bus.get_raw_logs("run-123")
    assert any("desktop_takeover" in entry["line"] for entry in logs)

    # Force takeover replaces old session
    sess2 = mgr.start_session(force=True, control_mode="view")
    assert sess2.id != sess.id
    assert sess2.control_mode == "view"

    # View-only blocks input and clipboard write
    with pytest.raises(DesktopTakeoverError):
        await mgr.handle_input(sess2.id, {"action": "mouse_click", "x": 0.2, "y": 0.2})
    with pytest.raises(DesktopTakeoverError):
        await mgr.set_clipboard(sess2.id, "blocked")

    # Update mode to control & fps
    mgr.update_session(sess2.id, control_mode="control", fps=10, quality=85)
    assert sess2.control_mode == "control"
    assert sess2.fps == 10
    assert sess2.quality == 85

    # End session
    ended = mgr.end_session(sess2.id)
    assert ended is not None and ended["id"] == sess2.id
    assert mgr.get_active_session() is None
    assert driver.display_awake_held is False
    with pytest.raises(DesktopNotFoundError):
        mgr.require_session(sess2.id)


@pytest.mark.asyncio
async def test_manager_disabled_and_idle_expiry_and_accessibility_downgrade() -> None:
    bus = EventBus()
    disabled_mgr = DesktopTakeoverManager(MockDesktopDriver(), bus, enabled=False)
    assert disabled_mgr.get_status()["available"] is False
    with pytest.raises(DesktopDisabledError):
        disabled_mgr.start_session()

    # No accessibility permission -> auto-downgrades to view-only
    no_ax_driver = MockDesktopDriver(accessibility=False)
    ax_mgr = DesktopTakeoverManager(no_ax_driver, bus, idle_timeout_seconds=30)
    sess = ax_mgr.start_session(control_mode="control")
    assert sess.control_mode == "view"
    with pytest.raises(DesktopTakeoverError):
        ax_mgr.update_session(sess.id, control_mode="control")

    # Simulate idle expiry
    sess.last_activity_ts = time.time() - 60
    assert ax_mgr.get_active_session() is None

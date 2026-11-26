"""REST and WebSocket integration tests for Kiki Server Desktop Takeover API."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from app.config import Settings
from app.desktop import MockDesktopDriver
from app.main import create_app


def _make_test_app(
    tmp_path: Path,
    *,
    driver: MockDesktopDriver | None = None,
    api_token: str = "",
    enabled: bool = True,
):
    cfg = Settings(
        gemini_api_key="",
        gemini_model="gemini-3.8-flash",
        workspace_root=tmp_path / "workspace",
        database_path=tmp_path / "kiki.db",
        artifacts_dir=tmp_path / "workspace" / "artifacts",
        api_token=api_token,
        desktop_takeover_enabled=enabled,
        desktop_driver="mock",
    )
    return create_app(settings=cfg, desktop_driver=driver or MockDesktopDriver())


def test_desktop_rest_lifecycle(tmp_path: Path) -> None:
    driver = MockDesktopDriver(width=1280, height=720)
    app = _make_test_app(tmp_path, driver=driver)

    with TestClient(app) as client:
        # 1. Status
        r = client.get("/api/desktop/status")
        assert r.status_code == 200
        body = r.json()
        assert body["available"] is True
        assert body["driver"] == "mock"
        assert body["active_session"] is None

        # 2. Start session
        r = client.post(
            "/api/desktop/sessions",
            json={"control_mode": "control", "fps": 5, "reason": "登录后台"},
        )
        assert r.status_code == 200
        sess = r.json()["session"]
        sid = sess["id"]
        ws_token = sess["ws_token"]
        assert ws_token
        assert sess["reason"] == "登录后台"

        # 3. Conflict on second start without force
        r = client.post("/api/desktop/sessions", json={})
        assert r.status_code == 409

        # 4. Screenshot fallback endpoint
        r = client.get(f"/api/desktop/sessions/{sid}/screenshot")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("image/png")
        assert r.content.startswith(b"\x89PNG")

        # 5. Clipboard read/write
        r = client.post(f"/api/desktop/sessions/{sid}/clipboard", json={"text": "kiki-clip"})
        assert r.status_code == 200
        r = client.get(f"/api/desktop/sessions/{sid}/clipboard")
        assert r.status_code == 200
        assert r.json()["text"] == "kiki-clip"

        # 6. Patch session
        r = client.patch(f"/api/desktop/sessions/{sid}", json={"control_mode": "view", "fps": 2})
        assert r.status_code == 200
        assert r.json()["session"]["control_mode"] == "view"
        assert r.json()["session"]["fps"] == 2

        # 7. Delete session
        r = client.delete(f"/api/desktop/sessions/{sid}")
        assert r.status_code == 200
        r = client.get("/api/desktop/sessions/active")
        assert r.json()["active"] is False


def test_desktop_websocket_streaming_input_and_auth(tmp_path: Path) -> None:
    driver = MockDesktopDriver()
    app = _make_test_app(tmp_path, driver=driver, api_token="secret-api-tok")
    headers = {"Authorization": "Bearer secret-api-tok"}

    with TestClient(app) as client:
        # Missing API token on REST -> 401
        assert client.get("/api/desktop/status").status_code == 401

        r = client.post("/api/desktop/sessions", json={"fps": 10}, headers=headers)
        assert r.status_code == 200
        sess = r.json()["session"]
        sid = sess["id"]
        ws_token = sess["ws_token"]

        # Connect WebSocket with valid session token + api_token
        with client.websocket_connect(
            f"/api/desktop/sessions/{sid}/ws?token={ws_token}&api_token=secret-api-tok"
        ) as ws:
            first = ws.receive_json()
            assert first["type"] == "session"
            assert first["session"]["id"] == sid

            frame_msg = ws.receive_json()
            assert frame_msg["type"] == "frame"
            assert frame_msg["mime_type"] == "image/png"
            assert len(frame_msg["data"]) > 20

            # Send ping -> receive pong
            ws.send_json({"type": "ping", "ts": 12345})
            pong = ws.receive_json()
            assert pong == {"type": "pong", "ts": 12345}

            # Send mouse click input
            ws.send_json(
                {
                    "type": "input",
                    "payload": {"action": "mouse_click", "x": 0.4, "y": 0.6, "button": "left"},
                }
            )
            # Send config update to view-only -> receive session update
            ws.send_json({"type": "config", "payload": {"control_mode": "view"}})
            msg = ws.receive_json()
            while msg.get("type") == "frame":
                msg = ws.receive_json()
            assert msg["type"] == "session"
            assert msg["session"]["control_mode"] == "view"

        assert len(driver.recorded_events) == 1
        assert driver.recorded_events[0].action == "mouse_click"


def test_desktop_multi_display_and_space_switching(tmp_path: Path) -> None:
    driver = MockDesktopDriver(displays_count=2, spaces_per_display=3)
    app = _make_test_app(tmp_path, driver=driver)

    with TestClient(app) as client:
        # Status reports all displays and spaces
        st = client.get("/api/desktop/status").json()
        assert len(st["displays"]) == 2
        assert len(st["spaces"]) == 6
        assert st["supports_space_switch"] is True

        # Start session auto-picks display 0 while returning available displays & spaces
        created = client.post("/api/desktop/sessions", json={"fps": 10}).json()
        sid = created["session"]["id"]
        assert created["session"]["display_index"] == 0
        assert len(created["displays"]) == 2
        assert len(created["spaces"]) == 6

        # Switch display to index 1
        patched = client.patch(f"/api/desktop/sessions/{sid}", json={"display_index": 1}).json()
        assert patched["session"]["display_index"] == 1

        # Switch space on display 1 via REST
        switched = client.post(f"/api/desktop/sessions/{sid}/space", json={"target_index": 2}).json()
        assert switched["ok"] is True
        d1_spaces = [s for s in switched["spaces"] if s["display_index"] == 1]
        assert d1_spaces[2]["is_active"] is True
        assert d1_spaces[0]["is_active"] is False

        # Switch prev space via direction
        prev_res = client.post(f"/api/desktop/sessions/{sid}/space", json={"direction": "prev"}).json()
        d1_spaces = [s for s in prev_res["spaces"] if s["display_index"] == 1]
        assert d1_spaces[1]["is_active"] is True


def test_desktop_ws_binary_frames_and_ack_backpressure(tmp_path: Path) -> None:
    driver = MockDesktopDriver()
    driver.dynamic_frames = True
    app = _make_test_app(tmp_path, driver=driver)

    with TestClient(app) as client:
        created = client.post("/api/desktop/sessions", json={"fps": 10}).json()
        sid = created["session"]["id"]
        ws_token = created["session"]["ws_token"]

        with client.websocket_connect(
            f"/api/desktop/sessions/{sid}/ws?token={ws_token}&binary=1&ack=1"
        ) as ws:
            init_msg = ws.receive_json()
            assert init_msg["type"] == "session"

            raw1 = ws.receive_bytes()
            assert raw1.startswith(b"\x89PNG\r\n\x1a\n")

            # Send ack + drag sequence -> receive next binary frame
            ws.send_json({"type": "frame_ack"})
            ws.send_json({"type": "input", "payload": {"action": "mouse_down", "x": 0.2, "y": 0.5, "button": "left"}})
            ws.send_json({"type": "input", "payload": {"action": "mouse_move", "x": 0.6, "y": 0.5}})
            ws.send_json({"type": "input", "payload": {"action": "mouse_up", "x": 0.6, "y": 0.5, "button": "left"}})

            raw2 = ws.receive_bytes()
            assert raw2.startswith(b"\x89PNG\r\n\x1a\n")
            assert raw2 != raw1

        assert [e.action for e in driver.recorded_events] == ["mouse_down", "mouse_move", "mouse_up"]

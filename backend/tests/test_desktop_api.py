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

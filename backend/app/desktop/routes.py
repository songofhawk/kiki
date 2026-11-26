"""FastAPI REST and WebSocket endpoints for Kiki Server Desktop Takeover."""

from __future__ import annotations

import asyncio
import base64
import hmac
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query, Response, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field

from .manager import (
    DesktopNotFoundError,
    DesktopTakeoverError,
    DesktopTakeoverManager,
    DesktopUnauthorizedError,
)
from .models import ControlMode


class DesktopSessionCreateBody(BaseModel):
    control_mode: ControlMode = "control"
    display_index: int = Field(default=0, ge=0)
    fps: Optional[int] = Field(default=None, ge=1, le=15)
    quality: int = Field(default=75, ge=30, le=95)
    max_width: Optional[int] = Field(default=None, ge=640, le=2560)
    run_id: Optional[str] = None
    topic_id: Optional[str] = None
    reason: Optional[str] = None
    instructions: Optional[str] = None
    force: bool = False


class DesktopSessionUpdateBody(BaseModel):
    control_mode: Optional[ControlMode] = None
    display_index: Optional[int] = Field(default=None, ge=0)
    fps: Optional[int] = Field(default=None, ge=1, le=15)
    quality: Optional[int] = Field(default=None, ge=30, le=95)
    max_width: Optional[int] = Field(default=None, ge=640, le=2560)


class DesktopClipboardBody(BaseModel):
    text: str = Field(default="", max_length=65536)


class DesktopSpaceSwitchBody(BaseModel):
    target_index: Optional[int] = Field(default=None, ge=0)
    direction: Optional[str] = None
    display_index: Optional[int] = Field(default=None, ge=0)


def _raise_http(exc: DesktopTakeoverError) -> None:
    raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc


def create_desktop_router(
    manager: DesktopTakeoverManager,
    *,
    configured_api_token: str = "",
) -> APIRouter:
    router = APIRouter(prefix="/api/desktop", tags=["desktop"])

    @router.get("/status")
    async def desktop_status() -> dict[str, Any]:
        return manager.get_status()

    @router.get("/sessions/active")
    async def desktop_active_session() -> dict[str, Any]:
        sess = manager.get_active_session()
        return {
            "active": sess is not None,
            "session": sess.to_public_dict() if sess else None,
        }

    @router.post("/sessions")
    async def create_desktop_session(body: DesktopSessionCreateBody) -> dict[str, Any]:
        try:
            sess = manager.start_session(
                control_mode=body.control_mode,
                display_index=body.display_index,
                fps=body.fps,
                quality=body.quality,
                max_width=body.max_width,
                run_id=body.run_id,
                topic_id=body.topic_id,
                reason=body.reason,
                instructions=body.instructions,
                force=body.force,
            )
            status = manager.get_status()
            return {
                "session": sess.to_public_dict(include_token=True),
                "displays": status["displays"],
                "spaces": status["spaces"],
            }
        except DesktopTakeoverError as exc:
            _raise_http(exc)

    @router.patch("/sessions/{session_id}")
    async def update_desktop_session(
        session_id: str,
        body: DesktopSessionUpdateBody,
    ) -> dict[str, Any]:
        try:
            sess = manager.update_session(
                session_id,
                control_mode=body.control_mode,
                display_index=body.display_index,
                fps=body.fps,
                quality=body.quality,
                max_width=body.max_width,
            )
            return {"session": sess.to_public_dict()}
        except DesktopTakeoverError as exc:
            _raise_http(exc)

    @router.delete("/sessions/{session_id}")
    async def delete_desktop_session(session_id: str) -> dict[str, Any]:
        try:
            ended = manager.end_session(session_id)
            return {"ok": True, "ended": ended}
        except DesktopTakeoverError as exc:
            _raise_http(exc)

    @router.get("/sessions/{session_id}/screenshot")
    async def desktop_screenshot(session_id: str) -> Response:
        try:
            frame = await manager.capture_frame(session_id)
        except DesktopTakeoverError as exc:
            _raise_http(exc)
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"桌面截屏失败: {exc}") from exc
        return Response(
            content=frame.data,
            media_type=frame.mime_type,
            headers={"Cache-Control": "no-store"},
        )

    @router.get("/sessions/{session_id}/clipboard")
    async def desktop_get_clipboard(session_id: str) -> dict[str, Any]:
        try:
            text = await manager.get_clipboard(session_id)
            return {"text": text}
        except DesktopTakeoverError as exc:
            _raise_http(exc)

    @router.post("/sessions/{session_id}/clipboard")
    async def desktop_set_clipboard(session_id: str, body: DesktopClipboardBody) -> dict[str, Any]:
        try:
            await manager.set_clipboard(session_id, body.text)
            return {"ok": True, "text": body.text}
        except DesktopTakeoverError as exc:
            _raise_http(exc)

    @router.post("/sessions/{session_id}/space")
    async def desktop_switch_space(session_id: str, body: DesktopSpaceSwitchBody) -> dict[str, Any]:
        try:
            updated = await manager.switch_space(
                session_id,
                target_index=body.target_index,
                direction=body.direction,
                display_index=body.display_index,
            )
            return {"ok": True, **updated}
        except DesktopTakeoverError as exc:
            _raise_http(exc)

    @router.websocket("/sessions/{session_id}/ws")
    async def desktop_ws(
        websocket: WebSocket,
        session_id: str,
        token: str = Query(default=""),
        api_token: str = Query(default=""),
    ) -> None:
        if configured_api_token:
            auth_header = websocket.headers.get("authorization", "")
            provided_api = auth_header[7:].strip() if auth_header.lower().startswith("bearer ") else api_token.strip()
            if not provided_api or not hmac.compare_digest(provided_api, configured_api_token):
                await websocket.close(code=4401, reason="Unauthorized API token")
                return

        try:
            sess = manager.require_session(session_id, token=token)
        except DesktopUnauthorizedError:
            await websocket.close(code=4401, reason="Invalid desktop session token")
            return
        except DesktopNotFoundError:
            await websocket.close(code=4404, reason="Desktop session not found")
            return

        await websocket.accept()
        manager.set_ws_connected(session_id, True)
        force_refresh = asyncio.Event()
        force_refresh.set()

        await websocket.send_json({"type": "session", "session": sess.to_public_dict()})

        async def _stream_frames() -> None:
            last_sha1 = ""
            while True:
                try:
                    active = manager.require_session(session_id)
                except DesktopTakeoverError:
                    await websocket.close(code=4404, reason="Desktop session ended")
                    return

                try:
                    frame = await manager.capture_frame(session_id)
                    if force_refresh.is_set() or frame.sha1 != last_sha1:
                        force_refresh.clear()
                        last_sha1 = frame.sha1
                        await websocket.send_json(
                            {
                                "type": "frame",
                                "mime_type": frame.mime_type,
                                "width": frame.width,
                                "height": frame.height,
                                "display_index": frame.display_index,
                                "captured_at": frame.captured_at,
                                "data": base64.b64encode(frame.data).decode("ascii"),
                            }
                        )
                except Exception as exc:
                    await websocket.send_json({"type": "error", "message": f"画面捕获失败: {exc}"})

                interval = 1.0 / max(1, min(15, active.fps))
                try:
                    await asyncio.wait_for(force_refresh.wait(), timeout=interval)
                except asyncio.TimeoutError:
                    pass

        sender_task = asyncio.create_task(_stream_frames())
        try:
            while True:
                msg = await websocket.receive_json()
                msg_type = str(msg.get("type") or "")
                if msg_type == "input":
                    try:
                        await manager.handle_input(session_id, msg.get("payload") or {})
                        force_refresh.set()
                    except Exception as exc:
                        await websocket.send_json({"type": "error", "message": str(exc)})
                elif msg_type == "config":
                    payload = msg.get("payload") or {}
                    try:
                        updated = manager.update_session(
                            session_id,
                            control_mode=payload.get("control_mode"),
                            display_index=payload.get("display_index"),
                            fps=payload.get("fps"),
                            quality=payload.get("quality"),
                            max_width=payload.get("max_width"),
                        )
                        force_refresh.set()
                        await websocket.send_json({"type": "session", "session": updated.to_public_dict()})
                    except Exception as exc:
                        await websocket.send_json({"type": "error", "message": str(exc)})
                elif msg_type == "switch_space":
                    payload = msg.get("payload") or {}
                    try:
                        updated = await manager.switch_space(
                            session_id,
                            target_index=payload.get("target_index"),
                            direction=payload.get("direction"),
                            display_index=payload.get("display_index"),
                        )
                        force_refresh.set()
                        await websocket.send_json({"type": "desktops", **updated})
                    except Exception as exc:
                        await websocket.send_json({"type": "error", "message": str(exc)})
                elif msg_type == "refresh":
                    force_refresh.set()
                elif msg_type == "ping":
                    await websocket.send_json({"type": "pong", "ts": msg.get("ts")})
        except WebSocketDisconnect:
            pass
        finally:
            sender_task.cancel()
            try:
                await sender_task
            except (asyncio.CancelledError, Exception):
                pass
            manager.set_ws_connected(session_id, False)

    return router

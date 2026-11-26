"""Session lifecycle, exclusive lock, token auth, and SSE sync for Desktop Takeover."""

from __future__ import annotations

import asyncio
import hmac
import secrets
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from ..sse import EventBus
from .models import (
    CapturedFrame,
    ControlMode,
    DesktopCapabilities,
    DesktopDriver,
    DesktopInputEvent,
)


def _utc_iso(ts: Optional[float] = None) -> str:
    dt = datetime.fromtimestamp(ts if ts is not None else time.time(), tz=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class DesktopTakeoverError(RuntimeError):
    """Base error for desktop takeover operations."""

    status_code: int = 400


class DesktopDisabledError(DesktopTakeoverError):
    status_code = 403


class DesktopConflictError(DesktopTakeoverError):
    status_code = 409


class DesktopNotFoundError(DesktopTakeoverError):
    status_code = 404


class DesktopUnauthorizedError(DesktopTakeoverError):
    status_code = 401


@dataclass
class DesktopSession:
    """State of an active server desktop takeover session."""

    id: str
    session_token: str
    control_mode: ControlMode
    display_index: int
    fps: int
    quality: int
    max_width: int
    idle_timeout_seconds: int
    created_at_ts: float = field(default_factory=time.time)
    last_activity_ts: float = field(default_factory=time.time)
    run_id: Optional[str] = None
    topic_id: Optional[str] = None
    reason: Optional[str] = None
    instructions: Optional[str] = None
    action_count: int = 0
    ws_connected: bool = False

    def is_expired(self, now: Optional[float] = None) -> bool:
        t = now if now is not None else time.time()
        return (t - self.last_activity_ts) >= self.idle_timeout_seconds

    def touch(self) -> None:
        self.last_activity_ts = time.time()

    def to_public_dict(self, *, include_token: bool = False) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": self.id,
            "control_mode": self.control_mode,
            "display_index": self.display_index,
            "fps": self.fps,
            "quality": self.quality,
            "max_width": self.max_width,
            "idle_timeout_seconds": self.idle_timeout_seconds,
            "created_at": _utc_iso(self.created_at_ts),
            "last_activity_at": _utc_iso(self.last_activity_ts),
            "expires_at": _utc_iso(self.last_activity_ts + self.idle_timeout_seconds),
            "run_id": self.run_id,
            "topic_id": self.topic_id,
            "reason": self.reason,
            "instructions": self.instructions,
            "action_count": self.action_count,
            "ws_connected": self.ws_connected,
        }
        if include_token:
            payload["ws_token"] = self.session_token
        return payload


class DesktopTakeoverManager:
    """Coordinates exclusive desktop takeover sessions, permissions, and SSE notifications."""

    def __init__(
        self,
        driver: DesktopDriver,
        event_bus: EventBus,
        *,
        enabled: bool = True,
        idle_timeout_seconds: int = 900,
        default_fps: int = 4,
        default_max_width: int = 1280,
    ) -> None:
        self.driver = driver
        self.event_bus = event_bus
        self.enabled = enabled
        self.idle_timeout_seconds = max(30, idle_timeout_seconds)
        self.default_fps = max(1, min(15, default_fps))
        self.default_max_width = max(640, min(2560, default_max_width))
        self._active_session: Optional[DesktopSession] = None

    def _evict_if_expired(self) -> None:
        if self._active_session and self._active_session.is_expired():
            expired = self._active_session
            self._active_session = None
            self._publish_session_event("expired", expired)

    def _publish_session_event(self, state: str, session: Optional[DesktopSession]) -> None:
        self.event_bus.publish(
            "desktop_session_updated",
            {
                "state": state,
                "active": self._active_session is not None,
                "session": self._active_session.to_public_dict() if self._active_session else None,
                "ended_session_id": session.id if session and self._active_session is None else None,
            },
        )
        if session and session.run_id:
            self.event_bus.append_raw_log(
                session.run_id,
                f"[desktop_takeover] session={session.id} state={state} mode={session.control_mode} actions={session.action_count}",
                stream="stdout",
            )

    def get_status(self) -> dict[str, Any]:
        self._evict_if_expired()
        caps: DesktopCapabilities = self.driver.probe()
        return {
            "enabled": self.enabled,
            "available": self.enabled and caps.available,
            "driver": caps.driver_name,
            "platform": caps.platform,
            "supports_control": caps.supports_control,
            "supports_clipboard": caps.supports_clipboard,
            "displays": [d.to_dict() for d in caps.displays],
            "spaces": [s.to_dict() for s in caps.spaces],
            "supports_space_switch": caps.supports_space_switch,
            "permissions": caps.permissions.to_dict(),
            "error": None if self.enabled else "服务端已禁用桌面接管 (KIKI_DESKTOP_TAKEOVER_ENABLED=false)",
            "driver_error": caps.error,
            "active_session": self._active_session.to_public_dict() if self._active_session else None,
        }

    def get_active_session(self) -> Optional[DesktopSession]:
        self._evict_if_expired()
        return self._active_session

    def start_session(
        self,
        *,
        control_mode: ControlMode = "control",
        display_index: int = 0,
        fps: Optional[int] = None,
        quality: int = 75,
        max_width: Optional[int] = None,
        run_id: Optional[str] = None,
        topic_id: Optional[str] = None,
        reason: Optional[str] = None,
        instructions: Optional[str] = None,
        force: bool = False,
    ) -> DesktopSession:
        if not self.enabled:
            raise DesktopDisabledError("服务端已禁用桌面接管功能")
        self._evict_if_expired()
        caps = self.driver.probe()
        if not caps.available:
            raise DesktopTakeoverError(caps.error or "当前服务器环境不支持桌面接管")

        if self._active_session is not None:
            if not force:
                raise DesktopConflictError("已有正在进行的桌面接管会话，请先结束或传入 force=true 强制接管")
            prev = self._active_session
            self._active_session = None
            self._publish_session_event("replaced", prev)

        effective_mode: ControlMode = control_mode if (control_mode == "view" or caps.supports_control) else "view"
        max_display = max(0, len(caps.displays) - 1)
        session = DesktopSession(
            id=f"desk-{uuid.uuid4().hex[:10]}",
            session_token=secrets.token_urlsafe(24),
            control_mode=effective_mode,
            display_index=min(max(0, display_index), max_display),
            fps=max(1, min(15, fps if fps is not None else self.default_fps)),
            quality=max(30, min(95, quality)),
            max_width=max(640, min(2560, max_width if max_width is not None else self.default_max_width)),
            idle_timeout_seconds=self.idle_timeout_seconds,
            run_id=run_id,
            topic_id=topic_id,
            reason=reason,
            instructions=instructions,
        )
        self._active_session = session
        self._publish_session_event("started", session)
        return session

    def require_session(self, session_id: str, *, token: Optional[str] = None) -> DesktopSession:
        self._evict_if_expired()
        sess = self._active_session
        if sess is None or sess.id != session_id:
            raise DesktopNotFoundError("桌面接管会话不存在或已过期")
        if token is not None and not hmac.compare_digest(token, sess.session_token):
            raise DesktopUnauthorizedError("无效的桌面会话令牌")
        return sess

    def update_session(
        self,
        session_id: str,
        *,
        control_mode: Optional[ControlMode] = None,
        display_index: Optional[int] = None,
        fps: Optional[int] = None,
        quality: Optional[int] = None,
        max_width: Optional[int] = None,
    ) -> DesktopSession:
        sess = self.require_session(session_id)
        if control_mode is not None:
            if control_mode == "control" and not self.driver.probe().supports_control:
                raise DesktopTakeoverError("系统辅助功能权限未开启，暂时只能以只读模式查看桌面")
            sess.control_mode = control_mode
        if display_index is not None:
            caps = self.driver.probe()
            max_display = max(0, len(caps.displays) - 1)
            sess.display_index = min(max(0, display_index), max_display)
        if fps is not None:
            sess.fps = max(1, min(15, fps))
        if quality is not None:
            sess.quality = max(30, min(95, quality))
        if max_width is not None:
            sess.max_width = max(640, min(2560, max_width))
        sess.touch()
        self._publish_session_event("updated", sess)
        return sess

    def end_session(self, session_id: Optional[str] = None) -> Optional[dict[str, Any]]:
        self._evict_if_expired()
        sess = self._active_session
        if sess is None:
            return None
        if session_id is not None and sess.id != session_id:
            raise DesktopNotFoundError("桌面接管会话不存在或已结束")
        self._active_session = None
        summary = sess.to_public_dict()
        self._publish_session_event("ended", sess)
        return summary

    def set_ws_connected(self, session_id: str, connected: bool) -> None:
        if self._active_session and self._active_session.id == session_id:
            self._active_session.ws_connected = connected
            self._active_session.touch()
            self._publish_session_event("connected" if connected else "disconnected", self._active_session)

    async def capture_frame(self, session_id: str) -> CapturedFrame:
        sess = self.require_session(session_id)
        return await asyncio.to_thread(
            self.driver.capture_frame,
            sess.display_index,
            sess.max_width,
            sess.quality,
        )

    async def handle_input(self, session_id: str, payload: dict[str, Any]) -> None:
        sess = self.require_session(session_id)
        if sess.control_mode != "control":
            raise DesktopTakeoverError("当前处于只读观察模式，请先切换为「接管控制」模式")
        event = DesktopInputEvent.from_payload(payload, default_display=sess.display_index)
        await asyncio.to_thread(self.driver.send_input, event)
        sess.action_count += 1
        sess.touch()

    async def get_clipboard(self, session_id: str) -> str:
        sess = self.require_session(session_id)
        sess.touch()
        return await asyncio.to_thread(self.driver.get_clipboard)

    async def set_clipboard(self, session_id: str, text: str) -> None:
        sess = self.require_session(session_id)
        if sess.control_mode != "control":
            raise DesktopTakeoverError("当前处于只读观察模式，无法写入服务器剪贴板")
        await asyncio.to_thread(self.driver.set_clipboard, text)
        sess.action_count += 1
        sess.touch()

    async def switch_space(
        self,
        session_id: str,
        *,
        target_index: Optional[int] = None,
        direction: Optional[str] = None,
        display_index: Optional[int] = None,
    ) -> dict[str, Any]:
        sess = self.require_session(session_id)
        if sess.control_mode != "control":
            raise DesktopTakeoverError("当前处于只读观察模式，请先切换为「接管控制」模式再切换桌面空间")
        d_idx = sess.display_index if display_index is None else max(0, display_index)
        norm_dir = direction if direction in ("prev", "next") else None
        await asyncio.to_thread(
            self.driver.switch_space,
            display_index=d_idx,
            target_index=target_index,
            direction=norm_dir,  # type: ignore[arg-type]
        )
        sess.action_count += 1
        sess.touch()
        caps = await asyncio.to_thread(self.driver.probe)
        return {
            "displays": [d.to_dict() for d in caps.displays],
            "spaces": [s.to_dict() for s in caps.spaces],
        }

    async def shutdown(self) -> None:
        if self._active_session is not None:
            self.end_session()

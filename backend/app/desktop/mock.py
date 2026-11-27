"""Deterministic mock desktop driver for tests and headless environments."""

from __future__ import annotations

import hashlib
import struct
import time
import zlib
from typing import Literal, Optional

from .models import (
    CapturedFrame,
    DesktopCapabilities,
    DesktopInputEvent,
    DesktopPermissions,
    DesktopSpaceInfo,
    DisplayInfo,
)


def _make_solid_png(width: int, height: int, rgb: tuple[int, int, int]) -> bytes:
    """Create a tiny valid RGB PNG using only Python stdlib (zlib + struct)."""
    r, g, b = rgb
    row = bytes([0]) + bytes([r, g, b]) * width
    raw = row * height

    def _chunk(tag: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(tag + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", crc)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", ihdr)
        + _chunk(b"IDAT", zlib.compress(raw, level=6))
        + _chunk(b"IEND", b"")
    )


class MockDesktopDriver:
    """In-memory desktop driver that records inputs and returns synthetic PNG frames."""

    driver_name = "mock"

    def __init__(
        self,
        width: int = 1280,
        height: int = 800,
        *,
        available: bool = True,
        screen_recording: bool = True,
        accessibility: bool = True,
        displays_count: int = 1,
        spaces_per_display: int = 1,
    ) -> None:
        self.width = width
        self.height = height
        self.available = available
        self.screen_recording = screen_recording
        self.accessibility = accessibility
        self.displays_count = max(1, displays_count)
        self.spaces_per_display = max(1, spaces_per_display)
        self.active_space_by_display: dict[int, int] = {i: 0 for i in range(self.displays_count)}
        self.recorded_events: list[DesktopInputEvent] = []
        self.opened_urls: list[str] = []
        self.opened_apps: list[str] = []
        self.clipboard_text: str = ""
        self._frame_seq: int = 0
        self._static_frame: Optional[bytes] = None
        self.dynamic_frames: bool = False

    def _build_displays(self) -> tuple[DisplayInfo, ...]:
        if not self.available:
            return ()
        return tuple(
            DisplayInfo(
                index=i,
                display_id=i + 1,
                name="主显示器 (1)" if i == 0 else f"扩展显示器 ({i + 1})",
                width=self.width,
                height=self.height,
                origin_x=float(i * self.width),
                origin_y=0.0,
                is_main=(i == 0),
            )
            for i in range(self.displays_count)
        )

    def _build_spaces(self) -> tuple[DesktopSpaceInfo, ...]:
        if not self.available:
            return ()
        items: list[DesktopSpaceInfo] = []
        for d_idx in range(self.displays_count):
            active_idx = self.active_space_by_display.get(d_idx, 0)
            for s_idx in range(self.spaces_per_display):
                items.append(
                    DesktopSpaceInfo(
                        index=s_idx,
                        space_id=f"mock-d{d_idx}-s{s_idx}",
                        display_index=d_idx,
                        name=f"桌面 {s_idx + 1}",
                        is_active=(s_idx == active_idx),
                        is_fullscreen=False,
                    )
                )
        return tuple(items)

    def probe(self) -> DesktopCapabilities:
        warnings: list[str] = []
        if not self.screen_recording:
            warnings.append("屏幕录制权限未开启（Mock 模拟状态）")
        if not self.accessibility:
            warnings.append("辅助功能权限未开启（Mock 模拟状态）")
        return DesktopCapabilities(
            available=self.available,
            driver_name=self.driver_name,
            platform="mock",
            supports_control=self.accessibility,
            supports_clipboard=True,
            displays=self._build_displays(),
            spaces=self._build_spaces(),
            supports_space_switch=self.accessibility,
            permissions=DesktopPermissions(
                screen_recording=self.screen_recording,
                accessibility=self.accessibility,
                warnings=tuple(warnings),
            ),
            error=None if self.available else "Mock desktop unavailable",
        )

    def capture_frame(
        self,
        display_index: int = 0,
        max_width: int = 1280,
        quality: int = 75,
    ) -> CapturedFrame:
        if not self.available:
            raise RuntimeError("Mock desktop unavailable")
        if self.dynamic_frames or self._static_frame is None:
            self._frame_seq += 1
            tone = (38 + (self._frame_seq * 17) % 160, 44, 52)
            data = _make_solid_png(64, 40, tone)
            if not self.dynamic_frames:
                self._static_frame = data
        else:
            data = self._static_frame

        return CapturedFrame(
            data=data,
            mime_type="image/png",
            width=min(self.width, max(64, max_width)),
            height=self.height,
            display_index=display_index,
            sha1=hashlib.sha1(data).hexdigest(),
            captured_at=time.time(),
        )

    def send_input(self, event: DesktopInputEvent) -> None:
        if not self.available:
            raise RuntimeError("Mock desktop unavailable")
        if not self.accessibility:
            raise PermissionError("Accessibility permission required")
        self.recorded_events.append(event)

    def get_clipboard(self) -> str:
        return self.clipboard_text

    def set_clipboard(self, text: str) -> None:
        self.clipboard_text = text

    def switch_space(
        self,
        *,
        display_index: int = 0,
        target_index: Optional[int] = None,
        direction: Optional[Literal["prev", "next"]] = None,
    ) -> None:
        if not self.available:
            raise RuntimeError("Mock desktop unavailable")
        if not self.accessibility:
            raise PermissionError("Accessibility permission required")
        d_idx = min(max(0, display_index), self.displays_count - 1)
        cur = self.active_space_by_display.get(d_idx, 0)
        if direction == "prev":
            nxt = max(0, cur - 1)
        elif direction == "next":
            nxt = min(self.spaces_per_display - 1, cur + 1)
        elif target_index is not None:
            nxt = min(max(0, target_index), self.spaces_per_display - 1)
        else:
            nxt = cur
        self.active_space_by_display[d_idx] = nxt
        self._static_frame = None

    def open_url(self, url: str) -> None:
        if not self.available:
            raise RuntimeError("Mock desktop unavailable")
        cleaned = (url or "").strip()
        if not cleaned.lower().startswith(("http://", "https://")):
            raise ValueError(f"Only http:// and https:// URLs are allowed: {url!r}")
        self.opened_urls.append(cleaned)

    def open_app(self, app_name: str) -> None:
        if not self.available:
            raise RuntimeError("Mock desktop unavailable")
        cleaned = (app_name or "").strip()
        if (
            not cleaned
            or cleaned.startswith("-")
            or "/" in cleaned
            or ".." in cleaned
            or any(ord(ch) < 32 or ord(ch) == 127 for ch in cleaned)
        ):
            raise ValueError(f"Invalid application name: {app_name!r}")
        self.opened_apps.append(cleaned)

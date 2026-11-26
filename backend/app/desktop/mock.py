"""Deterministic mock desktop driver for tests and headless environments."""

from __future__ import annotations

import hashlib
import struct
import time
import zlib
from typing import Optional

from .models import (
    CapturedFrame,
    DesktopCapabilities,
    DesktopInputEvent,
    DesktopPermissions,
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
    ) -> None:
        self.width = width
        self.height = height
        self.available = available
        self.screen_recording = screen_recording
        self.accessibility = accessibility
        self.recorded_events: list[DesktopInputEvent] = []
        self.clipboard_text: str = ""
        self._frame_seq: int = 0
        self._static_frame: Optional[bytes] = None
        self.dynamic_frames: bool = False

    def probe(self) -> DesktopCapabilities:
        warnings: list[str] = []
        if not self.screen_recording:
            warnings.append("屏幕录制权限未开启（Mock 模拟状态）")
        if not self.accessibility:
            warnings.append("辅助功能权限未开启（Mock 模拟状态）")
        display = DisplayInfo(
            index=0,
            display_id=1,
            name="Kiki Mock Display",
            width=self.width,
            height=self.height,
            is_main=True,
        )
        return DesktopCapabilities(
            available=self.available,
            driver_name=self.driver_name,
            platform="mock",
            supports_control=self.accessibility,
            supports_clipboard=True,
            displays=(display,) if self.available else (),
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

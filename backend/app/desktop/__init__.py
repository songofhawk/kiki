"""Kiki Server Desktop Takeover package (macOS native + Mock drivers)."""

from __future__ import annotations

import platform

from .computer_use import (
    COMPUTER_USE_TOOL_DECLARATIONS,
    COMPUTER_USE_TOOL_NAMES,
    ComputerUseExecutor,
    ComputerUseResult,
)
from .cua_bridge import CuaDriverBridge, CuaDriverStatus
from .macos import MacOSDesktopDriver
from .macos_a11y import MacOSA11yEngine
from .manager import (
    DesktopConflictError,
    DesktopDisabledError,
    DesktopNotFoundError,
    DesktopSession,
    DesktopTakeoverError,
    DesktopTakeoverManager,
    DesktopUnauthorizedError,
)
from .mock import MockDesktopDriver
from .models import (
    AXActionResult,
    CapturedFrame,
    ControlMode,
    DesktopCapabilities,
    DesktopDriver,
    DesktopInputAction,
    DesktopInputEvent,
    DesktopPermissions,
    DesktopSpaceInfo,
    DisplayInfo,
    MouseButton,
    UIElementNode,
    UITreeSnapshot,
)


def create_desktop_driver(
    driver_name: str = "auto",
    *,
    cua_mode: str = "auto",
    cua_bin: str | None = None,
) -> DesktopDriver:
    """Instantiate the configured desktop driver (auto -> macOS on Darwin, else Mock)."""
    name = (driver_name or "auto").strip().lower()
    if name == "mock":
        return MockDesktopDriver()
    if name == "macos":
        return MacOSDesktopDriver(cua_mode=cua_mode, cua_bin=cua_bin)
    if platform.system() == "Darwin":
        return MacOSDesktopDriver(cua_mode=cua_mode, cua_bin=cua_bin)
    return MockDesktopDriver(available=False)


__all__ = [
    "AXActionResult",
    "COMPUTER_USE_TOOL_DECLARATIONS",
    "COMPUTER_USE_TOOL_NAMES",
    "CapturedFrame",
    "ComputerUseExecutor",
    "ComputerUseResult",
    "ControlMode",
    "CuaDriverBridge",
    "CuaDriverStatus",
    "DesktopCapabilities",
    "DesktopConflictError",
    "DesktopDisabledError",
    "DesktopDriver",
    "DesktopInputAction",
    "DesktopInputEvent",
    "DesktopNotFoundError",
    "DesktopPermissions",
    "DesktopSession",
    "DesktopSpaceInfo",
    "DesktopTakeoverError",
    "DesktopTakeoverManager",
    "DesktopUnauthorizedError",
    "DisplayInfo",
    "MacOSA11yEngine",
    "MacOSDesktopDriver",
    "MockDesktopDriver",
    "MouseButton",
    "UIElementNode",
    "UITreeSnapshot",
    "create_desktop_driver",
]

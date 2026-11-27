"""Kiki Server Desktop Takeover package (macOS native + Mock drivers)."""

from __future__ import annotations

import platform

from .computer_use import (
    COMPUTER_USE_TOOL_DECLARATIONS,
    COMPUTER_USE_TOOL_NAMES,
    ComputerUseExecutor,
    ComputerUseResult,
)
from .macos import MacOSDesktopDriver
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
)


def create_desktop_driver(driver_name: str = "auto") -> DesktopDriver:
    """Instantiate the configured desktop driver (auto -> macOS on Darwin, else Mock)."""
    name = (driver_name or "auto").strip().lower()
    if name == "mock":
        return MockDesktopDriver()
    if name == "macos":
        return MacOSDesktopDriver()
    if platform.system() == "Darwin":
        return MacOSDesktopDriver()
    return MockDesktopDriver(available=False)


__all__ = [
    "COMPUTER_USE_TOOL_DECLARATIONS",
    "COMPUTER_USE_TOOL_NAMES",
    "CapturedFrame",
    "ComputerUseExecutor",
    "ComputerUseResult",
    "ControlMode",
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
    "MacOSDesktopDriver",
    "MockDesktopDriver",
    "MouseButton",
    "create_desktop_driver",
]

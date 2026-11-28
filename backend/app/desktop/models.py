"""Data models and driver protocol for Kiki Server Desktop Takeover."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal, Optional, Protocol, runtime_checkable

ControlMode = Literal["view", "control"]
MouseButton = Literal["left", "right", "middle"]
DesktopInputAction = Literal[
    "mouse_move",
    "mouse_down",
    "mouse_up",
    "mouse_click",
    "mouse_double_click",
    "mouse_scroll",
    "key_down",
    "key_up",
    "key_tap",
    "key_combo",
    "type_text",
]


@dataclass(frozen=True)
class DisplayInfo:
    """Describes one physical/virtual display on the Kiki server."""

    index: int
    display_id: int
    name: str
    width: int
    height: int
    origin_x: float = 0.0
    origin_y: float = 0.0
    is_main: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class DesktopSpaceInfo:
    """Describes one virtual desktop / Mission Control Space on a display."""

    index: int
    space_id: str
    display_index: int
    name: str
    is_active: bool = False
    is_fullscreen: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class DesktopPermissions:
    """OS-level screen recording and accessibility permission status."""

    screen_recording: bool = True
    accessibility: bool = True
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "screen_recording": self.screen_recording,
            "accessibility": self.accessibility,
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True)
class DesktopCapabilities:
    """Reports what the active desktop driver supports."""

    available: bool
    driver_name: str
    platform: str
    supports_control: bool
    supports_clipboard: bool
    displays: tuple[DisplayInfo, ...] = ()
    spaces: tuple[DesktopSpaceInfo, ...] = ()
    supports_space_switch: bool = False
    supports_a11y: bool = False
    cua_driver_available: bool = False
    permissions: DesktopPermissions = field(default_factory=DesktopPermissions)
    error: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "driver_name": self.driver_name,
            "platform": self.platform,
            "supports_control": self.supports_control,
            "supports_clipboard": self.supports_clipboard,
            "displays": [d.to_dict() for d in self.displays],
            "spaces": [s.to_dict() for s in self.spaces],
            "supports_space_switch": self.supports_space_switch,
            "supports_a11y": self.supports_a11y,
            "cua_driver_available": self.cua_driver_available,
            "permissions": self.permissions.to_dict(),
            "error": self.error,
        }


@dataclass(frozen=True)
class UIElementNode:
    """One semantic UI element from the OS Accessibility (A11y) tree or CUA driver."""

    ref: str
    role: str
    subrole: str = ""
    title: str = ""
    description: str = ""
    value: str = ""
    enabled: bool = True
    focused: bool = False
    actions: tuple[str, ...] = ()
    bounds: Optional[tuple[float, float, float, float]] = None
    center: Optional[tuple[float, float]] = None
    depth: int = 0

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"ref": self.ref, "role": self.role}
        if self.subrole:
            payload["subrole"] = self.subrole
        if self.title:
            payload["title"] = self.title
        if self.description and self.description != self.title:
            payload["description"] = self.description
        if self.value and self.value != self.title:
            payload["value"] = self.value
        if not self.enabled:
            payload["enabled"] = False
        if self.focused:
            payload["focused"] = True
        if self.actions:
            payload["actions"] = list(self.actions)
        if self.bounds is not None:
            payload["bounds"] = list(self.bounds)
        if self.center is not None:
            payload["center"] = list(self.center)
        return payload


@dataclass(frozen=True)
class UITreeSnapshot:
    """A structured A11y/CUA UI tree snapshot for a target application/display."""

    app_name: str
    pid: Optional[int]
    display_index: int
    backend: str
    elements: tuple[UIElementNode, ...]
    truncated: bool = False
    captured_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "app_name": self.app_name,
            "pid": self.pid,
            "display_index": self.display_index,
            "backend": self.backend,
            "coord_space": "0..1000",
            "count": len(self.elements),
            "truncated": self.truncated,
            "elements": [el.to_dict() for el in self.elements],
        }


@dataclass(frozen=True)
class AXActionResult:
    """Result of an A11y/CUA semantic action on a UI element reference."""

    ok: bool
    ref: str
    action: str
    backend: str
    method: str
    center: Optional[tuple[float, float]] = None
    display_index: int = 0
    error: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "ok": self.ok,
            "ref": self.ref,
            "action": self.action,
            "backend": self.backend,
            "method": self.method,
            "display_index": self.display_index,
        }
        if self.center is not None:
            payload["center"] = list(self.center)
        if self.error:
            payload["error"] = self.error
        return payload


@dataclass(frozen=True)
class CapturedFrame:
    """One captured desktop frame ready for HTTP or WebSocket streaming."""

    data: bytes
    mime_type: str
    width: int
    height: int
    display_index: int
    sha1: str
    captured_at: float


@dataclass(frozen=True)
class DesktopInputEvent:
    """Normalized remote pointer or keyboard event from the browser UI."""

    action: DesktopInputAction
    # Normalized [0.0, 1.0] coordinates relative to the active display
    x: Optional[float] = None
    y: Optional[float] = None
    button: MouseButton = "left"
    delta_x: float = 0.0
    delta_y: float = 0.0
    key: Optional[str] = None
    modifiers: tuple[str, ...] = ()
    text: Optional[str] = None
    display_index: int = 0

    @classmethod
    def from_payload(cls, payload: dict[str, Any], default_display: int = 0) -> "DesktopInputEvent":
        action = str(payload.get("action") or "").strip()
        if action not in {
            "mouse_move",
            "mouse_down",
            "mouse_up",
            "mouse_click",
            "mouse_double_click",
            "mouse_scroll",
            "key_down",
            "key_up",
            "key_tap",
            "key_combo",
            "type_text",
        }:
            raise ValueError(f"Unsupported desktop input action: {action!r}")

        def _norm_coord(raw: Any) -> Optional[float]:
            if raw is None:
                return None
            val = float(raw)
            return max(0.0, min(1.0, val))

        button = str(payload.get("button") or "left").lower()
        if button not in ("left", "right", "middle"):
            button = "left"

        raw_mods = payload.get("modifiers") or []
        mods = tuple(str(m).strip().lower() for m in raw_mods if str(m).strip())

        raw_display = payload.get("display_index")
        display_index = int(raw_display) if raw_display is not None else default_display

        return cls(
            action=action,  # type: ignore[arg-type]
            x=_norm_coord(payload.get("x")),
            y=_norm_coord(payload.get("y")),
            button=button,  # type: ignore[arg-type]
            delta_x=float(payload.get("delta_x") or 0.0),
            delta_y=float(payload.get("delta_y") or 0.0),
            key=str(payload["key"]) if payload.get("key") is not None else None,
            modifiers=mods,
            text=str(payload["text"]) if payload.get("text") is not None else None,
            display_index=max(0, display_index),
        )


@runtime_checkable
class DesktopDriver(Protocol):
    """Pluggable OS desktop capture and input injection driver."""

    driver_name: str

    def probe(self) -> DesktopCapabilities: ...

    def capture_frame(
        self,
        display_index: int = 0,
        max_width: int = 1280,
        quality: int = 75,
    ) -> CapturedFrame: ...

    def send_input(self, event: DesktopInputEvent) -> None: ...

    def get_clipboard(self) -> str: ...

    def set_clipboard(self, text: str) -> None: ...

    def switch_space(
        self,
        *,
        display_index: int = 0,
        target_index: Optional[int] = None,
        direction: Optional[Literal["prev", "next"]] = None,
    ) -> None: ...

    def open_url(self, url: str) -> None: ...

    def open_app(self, app_name: str) -> None: ...

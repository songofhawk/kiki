"""macOS native desktop driver using CoreGraphics (ctypes), screencapture, and pbcopy/pbpaste."""

from __future__ import annotations

import ctypes
import hashlib
import os
import platform
import plistlib
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Literal, Optional

from .cua_bridge import CuaDriverBridge
from .macos_a11y import MacOSA11yEngine
from .models import (
    AXActionResult,
    CapturedFrame,
    DesktopCapabilities,
    DesktopInputEvent,
    DesktopPermissions,
    DesktopSpaceInfo,
    DisplayInfo,
    UIElementNode,
    UITreeSnapshot,
)

# CoreGraphics constants
_KCG_HID_EVENT_TAP = 0
_KCG_SCROLL_PIXEL = 0
_KCG_MOUSE_LEFT = 0
_KCG_MOUSE_RIGHT = 1
_KCG_MOUSE_CENTER = 2

_EVT_LEFT_DOWN, _EVT_LEFT_UP, _EVT_LEFT_DRAG = 1, 2, 6
_EVT_RIGHT_DOWN, _EVT_RIGHT_UP, _EVT_RIGHT_DRAG = 3, 4, 7
_EVT_MOUSE_MOVED = 5
_EVT_OTHER_DOWN, _EVT_OTHER_UP = 25, 26

_FLAG_SHIFT = 0x00020000
_FLAG_CTRL = 0x00040000
_FLAG_ALT = 0x00080000
_FLAG_META = 0x00100000

_MODIFIER_FLAGS: dict[str, int] = {
    "shift": _FLAG_SHIFT,
    "control": _FLAG_CTRL,
    "ctrl": _FLAG_CTRL,
    "alt": _FLAG_ALT,
    "option": _FLAG_ALT,
    "meta": _FLAG_META,
    "cmd": _FLAG_META,
    "command": _FLAG_META,
}

# macOS virtual keycodes (ANSI US layout + navigation/control keys)
_KEYCODES: dict[str, int] = {
    "a": 0, "s": 1, "d": 2, "f": 3, "h": 4, "g": 5, "z": 6, "x": 7, "c": 8, "v": 9,
    "b": 11, "q": 12, "w": 13, "e": 14, "r": 15, "y": 16, "t": 17, "1": 18, "2": 19,
    "3": 20, "4": 21, "6": 22, "5": 23, "=": 24, "9": 25, "7": 26, "-": 27, "8": 28,
    "0": 29, "]": 30, "o": 31, "u": 32, "[": 33, "i": 34, "p": 35, "enter": 36,
    "return": 36, "l": 37, "j": 38, "'": 39, "k": 40, ";": 41, "\\": 42, ",": 43,
    "/": 44, "n": 45, "m": 46, ".": 47, "tab": 48, " ": 49, "space": 49, "`": 50,
    "backspace": 51, "escape": 53, "esc": 53, "delete": 117, "home": 115, "end": 119,
    "pageup": 116, "pagedown": 121, "arrowleft": 123, "arrowright": 124,
    "arrowdown": 125, "arrowup": 126, "f1": 122, "f2": 120, "f3": 99, "f4": 118,
    "f5": 96, "f6": 97, "f7": 98, "f8": 100, "f9": 101, "f10": 109, "f11": 103, "f12": 111,
}


class _CGPoint(ctypes.Structure):
    _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double)]


class _CGSize(ctypes.Structure):
    _fields_ = [("width", ctypes.c_double), ("height", ctypes.c_double)]


class _CGRect(ctypes.Structure):
    _fields_ = [("origin", _CGPoint), ("size", _CGSize)]


class MacOSDesktopDriver:
    """Zero-dependency macOS desktop capture, A11y tree, optional CUA bridge & CoreGraphics driver."""

    driver_name = "macos"

    def __init__(self, *, cua_mode: str = "auto", cua_bin: Optional[str] = None) -> None:
        self._cg: Any = None
        self._cf: Any = None
        self._ax: Any = None
        self._pressed_buttons: set[str] = set()
        self._screencapture = shutil.which("screencapture") or "/usr/sbin/screencapture"
        self._sips = shutil.which("sips") or "/usr/bin/sips"
        self._load_frameworks()
        self._a11y = MacOSA11yEngine()
        self._cua = CuaDriverBridge(mode=cua_mode, binary=cua_bin)

    def _load_frameworks(self) -> None:
        if platform.system() != "Darwin":
            return
        try:
            cg = ctypes.CDLL("/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")
            cg.CGMainDisplayID.restype = ctypes.c_uint32
            cg.CGGetActiveDisplayList.argtypes = [ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32), ctypes.POINTER(ctypes.c_uint32)]
            cg.CGGetActiveDisplayList.restype = ctypes.c_int32
            cg.CGDisplayBounds.argtypes = [ctypes.c_uint32]
            cg.CGDisplayBounds.restype = _CGRect
            cg.CGWindowListCopyWindowInfo.argtypes = [ctypes.c_uint32, ctypes.c_uint32]
            cg.CGWindowListCopyWindowInfo.restype = ctypes.c_void_p
            cg.CGEventCreateMouseEvent.argtypes = [ctypes.c_void_p, ctypes.c_uint32, _CGPoint, ctypes.c_uint32]
            cg.CGEventCreateMouseEvent.restype = ctypes.c_void_p
            cg.CGEventCreateKeyboardEvent.argtypes = [ctypes.c_void_p, ctypes.c_uint16, ctypes.c_bool]
            cg.CGEventCreateKeyboardEvent.restype = ctypes.c_void_p
            cg.CGEventCreateScrollWheelEvent.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_int32, ctypes.c_int32]
            cg.CGEventCreateScrollWheelEvent.restype = ctypes.c_void_p
            cg.CGEventSetFlags.argtypes = [ctypes.c_void_p, ctypes.c_uint64]
            cg.CGEventSetIntegerValueField.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int64]
            cg.CGEventKeyboardSetUnicodeString.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_uint16)]
            cg.CGEventPost.argtypes = [ctypes.c_uint32, ctypes.c_void_p]
            cg.CFRelease.argtypes = [ctypes.c_void_p]
            if hasattr(cg, "CGPreflightScreenCaptureAccess"):
                cg.CGPreflightScreenCaptureAccess.restype = ctypes.c_bool
            self._cg = cg
        except OSError:
            self._cg = None
        try:
            cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
            cf.CFArrayGetCount.argtypes = [ctypes.c_void_p]
            cf.CFArrayGetCount.restype = ctypes.c_long
            cf.CFArrayGetValueAtIndex.argtypes = [ctypes.c_void_p, ctypes.c_long]
            cf.CFArrayGetValueAtIndex.restype = ctypes.c_void_p
            cf.CFDictionaryGetValue.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
            cf.CFDictionaryGetValue.restype = ctypes.c_void_p
            cf.CFStringCreateWithCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32]
            cf.CFStringCreateWithCString.restype = ctypes.c_void_p
            cf.CFStringGetCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_long, ctypes.c_uint32]
            cf.CFStringGetCString.restype = ctypes.c_bool
            cf.CFNumberGetValue.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]
            cf.CFNumberGetValue.restype = ctypes.c_bool
            cf.CFRelease.argtypes = [ctypes.c_void_p]
            self._cf = cf
        except OSError:
            self._cf = None
        try:
            ax = ctypes.CDLL("/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices")
            ax.AXIsProcessTrusted.restype = ctypes.c_bool
            self._ax = ax
        except OSError:
            self._ax = None

    def _list_displays(self) -> tuple[DisplayInfo, ...]:
        if not self._cg:
            return ()
        main_id = int(self._cg.CGMainDisplayID())
        buf = (ctypes.c_uint32 * 8)()
        count = ctypes.c_uint32(0)
        self._cg.CGGetActiveDisplayList(8, buf, ctypes.byref(count))
        ids = [int(buf[i]) for i in range(count.value)] or [main_id]
        displays: list[DisplayInfo] = []
        for idx, did in enumerate(ids):
            rect = self._cg.CGDisplayBounds(did)
            displays.append(
                DisplayInfo(
                    index=idx,
                    display_id=did,
                    name=f"主显示器 ({idx + 1})" if did == main_id else f"扩展显示器 ({idx + 1})",
                    width=max(1, int(rect.size.width)),
                    height=max(1, int(rect.size.height)),
                    origin_x=float(rect.origin.x),
                    origin_y=float(rect.origin.y),
                    is_main=(did == main_id),
                )
            )
        return tuple(displays)

    def probe(self) -> DesktopCapabilities:
        if platform.system() != "Darwin" or not self._cg or not os.path.exists(self._screencapture):
            return DesktopCapabilities(
                available=False, driver_name=self.driver_name, platform=platform.system(),
                supports_control=False, supports_clipboard=False,
                error="当前系统不是 macOS 或缺少 CoreGraphics / screencapture 支持",
            )
        screen_ok = bool(self._cg.CGPreflightScreenCaptureAccess()) if hasattr(self._cg, "CGPreflightScreenCaptureAccess") else True
        ax_ok = bool(self._ax.AXIsProcessTrusted()) if self._ax else False
        warnings: list[str] = []
        if not screen_ok:
            warnings.append("请在「系统设置 → 隐私与安全性 → 屏幕录制与系统录音」中勾选运行 Kiki 的终端或 Python 进程。")
        if not ax_ok:
            warnings.append("请在「系统设置 → 隐私与安全性 → 辅助功能」中勾选运行 Kiki 的终端或 Python 进程，以启用远程鼠键控制。")
        displays = self._list_displays()
        spaces = self._list_spaces(displays)
        return DesktopCapabilities(
            available=True, driver_name=self.driver_name, platform="Darwin",
            supports_control=ax_ok, supports_clipboard=bool(shutil.which("pbcopy")),
            displays=displays,
            spaces=spaces,
            supports_space_switch=ax_ok,
            supports_a11y=ax_ok,
            cua_driver_available=self._cua.is_available,
            permissions=DesktopPermissions(screen_recording=screen_ok, accessibility=ax_ok, warnings=tuple(warnings)),
        )

    def _list_spaces(self, displays: tuple[DisplayInfo, ...]) -> tuple[DesktopSpaceInfo, ...]:
        if not displays:
            return ()
        plist_path = Path.home() / "Library" / "Preferences" / "com.apple.spaces.plist"
        try:
            raw = plistlib.loads(plist_path.read_bytes())
            monitors = raw.get("SpacesDisplayConfiguration", {}).get("Management Data", {}).get("Monitors", [])
            if not isinstance(monitors, list) or not monitors:
                raise ValueError("empty monitors")
            result: list[DesktopSpaceInfo] = []
            for d_idx, display in enumerate(displays):
                mon = None
                if display.is_main:
                    mon = next((m for m in monitors if m.get("Display Identifier") == "Main"), None)
                if mon is None and d_idx < len(monitors):
                    mon = monitors[d_idx]
                if not isinstance(mon, dict):
                    result.append(DesktopSpaceInfo(index=0, space_id=f"d{d_idx}-s0", display_index=d_idx, name="桌面 1", is_active=True))
                    continue
                cur_id = str((mon.get("Current Space") or {}).get("id64") or (mon.get("Current Space") or {}).get("uuid") or "")
                raw_spaces = mon.get("Spaces") or []
                desk_num, fs_num = 0, 0
                for s_idx, sp in enumerate(raw_spaces):
                    sid = str(sp.get("id64") or sp.get("uuid") or f"d{d_idx}-s{s_idx}")
                    is_fs = int(sp.get("type", 0)) == 4
                    if is_fs:
                        fs_num += 1
                        label = f"全屏空间 {fs_num}"
                    else:
                        desk_num += 1
                        label = f"桌面 {desk_num}"
                    result.append(
                        DesktopSpaceInfo(
                            index=s_idx,
                            space_id=sid,
                            display_index=d_idx,
                            name=label,
                            is_active=(sid == cur_id) if cur_id else (s_idx == 0),
                            is_fullscreen=is_fs,
                        )
                    )
            if result:
                return tuple(result)
        except Exception:
            pass
        return tuple(
            DesktopSpaceInfo(index=0, space_id=f"d{d.index}-s0", display_index=d.index, name="桌面 1", is_active=True)
            for d in displays
        )

    def capture_frame(self, display_index: int = 0, max_width: int = 1280, quality: int = 75) -> CapturedFrame:
        displays = self._list_displays()
        if not displays:
            raise RuntimeError("No active macOS displays found")
        target = displays[min(max(0, display_index), len(displays) - 1)]
        fd, tmp_path = tempfile.mkstemp(prefix="kiki_desk_", suffix=".jpg")
        os.close(fd)
        try:
            cmd = [self._screencapture, "-x", "-C", "-t", "jpg", "-D", str(target.index + 1), tmp_path]
            res = subprocess.run(cmd, capture_output=True, timeout=5)
            if res.returncode != 0 or not os.path.exists(tmp_path) or os.path.getsize(tmp_path) == 0:
                raise RuntimeError(res.stderr.decode("utf-8", errors="ignore").strip() or "screencapture failed")
            if max_width > 0 and os.path.exists(self._sips):
                q = max(30, min(95, quality))
                subprocess.run(
                    [self._sips, "-Z", str(max_width), "-s", "formatOptions", str(q), tmp_path],
                    capture_output=True, timeout=5,
                )
            with open(tmp_path, "rb") as f:
                data = f.read()
            return CapturedFrame(
                data=data, mime_type="image/jpeg", width=target.width, height=target.height,
                display_index=target.index, sha1=hashlib.sha1(data).hexdigest(), captured_at=time.time(),
            )
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    def _post_and_release(self, evt: Any) -> None:
        if evt:
            self._cg.CGEventPost(_KCG_HID_EVENT_TAP, evt)
            self._cg.CFRelease(evt)

    def _point_for_event(self, event: DesktopInputEvent) -> _CGPoint:
        displays = self._list_displays()
        target = displays[min(max(0, event.display_index), len(displays) - 1)]
        px = target.origin_x + (event.x or 0.0) * target.width
        py = target.origin_y + (event.y or 0.0) * target.height
        return _CGPoint(px, py)

    def _flags_from_modifiers(self, modifiers: tuple[str, ...]) -> int:
        flags = 0
        for m in modifiers:
            flags |= _MODIFIER_FLAGS.get(m.lower(), 0)
        return flags

    def send_input(self, event: DesktopInputEvent) -> None:
        if not self._cg:
            raise RuntimeError("CoreGraphics not available")
        if self._ax and not self._ax.AXIsProcessTrusted():
            raise PermissionError("macOS 辅助功能权限未开启，无法注入远程鼠键事件")

        act = event.action
        btn_map = {"left": (_EVT_LEFT_DOWN, _EVT_LEFT_UP, _KCG_MOUSE_LEFT),
                   "right": (_EVT_RIGHT_DOWN, _EVT_RIGHT_UP, _KCG_MOUSE_RIGHT),
                   "middle": (_EVT_OTHER_DOWN, _EVT_OTHER_UP, _KCG_MOUSE_CENTER)}

        if act == "mouse_move":
            if "left" in self._pressed_buttons:
                evt_type, btn_code = _EVT_LEFT_DRAG, _KCG_MOUSE_LEFT
            elif "right" in self._pressed_buttons:
                evt_type, btn_code = _EVT_RIGHT_DRAG, _KCG_MOUSE_RIGHT
            else:
                evt_type, btn_code = _EVT_MOUSE_MOVED, _KCG_MOUSE_LEFT
            self._post_and_release(self._cg.CGEventCreateMouseEvent(None, evt_type, self._point_for_event(event), btn_code))
        elif act in ("mouse_down", "mouse_up", "mouse_click", "mouse_double_click"):
            pt = self._point_for_event(event)
            down_t, up_t, btn_code = btn_map[event.button]
            if act == "mouse_down":
                self._pressed_buttons.add(event.button)
                self._post_and_release(self._cg.CGEventCreateMouseEvent(None, down_t, pt, btn_code))
            elif act == "mouse_up":
                self._pressed_buttons.discard(event.button)
                self._post_and_release(self._cg.CGEventCreateMouseEvent(None, up_t, pt, btn_code))
            else:
                clicks = 2 if act == "mouse_double_click" else 1
                for click_idx in range(1, clicks + 1):
                    d_evt = self._cg.CGEventCreateMouseEvent(None, down_t, pt, btn_code)
                    u_evt = self._cg.CGEventCreateMouseEvent(None, up_t, pt, btn_code)
                    self._cg.CGEventSetIntegerValueField(d_evt, 1, click_idx)
                    self._cg.CGEventSetIntegerValueField(u_evt, 1, click_idx)
                    self._post_and_release(d_evt)
                    self._post_and_release(u_evt)
        elif act == "mouse_scroll":
            dy = int(round(event.delta_y))
            dx = int(round(event.delta_x))
            evt = self._cg.CGEventCreateScrollWheelEvent(None, _KCG_SCROLL_PIXEL, 2, dy, dx)
            self._post_and_release(evt)
        elif act == "type_text" and event.text:
            for ch in event.text:
                utf16 = ch.encode("utf-16-le")
                units = [int.from_bytes(utf16[i : i + 2], "little") for i in range(0, len(utf16), 2)]
                arr = (ctypes.c_uint16 * len(units))(*units)
                for is_down in (True, False):
                    evt = self._cg.CGEventCreateKeyboardEvent(None, 0, is_down)
                    self._cg.CGEventKeyboardSetUnicodeString(evt, len(units), arr)
                    self._post_and_release(evt)
        elif act in ("key_down", "key_up", "key_tap", "key_combo") and event.key:
            code = _KEYCODES.get(event.key.lower())
            if code is None:
                if len(event.key) == 1 and act in ("key_tap", "key_down"):
                    self.send_input(DesktopInputEvent(action="type_text", text=event.key))
                return
            flags = self._flags_from_modifiers(event.modifiers)
            steps = (True,) if act == "key_down" else ((False,) if act == "key_up" else (True, False))
            for is_down in steps:
                evt = self._cg.CGEventCreateKeyboardEvent(None, code, is_down)
                if flags:
                    self._cg.CGEventSetFlags(evt, flags)
                self._post_and_release(evt)

    def get_clipboard(self) -> str:
        res = subprocess.run(["pbpaste"], capture_output=True, timeout=3)
        return res.stdout.decode("utf-8", errors="replace") if res.returncode == 0 else ""

    def set_clipboard(self, text: str) -> None:
        subprocess.run(["pbcopy"], input=text.encode("utf-8"), capture_output=True, timeout=3)

    def switch_space(
        self,
        *,
        display_index: int = 0,
        target_index: Optional[int] = None,
        direction: Optional[Literal["prev", "next"]] = None,
    ) -> None:
        if not self._cg:
            raise RuntimeError("CoreGraphics not available")
        if self._ax and not self._ax.AXIsProcessTrusted():
            raise PermissionError("macOS 辅助功能权限未开启，无法切换虚拟桌面")

        displays = self._list_displays()
        if len(displays) > 1:
            # Focus target display by moving cursor to its center first
            self.send_input(DesktopInputEvent(action="mouse_move", x=0.5, y=0.5, display_index=display_index))
            time.sleep(0.04)

        steps = 0
        if direction == "prev":
            steps = -1
        elif direction == "next":
            steps = 1
        elif target_index is not None:
            spaces = [s for s in self._list_spaces(displays) if s.display_index == display_index]
            active = next((s.index for s in spaces if s.is_active), 0)
            steps = target_index - active

        if steps == 0:
            return
        key = "arrowright" if steps > 0 else "arrowleft"
        for i in range(min(abs(steps), 12)):
            self.send_input(DesktopInputEvent(action="key_combo", key=key, modifiers=("ctrl",), display_index=display_index))
            if i + 1 < abs(steps):
                time.sleep(0.14)

    def open_url(self, url: str) -> None:
        cleaned = (url or "").strip()
        if not cleaned.lower().startswith(("http://", "https://")):
            raise ValueError(f"Only http:// and https:// URLs are allowed: {url!r}")
        res = subprocess.run(["open", cleaned], capture_output=True, timeout=5, check=False)
        if res.returncode != 0:
            err = res.stderr.decode("utf-8", errors="ignore").strip()
            raise RuntimeError(err or f"Failed to open URL (exit {res.returncode})")

    def open_app(self, app_name: str) -> None:
        cleaned = (app_name or "").strip()
        if (
            not cleaned
            or cleaned.startswith("-")
            or "/" in cleaned
            or ".." in cleaned
            or any(ord(ch) < 32 or ord(ch) == 127 for ch in cleaned)
        ):
            raise ValueError(f"Invalid application name: {app_name!r}")
        res = subprocess.run(["open", "-a", cleaned], capture_output=True, timeout=5, check=False)
        if res.returncode != 0:
            err = res.stderr.decode("utf-8", errors="ignore").strip()
            raise RuntimeError(err or f"Failed to open app {cleaned!r} (exit {res.returncode})")

    def _enumerate_visible_windows(self) -> list[tuple[str, float, float, float, float]]:
        """Return visible normal app windows in front-to-back order as (owner, x, y, w, h)."""
        if not self._cg or not self._cf:
            return []
        cf = self._cf
        # kCGWindowListOptionOnScreenOnly (1) | kCGWindowListExcludeDesktopElements (16) = 17
        arr = self._cg.CGWindowListCopyWindowInfo(17, 0)
        if not arr:
            return []

        cf_keys: list[Any] = []
        try:
            def _cfstr(s: str) -> Any:
                ref = cf.CFStringCreateWithCString(None, s.encode("utf-8"), 0x08000100)
                if ref:
                    cf_keys.append(ref)
                return ref

            def _to_str(ref: Any) -> str:
                if not ref:
                    return ""
                buf = ctypes.create_string_buffer(512)
                if cf.CFStringGetCString(ref, buf, 512, 0x08000100):
                    return buf.value.decode("utf-8", errors="replace")
                return ""

            def _to_double(ref: Any) -> float:
                if not ref:
                    return 0.0
                v = ctypes.c_double(0.0)
                cf.CFNumberGetValue(ref, 13, ctypes.byref(v))  # kCFNumberFloat64Type
                return v.value

            def _to_int(ref: Any) -> int:
                if not ref:
                    return 0
                v = ctypes.c_longlong(0)
                cf.CFNumberGetValue(ref, 4, ctypes.byref(v))  # kCFNumberSInt64Type
                return v.value

            k_owner = _cfstr("kCGWindowOwnerName")
            k_layer = _cfstr("kCGWindowLayer")
            k_bounds = _cfstr("kCGWindowBounds")
            k_x = _cfstr("X")
            k_y = _cfstr("Y")
            k_w = _cfstr("Width")
            k_h = _cfstr("Height")
            ignored_owners = {"Window Server", "Dock", "Control Center", "Spotlight", "Notification Center"}

            results: list[tuple[str, float, float, float, float]] = []
            count = int(cf.CFArrayGetCount(arr))
            for i in range(count):
                entry = cf.CFArrayGetValueAtIndex(arr, i)
                if not entry:
                    continue
                if _to_int(cf.CFDictionaryGetValue(entry, k_layer)) != 0:
                    continue
                owner = _to_str(cf.CFDictionaryGetValue(entry, k_owner))
                if not owner or owner in ignored_owners:
                    continue
                bounds = cf.CFDictionaryGetValue(entry, k_bounds)
                if not bounds:
                    continue
                x = _to_double(cf.CFDictionaryGetValue(bounds, k_x))
                y = _to_double(cf.CFDictionaryGetValue(bounds, k_y))
                w = _to_double(cf.CFDictionaryGetValue(bounds, k_w))
                h = _to_double(cf.CFDictionaryGetValue(bounds, k_h))
                if w >= 80.0 and h >= 80.0:
                    results.append((owner, x, y, w, h))
            return results
        finally:
            cf.CFRelease(arr)
            for ref in cf_keys:
                cf.CFRelease(ref)

    @staticmethod
    def _match_display_for_rect(
        displays: tuple[DisplayInfo, ...],
        x: float,
        y: float,
        w: float,
        h: float,
    ) -> Optional[int]:
        if not displays:
            return None
        cx, cy = x + w * 0.5, y + h * 0.5
        for d in displays:
            if d.origin_x <= cx < d.origin_x + d.width and d.origin_y <= cy < d.origin_y + d.height:
                return d.index
        best_idx: Optional[int] = None
        best_area = 0.0
        for d in displays:
            ix = max(0.0, min(x + w, d.origin_x + d.width) - max(x, d.origin_x))
            iy = max(0.0, min(y + h, d.origin_y + d.height) - max(y, d.origin_y))
            area = ix * iy
            if area > best_area:
                best_area = area
                best_idx = d.index
        return best_idx

    def get_frontmost_window_display(
        self,
        *,
        preferred_app: Optional[str] = None,
    ) -> tuple[Optional[int], Optional[str]]:
        """Return (display_index, owner_name) for the frontmost (or preferred_app) visible window."""
        displays = self._list_displays()
        if not displays:
            return None, None
        windows = self._enumerate_visible_windows()
        if not windows:
            return None, None
        if preferred_app:
            needle = preferred_app.strip().lower()
            for owner, x, y, w, h in windows:
                if needle in owner.lower():
                    d_idx = self._match_display_for_rect(displays, x, y, w, h)
                    if d_idx is not None:
                        return d_idx, owner
        owner, x, y, w, h = windows[0]
        return self._match_display_for_rect(displays, x, y, w, h), owner

    def list_visible_windows_by_display(self) -> dict[int, list[str]]:
        """Return deduplicated visible app names per display index (top 5 each)."""
        displays = self._list_displays()
        by_display: dict[int, list[str]] = {d.index: [] for d in displays}
        if not displays:
            return by_display
        for owner, x, y, w, h in self._enumerate_visible_windows():
            d_idx = self._match_display_for_rect(displays, x, y, w, h)
            if d_idx is None:
                continue
            bucket = by_display.setdefault(d_idx, [])
            if owner not in bucket and len(bucket) < 5:
                bucket.append(owner)
        return by_display

    def _find_app_pid(self, app_name: str) -> tuple[Optional[int], Optional[str], Optional[int]]:
        """Find (pid, owner_name, display_index) for a visible app window matching app_name."""
        needle = (app_name or "").strip().lower()
        if not needle or not self._cg or not self._cf:
            return None, None, None
        cf = self._cf
        arr = self._cg.CGWindowListCopyWindowInfo(17, 0)
        if not arr:
            return None, None, None
        displays = self._list_displays()
        cf_keys: list[Any] = []
        try:
            def _cfstr(s: str) -> Any:
                ref = cf.CFStringCreateWithCString(None, s.encode("utf-8"), 0x08000100)
                if ref:
                    cf_keys.append(ref)
                return ref

            def _to_str(ref: Any) -> str:
                if not ref:
                    return ""
                buf = ctypes.create_string_buffer(512)
                if cf.CFStringGetCString(ref, buf, 512, 0x08000100):
                    return buf.value.decode("utf-8", errors="replace")
                return ""

            def _to_int(ref: Any) -> int:
                if not ref:
                    return 0
                v = ctypes.c_longlong(0)
                cf.CFNumberGetValue(ref, 4, ctypes.byref(v))
                return v.value

            def _to_double(ref: Any) -> float:
                if not ref:
                    return 0.0
                v = ctypes.c_double(0.0)
                cf.CFNumberGetValue(ref, 13, ctypes.byref(v))
                return v.value

            k_owner = _cfstr("kCGWindowOwnerName")
            k_pid = _cfstr("kCGWindowOwnerPID")
            k_layer = _cfstr("kCGWindowLayer")
            k_bounds = _cfstr("kCGWindowBounds")
            k_x, k_y, k_w, k_h = _cfstr("X"), _cfstr("Y"), _cfstr("Width"), _cfstr("Height")

            count = int(cf.CFArrayGetCount(arr))
            for i in range(count):
                entry = cf.CFArrayGetValueAtIndex(arr, i)
                if not entry or _to_int(cf.CFDictionaryGetValue(entry, k_layer)) != 0:
                    continue
                owner = _to_str(cf.CFDictionaryGetValue(entry, k_owner))
                if not owner or needle not in owner.lower():
                    continue
                pid = _to_int(cf.CFDictionaryGetValue(entry, k_pid))
                bounds = cf.CFDictionaryGetValue(entry, k_bounds)
                d_idx: Optional[int] = None
                if bounds and displays:
                    x = _to_double(cf.CFDictionaryGetValue(bounds, k_x))
                    y = _to_double(cf.CFDictionaryGetValue(bounds, k_y))
                    w = _to_double(cf.CFDictionaryGetValue(bounds, k_w))
                    h = _to_double(cf.CFDictionaryGetValue(bounds, k_h))
                    d_idx = self._match_display_for_rect(displays, x, y, w, h)
                if pid > 0:
                    return pid, owner, d_idx
            return None, None, None
        finally:
            cf.CFRelease(arr)
            for ref in cf_keys:
                cf.CFRelease(ref)

    def get_cua_status(self) -> dict[str, Any]:
        return self._cua.probe().to_dict()

    def get_ui_tree(
        self,
        *,
        display_index: int = 0,
        app_name: Optional[str] = None,
        interactive_only: bool = True,
        query: Optional[str] = None,
        max_depth: int = 8,
        max_nodes: int = 120,
        prefer_cua: bool = True,
    ) -> UITreeSnapshot:
        """Inspect UI accessibility tree via cua-driver (if installed) or native macOS AXUIElement."""
        displays = self._list_displays()
        if not displays:
            raise RuntimeError("No active macOS displays found")
        target_display = displays[min(max(0, display_index), len(displays) - 1)]

        pid: Optional[int] = None
        resolved_app_name: Optional[str] = app_name
        if app_name:
            found_pid, found_owner, found_display = self._find_app_pid(app_name)
            if found_pid:
                pid = found_pid
                resolved_app_name = found_owner or app_name
                if found_display is not None and 0 <= found_display < len(displays):
                    target_display = displays[found_display]

        if prefer_cua and self._cua.is_available and not query:
            cua_tree = self._cua.get_ui_tree(
                display=target_display,
                app_name=resolved_app_name,
                max_nodes=max_nodes,
            )
            if cua_tree is not None:
                return cua_tree

        return self._a11y.get_ui_tree(
            display=target_display,
            pid=pid,
            app_name_hint=resolved_app_name,
            interactive_only=interactive_only,
            query=query,
            max_depth=max_depth,
            max_nodes=max_nodes,
        )

    def get_cached_ui_node(self, ref: str) -> Optional[tuple[UIElementNode, int]]:
        return self._a11y.get_cached_node(ref)

    def perform_ax_action(
        self,
        *,
        ref: str,
        action: str,
        value: Optional[str] = None,
        display_index: int = 0,
    ) -> AXActionResult:
        """Execute a background A11y/CUA semantic action on a cached element ref."""
        if ref.startswith("cua_") and self._cua.is_available:
            cua_res = self._cua.perform_ax_action(
                ref=ref,
                action=action,
                value=value,
                display_index=display_index,
            )
            if cua_res is not None:
                return cua_res
        return self._a11y.perform_ax_action(ref=ref, action=action, value=value)

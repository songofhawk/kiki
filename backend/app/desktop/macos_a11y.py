"""Zero-dependency macOS Accessibility (AXUIElement) tree & background action engine."""

from __future__ import annotations

import ctypes
import platform
import threading
import time
from typing import Any, Optional

from .models import AXActionResult, DisplayInfo, UIElementNode, UITreeSnapshot

_KCF_STRING_ENCODING_UTF8 = 0x08000100
_KAX_ERROR_SUCCESS = 0
_KAX_VALUE_CGPOINT = 1
_KAX_VALUE_CGSIZE = 2

# Roles that are almost always interactive or carry meaningful UI semantics
_INTERACTIVE_ROLES = frozenset(
    {
        "AXButton",
        "AXTextField",
        "AXTextArea",
        "AXComboBox",
        "AXPopUpButton",
        "AXMenuButton",
        "AXMenuItem",
        "AXMenuBarItem",
        "AXCheckBox",
        "AXRadioButton",
        "AXLink",
        "AXTab",
        "AXSlider",
        "AXIncrementor",
        "AXDisclosureTriangle",
        "AXSegmentedControl",
        "AXSwitch",
        "AXSearchField",
        "AXSecureTextField",
        "AXCell",
        "AXRow",
        "AXOutlineRow",
        "AXToolbarButton",
    }
)

_CONTAINER_ROLES = frozenset(
    {
        "AXApplication",
        "AXWindow",
        "AXSheet",
        "AXDrawer",
        "AXGroup",
        "AXScrollArea",
        "AXSplitGroup",
        "AXTabGroup",
        "AXToolbar",
        "AXWebArea",
        "AXList",
        "AXTable",
        "AXOutline",
        "AXMenu",
        "AXMenuBar",
        "AXLayoutArea",
        "AXPopover",
        "AXDialog",
        "AXGenericElement",
    }
)

_USEFUL_ACTIONS = frozenset(
    {
        "AXPress",
        "AXConfirm",
        "AXShowMenu",
        "AXRaise",
        "AXPick",
        "AXIncrement",
        "AXDecrement",
        "AXCancel",
    }
)


class _CGPoint(ctypes.Structure):
    _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double)]


class _CGSize(ctypes.Structure):
    _fields_ = [("width", ctypes.c_double), ("height", ctypes.c_double)]


def _truncate_text(raw: str, max_len: int = 120) -> str:
    cleaned = " ".join(raw.split())
    if len(cleaned) <= max_len:
        return cleaned
    return cleaned[: max_len - 1] + "…"


class MacOSA11yEngine:
    """Native macOS AXUIElement inspector and background action dispatcher via ctypes."""

    def __init__(self) -> None:
        self._ax: Any = None
        self._cf: Any = None
        self._cf_str_type_id: int = 0
        self._cf_bool_type_id: int = 0
        self._cf_num_type_id: int = 0
        self._cf_arr_type_id: int = 0
        self._ax_val_type_id: int = 0
        self._cf_bool_true: Any = None
        self._cf_bool_false: Any = None
        self._lock = threading.RLock()
        # Snapshot cache: ref -> (retained AXUIElementRef, UIElementNode, display_index)
        self._ref_cache: dict[str, tuple[int, UIElementNode, int]] = {}
        self._load_frameworks()

    def _load_frameworks(self) -> None:
        if platform.system() != "Darwin":
            return
        try:
            cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
            cf.CFGetTypeID.argtypes = [ctypes.c_void_p]
            cf.CFGetTypeID.restype = ctypes.c_ulong
            cf.CFStringGetTypeID.restype = ctypes.c_ulong
            cf.CFBooleanGetTypeID.restype = ctypes.c_ulong
            cf.CFNumberGetTypeID.restype = ctypes.c_ulong
            cf.CFArrayGetTypeID.restype = ctypes.c_ulong
            cf.CFBooleanGetValue.argtypes = [ctypes.c_void_p]
            cf.CFBooleanGetValue.restype = ctypes.c_bool
            cf.CFStringCreateWithCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32]
            cf.CFStringCreateWithCString.restype = ctypes.c_void_p
            cf.CFStringGetLength.argtypes = [ctypes.c_void_p]
            cf.CFStringGetLength.restype = ctypes.c_long
            cf.CFStringGetMaximumSizeForEncoding.argtypes = [ctypes.c_long, ctypes.c_uint32]
            cf.CFStringGetMaximumSizeForEncoding.restype = ctypes.c_long
            cf.CFStringGetCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_long, ctypes.c_uint32]
            cf.CFStringGetCString.restype = ctypes.c_bool
            cf.CFNumberGetValue.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]
            cf.CFNumberGetValue.restype = ctypes.c_bool
            cf.CFArrayGetCount.argtypes = [ctypes.c_void_p]
            cf.CFArrayGetCount.restype = ctypes.c_long
            cf.CFArrayGetValueAtIndex.argtypes = [ctypes.c_void_p, ctypes.c_long]
            cf.CFArrayGetValueAtIndex.restype = ctypes.c_void_p
            cf.CFRetain.argtypes = [ctypes.c_void_p]
            cf.CFRetain.restype = ctypes.c_void_p
            cf.CFRelease.argtypes = [ctypes.c_void_p]

            self._cf = cf
            self._cf_str_type_id = int(cf.CFStringGetTypeID())
            self._cf_bool_type_id = int(cf.CFBooleanGetTypeID())
            self._cf_num_type_id = int(cf.CFNumberGetTypeID())
            self._cf_arr_type_id = int(cf.CFArrayGetTypeID())
            self._cf_bool_true = ctypes.c_void_p.in_dll(cf, "kCFBooleanTrue").value
            self._cf_bool_false = ctypes.c_void_p.in_dll(cf, "kCFBooleanFalse").value
        except (OSError, AttributeError, ValueError):
            self._cf = None
            return

        try:
            ax = ctypes.CDLL("/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices")
            ax.AXIsProcessTrusted.restype = ctypes.c_bool
            ax.AXUIElementCreateSystemWide.restype = ctypes.c_void_p
            ax.AXUIElementCreateApplication.argtypes = [ctypes.c_int32]
            ax.AXUIElementCreateApplication.restype = ctypes.c_void_p
            ax.AXUIElementGetPid.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_int32)]
            ax.AXUIElementGetPid.restype = ctypes.c_int32
            ax.AXUIElementCopyAttributeValue.argtypes = [
                ctypes.c_void_p,
                ctypes.c_void_p,
                ctypes.POINTER(ctypes.c_void_p),
            ]
            ax.AXUIElementCopyAttributeValue.restype = ctypes.c_int32
            ax.AXUIElementSetAttributeValue.argtypes = [
                ctypes.c_void_p,
                ctypes.c_void_p,
                ctypes.c_void_p,
            ]
            ax.AXUIElementSetAttributeValue.restype = ctypes.c_int32
            ax.AXUIElementCopyActionNames.argtypes = [
                ctypes.c_void_p,
                ctypes.POINTER(ctypes.c_void_p),
            ]
            ax.AXUIElementCopyActionNames.restype = ctypes.c_int32
            ax.AXUIElementPerformAction.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
            ax.AXUIElementPerformAction.restype = ctypes.c_int32
            ax.AXValueGetTypeID.restype = ctypes.c_ulong
            ax.AXValueGetValue.argtypes = [ctypes.c_void_p, ctypes.c_int32, ctypes.c_void_p]
            ax.AXValueGetValue.restype = ctypes.c_bool

            self._ax = ax
            self._ax_val_type_id = int(ax.AXValueGetTypeID())
        except (OSError, AttributeError):
            self._ax = None

    @property
    def available(self) -> bool:
        return bool(self._ax and self._cf and self._ax.AXIsProcessTrusted())

    def clear_cache(self) -> None:
        with self._lock:
            if self._cf:
                for raw_ptr, _, _ in self._ref_cache.values():
                    if raw_ptr:
                        self._cf.CFRelease(ctypes.c_void_p(raw_ptr))
            self._ref_cache.clear()

    def __del__(self) -> None:
        try:
            self.clear_cache()
        except Exception:
            pass

    def _cfstr(self, text: str) -> Any:
        return self._cf.CFStringCreateWithCString(
            None, text.encode("utf-8"), _KCF_STRING_ENCODING_UTF8
        )

    def _cf_to_str(self, ref: Any) -> str:
        if not ref or not self._cf:
            return ""
        tid = int(self._cf.CFGetTypeID(ref))
        if tid == self._cf_str_type_id:
            length = int(self._cf.CFStringGetLength(ref))
            if length <= 0:
                return ""
            max_bytes = int(self._cf.CFStringGetMaximumSizeForEncoding(length, _KCF_STRING_ENCODING_UTF8)) + 1
            buf_size = min(max(64, max_bytes), 4096)
            buf = ctypes.create_string_buffer(buf_size)
            if self._cf.CFStringGetCString(ref, buf, buf_size, _KCF_STRING_ENCODING_UTF8):
                return buf.value.decode("utf-8", errors="replace")
            return ""
        if tid == self._cf_bool_type_id:
            return "true" if self._cf.CFBooleanGetValue(ref) else "false"
        if tid == self._cf_num_type_id:
            val = ctypes.c_double(0.0)
            if self._cf.CFNumberGetValue(ref, 13, ctypes.byref(val)):  # kCFNumberFloat64Type
                if val.value.is_integer():
                    return str(int(val.value))
                return f"{val.value:.2f}"
        return ""

    def _copy_attr(self, elem: Any, attr_name: str) -> Any:
        attr_ref = self._cfstr(attr_name)
        if not attr_ref:
            return None
        out = ctypes.c_void_p(0)
        try:
            err = int(self._ax.AXUIElementCopyAttributeValue(elem, attr_ref, ctypes.byref(out)))
            if err != _KAX_ERROR_SUCCESS or not out.value:
                return None
            return out.value
        finally:
            self._cf.CFRelease(attr_ref)

    def _get_str_attr(self, elem: Any, attr_name: str) -> str:
        val = self._copy_attr(elem, attr_name)
        if not val:
            return ""
        try:
            return self._cf_to_str(val)
        finally:
            self._cf.CFRelease(val)

    def _get_bool_attr(self, elem: Any, attr_name: str, default: bool = False) -> bool:
        val = self._copy_attr(elem, attr_name)
        if not val:
            return default
        try:
            if int(self._cf.CFGetTypeID(val)) == self._cf_bool_type_id:
                return bool(self._cf.CFBooleanGetValue(val))
            return default
        finally:
            self._cf.CFRelease(val)

    def _get_bounds_px(self, elem: Any) -> Optional[tuple[float, float, float, float]]:
        pos_val = self._copy_attr(elem, "AXPosition")
        size_val = self._copy_attr(elem, "AXSize")
        if not pos_val or not size_val:
            if pos_val:
                self._cf.CFRelease(pos_val)
            if size_val:
                self._cf.CFRelease(size_val)
            return None
        try:
            if (
                int(self._cf.CFGetTypeID(pos_val)) != self._ax_val_type_id
                or int(self._cf.CFGetTypeID(size_val)) != self._ax_val_type_id
            ):
                return None
            pt = _CGPoint()
            sz = _CGSize()
            ok_p = bool(self._ax.AXValueGetValue(pos_val, _KAX_VALUE_CGPOINT, ctypes.byref(pt)))
            ok_s = bool(self._ax.AXValueGetValue(size_val, _KAX_VALUE_CGSIZE, ctypes.byref(sz)))
            if not (ok_p and ok_s) or sz.width <= 0 or sz.height <= 0:
                return None
            return (float(pt.x), float(pt.y), float(sz.width), float(sz.height))
        finally:
            self._cf.CFRelease(pos_val)
            self._cf.CFRelease(size_val)

    def _get_actions(self, elem: Any) -> tuple[str, ...]:
        out = ctypes.c_void_p(0)
        err = int(self._ax.AXUIElementCopyActionNames(elem, ctypes.byref(out)))
        if err != _KAX_ERROR_SUCCESS or not out.value:
            return ()
        try:
            if int(self._cf.CFGetTypeID(out.value)) != self._cf_arr_type_id:
                return ()
            count = int(self._cf.CFArrayGetCount(out.value))
            actions: list[str] = []
            for i in range(min(count, 20)):
                item = self._cf.CFArrayGetValueAtIndex(out.value, i)
                name = self._cf_to_str(item)
                if name in _USEFUL_ACTIONS:
                    actions.append(name)
            return tuple(actions)
        finally:
            self._cf.CFRelease(out.value)

    @staticmethod
    def _normalize_bounds(
        bounds_px: Optional[tuple[float, float, float, float]],
        display: DisplayInfo,
    ) -> tuple[Optional[tuple[float, float, float, float]], Optional[tuple[float, float]]]:
        if bounds_px is None or display.width <= 0 or display.height <= 0:
            return None, None
        x, y, w, h = bounds_px
        nx = ((x - display.origin_x) / float(display.width)) * 1000.0
        ny = ((y - display.origin_y) / float(display.height)) * 1000.0
        nw = (w / float(display.width)) * 1000.0
        nh = (h / float(display.height)) * 1000.0
        # Skip elements completely outside this display
        if nx + nw < -50 or ny + nh < -50 or nx > 1050 or ny > 1050:
            return None, None
        cx = max(0.0, min(1000.0, nx + nw * 0.5))
        cy = max(0.0, min(1000.0, ny + nh * 0.5))
        return (
            (round(nx, 1), round(ny, 1), round(nw, 1), round(nh, 1)),
            (round(cx, 1), round(cy, 1)),
        )

    def _resolve_target_app(
        self,
        *,
        pid: Optional[int] = None,
    ) -> tuple[Any, Optional[int], str]:
        if pid is not None and pid > 0:
            app_ref = self._ax.AXUIElementCreateApplication(int(pid))
            if not app_ref:
                raise RuntimeError(f"无法创建目标进程 (pid={pid}) 的 AXUIElement 句柄")
            title = self._get_str_attr(app_ref, "AXTitle") or f"pid:{pid}"
            return app_ref, int(pid), title

        sys_wide = self._ax.AXUIElementCreateSystemWide()
        if not sys_wide:
            raise RuntimeError("无法初始化 macOS SystemWide AXUIElement")
        try:
            focused_app = self._copy_attr(sys_wide, "AXFocusedApplication")
            if not focused_app:
                raise RuntimeError("当前未检测到前台聚焦应用")
            c_pid = ctypes.c_int32(0)
            self._ax.AXUIElementGetPid(focused_app, ctypes.byref(c_pid))
            title = self._get_str_attr(focused_app, "AXTitle") or "FrontmostApp"
            return focused_app, int(c_pid.value) or None, title
        finally:
            self._cf.CFRelease(sys_wide)

    def get_ui_tree(
        self,
        *,
        display: DisplayInfo,
        pid: Optional[int] = None,
        app_name_hint: Optional[str] = None,
        interactive_only: bool = True,
        query: Optional[str] = None,
        max_depth: int = 8,
        max_nodes: int = 120,
    ) -> UITreeSnapshot:
        """Traverse the target app's AX tree and cache element refs for follow-up AX actions."""
        if not self.available:
            raise PermissionError("macOS 辅助功能权限未开启，无法读取 A11y UI 树")

        max_depth = max(1, min(20, int(max_depth)))
        max_nodes = max(10, min(400, int(max_nodes)))
        needle = (query or "").strip().lower()

        app_ref, resolved_pid, resolved_title = self._resolve_target_app(pid=pid)
        app_title = app_name_hint or resolved_title

        nodes: list[UIElementNode] = []
        new_cache: dict[str, tuple[int, UIElementNode, int]] = {}
        truncated = False
        visited_count = 0
        visit_budget = max(max_nodes * 12, 800)

        try:
            # Prefer focused window first, fall back to AXWindows[0], then app_ref itself
            roots: list[Any] = []
            focused_win = self._copy_attr(app_ref, "AXFocusedWindow")
            if focused_win:
                roots.append(focused_win)
            else:
                wins_arr = self._copy_attr(app_ref, "AXWindows")
                if wins_arr:
                    try:
                        if int(self._cf.CFGetTypeID(wins_arr)) == self._cf_arr_type_id:
                            cnt = int(self._cf.CFArrayGetCount(wins_arr))
                            for i in range(min(cnt, 2)):
                                w_elem = self._cf.CFArrayGetValueAtIndex(wins_arr, i)
                                if w_elem:
                                    self._cf.CFRetain(w_elem)
                                    roots.append(w_elem)
                    finally:
                        self._cf.CFRelease(wins_arr)
            if not roots:
                self._cf.CFRetain(app_ref)
                roots.append(app_ref)

            # Iterative DFS stack: (retained_elem_ptr, depth)
            stack: list[tuple[Any, int]] = [(r, 0) for r in reversed(roots)]
            try:
                while stack:
                    elem, depth = stack.pop()
                    try:
                        visited_count += 1
                        if visited_count > visit_budget or len(nodes) >= max_nodes:
                            truncated = True
                            break

                        role = self._get_str_attr(elem, "AXRole")
                        subrole = self._get_str_attr(elem, "AXSubrole")
                        title = _truncate_text(self._get_str_attr(elem, "AXTitle"))
                        desc = _truncate_text(self._get_str_attr(elem, "AXDescription"))
                        val = _truncate_text(self._get_str_attr(elem, "AXValue"))
                        enabled = self._get_bool_attr(elem, "AXEnabled", default=True)
                        focused = self._get_bool_attr(elem, "AXFocused", default=False)
                        actions = self._get_actions(elem)

                        bounds_px = self._get_bounds_px(elem)
                        norm_bounds, norm_center = self._normalize_bounds(bounds_px, display)

                        is_interactive = (
                            role in _INTERACTIVE_ROLES
                            or bool(actions)
                            or focused
                        )
                        has_text = bool(title or desc or val)

                        include = True
                        if interactive_only:
                            include = is_interactive or (role == "AXStaticText" and has_text and bool(needle))
                        else:
                            include = is_interactive or (has_text and role not in _CONTAINER_ROLES)

                        if include and not (norm_bounds or has_text or actions):
                            include = False

                        if include and needle:
                            haystack = f"{role} {subrole} {title} {desc} {val}".lower()
                            if needle not in haystack:
                                include = False

                        if include:
                            ref_id = f"e{len(nodes) + 1}"
                            node = UIElementNode(
                                ref=ref_id,
                                role=role or "AXUnknown",
                                subrole=subrole,
                                title=title,
                                description=desc,
                                value=val,
                                enabled=enabled,
                                focused=focused,
                                actions=actions,
                                bounds=norm_bounds,
                                center=norm_center,
                                depth=depth,
                            )
                            nodes.append(node)
                            # Retain element pointer for subsequent ax_press / ax_set_value / ax_focus
                            self._cf.CFRetain(elem)
                            new_cache[ref_id] = (int(elem), node, display.index)

                        if depth < max_depth and len(nodes) < max_nodes:
                            children_arr = self._copy_attr(elem, "AXChildren")
                            if children_arr:
                                try:
                                    if int(self._cf.CFGetTypeID(children_arr)) == self._cf_arr_type_id:
                                        child_count = int(self._cf.CFArrayGetCount(children_arr))
                                        # Limit fan-out per container so huge web DOMs don't stall traversal
                                        limit = min(child_count, 80)
                                        if child_count > limit:
                                            truncated = True
                                        for i in range(limit - 1, -1, -1):
                                            c_elem = self._cf.CFArrayGetValueAtIndex(children_arr, i)
                                            if c_elem:
                                                self._cf.CFRetain(c_elem)
                                                stack.append((c_elem, depth + 1))
                                finally:
                                    self._cf.CFRelease(children_arr)
                    finally:
                        self._cf.CFRelease(elem)
            finally:
                # Release any unpopped items still on the stack
                for leftover, _ in stack:
                    self._cf.CFRelease(leftover)
        finally:
            self._cf.CFRelease(app_ref)

        with self._lock:
            self.clear_cache()
            self._ref_cache = new_cache

        return UITreeSnapshot(
            app_name=app_title,
            pid=resolved_pid,
            display_index=display.index,
            backend="macos_ax",
            elements=tuple(nodes),
            truncated=truncated,
            captured_at=time.time(),
        )

    def get_cached_node(self, ref: str) -> Optional[tuple[UIElementNode, int]]:
        with self._lock:
            entry = self._ref_cache.get(ref)
            if not entry:
                return None
            return entry[1], entry[2]

    def perform_ax_action(
        self,
        *,
        ref: str,
        action: str,
        value: Optional[str] = None,
    ) -> AXActionResult:
        """Execute a background A11y action (press, set_value, focus, show_menu) on a cached ref."""
        if not self.available:
            return AXActionResult(
                ok=False,
                ref=ref,
                action=action,
                backend="macos_ax",
                method="unavailable",
                error="macOS 辅助功能权限未开启",
            )

        with self._lock:
            entry = self._ref_cache.get(ref)
            if not entry:
                return AXActionResult(
                    ok=False,
                    ref=ref,
                    action=action,
                    backend="macos_ax",
                    method="cache_miss",
                    error=f"未找到元素引用 '{ref}'，请先调用 computer_ui_tree 刷新 UI 树。",
                )
            raw_ptr, node, display_index = entry
            elem = ctypes.c_void_p(raw_ptr)
            self._cf.CFRetain(elem)

        try:
            act = action.strip().lower()
            if act in ("press", "ax_press", "click"):
                ax_act = self._cfstr("AXPress")
                try:
                    err = int(self._ax.AXUIElementPerformAction(elem, ax_act))
                finally:
                    self._cf.CFRelease(ax_act)
                if err == _KAX_ERROR_SUCCESS:
                    return AXActionResult(
                        ok=True,
                        ref=ref,
                        action=act,
                        backend="macos_ax",
                        method="ax_press",
                        center=node.center,
                        display_index=display_index,
                    )
                return AXActionResult(
                    ok=False,
                    ref=ref,
                    action=act,
                    backend="macos_ax",
                    method="ax_press",
                    center=node.center,
                    display_index=display_index,
                    error=f"AXPress 返回错误码 {err}",
                )

            if act in ("show_menu", "ax_show_menu"):
                ax_act = self._cfstr("AXShowMenu")
                try:
                    err = int(self._ax.AXUIElementPerformAction(elem, ax_act))
                finally:
                    self._cf.CFRelease(ax_act)
                return AXActionResult(
                    ok=(err == _KAX_ERROR_SUCCESS),
                    ref=ref,
                    action=act,
                    backend="macos_ax",
                    method="ax_show_menu",
                    center=node.center,
                    display_index=display_index,
                    error=None if err == _KAX_ERROR_SUCCESS else f"AXShowMenu 返回错误码 {err}",
                )

            if act in ("focus", "ax_focus"):
                attr_ref = self._cfstr("AXFocused")
                try:
                    err = int(self._ax.AXUIElementSetAttributeValue(elem, attr_ref, self._cf_bool_true))
                finally:
                    self._cf.CFRelease(attr_ref)
                return AXActionResult(
                    ok=(err == _KAX_ERROR_SUCCESS),
                    ref=ref,
                    action=act,
                    backend="macos_ax",
                    method="ax_focus",
                    center=node.center,
                    display_index=display_index,
                    error=None if err == _KAX_ERROR_SUCCESS else f"AXFocused 设置失败 (code={err})",
                )

            if act in ("set_value", "ax_set_value", "type"):
                if value is None:
                    return AXActionResult(
                        ok=False,
                        ref=ref,
                        action=act,
                        backend="macos_ax",
                        method="ax_set_value",
                        center=node.center,
                        display_index=display_index,
                        error="set_value 需要提供文本内容",
                    )
                attr_ref = self._cfstr("AXValue")
                val_ref = self._cfstr(str(value))
                try:
                    err = int(self._ax.AXUIElementSetAttributeValue(elem, attr_ref, val_ref))
                finally:
                    self._cf.CFRelease(attr_ref)
                    self._cf.CFRelease(val_ref)
                return AXActionResult(
                    ok=(err == _KAX_ERROR_SUCCESS),
                    ref=ref,
                    action=act,
                    backend="macos_ax",
                    method="ax_set_value",
                    center=node.center,
                    display_index=display_index,
                    error=None if err == _KAX_ERROR_SUCCESS else f"AXValue 设置失败 (code={err})",
                )

            return AXActionResult(
                ok=False,
                ref=ref,
                action=act,
                backend="macos_ax",
                method="unsupported",
                center=node.center,
                display_index=display_index,
                error=f"不支持的 A11y 动作: {action!r}",
            )
        finally:
            self._cf.CFRelease(elem)

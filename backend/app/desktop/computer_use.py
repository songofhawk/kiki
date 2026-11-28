"""Computer-Use tool declarations and executor for Kiki's built-in agent."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from ..tools import (
    WorkspaceToolExecutor,
    _SAFE_ENV_TEMPLATES,
    _SENSITIVE_BASENAMES,
    _SENSITIVE_EXTENSIONS,
)
from .manager import DesktopTakeoverManager
from .models import CapturedFrame, DesktopCapabilities, DesktopInputEvent

COMPUTER_USE_TOOL_NAMES = frozenset({"computer_screenshot", "computer_ui_tree", "computer_action"})

_MUTATING_ACTIONS = frozenset(
    {
        "click",
        "double_click",
        "right_click",
        "move",
        "drag",
        "scroll",
        "type",
        "key",
        "open_url",
        "open_app",
        "select_file_in_dialog",
        "switch_space",
        "set_clipboard",
    }
)

_AX_ACTIONS = frozenset({"ax_press", "ax_set_value", "ax_focus"})

_INPUT_INJECTION_ACTIONS = frozenset(
    {
        "click",
        "double_click",
        "right_click",
        "move",
        "drag",
        "scroll",
        "type",
        "key",
        "select_file_in_dialog",
        "switch_space",
    }
)

_HUMAN_TAKEOVER_CONFLICT_ERROR = (
    "主人当前正在主动接管控制服务器桌面，为避免抢夺鼠键，请先暂停操作或等待主人交还控制权。"
)

COMPUTER_USE_TOOL_DECLARATIONS: list[dict[str, Any]] = [
    {
        "type": "function",
        "name": "computer_screenshot",
        "description": (
            "Capture the current server desktop screenshot for visual inspection. "
            "Coordinates on the returned image use a normalized 0..1000 grid where "
            "(0, 0) is top-left and (1000, 1000) is bottom-right."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "display_index": {
                    "type": "integer",
                    "description": "Display index to capture (default 0).",
                },
                "save_artifact": {
                    "type": "boolean",
                    "description": "Whether to persist the screenshot as a run artifact (default false).",
                },
                "artifact_label": {
                    "type": "string",
                    "description": "Optional short Chinese label when saving the screenshot as an artifact.",
                },
            },
        },
    },
    {
        "type": "function",
        "name": "computer_ui_tree",
        "description": (
            "Inspect the structured OS Accessibility (A11y) / CUA UI tree for the frontmost app "
            "or a specified background app_name. Returns compact JSON elements with short refs "
            "('e1', 'e2', ...), roles, titles, values, supported AX actions, and 0..1000 bounds/centers. "
            "Prefer this over repeated screenshots for fast, accurate, low-token UI inspection."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "app_name": {
                    "type": "string",
                    "description": "Optional app name (e.g. 'Google Chrome', 'Finder'). Supports inspecting background apps without stealing focus.",
                },
                "display_index": {
                    "type": "integer",
                    "description": "Target display index (default active display).",
                },
                "interactive_only": {
                    "type": "boolean",
                    "description": "Only return interactive/actionable controls (default true).",
                },
                "query": {
                    "type": "string",
                    "description": "Optional case-insensitive substring filter on role/title/description/value.",
                },
                "max_depth": {
                    "type": "integer",
                    "description": "Maximum tree depth to traverse (default 8, max 20).",
                },
                "max_nodes": {
                    "type": "integer",
                    "description": "Maximum elements to return (default 120, max 400).",
                },
                "include_screenshot": {
                    "type": "boolean",
                    "description": "Also attach a desktop screenshot (default false to save tokens).",
                },
            },
        },
    },
    {
        "type": "function",
        "name": "computer_action",
        "description": (
            "Execute a desktop GUI action. Supports both Computer-Use 2.0 background A11y/CUA semantic "
            "actions by element ref ('ax_press', 'ax_set_value', 'ax_focus', or 'click'/'type' with 'ref') "
            "and pixel-coordinate actions ('click', 'double_click', 'right_click', 'move', 'drag', 'scroll', "
            "'type', 'key', 'wait', 'open_url', 'open_app', 'select_file_in_dialog', 'switch_space', "
            "'get_clipboard', 'set_clipboard'). Coordinates use the 0..1000 normalized grid."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": [
                        "ax_press",
                        "ax_set_value",
                        "ax_focus",
                        "click",
                        "double_click",
                        "right_click",
                        "move",
                        "drag",
                        "scroll",
                        "type",
                        "key",
                        "wait",
                        "open_url",
                        "open_app",
                        "select_file_in_dialog",
                        "switch_space",
                        "get_clipboard",
                        "set_clipboard",
                    ],
                    "description": "Desktop action to perform.",
                },
                "ref": {
                    "type": "string",
                    "description": "Element reference ID from computer_ui_tree (e.g. 'e3') for ax_press/ax_set_value/ax_focus or ref-targeted click/type.",
                },
                "fallback_to_click": {
                    "type": "boolean",
                    "description": "For ref-based actions: if background AX action fails, automatically fall back to clicking/typing at the element's center (default true).",
                },
                "x": {
                    "type": "number",
                    "description": "Horizontal coordinate on the 0..1000 normalized grid.",
                },
                "y": {
                    "type": "number",
                    "description": "Vertical coordinate on the 0..1000 normalized grid.",
                },
                "end_x": {
                    "type": "number",
                    "description": "Drag target horizontal coordinate on the 0..1000 normalized grid.",
                },
                "end_y": {
                    "type": "number",
                    "description": "Drag target vertical coordinate on the 0..1000 normalized grid.",
                },
                "button": {
                    "type": "string",
                    "enum": ["left", "right", "middle"],
                    "description": "Mouse button (default 'left').",
                },
                "delta_x": {
                    "type": "number",
                    "description": "Horizontal scroll delta in pixels (default 0).",
                },
                "delta_y": {
                    "type": "number",
                    "description": "Vertical scroll delta in pixels (default 0).",
                },
                "text": {
                    "type": "string",
                    "description": "Text to type or place into the clipboard.",
                },
                "paste": {
                    "type": "boolean",
                    "description": (
                        "Optional override for 'type': if omitted, automatically uses clipboard paste "
                        "for non-ASCII/CJK text, multiline text, or text longer than 40 characters."
                    ),
                },
                "replace": {
                    "type": "boolean",
                    "description": (
                        "For 'type' action: if true, select all (Cmd+A) and clear existing text in the "
                        "focused input before typing/pasting, saving multiple turns."
                    ),
                },
                "key": {
                    "type": "string",
                    "description": "Key name for 'key' action (e.g. 'enter', 'tab', 'escape', 'backspace', 'a', 'v', 'arrowdown').",
                },
                "modifiers": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional modifier keys (e.g. ['cmd'], ['cmd', 'shift'], ['ctrl']).",
                },
                "seconds": {
                    "type": "number",
                    "description": "Duration in seconds (0.2..30.0) for 'wait' action.",
                },
                "url": {
                    "type": "string",
                    "description": "Public http:// or https:// URL for 'open_url'.",
                },
                "app_name": {
                    "type": "string",
                    "description": "macOS application name for 'open_app' (e.g. 'Google Chrome', 'Safari').",
                },
                "file_path": {
                    "type": "string",
                    "description": (
                        "Workspace-relative or absolute path to a local video/image/file to select "
                        "in an open macOS file dialog ('select_file_in_dialog')."
                    ),
                },
                "direction": {
                    "type": "string",
                    "enum": ["prev", "next"],
                    "description": "Space switch direction for 'switch_space'.",
                },
                "target_index": {
                    "type": "integer",
                    "description": "Target space index for 'switch_space'.",
                },
                "display_index": {
                    "type": "integer",
                    "description": "Target display index (default 0).",
                },
                "wait_after": {
                    "type": "number",
                    "description": "Seconds to wait after the action before capturing a screenshot (default 0.6, clamped 0..10).",
                },
                "return_screenshot": {
                    "type": "boolean",
                    "description": "Whether to return a fresh post-action screenshot (default true).",
                },
                "save_artifact": {
                    "type": "boolean",
                    "description": "Whether to save the post-action screenshot as a run artifact (default false).",
                },
                "artifact_label": {
                    "type": "string",
                    "description": "Optional short Chinese label when saving the screenshot as an artifact.",
                },
            },
            "required": ["action"],
        },
    },
]


def _is_sensitive_dialog_file(target: Path) -> bool:
    name = target.name.lower()
    if name == ".env" or (name.startswith(".env.") and name not in _SAFE_ENV_TEMPLATES):
        return True
    if name in _SENSITIVE_BASENAMES or target.suffix.lower() in _SENSITIVE_EXTENSIONS:
        return True
    parts = [p.lower() for p in target.parts]
    if ".git" in parts and name in {"config", "credentials"}:
        return True
    return False


@dataclass
class ComputerUseResult:
    """Structured result returned by ComputerUseExecutor."""

    text: str
    is_error: bool = False
    image_bytes: Optional[bytes] = None
    mime_type: Optional[str] = None
    artifact: Optional[dict[str, Any]] = None

    def to_function_result_value(self) -> Any:
        if self.image_bytes and self.mime_type:
            return [
                {"type": "text", "text": self.text},
                {
                    "type": "image",
                    "data": base64.b64encode(self.image_bytes).decode("ascii"),
                    "mime_type": self.mime_type,
                },
            ]
        return self.text


class ComputerUseExecutor:
    """Executes GUI Computer-Use tools against the active DesktopDriver."""

    def __init__(
        self,
        desktop_manager: DesktopTakeoverManager,
        tool_executor: WorkspaceToolExecutor,
        *,
        max_width: int = 1280,
        quality: int = 70,
    ) -> None:
        self.desktop_manager = desktop_manager
        self.tool_executor = tool_executor
        self.max_width = max(320, min(2560, int(max_width)))
        self.quality = max(30, min(95, int(quality)))
        self._active_display_index: int = 0

    def _resolve_display_index(self, raw_display: Any, caps: DesktopCapabilities) -> int:
        max_idx = max(0, len(caps.displays) - 1)
        if raw_display is not None:
            resolved = min(max(0, int(raw_display)), max_idx)
            self._active_display_index = resolved
            return resolved
        self._active_display_index = min(max(0, self._active_display_index), max_idx)
        return self._active_display_index

    async def _collect_display_context(
        self,
        caps: DesktopCapabilities,
        *,
        preferred_app: Optional[str] = None,
    ) -> tuple[list[dict[str, Any]], Optional[int], Optional[str]]:
        driver = self.desktop_manager.driver
        windows_by_display: dict[int, list[str]] = {}
        list_win_fn = getattr(driver, "list_visible_windows_by_display", None)
        if callable(list_win_fn):
            try:
                windows_by_display = await asyncio.to_thread(list_win_fn)
            except Exception:
                windows_by_display = {}

        front_display: Optional[int] = None
        front_app: Optional[str] = None
        front_fn = getattr(driver, "get_frontmost_window_display", None)
        if callable(front_fn):
            try:
                front_display, front_app = await asyncio.to_thread(
                    front_fn, preferred_app=preferred_app
                )
            except Exception:
                front_display, front_app = None, None

        displays_summary = [
            {
                "index": d.index,
                "name": d.name,
                "width": d.width,
                "height": d.height,
                "is_main": d.is_main,
                "windows": windows_by_display.get(d.index, []),
            }
            for d in caps.displays
        ]
        return displays_summary, front_display, front_app

    @staticmethod
    def _norm_coord(val: Any) -> float:
        clamped = max(0.0, min(1000.0, float(val)))
        return clamped / 1000.0

    @staticmethod
    def _error_result(message: str, **extra: Any) -> ComputerUseResult:
        payload: dict[str, Any] = {"ok": False, "error": message}
        payload.update(extra)
        return ComputerUseResult(text=json.dumps(payload, ensure_ascii=False), is_error=True)

    def _check_human_takeover_conflict(self) -> Optional[ComputerUseResult]:
        active = self.desktop_manager.get_active_session()
        if active is not None and active.control_mode == "control":
            return self._error_result(_HUMAN_TAKEOVER_CONFLICT_ERROR)
        return None

    async def _capture_and_optionally_save(
        self,
        run_id: str,
        *,
        display_index: int,
        save_artifact: bool,
        artifact_label: str,
        default_label: str,
    ) -> tuple[CapturedFrame, Optional[dict[str, Any]]]:
        frame = await asyncio.to_thread(
            self.desktop_manager.driver.capture_frame,
            display_index,
            self.max_width,
            self.quality,
        )
        artifact: Optional[dict[str, Any]] = None
        if save_artifact:
            ext = ".png" if "png" in frame.mime_type.lower() else ".jpg"
            filename = f"desktop_{int(time.time() * 1000)}_{uuid.uuid4().hex[:6]}{ext}"
            artifact = await asyncio.to_thread(
                self.tool_executor.write_binary_artifact,
                run_id,
                filename,
                frame.data,
                "image",
                artifact_label or default_label,
            )
        return frame, artifact

    async def _handle_screenshot(self, run_id: str, arguments: dict[str, Any]) -> ComputerUseResult:
        driver = self.desktop_manager.driver
        caps: DesktopCapabilities = await asyncio.to_thread(driver.probe)
        if not caps.available:
            return self._error_result(caps.error or "当前服务器桌面不可用")
        if not caps.permissions.screen_recording:
            return self._error_result("屏幕录制权限未开启，无法截取服务器桌面画面。")

        display_index = self._resolve_display_index(arguments.get("display_index"), caps)
        save_artifact = bool(arguments.get("save_artifact", False))
        artifact_label = str(arguments.get("artifact_label") or "").strip()

        frame, artifact = await self._capture_and_optionally_save(
            run_id,
            display_index=display_index,
            save_artifact=save_artifact,
            artifact_label=artifact_label,
            default_label="桌面截图",
        )
        displays_summary, front_display, front_app = await self._collect_display_context(caps)
        meta: dict[str, Any] = {
            "ok": True,
            "action": "screenshot",
            "display_index": frame.display_index,
            "width": frame.width,
            "height": frame.height,
            "coord_space": "0..1000",
            "displays": displays_summary,
        }
        if front_app is not None:
            meta["frontmost_app"] = front_app
        if front_display is not None:
            meta["frontmost_display_index"] = front_display
            if len(caps.displays) > 1 and front_display != frame.display_index:
                meta["hint"] = (
                    f"当前前台窗口（{front_app or '未知应用'}）位于显示器 {front_display}，"
                    f"而本次截取的是显示器 {frame.display_index}；如需查看该窗口请传 display_index={front_display}。"
                )
        if artifact:
            meta["artifact_id"] = artifact.get("id")
            meta["artifact_path"] = artifact.get("file_path")
        return ComputerUseResult(
            text=json.dumps(meta, ensure_ascii=False),
            is_error=False,
            image_bytes=frame.data,
            mime_type=frame.mime_type,
            artifact=artifact,
        )

    async def _send_input(self, event: DesktopInputEvent) -> None:
        await asyncio.to_thread(self.desktop_manager.driver.send_input, event)

    async def _handle_type(
        self,
        *,
        text: str,
        paste_override: Any,
        replace: bool,
        display_index: int,
        caps: DesktopCapabilities,
    ) -> dict[str, Any]:
        driver = self.desktop_manager.driver
        paste_mod = "cmd" if caps.platform in ("Darwin", "mock") else "ctrl"
        if replace:
            await self._send_input(
                DesktopInputEvent(
                    action="key_combo",
                    key="a",
                    modifiers=(paste_mod,),
                    display_index=display_index,
                )
            )
            await asyncio.sleep(0.05)
            await self._send_input(
                DesktopInputEvent(
                    action="key_tap",
                    key="backspace",
                    display_index=display_index,
                )
            )
            await asyncio.sleep(0.05)

        if paste_override is None:
            use_paste = any(ord(c) > 127 for c in text) or "\n" in text or len(text) > 40
        else:
            use_paste = bool(paste_override)

        if use_paste:
            prev_clipboard: Optional[str] = None
            try:
                prev_clipboard = await asyncio.to_thread(driver.get_clipboard)
            except Exception:
                prev_clipboard = None

            await asyncio.to_thread(driver.set_clipboard, text)
            await asyncio.sleep(0.05)
            await self._send_input(
                DesktopInputEvent(
                    action="key_combo",
                    key="v",
                    modifiers=(paste_mod,),
                    display_index=display_index,
                )
            )
            await asyncio.sleep(0.25)

            if prev_clipboard is not None:
                try:
                    await asyncio.to_thread(driver.set_clipboard, prev_clipboard)
                except Exception:
                    pass
            return {"method": "paste", "length": len(text), "replace": replace}

        await self._send_input(
            DesktopInputEvent(
                action="type_text",
                text=text,
                display_index=display_index,
            )
        )
        return {"method": "type_text", "length": len(text), "replace": replace}

    async def _handle_drag(
        self,
        *,
        x: Any,
        y: Any,
        end_x: Any,
        end_y: Any,
        button: str,
        display_index: int,
    ) -> dict[str, Any]:
        nx, ny = self._norm_coord(x), self._norm_coord(y)
        nex, ney = self._norm_coord(end_x), self._norm_coord(end_y)
        btn = button if button in ("left", "right", "middle") else "left"

        await self._send_input(
            DesktopInputEvent(action="mouse_move", x=nx, y=ny, display_index=display_index)
        )
        await self._send_input(
            DesktopInputEvent(
                action="mouse_down",
                x=nx,
                y=ny,
                button=btn,  # type: ignore[arg-type]
                display_index=display_index,
            )
        )
        steps = 5
        for step in range(1, steps + 1):
            t = step / float(steps)
            mx = nx + (nex - nx) * t
            my = ny + (ney - ny) * t
            await self._send_input(
                DesktopInputEvent(action="mouse_move", x=mx, y=my, display_index=display_index)
            )
            await asyncio.sleep(0.01)
        await self._send_input(
            DesktopInputEvent(
                action="mouse_up",
                x=nex,
                y=ney,
                button=btn,  # type: ignore[arg-type]
                display_index=display_index,
            )
        )
        return {"from": [float(x), float(y)], "to": [float(end_x), float(end_y)], "steps": steps}

    async def _handle_select_file_in_dialog(
        self,
        *,
        raw_file_path: str,
        display_index: int,
    ) -> dict[str, Any]:
        cleaned = (raw_file_path or "").strip()
        if not cleaned:
            raise ValueError("select_file_in_dialog requires a non-empty 'file_path'")
        expanded = Path(os.path.expanduser(cleaned))
        resolved = (
            expanded.resolve()
            if expanded.is_absolute()
            else (self.tool_executor.workspace_root / expanded).resolve()
        )
        if _is_sensitive_dialog_file(resolved):
            raise PermissionError(f"Access denied to sensitive file: {cleaned}")
        if not resolved.exists() or not resolved.is_file():
            raise FileNotFoundError(f"File not found: {resolved}")

        driver = self.desktop_manager.driver
        await self._send_input(
            DesktopInputEvent(
                action="key_combo",
                key="g",
                modifiers=("cmd", "shift"),
                display_index=display_index,
            )
        )
        await asyncio.sleep(0.25)
        await asyncio.to_thread(driver.set_clipboard, str(resolved))
        await self._send_input(
            DesktopInputEvent(
                action="key_combo",
                key="v",
                modifiers=("cmd",),
                display_index=display_index,
            )
        )
        await asyncio.sleep(0.15)
        await self._send_input(
            DesktopInputEvent(action="key_tap", key="enter", display_index=display_index)
        )
        await asyncio.sleep(0.35)
        await self._send_input(
            DesktopInputEvent(action="key_tap", key="enter", display_index=display_index)
        )
        return {"selected_file": str(resolved)}

    async def _handle_ui_tree(self, run_id: str, arguments: dict[str, Any]) -> ComputerUseResult:
        driver = self.desktop_manager.driver
        caps: DesktopCapabilities = await asyncio.to_thread(driver.probe)
        if not caps.available:
            return self._error_result(caps.error or "当前服务器桌面不可用")
        if not caps.permissions.accessibility:
            return self._error_result("辅助功能权限未开启，无法读取桌面 A11y UI 树。")

        get_tree_fn = getattr(driver, "get_ui_tree", None)
        if not callable(get_tree_fn):
            return self._error_result("当前桌面驱动不支持 A11y UI 树读取，请使用 computer_screenshot。")

        display_index = self._resolve_display_index(arguments.get("display_index"), caps)
        app_name = str(arguments.get("app_name") or "").strip() or None
        interactive_only = bool(arguments.get("interactive_only", True))
        query = str(arguments.get("query") or "").strip() or None
        max_depth = max(1, min(20, int(arguments.get("max_depth") or 8)))
        max_nodes = max(10, min(400, int(arguments.get("max_nodes") or 120)))
        include_screenshot = bool(arguments.get("include_screenshot", False))

        snapshot = await asyncio.to_thread(
            get_tree_fn,
            display_index=display_index,
            app_name=app_name,
            interactive_only=interactive_only,
            query=query,
            max_depth=max_depth,
            max_nodes=max_nodes,
        )
        self._active_display_index = snapshot.display_index

        meta: dict[str, Any] = {
            "ok": True,
            "action": "ui_tree",
            "cua_driver_available": caps.cua_driver_available,
            **snapshot.to_dict(),
        }

        frame: Optional[CapturedFrame] = None
        if include_screenshot:
            if not caps.permissions.screen_recording:
                meta["screenshot_warning"] = "屏幕录制权限未开启，已仅返回 A11y UI 树。"
            else:
                frame, _ = await self._capture_and_optionally_save(
                    run_id,
                    display_index=snapshot.display_index,
                    save_artifact=False,
                    artifact_label="",
                    default_label="桌面截图",
                )
                meta["width"] = frame.width
                meta["height"] = frame.height

        return ComputerUseResult(
            text=json.dumps(meta, ensure_ascii=False),
            is_error=False,
            image_bytes=frame.data if frame else None,
            mime_type=frame.mime_type if frame else None,
        )

    async def _handle_ax_ref_action(
        self,
        *,
        action: str,
        ref: str,
        text: Optional[str],
        replace: bool,
        paste_override: Any,
        fallback_to_click: bool,
        display_index: int,
        caps: DesktopCapabilities,
    ) -> tuple[Optional[dict[str, Any]], Optional[ComputerUseResult]]:
        """Execute a background A11y/CUA action on `ref`, with optional coordinate fallback."""
        driver = self.desktop_manager.driver
        perform_fn = getattr(driver, "perform_ax_action", None)
        if not callable(perform_fn):
            return None, self._error_result("当前桌面驱动不支持 A11y 元素引用操作。")

        ax_verb = {
            "ax_press": "press",
            "click": "press",
            "ax_set_value": "set_value",
            "type": "set_value",
            "ax_focus": "focus",
        }.get(action, action)

        ax_res = await asyncio.to_thread(
            perform_fn,
            ref=ref,
            action=ax_verb,
            value=text,
            display_index=display_index,
        )
        if ax_res.ok:
            return {
                "ref": ref,
                "backend": ax_res.backend,
                "method": ax_res.method,
                "background": True,
                **({"center": list(ax_res.center)} if ax_res.center else {}),
            }, None

        # Fallback to exact center-coordinate HID click/type when allowed and center is known
        center = ax_res.center
        target_display = ax_res.display_index
        if center is None:
            cached_fn = getattr(driver, "get_cached_ui_node", None)
            if callable(cached_fn):
                cached = cached_fn(ref)
                if cached:
                    center = cached[0].center
                    target_display = cached[1]

        if not fallback_to_click or center is None or ax_verb == "focus":
            return None, self._error_result(
                ax_res.error or f"A11y 动作 {action} 失败 (ref={ref})",
                ref=ref,
                backend=ax_res.backend,
            )

        conflict = self._check_human_takeover_conflict()
        if conflict is not None:
            return None, conflict

        nx, ny = self._norm_coord(center[0]), self._norm_coord(center[1])
        await self._send_input(
            DesktopInputEvent(
                action="mouse_click",
                x=nx,
                y=ny,
                button="left",
                display_index=target_display,
            )
        )
        if ax_verb == "set_value" and text is not None:
            await asyncio.sleep(0.08)
            type_detail = await self._handle_type(
                text=str(text),
                paste_override=paste_override,
                replace=replace,
                display_index=target_display,
                caps=caps,
            )
            return {
                "ref": ref,
                "backend": ax_res.backend,
                "method": "fallback_click_type",
                "background": False,
                "ax_error": ax_res.error,
                "center": [center[0], center[1]],
                **type_detail,
            }, None

        return {
            "ref": ref,
            "backend": ax_res.backend,
            "method": "fallback_click",
            "background": False,
            "ax_error": ax_res.error,
            "center": [center[0], center[1]],
        }, None

    async def _handle_action(self, run_id: str, arguments: dict[str, Any]) -> ComputerUseResult:
        action = str(arguments.get("action") or "").strip().lower()
        if not action:
            return self._error_result("Missing required parameter: 'action'")

        ref = str(arguments.get("ref") or "").strip() or None
        is_ref_action = action in _AX_ACTIONS or (ref is not None and action in ("click", "type"))

        if action in _MUTATING_ACTIONS and not is_ref_action:
            conflict = self._check_human_takeover_conflict()
            if conflict is not None:
                return conflict

        driver = self.desktop_manager.driver
        caps: DesktopCapabilities = await asyncio.to_thread(driver.probe)
        if not caps.available:
            return self._error_result(caps.error or "当前服务器桌面不可用")

        if (action in _INPUT_INJECTION_ACTIONS or is_ref_action) and (
            not caps.supports_control or not caps.permissions.accessibility
        ):
            return self._error_result("辅助功能权限未开启，无法执行桌面鼠键或 A11y 操作。")

        if action in ("get_clipboard", "set_clipboard") and not caps.supports_clipboard:
            return self._error_result("当前桌面驱动不支持剪贴板读写。")

        explicit_display = arguments.get("display_index") is not None
        display_index = self._resolve_display_index(arguments.get("display_index"), caps)
        button_raw = str(arguments.get("button") or "left").strip().lower()
        button = button_raw if button_raw in ("left", "right", "middle") else "left"

        detail: dict[str, Any] = {}

        if is_ref_action:
            if not ref:
                return self._error_result(f"Action '{action}' requires 'ref' from computer_ui_tree.")
            text_val = arguments.get("text")
            if action in ("ax_set_value", "type") and text_val is None:
                return self._error_result(f"Action '{action}' requires 'text'.")
            ax_detail, ax_err = await self._handle_ax_ref_action(
                action=action,
                ref=ref,
                text=str(text_val) if text_val is not None else None,
                replace=bool(arguments.get("replace", True)),
                paste_override=arguments.get("paste"),
                fallback_to_click=bool(arguments.get("fallback_to_click", True)),
                display_index=display_index,
                caps=caps,
            )
            if ax_err is not None:
                return ax_err
            detail = ax_detail or {}

        elif action in ("click", "double_click", "right_click", "move"):
            x, y = arguments.get("x"), arguments.get("y")
            if x is None or y is None:
                return self._error_result(f"Action '{action}' requires 'x' and 'y' coordinates.")
            nx, ny = self._norm_coord(x), self._norm_coord(y)
            if action == "click":
                await self._send_input(
                    DesktopInputEvent(
                        action="mouse_click",
                        x=nx,
                        y=ny,
                        button=button,  # type: ignore[arg-type]
                        display_index=display_index,
                    )
                )
            elif action == "double_click":
                await self._send_input(
                    DesktopInputEvent(
                        action="mouse_double_click",
                        x=nx,
                        y=ny,
                        button=button,  # type: ignore[arg-type]
                        display_index=display_index,
                    )
                )
            elif action == "right_click":
                await self._send_input(
                    DesktopInputEvent(
                        action="mouse_click",
                        x=nx,
                        y=ny,
                        button="right",
                        display_index=display_index,
                    )
                )
            else:
                await self._send_input(
                    DesktopInputEvent(
                        action="mouse_move",
                        x=nx,
                        y=ny,
                        display_index=display_index,
                    )
                )
            detail = {"x": float(x), "y": float(y)}

        elif action == "drag":
            x, y = arguments.get("x"), arguments.get("y")
            end_x, end_y = arguments.get("end_x"), arguments.get("end_y")
            if x is None or y is None or end_x is None or end_y is None:
                return self._error_result("Action 'drag' requires 'x', 'y', 'end_x', and 'end_y'.")
            detail = await self._handle_drag(
                x=x,
                y=y,
                end_x=end_x,
                end_y=end_y,
                button=button,
                display_index=display_index,
            )

        elif action == "scroll":
            x, y = arguments.get("x"), arguments.get("y")
            nx = self._norm_coord(x) if x is not None else None
            ny = self._norm_coord(y) if y is not None else None
            if nx is not None and ny is not None:
                await self._send_input(
                    DesktopInputEvent(
                        action="mouse_move",
                        x=nx,
                        y=ny,
                        display_index=display_index,
                    )
                )
            delta_x = float(arguments.get("delta_x") or 0.0)
            delta_y = float(arguments.get("delta_y") or 0.0)
            await self._send_input(
                DesktopInputEvent(
                    action="mouse_scroll",
                    x=nx,
                    y=ny,
                    delta_x=delta_x,
                    delta_y=delta_y,
                    display_index=display_index,
                )
            )
            detail = {"delta_x": delta_x, "delta_y": delta_y}

        elif action == "type":
            text = arguments.get("text")
            if text is None or str(text) == "":
                return self._error_result("Action 'type' requires non-empty 'text'.")
            detail = await self._handle_type(
                text=str(text),
                paste_override=arguments.get("paste"),
                replace=bool(arguments.get("replace", False)),
                display_index=display_index,
                caps=caps,
            )

        elif action == "key":
            key = str(arguments.get("key") or "").strip()
            if not key:
                return self._error_result("Action 'key' requires 'key'.")
            raw_mods = arguments.get("modifiers") or []
            mods = tuple(str(m).strip().lower() for m in raw_mods if str(m).strip())
            input_action = "key_combo" if mods else "key_tap"
            await self._send_input(
                DesktopInputEvent(
                    action=input_action,  # type: ignore[arg-type]
                    key=key,
                    modifiers=mods,
                    display_index=display_index,
                )
            )
            detail = {"key": key, "modifiers": list(mods)}

        elif action == "wait":
            raw_secs = arguments.get("seconds")
            seconds = max(0.2, min(30.0, float(1.0 if raw_secs is None else raw_secs)))
            await asyncio.sleep(seconds)
            detail = {"seconds": seconds}

        elif action == "open_url":
            url = str(arguments.get("url") or "").strip()
            if not url:
                return self._error_result("Action 'open_url' requires 'url'.")
            await asyncio.to_thread(driver.open_url, url)
            detail = {"url": url}

        elif action == "open_app":
            app_name = str(arguments.get("app_name") or "").strip()
            if not app_name:
                return self._error_result("Action 'open_app' requires 'app_name'.")
            await asyncio.to_thread(driver.open_app, app_name)
            detail = {"app_name": app_name}

        elif action == "select_file_in_dialog":
            detail = await self._handle_select_file_in_dialog(
                raw_file_path=str(arguments.get("file_path") or ""),
                display_index=display_index,
            )

        elif action == "switch_space":
            raw_dir = arguments.get("direction")
            direction = raw_dir if raw_dir in ("prev", "next") else None
            raw_target = arguments.get("target_index")
            target_index = int(raw_target) if raw_target is not None else None
            if direction is None and target_index is None:
                return self._error_result(
                    "Action 'switch_space' requires 'direction' ('prev'/'next') or 'target_index'."
                )
            await asyncio.to_thread(
                driver.switch_space,
                display_index=display_index,
                target_index=target_index,
                direction=direction,  # type: ignore[arg-type]
            )
            detail = {"direction": direction, "target_index": target_index}

        elif action == "get_clipboard":
            clip = await asyncio.to_thread(driver.get_clipboard)
            detail = {"clipboard": clip}

        elif action == "set_clipboard":
            text = arguments.get("text")
            if text is None:
                return self._error_result("Action 'set_clipboard' requires 'text'.")
            await asyncio.to_thread(driver.set_clipboard, str(text))
            detail = {"length": len(str(text))}

        else:
            return self._error_result(f"Unsupported computer_action: {action!r}")

        raw_wait = arguments.get("wait_after")
        if raw_wait is None and action in ("wait", "get_clipboard"):
            wait_after = 0.0
        elif raw_wait is None and action in ("open_url", "open_app"):
            wait_after = 1.5
        elif raw_wait is None and is_ref_action:
            wait_after = 0.25
        else:
            wait_after = max(0.0, min(10.0, float(0.6 if raw_wait is None else raw_wait)))
        if wait_after > 0:
            await asyncio.sleep(wait_after)

        preferred_app = str(arguments.get("app_name") or "").strip() or None if action == "open_app" else None
        displays_summary, front_display, front_app = await self._collect_display_context(
            caps, preferred_app=preferred_app
        )

        # Auto-follow the newly focused window's display on open_url / open_app when caller
        # didn't pin display_index, so multi-monitor setups capture the actual browser/app screen.
        auto_switched_display = False
        if (
            not explicit_display
            and action in ("open_url", "open_app")
            and front_display is not None
            and 0 <= front_display < max(1, len(caps.displays))
            and front_display != display_index
        ):
            display_index = front_display
            self._active_display_index = front_display
            auto_switched_display = True

        default_return_screenshot = not (action in _AX_ACTIONS)
        return_screenshot = bool(arguments.get("return_screenshot", default_return_screenshot))
        save_artifact = bool(arguments.get("save_artifact", False))
        artifact_label = str(arguments.get("artifact_label") or "").strip()

        frame: Optional[CapturedFrame] = None
        artifact: Optional[dict[str, Any]] = None
        if return_screenshot or save_artifact:
            if not caps.permissions.screen_recording:
                return self._error_result("操作已执行，但屏幕录制权限未开启，无法截取桌面画面。", action=action, **detail)
            frame, artifact = await self._capture_and_optionally_save(
                run_id,
                display_index=display_index,
                save_artifact=save_artifact,
                artifact_label=artifact_label,
                default_label=f"桌面操作截图 ({action})",
            )

        meta: dict[str, Any] = {
            "ok": True,
            "action": action,
            "display_index": frame.display_index if frame else display_index,
            "coord_space": "0..1000",
            "displays": displays_summary,
            **detail,
        }
        if front_app is not None:
            meta["frontmost_app"] = front_app
        if front_display is not None:
            meta["frontmost_display_index"] = front_display
        if auto_switched_display:
            meta["auto_switched_display"] = True
        if frame:
            meta["width"] = frame.width
            meta["height"] = frame.height
        if artifact:
            meta["artifact_id"] = artifact.get("id")
            meta["artifact_path"] = artifact.get("file_path")

        return ComputerUseResult(
            text=json.dumps(meta, ensure_ascii=False),
            is_error=False,
            image_bytes=frame.data if (frame and return_screenshot) else None,
            mime_type=frame.mime_type if (frame and return_screenshot) else None,
            artifact=artifact,
        )

    async def execute(self, run_id: str, name: str, arguments: dict[str, Any]) -> ComputerUseResult:
        """Execute a Computer-Use tool call for a specific SessionRun."""
        try:
            args = arguments if isinstance(arguments, dict) else {}
            if name == "computer_screenshot":
                return await self._handle_screenshot(run_id, args)
            if name == "computer_ui_tree":
                return await self._handle_ui_tree(run_id, args)
            if name == "computer_action":
                return await self._handle_action(run_id, args)
            return self._error_result(f"Unknown computer use tool: {name}")
        except Exception as exc:
            return self._error_result(f"Tool '{name}' failed: {exc}")

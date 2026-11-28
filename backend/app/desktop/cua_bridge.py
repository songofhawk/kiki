"""Optional bridge to trycua/cua (cua-driver) for background-first Computer-Use 2.0."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from dataclasses import asdict, dataclass
from typing import Any, Optional

from .models import AXActionResult, DisplayInfo, UIElementNode, UITreeSnapshot

CUA_DRIVER_INSTALL_HINT = (
    '可选安装 trycua/cua-driver 以启用跨应用后台无焦点点击与增强 A11y：'
    '/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/trycua/cua/main/libs/cua-driver/scripts/install.sh)"'
)


@dataclass(frozen=True)
class CuaDriverStatus:
    """Availability probe result for external trycua/cua-driver."""

    mode: str
    available: bool
    binary_path: Optional[str] = None
    error: Optional[str] = None
    install_hint: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class CuaDriverBridge:
    """Thin subprocess bridge to `cua-driver call <tool> '<json>'` with graceful fallback.

    When `mode="auto"` (default), Kiki automatically uses `cua-driver` if installed on the host,
    and otherwise falls back seamlessly to Kiki's built-in macOS A11y + CoreGraphics driver.
    """

    def __init__(
        self,
        *,
        mode: str = "auto",
        binary: Optional[str] = None,
        timeout_seconds: float = 10.0,
    ) -> None:
        cleaned_mode = (mode or "auto").strip().lower()
        self.mode = cleaned_mode if cleaned_mode in ("auto", "prefer", "off") else "auto"
        self.binary_override = (binary or "").strip() or None
        self.timeout_seconds = max(2.0, min(30.0, float(timeout_seconds)))
        self._resolved_bin: Optional[str] = None
        self._checked_at: float = 0.0
        self._cache_ttl: float = 15.0

    def _resolve_binary(self) -> Optional[str]:
        if self.mode == "off":
            return None
        now = time.time()
        if (now - self._checked_at) < self._cache_ttl:
            return self._resolved_bin
        self._checked_at = now
        if self.binary_override:
            if os.path.isfile(self.binary_override) and os.access(self.binary_override, os.X_OK):
                self._resolved_bin = self.binary_override
            else:
                self._resolved_bin = shutil.which(self.binary_override)
        else:
            self._resolved_bin = shutil.which("cua-driver")
        return self._resolved_bin

    def probe(self) -> CuaDriverStatus:
        if self.mode == "off":
            return CuaDriverStatus(mode="off", available=False, error="disabled via config")
        binary = self._resolve_binary()
        if not binary:
            return CuaDriverStatus(
                mode=self.mode,
                available=False,
                binary_path=None,
                error="cua-driver binary not found on PATH",
                install_hint=CUA_DRIVER_INSTALL_HINT,
            )
        return CuaDriverStatus(mode=self.mode, available=True, binary_path=binary)

    @property
    def is_available(self) -> bool:
        return self._resolve_binary() is not None

    def call_tool(self, tool_name: str, arguments: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        """Invoke `cua-driver call <tool_name> '<json>'` and parse its JSON response."""
        binary = self._resolve_binary()
        if not binary:
            raise RuntimeError("cua-driver is not installed or disabled")
        payload = json.dumps(arguments or {}, ensure_ascii=False)
        res = subprocess.run(
            [binary, "call", tool_name, payload],
            capture_output=True,
            text=True,
            timeout=self.timeout_seconds,
            check=False,
        )
        stdout = (res.stdout or "").strip()
        stderr = (res.stderr or "").strip()
        if res.returncode != 0:
            raise RuntimeError(stderr or stdout or f"cua-driver exited with code {res.returncode}")
        if not stdout:
            return {"ok": True}
        try:
            parsed = json.loads(stdout)
            return parsed if isinstance(parsed, dict) else {"ok": True, "data": parsed}
        except json.JSONDecodeError:
            return {"ok": True, "raw_output": stdout}

    @staticmethod
    def _parse_elements_from_payload(
        payload: dict[str, Any],
        display: DisplayInfo,
        max_nodes: int,
    ) -> tuple[list[UIElementNode], bool]:
        raw_nodes: list[dict[str, Any]] = []
        for key in ("elements", "nodes", "tree", "accessibility_tree"):
            candidate = payload.get(key)
            if isinstance(candidate, list):
                raw_nodes = [item for item in candidate if isinstance(item, dict)]
                break

        elements: list[UIElementNode] = []
        truncated = len(raw_nodes) > max_nodes
        for idx, item in enumerate(raw_nodes[:max_nodes], start=1):
            ref = str(item.get("ref") or item.get("id") or f"cua_{idx}")
            role = str(item.get("role") or item.get("type") or "AXElement")
            title = str(item.get("title") or item.get("name") or item.get("label") or "")[:120]
            desc = str(item.get("description") or "")[:120]
            value = str(item.get("value") or item.get("text") or "")[:120]
            enabled = bool(item.get("enabled", True))
            focused = bool(item.get("focused", False))
            raw_actions = item.get("actions") or []
            actions = tuple(str(a) for a in raw_actions if a) if isinstance(raw_actions, list) else ()

            bounds: Optional[tuple[float, float, float, float]] = None
            center: Optional[tuple[float, float]] = None
            raw_bounds = item.get("bounds") or item.get("bbox") or item.get("frame")
            if isinstance(raw_bounds, dict) and display.width > 0 and display.height > 0:
                bx = float(raw_bounds.get("x", 0.0))
                by = float(raw_bounds.get("y", 0.0))
                bw = float(raw_bounds.get("width", raw_bounds.get("w", 0.0)))
                bh = float(raw_bounds.get("height", raw_bounds.get("h", 0.0)))
                if bw > 0 and bh > 0:
                    nx = ((bx - display.origin_x) / float(display.width)) * 1000.0
                    ny = ((by - display.origin_y) / float(display.height)) * 1000.0
                    nw = (bw / float(display.width)) * 1000.0
                    nh = (bh / float(display.height)) * 1000.0
                    bounds = (round(nx, 1), round(ny, 1), round(nw, 1), round(nh, 1))
                    center = (
                        round(max(0.0, min(1000.0, nx + nw * 0.5)), 1),
                        round(max(0.0, min(1000.0, ny + nh * 0.5)), 1),
                    )

            elements.append(
                UIElementNode(
                    ref=ref,
                    role=role,
                    title=title,
                    description=desc,
                    value=value,
                    enabled=enabled,
                    focused=focused,
                    actions=actions,
                    bounds=bounds,
                    center=center,
                    depth=int(item.get("depth") or 0),
                )
            )
        return elements, truncated

    def get_ui_tree(
        self,
        *,
        display: DisplayInfo,
        app_name: Optional[str] = None,
        max_nodes: int = 120,
    ) -> Optional[UITreeSnapshot]:
        """Query `cua-driver` for window/desktop accessibility state if installed."""
        if not self.is_available:
            return None
        tool_name = "get_window_state" if app_name else "get_desktop_state"
        args: dict[str, Any] = {"include_screenshot": False}
        if app_name:
            args["app_name"] = app_name
        try:
            payload = self.call_tool(tool_name, args)
        except Exception:
            return None
        elements, truncated = self._parse_elements_from_payload(payload, display, max_nodes)
        if not elements:
            return None
        return UITreeSnapshot(
            app_name=str(payload.get("app_name") or app_name or "Desktop"),
            pid=int(payload["pid"]) if isinstance(payload.get("pid"), int) else None,
            display_index=display.index,
            backend="cua_driver",
            elements=tuple(elements),
            truncated=truncated,
            captured_at=time.time(),
        )

    def perform_ax_action(
        self,
        *,
        ref: str,
        action: str,
        value: Optional[str] = None,
        display_index: int = 0,
    ) -> Optional[AXActionResult]:
        """Delegate ref-based background action to `cua-driver` when available."""
        if not self.is_available:
            return None
        act = action.strip().lower()
        tool_args: dict[str, Any] = {"ref": ref}
        if act in ("press", "ax_press", "click"):
            tool_name = "click"
        elif act in ("set_value", "ax_set_value", "type"):
            tool_name = "type"
            tool_args["text"] = "" if value is None else str(value)
        elif act in ("focus", "ax_focus"):
            tool_name = "focus"
        else:
            return None
        try:
            res = self.call_tool(tool_name, tool_args)
            ok = bool(res.get("ok", True))
            return AXActionResult(
                ok=ok,
                ref=ref,
                action=act,
                backend="cua_driver",
                method=f"cua_{tool_name}",
                display_index=display_index,
                error=None if ok else str(res.get("error") or "cua-driver action failed"),
            )
        except Exception as exc:
            return AXActionResult(
                ok=False,
                ref=ref,
                action=act,
                backend="cua_driver",
                method="cua_error",
                display_index=display_index,
                error=str(exc),
            )

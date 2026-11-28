"""Workspace directory management for Kiki (global workspace, per-topic subdirs, and user mounts)."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

_SAFE_SEGMENT_RE = re.compile(r"[^A-Za-z0-9._-]+")


def default_kiki_workspace_root() -> Path:
    """Return the default Kiki personal workspace root (~/.kiki/workspace)."""
    return (Path.home() / ".kiki" / "workspace").expanduser().resolve()


def normalize_directory_path(raw_path: str) -> Path:
    """Expand ~ and environment variables, then resolve to an absolute Path."""
    cleaned = (raw_path or "").strip()
    if not cleaned:
        raise ValueError("目录路径不能为空")
    expanded = os.path.expandvars(os.path.expanduser(cleaned))
    return Path(expanded).resolve()


def _safe_topic_dirname(topic_id: str) -> str:
    cleaned = _SAFE_SEGMENT_RE.sub("-", (topic_id or "").strip()).strip(".-")
    return cleaned or "topic-default"


def _dedupe_paths(paths: list[Path]) -> list[Path]:
    seen: set[str] = set()
    result: list[Path] = []
    for p in paths:
        resolved = p.resolve()
        key = str(resolved)
        if key not in seen:
            seen.add(key)
            result.append(resolved)
    return result


@dataclass(frozen=True)
class ResolvedWorkspace:
    """Resolved workspace layout & mount set for a specific topic or run."""

    workspace_root: Path
    rules_dir: Path
    shared_dir: Path
    topics_dir: Path
    topic_id: str
    topic_dir: Path
    topic_files_dir: Path
    topic_artifacts_dir: Path
    primary_dir: Path
    extra_dirs: tuple[Path, ...] = ()
    global_mounts: tuple[dict[str, Any], ...] = ()
    topic_mounts: tuple[dict[str, Any], ...] = ()
    legacy_artifacts_dir: Optional[Path] = None

    @property
    def allowed_roots(self) -> list[Path]:
        candidates: list[Path] = [
            self.primary_dir,
            *self.extra_dirs,
            self.topic_dir,
            self.topic_artifacts_dir,
            self.shared_dir,
            self.rules_dir,
            self.workspace_root,
        ]
        if self.legacy_artifacts_dir is not None:
            candidates.append(self.legacy_artifacts_dir)
        return _dedupe_paths(candidates)

    def to_dict(self) -> dict[str, Any]:
        return {
            "workspace_root": str(self.workspace_root),
            "rules_dir": str(self.rules_dir),
            "shared_dir": str(self.shared_dir),
            "topic_id": self.topic_id,
            "topic_dir": str(self.topic_dir),
            "topic_files_dir": str(self.topic_files_dir),
            "topic_artifacts_dir": str(self.topic_artifacts_dir),
            "primary_dir": str(self.primary_dir),
            "extra_dirs": [str(p) for p in self.extra_dirs],
            "global_mounts": list(self.global_mounts),
            "topic_mounts": list(self.topic_mounts),
        }


class WorkspaceManager:
    """Manages Kiki's workspace directory hierarchy and resolves per-topic working directories."""

    def __init__(
        self,
        workspace_root: Path,
        legacy_artifacts_dir: Optional[Path] = None,
    ) -> None:
        self.workspace_root = Path(workspace_root).expanduser().resolve()
        self.rules_dir = self.workspace_root / "rules"
        self.shared_dir = self.workspace_root / "shared"
        self.topics_dir = self.workspace_root / "topics"
        self.legacy_artifacts_dir = (
            Path(legacy_artifacts_dir).expanduser().resolve()
            if legacy_artifacts_dir is not None
            else None
        )

    def ensure_base_layout(self) -> None:
        """Create Kiki's top-level workspace directories (rules, shared, topics) if missing."""
        self.workspace_root.mkdir(parents=True, exist_ok=True)
        self.rules_dir.mkdir(parents=True, exist_ok=True)
        self.shared_dir.mkdir(parents=True, exist_ok=True)
        self.topics_dir.mkdir(parents=True, exist_ok=True)

    def topic_dir_path(self, topic_id: str) -> Path:
        return (self.topics_dir / _safe_topic_dirname(topic_id)).resolve()

    def ensure_topic_dir(self, topic_id: str) -> Path:
        """Ensure the dedicated topic directory and its standard subdirectories exist."""
        self.ensure_base_layout()
        tdir = self.topic_dir_path(topic_id)
        tdir.mkdir(parents=True, exist_ok=True)
        (tdir / "artifacts").mkdir(parents=True, exist_ok=True)
        (tdir / "files").mkdir(parents=True, exist_ok=True)
        return tdir

    def resolve_for_topic(
        self,
        topic_id: str,
        *,
        topic_mounts: Optional[list[dict[str, Any]]] = None,
        global_mounts: Optional[list[dict[str, Any]]] = None,
        topic_workspace_dir: Optional[str] = None,
        ensure_dirs: bool = True,
    ) -> ResolvedWorkspace:
        """Resolve effective cwd (primary_dir) and extra_dirs for a topic.

        Priority for primary_dir (Agent cwd):
        1. Topic-level mount explicitly marked `is_primary=True` (if exists)
        2. First existing topic-level mount
        3. Global (main stream) mount explicitly marked `is_primary=True` (if exists)
        4. The topic's own auto-created directory (`<workspace_root>/topics/<topic_id>`)
        """
        if ensure_dirs:
            self.ensure_base_layout()

        if topic_workspace_dir:
            tdir = Path(topic_workspace_dir).expanduser().resolve()
        else:
            tdir = self.topic_dir_path(topic_id)

        if ensure_dirs:
            tdir.mkdir(parents=True, exist_ok=True)
            (tdir / "artifacts").mkdir(parents=True, exist_ok=True)
            (tdir / "files").mkdir(parents=True, exist_ok=True)

        t_mounts = list(topic_mounts or [])
        g_mounts = list(global_mounts or [])

        def _valid_mount_path(item: dict[str, Any]) -> Optional[Path]:
            raw = str(item.get("path") or "").strip()
            if not raw:
                return None
            try:
                p = normalize_directory_path(raw)
                return p if p.is_dir() else None
            except (ValueError, OSError):
                return None

        primary_dir: Optional[Path] = None

        # 1. Explicit primary topic mount
        for m in t_mounts:
            if m.get("is_primary"):
                p = _valid_mount_path(m)
                if p is not None:
                    primary_dir = p
                    break

        # 2. First valid topic mount
        if primary_dir is None:
            for m in t_mounts:
                p = _valid_mount_path(m)
                if p is not None:
                    primary_dir = p
                    break

        # 3. Explicit primary global mount (only when explicitly marked primary)
        if primary_dir is None:
            for m in g_mounts:
                if m.get("is_primary"):
                    p = _valid_mount_path(m)
                    if p is not None:
                        primary_dir = p
                        break

        # 4. Fallback to topic's own subdirectory
        if primary_dir is None:
            primary_dir = tdir

        # Build ordered extra_dirs excluding primary_dir
        raw_extras: list[Path] = []
        if tdir != primary_dir:
            raw_extras.append(tdir)
        for m in t_mounts:
            p = _valid_mount_path(m)
            if p is not None and p != primary_dir:
                raw_extras.append(p)
        for std_dir in (self.shared_dir, self.rules_dir):
            if std_dir != primary_dir:
                raw_extras.append(std_dir)
        for m in g_mounts:
            p = _valid_mount_path(m)
            if p is not None and p != primary_dir:
                raw_extras.append(p)

        extra_dirs = tuple(_dedupe_paths(raw_extras))

        return ResolvedWorkspace(
            workspace_root=self.workspace_root,
            rules_dir=self.rules_dir,
            shared_dir=self.shared_dir,
            topics_dir=self.topics_dir,
            topic_id=topic_id,
            topic_dir=tdir,
            topic_files_dir=tdir / "files",
            topic_artifacts_dir=tdir / "artifacts",
            primary_dir=primary_dir,
            extra_dirs=extra_dirs,
            global_mounts=tuple(g_mounts),
            topic_mounts=tuple(t_mounts),
            legacy_artifacts_dir=self.legacy_artifacts_dir,
        )

    def list_rule_files(self, topic_dir: Optional[Path] = None) -> list[Path]:
        """Return existing rule markdown/text files in global rules/ and optional topic_dir."""
        files: list[Path] = []
        if self.rules_dir.is_dir():
            for item in sorted(self.rules_dir.iterdir()):
                if item.is_file() and item.suffix.lower() in {".md", ".txt", ".yaml", ".yml", ".json"}:
                    files.append(item)
        if topic_dir is not None:
            topic_rule = topic_dir / "rules.md"
            if topic_rule.is_file():
                files.append(topic_rule)
        return files

    @property
    def memory_file(self) -> Path:
        return (self.workspace_root / "MEMORY.md").resolve()

    def sync_memory_markdown(self, memories: list[dict[str, Any]]) -> Path:
        """Mirror active SQLite memories to ~/.kiki/workspace/MEMORY.md for transparency & external CLI agents."""
        self.workspace_root.mkdir(parents=True, exist_ok=True)
        category_titles = (
            ("profile", "个人画像 (Profile)"),
            ("preference", "偏好与约束 (Preference)"),
            ("project", "长期项目背景 (Project)"),
            ("lesson", "经验与踩坑 (Lesson)"),
        )
        grouped: dict[str, list[dict[str, Any]]] = {k: [] for k, _ in category_titles}
        for mem in memories:
            if mem.get("status", "active") != "active":
                continue
            cat = str(mem.get("category") or "preference")
            grouped.setdefault(cat, []).append(mem)

        lines: list[str] = [
            "# Kiki Long-Term Memory",
            "",
            "> 本文件由 Kiki 记忆系统自动同步自本地 SQLite，方便查阅或供外部 Agent 参考。",
            "",
        ]
        has_any = False
        for key, label in category_titles:
            items = grouped.get(key) or []
            if not items:
                continue
            has_any = True
            lines.append(f"## {label}")
            for item in items:
                pin_prefix = "📌 " if item.get("pinned") else ""
                content = str(item.get("content") or "").strip()
                if content:
                    lines.append(f"- {pin_prefix}{content}")
            lines.append("")
        if not has_any:
            lines.append("（暂无长期记忆）")
            lines.append("")

        target = self.memory_file
        target.write_text("\n".join(lines), encoding="utf-8")
        return target

"""Workspace-scoped safe tools and tool declarations for internal_agent."""

from __future__ import annotations

import asyncio
import os
import re
import shlex
from pathlib import Path
from typing import Any, Optional

from .config import load_settings
from .db import Database
from .search import WebSearchService
from .sse import EventBus
from .workspace import ResolvedWorkspace

IGNORED_DIRS = {".git", ".venv", "node_modules", "__pycache__", ".pytest_cache", "dist", "build"}

_SAFE_ENV_TEMPLATES = {".env.example", ".env.sample", ".env.template"}
_SENSITIVE_EXTENSIONS = {".pem", ".key", ".p12", ".pfx", ".jks"}
_SENSITIVE_BASENAMES = {"id_rsa", "id_ed25519", "id_ecdsa", ".netrc", ".npmrc", ".pypirc"}
_SECRET_ENV_SUFFIXES = ("_API_KEY", "_KEY", "_SECRET", "_TOKEN", "_PASSWORD", "_CREDENTIAL", "_CREDENTIALS")

BLOCKED_SHELL_PATTERNS = [
    r"(?:^|(?:&&|\|\||[;&|])\s*)(?:/\S*/)?rm\s+[^\n]*(?:-r\b|-rf\b|-fr\b|--recursive\b)",
    r"\bfind\b[^\n]*-delete\b",
    r"\bshutil\.rmtree\b",
    r"\bsudo\b",
    r"\bmkfs\b",
    r"\bdd\s+if=",
    r"\bshutdown\b",
    r"\breboot\b",
    r":\(\)\s*\{",
    r">\s*/dev/sd",
    r"\bgit\s+push\s+--force\b",
    r"\bgit\s+reset\s+--hard\b",
    r"(?:^|[;&|]\s*)(?:cat|head|tail|less|more|sed|awk|grep)\b[^\n]*\.env(?:\b|\.(?!example|sample|template))",
]


def _is_sensitive_path(target: Path, allowed_roots: list[Path] | Path) -> bool:
    name = target.name.lower()
    if name == ".env" or (name.startswith(".env.") and name not in _SAFE_ENV_TEMPLATES):
        return True
    if name in _SENSITIVE_BASENAMES or target.suffix.lower() in _SENSITIVE_EXTENSIONS:
        return True
    roots = [allowed_roots] if isinstance(allowed_roots, Path) else allowed_roots
    for root in roots:
        try:
            rel_parts = [p.lower() for p in target.relative_to(root).parts]
            if ".git" in rel_parts and name in {"config", "credentials"}:
                return True
            return False
        except ValueError:
            continue
    return True


_SEARCH_NO_MATCH_COMMANDS = {"grep", "egrep", "fgrep", "rg"}
_ENV_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def _extract_last_shell_command(cmd: str) -> Optional[str]:
    """Return the executable basename of the final command segment in a shell pipeline/list."""
    try:
        lexer = shlex.shlex(cmd, posix=True, punctuation_chars="|;&")
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError:
        tokens = cmd.split()

    segment: list[str] = []
    for tok in tokens:
        if tok in {"|", ";", "&&", "||", "&"}:
            segment = []
        else:
            segment.append(tok)

    for tok in segment:
        if _ENV_ASSIGN_RE.match(tok):
            continue
        return Path(tok).name
    return None


def _is_search_no_match(cmd: str, exit_code: Optional[int], stderr_lines: list[str]) -> bool:
    """POSIX grep/rg return exit code 1 when no matches are found (exit code 2+ indicates real error)."""
    if exit_code != 1 or stderr_lines:
        return False
    return _extract_last_shell_command(cmd) in _SEARCH_NO_MATCH_COMMANDS


def _sanitized_shell_env() -> dict[str, str]:
    clean: dict[str, str] = {}
    for k, v in os.environ.items():
        upper = k.upper()
        if any(upper.endswith(suffix) for suffix in _SECRET_ENV_SUFFIXES):
            continue
        clean[k] = v
    return clean

AGENT_TOOL_DECLARATIONS: list[dict[str, Any]] = [
    {
        "type": "function",
        "name": "list_files",
        "description": "List files and directories under a relative path inside the workspace.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Relative directory path inside workspace, defaults to '.'",
                }
            },
        },
    },
    {
        "type": "function",
        "name": "read_file",
        "description": "Read text content of a file inside the workspace.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Relative file path inside workspace.",
                },
                "max_lines": {
                    "type": "integer",
                    "description": "Maximum number of lines to read (default 120, max 300).",
                },
            },
            "required": ["path"],
        },
    },
    {
        "type": "function",
        "name": "write_artifact",
        "description": "Save a generated deliverable (report, code snippet, summary, markdown) as a run artifact.",
        "parameters": {
            "type": "object",
            "properties": {
                "filename": {
                    "type": "string",
                    "description": "Filename for the artifact, e.g. 'analysis.md' or 'report.txt'.",
                },
                "content": {
                    "type": "string",
                    "description": "Full text content to write into the artifact file.",
                },
                "file_type": {
                    "type": "string",
                    "description": "Artifact type: 'markdown', 'code', 'text', 'json', etc.",
                },
                "description": {
                    "type": "string",
                    "description": "Short Chinese description of what this artifact contains.",
                },
            },
            "required": ["filename", "content"],
        },
    },
    {
        "type": "function",
        "name": "run_shell",
        "description": "Execute a safe, non-destructive shell command in the workspace root (e.g. git status, grep, wc, ls, pytest).",
        "parameters": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "Shell command to run inside the workspace.",
                }
            },
            "required": ["command"],
        },
    },
    {
        "type": "function",
        "name": "web_search",
        "description": "Search the public web for up-to-date facts, docs, news, or references across configured search engines (with automatic failover).",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search keywords or question.",
                },
                "max_results": {
                    "type": "integer",
                    "description": "Maximum number of results to return (1-10, default 5).",
                },
                "provider": {
                    "type": "string",
                    "description": "Optional engine override: 'auto' (default), 'tavily', 'serper', 'exa', 'brave', 'bocha', 'jina', or 'serpapi'.",
                },
                "search_depth": {
                    "type": "string",
                    "enum": ["basic", "advanced"],
                    "description": "Search depth ('basic' or 'advanced', default 'basic').",
                },
            },
            "required": ["query"],
        },
    },
    {
        "type": "function",
        "name": "fetch_webpage",
        "description": "Fetch and extract readable text content from a public http/https webpage URL.",
        "parameters": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "Public http:// or https:// URL to fetch.",
                },
                "max_chars": {
                    "type": "integer",
                    "description": "Maximum characters to return (500-20000, default 6000).",
                },
            },
            "required": ["url"],
        },
    },
    {
        "type": "function",
        "name": "recall_memory",
        "description": "Search Kiki's local long-term memories and cross-topic conversation history for past decisions, preferences, project context, or lessons learned.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Keywords or phrase to search in memories and past conversations.",
                },
                "include_history": {
                    "type": "boolean",
                    "description": "Whether to also search cross-topic message history (default true).",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of results per section (1-15, default 6).",
                },
            },
            "required": ["query"],
        },
    },
    {
        "type": "function",
        "name": "save_memory",
        "description": "Save a durable long-term memory about the user (profile, preference, project context, or lesson learned) when explicitly asked to remember something or when discovering a reusable long-term constraint.",
        "parameters": {
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": "Concise, self-contained fact or preference to remember (under 100 chars).",
                },
                "category": {
                    "type": "string",
                    "enum": ["profile", "preference", "project", "lesson"],
                    "description": "Memory category (default 'preference').",
                },
                "pinned": {
                    "type": "boolean",
                    "description": "Pin so this memory is always included in core prompt context (default false).",
                },
            },
            "required": ["content"],
        },
    },
    {
        "type": "function",
        "name": "forget_memory",
        "description": "Delete/forget a previously saved long-term memory by memory_id or matching query when the user asks to forget or correct it.",
        "parameters": {
            "type": "object",
            "properties": {
                "memory_id": {
                    "type": "string",
                    "description": "Exact memory ID (e.g. 'mem-...') to delete.",
                },
                "query": {
                    "type": "string",
                    "description": "Keywords to locate and delete matching memory if memory_id is unknown.",
                },
            },
        },
    },
    {
        "type": "function",
        "name": "request_human_approval",
        "description": "Pause execution and ask the human user to approve or choose an option before proceeding with a risky, destructive, or ambiguous action.",
        "parameters": {
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": "Clear Chinese question explaining what needs human approval.",
                },
                "options": {
                    "type": "array",
                    "description": "Quick action buttons for the user to click.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "label": {"type": "string"},
                            "value": {"type": "string"},
                            "approved": {"type": "boolean"},
                            "style": {
                                "type": "string",
                                "enum": ["primary", "danger", "secondary"],
                            },
                        },
                        "required": ["label", "value", "approved"],
                    },
                },
            },
            "required": ["prompt"],
        },
    },
    {
        "type": "function",
        "name": "request_desktop_takeover",
        "description": (
            "Pause execution and hand over the Kiki server desktop to the human user when manual GUI "
            "interaction is required (e.g. login/2FA, CAPTCHA, OAuth consent, macOS permission prompt, "
            "or visual verification in a desktop app/browser)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "reason": {
                    "type": "string",
                    "description": "Clear Chinese summary explaining why the user needs to take over the desktop.",
                },
                "instructions": {
                    "type": "string",
                    "description": "Optional step-by-step Chinese guidance for what the user should do on the server desktop.",
                },
                "url": {
                    "type": "string",
                    "description": "Optional relevant http/https URL to display as context for the user.",
                },
            },
            "required": ["reason"],
        },
    },
]


class WorkspaceToolExecutor:
    """Executes safe workspace-scoped tools for a specific SessionRun."""

    def __init__(
        self,
        workspace_root: Path,
        artifacts_dir: Path,
        db: Database,
        event_bus: EventBus,
        search_service: Optional[WebSearchService] = None,
        extra_roots: Optional[list[Path]] = None,
    ) -> None:
        self.workspace_root = workspace_root.resolve()
        self.artifacts_dir = artifacts_dir.resolve()
        self.db = db
        self.event_bus = event_bus
        self.search_service = search_service or WebSearchService(load_settings())
        roots: list[Path] = [self.workspace_root]
        if extra_roots:
            for r in extra_roots:
                resolved = Path(r).resolve()
                if resolved not in roots:
                    roots.append(resolved)
        self.allowed_roots: list[Path] = roots

    def for_workspace(self, resolved: ResolvedWorkspace) -> "WorkspaceToolExecutor":
        """Create a run-scoped executor bound to a resolved topic workspace."""
        return WorkspaceToolExecutor(
            workspace_root=resolved.primary_dir,
            artifacts_dir=resolved.topic_artifacts_dir,
            db=self.db,
            event_bus=self.event_bus,
            search_service=self.search_service,
            extra_roots=resolved.allowed_roots,
        )

    def _resolve_safe_path(self, rel_path: str) -> Path:
        raw = (rel_path or ".").strip() or "."
        expanded = Path(os.path.expanduser(raw))
        candidate = expanded.resolve() if expanded.is_absolute() else (self.workspace_root / expanded).resolve()
        for root in self.allowed_roots:
            try:
                candidate.relative_to(root)
                return candidate
            except ValueError:
                continue
        raise PermissionError(f"Access denied outside workspace: {rel_path}")

    def _display_path(self, target: Path) -> str:
        try:
            return str(target.relative_to(self.workspace_root))
        except ValueError:
            return str(target)

    def list_files(self, path: str = ".") -> str:
        target = self._resolve_safe_path(path)
        if not target.exists():
            return f"Error: path '{path}' does not exist."
        if target.is_file():
            return self._display_path(target)

        entries: list[str] = []
        for item in sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name)):
            if item.name in IGNORED_DIRS or _is_sensitive_path(item, self.allowed_roots):
                continue
            rel = self._display_path(item)
            entries.append(f"{rel}/" if item.is_dir() else rel)
            if len(entries) >= 200:
                entries.append("... (truncated at 200 entries)")
                break
        return "\n".join(entries) if entries else "(empty directory)"

    def read_file(self, path: str, max_lines: int = 120) -> str:
        target = self._resolve_safe_path(path)
        if _is_sensitive_path(target, self.allowed_roots):
            raise PermissionError(f"Access denied to sensitive file: {path}")
        if not target.exists() or not target.is_file():
            return f"Error: file '{path}' not found."
        capped_lines = max(1, min(int(max_lines or 120), 300))
        try:
            text = target.read_text(encoding="utf-8", errors="replace")
        except Exception as exc:
            return f"Error reading '{path}': {exc}"
        lines = text.splitlines()
        if len(lines) > capped_lines:
            shown = "\n".join(lines[:capped_lines])
            return f"{shown}\n... (truncated: showing {capped_lines}/{len(lines)} lines)"
        return text

    def write_artifact(
        self,
        run_id: str,
        filename: str,
        content: str,
        file_type: str = "markdown",
        description: str = "",
    ) -> str:
        safe_name = Path(filename).name
        if not safe_name or safe_name in {".", ".."}:
            safe_name = "artifact.md"

        run_dir = self.artifacts_dir / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        out_path = (run_dir / safe_name).resolve()
        out_path.write_text(content, encoding="utf-8")

        try:
            display_path = str(out_path.relative_to(self.workspace_root))
        except ValueError:
            display_path = str(out_path)

        artifact = self.db.add_artifact(
            run_id=run_id,
            file_path=display_path,
            file_type=file_type or "markdown",
            description=description or f"产出文件 {safe_name}",
        )
        self.event_bus.publish("artifact_created", {"run_id": run_id, "artifact": artifact})
        return f"Artifact saved at {display_path} (id={artifact['id']})"

    def write_binary_artifact(
        self,
        run_id: str,
        filename: str,
        data: bytes,
        file_type: str = "image",
        description: str = "",
    ) -> dict[str, Any]:
        safe_name = Path(filename).name
        if not safe_name or safe_name in {".", ".."}:
            safe_name = "artifact.bin"

        run_dir = self.artifacts_dir / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        out_path = (run_dir / safe_name).resolve()
        out_path.write_bytes(data)

        try:
            display_path = str(out_path.relative_to(self.workspace_root))
        except ValueError:
            display_path = str(out_path)

        artifact = self.db.add_artifact(
            run_id=run_id,
            file_path=display_path,
            file_type=file_type or "image",
            description=description or f"产出文件 {safe_name}",
        )
        self.event_bus.publish("artifact_created", {"run_id": run_id, "artifact": artifact})
        return artifact

    async def run_shell(self, run_id: str, command: str, timeout_sec: float = 300.0) -> tuple[str, bool]:
        cmd = (command or "").strip()
        if not cmd:
            return "Error: empty command.", True
        for pattern in BLOCKED_SHELL_PATTERNS:
            if re.search(pattern, cmd):
                return f"Blocked unsafe command matching safety rule: {cmd}", True

        self.event_bus.append_raw_log(run_id, f"$ {cmd}", stream="stdout")
        proc = await asyncio.create_subprocess_shell(
            cmd,
            cwd=str(self.workspace_root),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=_sanitized_shell_env(),
        )

        stdout_lines: list[str] = []
        stderr_lines: list[str] = []

        async def _drain(stream: asyncio.StreamReader | None, target: list[str], stream_name: str) -> None:
            if stream is None:
                return
            while True:
                raw = await stream.readline()
                if not raw:
                    break
                line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                target.append(line)
                self.event_bus.append_raw_log(run_id, line, stream=stream_name)  # type: ignore[arg-type]

        try:
            await asyncio.wait_for(
                asyncio.gather(
                    _drain(proc.stdout, stdout_lines, "stdout"),
                    _drain(proc.stderr, stderr_lines, "stderr"),
                    proc.wait(),
                ),
                timeout=timeout_sec,
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            msg = f"Command timed out after {timeout_sec}s"
            self.event_bus.append_raw_log(run_id, msg, stream="stderr")
            return msg, True

        exit_code = proc.returncode
        no_match = _is_search_no_match(cmd, exit_code, stderr_lines)
        is_error = exit_code != 0 and not no_match
        combined = "\n".join(stdout_lines)
        if stderr_lines:
            err_text = "\n".join(stderr_lines)
            combined = f"{combined}\n[stderr]\n{err_text}" if combined else f"[stderr]\n{err_text}"
        if not combined:
            combined = (
                "(no matches, exit code 1)"
                if no_match
                else f"(exit code {exit_code}, no output)"
            )
        if exit_code != 0 and not no_match:
            combined = f"{combined}\n[exit code {exit_code}]"
        if len(combined) > 4000:
            combined = combined[:4000] + "\n... (output truncated)"
        return combined, is_error

    def recall_memory(
        self,
        query: str,
        *,
        include_history: bool = True,
        limit: int = 6,
    ) -> str:
        clean_q = (query or "").strip()
        if not clean_q:
            raise ValueError("recall_memory 需要提供非空的查询词 query。")
        capped = max(1, min(int(limit or 6), 15))
        memories = self.db.search_memories(clean_q, limit=capped)
        if memories:
            self.db.touch_memories([m["id"] for m in memories])

        lines: list[str] = [f"记忆检索结果（关键词：{clean_q}）："]
        if memories:
            lines.append("\n### 长期记忆")
            for m in memories:
                pin = " [置顶]" if m.get("pinned") else ""
                lines.append(f"- ({m['id']}) [{m['category']}]{pin} {m['content']}")
        else:
            lines.append("\n### 长期记忆\n（未找到匹配的长期记忆）")

        if include_history:
            history_hits = self.db.search_history(clean_q, limit=capped)
            if history_hits:
                lines.append("\n### 跨话题历史消息")
                for h in history_hits:
                    snippet = " ".join(str(h.get("content") or "").split())
                    if len(snippet) > 180:
                        snippet = snippet[:180] + "…"
                    role_label = "用户" if h.get("role") == "user" else "Kiki"
                    lines.append(f"- [#{h.get('topic_title')}] {role_label}: {snippet}")
            else:
                lines.append("\n### 跨话题历史消息\n（未找到匹配的历史消息）")
        return "\n".join(lines)

    def save_memory(
        self,
        *,
        run_id: str,
        content: str,
        category: str = "preference",
        pinned: bool = False,
    ) -> str:
        run = self.db.get_session_run(run_id) if run_id else None
        source_topic_id = run["topic_id"] if run else None
        mem = self.db.create_memory(
            content=content,
            category=category,
            source_topic_id=source_topic_id,
            confidence=1.0,
            pinned=pinned,
        )
        self.event_bus.publish("memory_updated", {"action": "created", "memory": mem})
        return f"已记住长期记忆 ({mem['id']}, category={mem['category']}): {mem['content']}"

    def forget_memory(
        self,
        *,
        memory_id: Optional[str] = None,
        query: Optional[str] = None,
    ) -> str:
        mid = (memory_id or "").strip() if isinstance(memory_id, str) else ""
        if mid:
            ok = self.db.delete_memory(mid)
            if not ok:
                return f"未找到记忆 ID: {mid}"
            self.event_bus.publish("memory_updated", {"action": "deleted", "memory_id": mid})
            return f"已删除记忆 ({mid})。"

        q = (query or "").strip() if isinstance(query, str) else ""
        if not q:
            raise ValueError("forget_memory 需要提供 memory_id 或 query。")
        matches = self.db.search_memories(q, limit=3)
        if not matches:
            return f"未找到与「{q}」匹配的长期记忆。"
        target = matches[0]
        self.db.delete_memory(target["id"])
        self.event_bus.publish("memory_updated", {"action": "deleted", "memory_id": target["id"]})
        return f"已删除匹配记忆 ({target['id']}): {target['content']}"

    async def execute(self, run_id: str, name: str, arguments: dict[str, Any]) -> tuple[str, bool]:
        """Execute a non-HITL workspace tool. Returns (result_text, is_error)."""
        try:
            if name == "list_files":
                return self.list_files(path=str(arguments.get("path", "."))), False
            if name == "read_file":
                return (
                    self.read_file(
                        path=str(arguments.get("path", "")),
                        max_lines=int(arguments.get("max_lines", 120)),
                    ),
                    False,
                )
            if name == "write_artifact":
                return (
                    self.write_artifact(
                        run_id=run_id,
                        filename=str(arguments.get("filename", "artifact.md")),
                        content=str(arguments.get("content", "")),
                        file_type=str(arguments.get("file_type", "markdown")),
                        description=str(arguments.get("description", "")),
                    ),
                    False,
                )
            if name == "run_shell":
                out, is_error = await self.run_shell(
                    run_id=run_id,
                    command=str(arguments.get("command", "")),
                )
                return out, is_error
            if name == "web_search":
                resp = await self.search_service.search(
                    query=str(arguments.get("query", "")),
                    max_results=int(arguments.get("max_results", 5) or 5),
                    provider=str(arguments.get("provider", "auto") or "auto"),
                    search_depth=str(arguments.get("search_depth", "basic") or "basic"),
                )
                return resp.format_markdown(), False
            if name == "fetch_webpage":
                content = await self.search_service.fetch_webpage(
                    url=str(arguments.get("url", "")),
                    max_chars=int(arguments.get("max_chars", 6000) or 6000),
                )
                return content, False
            if name == "recall_memory":
                return self.recall_memory(
                    query=str(arguments.get("query", "")),
                    include_history=bool(arguments.get("include_history", True)),
                    limit=int(arguments.get("limit", 6) or 6),
                ), False
            if name == "save_memory":
                return self.save_memory(
                    run_id=run_id,
                    content=str(arguments.get("content", "")),
                    category=str(arguments.get("category", "preference") or "preference"),
                    pinned=bool(arguments.get("pinned", False)),
                ), False
            if name == "forget_memory":
                return self.forget_memory(
                    memory_id=arguments.get("memory_id"),
                    query=arguments.get("query"),
                ), False
            return f"Unknown tool: {name}", True
        except Exception as exc:
            return f"Tool '{name}' failed: {exc}", True

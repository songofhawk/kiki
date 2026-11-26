"""FastAPI application and REST + SSE endpoints for Kiki V1."""

from __future__ import annotations

from contextlib import asynccontextmanager
import asyncio
import hmac
import signal
import threading
from pathlib import Path
from typing import Any, Literal, Optional

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

from .adapters import AdapterRegistry, create_default_registry
from .config import Settings, get_settings
from .db import Database
from .desktop import DesktopDriver, DesktopTakeoverManager, create_desktop_driver
from .desktop.routes import create_desktop_router
from .dream import DreamConsolidator
from .llm import KikiLLMClient
from .router import DecisionRouter, FastPathService
from .search import WebSearchService
from .schemas import (
    AdapterInfoResponse,
    BatchMessageTopicReassignRequest,
    BatchMessageTopicReassignResponse,
    ChatRequest,
    ChatResponse,
    DirectoryBrowseResponse,
    DirectoryEntry,
    GlobalWorkspaceResponse,
    HealthResponse,
    MessageItem,
    MessagePageResponse,
    MessageTopicReassignRequest,
    MessageTopicReassignResponse,
    RunActionResponse,
    RunResumeRequest,
    SessionRunDetail,
    TopicCreateRequest,
    TopicSummary,
    TopicUpdateRequest,
    TopicWorkspaceResponse,
    WorkspaceMountCreateRequest,
    WorkspaceMountItem,
    WorkspaceMountUpdateRequest,
)
from .sse import EventBus
from .tools import WorkspaceToolExecutor
from .task_manager import TaskManager
from .workspace import WorkspaceManager, normalize_directory_path


def _enrich_message(msg: dict[str, Any], event_bus: EventBus) -> dict[str, Any]:
    if msg.get("session_run"):
        msg = dict(msg)
        msg["session_run"] = event_bus.enrich_run(msg["session_run"])
    return msg


def _build_quoted_prompt_content(
    user_content: str,
    quoted_msg: dict[str, Any],
) -> str:
    role = quoted_msg.get("role")
    sender = "用户自己" if role == "user" else ("Kiki" if role == "assistant" else "系统")
    raw = str(quoted_msg.get("content") or "").strip()
    excerpt = raw[:1200] + "…（内容已截断）" if len(raw) > 1200 else raw
    quoted_lines = "\n".join(f"> {line}" for line in excerpt.splitlines()) or "> （空消息）"
    return (
        f"[用户引用了 {sender} 的历史消息]\n"
        f"{quoted_lines}\n\n"
        f"[用户的新消息]\n"
        f"{user_content}"
    )


def create_app(
    *,
    settings: Optional[Settings] = None,
    db: Optional[Database] = None,
    llm: Optional[KikiLLMClient] = None,
    event_bus: Optional[EventBus] = None,
    adapter_registry: Optional[AdapterRegistry] = None,
    desktop_driver: Optional[DesktopDriver] = None,
) -> FastAPI:
    cfg = settings or get_settings()
    database = db or Database(cfg.database_path)
    bus = event_bus or EventBus()
    llm_client = llm or KikiLLMClient(cfg)
    registry = adapter_registry or create_default_registry(
        gemini_model=cfg.gemini_model,
        gemini_api_key_configured=cfg.any_llm_configured,
    )
    search_service = WebSearchService(cfg)
    workspace_manager = WorkspaceManager(
        workspace_root=cfg.workspace_root,
        legacy_artifacts_dir=cfg.artifacts_dir,
    )
    database.set_workspace_manager(workspace_manager)
    tool_executor = WorkspaceToolExecutor(
        workspace_root=cfg.workspace_root,
        artifacts_dir=cfg.artifacts_dir,
        db=database,
        event_bus=bus,
        search_service=search_service,
    )
    task_manager = TaskManager(
        db=database,
        llm=llm_client,
        event_bus=bus,
        tool_executor=tool_executor,
        adapter_registry=registry,
        settings=cfg,
        workspace_manager=workspace_manager,
    )
    decision_router = DecisionRouter(
        db=database,
        llm=llm_client,
        event_bus=bus,
        adapter_registry=registry,
    )
    fast_path = FastPathService(
        db=database,
        llm=llm_client,
        event_bus=bus,
    )

    def compute_global_kiki_status() -> Literal["idle", "thinking", "running", "waiting_input"]:
        run_status = task_manager.compute_kiki_status()
        if run_status == "waiting_input":
            return "waiting_input"
        if decision_router.is_busy or fast_path.is_busy:
            return "thinking"
        return run_status

    task_manager.set_status_provider(compute_global_kiki_status)
    decision_router.set_status_provider(compute_global_kiki_status)
    fast_path.set_idle_status_provider(compute_global_kiki_status)

    dream_consolidator = DreamConsolidator(
        db=database,
        llm=llm_client,
        event_bus=bus,
        is_busy_provider=lambda: compute_global_kiki_status() != "idle",
    )
    resolved_desktop_driver = desktop_driver or create_desktop_driver(cfg.desktop_driver)
    desktop_manager = DesktopTakeoverManager(
        driver=resolved_desktop_driver,
        event_bus=bus,
        enabled=cfg.desktop_takeover_enabled,
        idle_timeout_seconds=cfg.desktop_idle_timeout_seconds,
        default_fps=cfg.desktop_default_fps,
        default_max_width=cfg.desktop_max_width,
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        workspace_manager.ensure_base_layout()
        database.init_db()
        database.ensure_all_topic_workspaces()
        database.reconcile_orphaned_state()
        cfg.artifacts_dir.mkdir(parents=True, exist_ok=True)
        dream_consolidator.start()

        # Close open SSE streams immediately on SIGTERM/SIGINT so `uvicorn --reload`
        # never deadlocks waiting for infinite /api/events connections to drain.
        loop = asyncio.get_running_loop()
        prev_handlers: dict[int, Any] = {}
        if threading.current_thread() is threading.main_thread():
            for sig in (signal.SIGINT, signal.SIGTERM):
                try:
                    prev = signal.getsignal(sig)
                    prev_handlers[sig] = prev

                    def _make_handler(original: Any):
                        def _handler(signum: int, frame: Any) -> None:
                            try:
                                loop.call_soon_threadsafe(bus.close_all)
                            except RuntimeError:
                                bus.close_all()
                            if callable(original):
                                original(signum, frame)
                        return _handler

                    signal.signal(sig, _make_handler(prev))
                except (ValueError, OSError):
                    pass

        try:
            yield
        finally:
            bus.close_all()
            for sig, prev in prev_handlers.items():
                try:
                    signal.signal(sig, prev)
                except (ValueError, OSError):
                    pass
            await dream_consolidator.shutdown()
            await desktop_manager.shutdown()
            await fast_path.shutdown()
            await task_manager.shutdown()
            aclose_fn = getattr(llm_client, "aclose", None)
            if callable(aclose_fn):
                await aclose_fn()
            database.close()

    app = FastAPI(
        title="Kiki V1 Personal Agent API",
        version="1.0.0",
        lifespan=lifespan,
    )

    # Expose components on app.state for convenient inspection & testing
    app.state.settings = cfg
    app.state.db = database
    app.state.llm = llm_client
    app.state.event_bus = bus
    app.state.adapter_registry = registry
    app.state.search_service = search_service
    app.state.workspace_manager = workspace_manager
    app.state.task_manager = task_manager
    app.state.decision_router = decision_router
    app.state.fast_path = fast_path
    app.state.dream_consolidator = dream_consolidator
    app.state.desktop_manager = desktop_manager
    app.include_router(
        create_desktop_router(
            desktop_manager,
            configured_api_token=cfg.api_token.strip(),
        )
    )

    allowed_origins = [o for o in cfg.cors_origins if o and o != "*"]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=allowed_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    configured_api_token = cfg.api_token.strip()
    if configured_api_token:
        @app.middleware("http")
        async def verify_api_token(request: Request, call_next: Any) -> Any:
            if (
                request.method == "OPTIONS"
                or not request.url.path.startswith("/api/")
                or request.url.path == "/api/health"
            ):
                return await call_next(request)

            auth_header = request.headers.get("authorization", "")
            provided = ""
            if auth_header.lower().startswith("bearer "):
                provided = auth_header[7:].strip()
            elif request.url.path == "/api/events":
                provided = request.query_params.get("token", "").strip()

            if not provided or not hmac.compare_digest(provided, configured_api_token):
                return JSONResponse(
                    status_code=401,
                    content={"detail": "Unauthorized: invalid or missing API token"},
                )
            return await call_next(request)

    # ------------------------------------------------------------------
    # Health & Heavy-Path Adapters
    # ------------------------------------------------------------------
    @app.get("/api/health", response_model=HealthResponse)
    async def health() -> HealthResponse:
        return HealthResponse(
            status="ok",
            model=cfg.gemini_model,
            database_path=str(cfg.database_path),
            api_key_configured=cfg.any_llm_configured,
            workspace_root=str(workspace_manager.workspace_root),
        )

    # ------------------------------------------------------------------
    # Workspace & Custom Directory Mounts
    # ------------------------------------------------------------------
    def _decorate_mount(item: dict[str, Any]) -> dict[str, Any]:
        decorated = dict(item)
        try:
            decorated["exists"] = Path(str(item["path"])).is_dir()
        except OSError:
            decorated["exists"] = False
        return decorated

    @app.get("/api/workspace", response_model=GlobalWorkspaceResponse)
    async def get_global_workspace() -> GlobalWorkspaceResponse:
        workspace_manager.ensure_base_layout()
        global_mounts = [
            _decorate_mount(m)
            for m in database.list_workspace_mounts(scope="global")
        ]
        return GlobalWorkspaceResponse(
            workspace_root=str(workspace_manager.workspace_root),
            rules_dir=str(workspace_manager.rules_dir),
            shared_dir=str(workspace_manager.shared_dir),
            topics_dir=str(workspace_manager.topics_dir),
            global_mounts=[WorkspaceMountItem.model_validate(m) for m in global_mounts],
        )

    @app.get("/api/workspace/browse", response_model=DirectoryBrowseResponse)
    async def browse_workspace_directories(
        path: Optional[str] = Query(default=None),
        show_hidden: bool = Query(default=False),
    ) -> DirectoryBrowseResponse:
        home_dir = Path.home().resolve()
        raw = (path or "").strip()
        target = Path(raw).expanduser().resolve() if raw else home_dir
        if not target.exists():
            raise HTTPException(status_code=400, detail=f"目录不存在: {target}")
        if not target.is_dir():
            raise HTTPException(status_code=400, detail=f"路径不是文件夹: {target}")

        entries: list[DirectoryEntry] = []
        try:
            children = sorted(
                target.iterdir(),
                key=lambda p: p.name.lower(),
            )
            for child in children:
                if not show_hidden and child.name.startswith("."):
                    continue
                try:
                    if child.is_dir():
                        entries.append(DirectoryEntry(name=child.name, path=str(child)))
                except OSError:
                    continue
                if len(entries) >= 500:
                    break
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=f"无权限访问目录: {target}") from exc

        shortcut_candidates = [
            ("主目录 (~)", home_dir),
            ("桌面", home_dir / "Desktop"),
            ("文稿", home_dir / "Documents"),
            ("下载", home_dir / "Downloads"),
            ("Kiki 工作空间", workspace_manager.workspace_root),
        ]
        shortcuts: list[DirectoryEntry] = []
        seen_paths: set[str] = set()
        for label, p in shortcut_candidates:
            try:
                resolved_p = p.resolve()
                if resolved_p.is_dir() and str(resolved_p) not in seen_paths:
                    seen_paths.add(str(resolved_p))
                    shortcuts.append(DirectoryEntry(name=label, path=str(resolved_p)))
            except OSError:
                continue

        parent_path = str(target.parent) if target.parent != target else None
        return DirectoryBrowseResponse(
            current_path=str(target),
            parent_path=parent_path,
            entries=entries,
            shortcuts=shortcuts,
        )

    @app.get("/api/topics/{topic_id}/workspace", response_model=TopicWorkspaceResponse)
    async def get_topic_workspace(topic_id: str) -> TopicWorkspaceResponse:
        topic = database.get_topic(topic_id)
        if topic is None:
            raise HTTPException(status_code=404, detail="Topic not found")
        topic_mounts = [
            _decorate_mount(m)
            for m in database.list_workspace_mounts(scope="topic", topic_id=topic_id)
        ]
        global_mounts = [
            _decorate_mount(m)
            for m in database.list_workspace_mounts(scope="global")
        ]
        resolved = workspace_manager.resolve_for_topic(
            topic_id,
            topic_mounts=topic_mounts,
            global_mounts=global_mounts,
            topic_workspace_dir=topic.get("workspace_dir"),
            ensure_dirs=True,
        )
        if not topic.get("workspace_dir"):
            database.update_topic(topic_id, workspace_dir=str(resolved.topic_dir))
        return TopicWorkspaceResponse(
            workspace_root=str(resolved.workspace_root),
            rules_dir=str(resolved.rules_dir),
            shared_dir=str(resolved.shared_dir),
            topic_id=topic_id,
            topic_dir=str(resolved.topic_dir),
            topic_files_dir=str(resolved.topic_files_dir),
            topic_artifacts_dir=str(resolved.topic_artifacts_dir),
            primary_dir=str(resolved.primary_dir),
            extra_dirs=[str(p) for p in resolved.extra_dirs],
            global_mounts=[WorkspaceMountItem.model_validate(m) for m in global_mounts],
            topic_mounts=[WorkspaceMountItem.model_validate(m) for m in topic_mounts],
        )

    @app.post("/api/workspace/mounts", response_model=WorkspaceMountItem, status_code=201)
    async def add_workspace_mount(req: WorkspaceMountCreateRequest) -> WorkspaceMountItem:
        if req.scope == "topic":
            if not req.topic_id:
                raise HTTPException(status_code=400, detail="topic_id is required for topic mount")
            if database.get_topic(req.topic_id) is None:
                raise HTTPException(status_code=404, detail="Topic not found")
        try:
            resolved_path = normalize_directory_path(req.path)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if not resolved_path.exists() or not resolved_path.is_dir():
            raise HTTPException(
                status_code=400,
                detail=f"指定目录不存在或不是文件夹: {resolved_path}",
            )
        mount = database.add_workspace_mount(
            scope=req.scope,
            path=str(resolved_path),
            topic_id=req.topic_id if req.scope == "topic" else None,
            label=req.label,
            is_primary=req.is_primary,
        )
        return WorkspaceMountItem.model_validate(_decorate_mount(mount))

    @app.patch("/api/workspace/mounts/{mount_id}", response_model=WorkspaceMountItem)
    async def update_workspace_mount(
        mount_id: str,
        req: WorkspaceMountUpdateRequest,
    ) -> WorkspaceMountItem:
        updated = database.update_workspace_mount(
            mount_id,
            label=req.label,
            is_primary=req.is_primary,
        )
        if updated is None:
            raise HTTPException(status_code=404, detail="Workspace mount not found")
        return WorkspaceMountItem.model_validate(_decorate_mount(updated))

    @app.delete("/api/workspace/mounts/{mount_id}", status_code=204)
    async def delete_workspace_mount(mount_id: str) -> None:
        deleted = database.delete_workspace_mount(mount_id)
        if not deleted:
            raise HTTPException(status_code=404, detail="Workspace mount not found")

    @app.get("/api/adapters", response_model=list[AdapterInfoResponse])
    async def list_adapters() -> list[AdapterInfoResponse]:
        return [AdapterInfoResponse.model_validate(info.to_dict()) for info in registry.list_infos()]

    @app.get("/api/search/status")
    async def get_search_status() -> dict[str, Any]:
        return search_service.status()

    @app.get("/api/llm/status")
    async def get_llm_status() -> dict[str, Any]:
        status_fn = getattr(llm_client, "status", None)
        if callable(status_fn):
            return status_fn()
        return {
            "active_provider": "gemini",
            "active_model": cfg.gemini_model,
            "gemini_configured": cfg.api_key_configured,
            "deepseek_configured": cfg.deepseek_configured,
            "deepseek_model": cfg.deepseek_model,
        }

    @app.post("/api/llm/probe")
    async def probe_llm() -> dict[str, Any]:
        probe_fn = getattr(llm_client, "probe_gemini_free_tier", None)
        if callable(probe_fn):
            return await probe_fn()
        return await get_llm_status()

    # ------------------------------------------------------------------
    # Topics
    # ------------------------------------------------------------------
    @app.get("/api/topics", response_model=list[TopicSummary])
    async def list_topics(include_archived: bool = Query(default=False)) -> list[TopicSummary]:
        rows = database.list_topics(include_archived=include_archived)
        return [TopicSummary.model_validate(r) for r in rows]

    @app.post("/api/topics", response_model=TopicSummary, status_code=201)
    async def create_topic(req: TopicCreateRequest) -> TopicSummary:
        topic = database.create_topic(title=req.title, summary=req.summary)
        dream_consolidator.notify_activity(topic_created=True)
        bus.publish("topic_created", {"topic": topic})
        return TopicSummary.model_validate(topic)

    @app.patch("/api/topics/{topic_id}", response_model=TopicSummary)
    async def update_topic(topic_id: str, req: TopicUpdateRequest) -> TopicSummary:
        updated = database.update_topic(
            topic_id,
            title=req.title,
            summary=req.summary,
            status=req.status,
            unread=req.unread,
        )
        if updated is None:
            raise HTTPException(status_code=404, detail="Topic not found")
        bus.publish("topic_updated", {"topic": updated})
        return TopicSummary.model_validate(updated)

    # ------------------------------------------------------------------
    # Messages
    # ------------------------------------------------------------------
    @app.get("/api/messages", response_model=MessagePageResponse)
    async def list_messages(
        topic_id: Optional[str] = Query(default=None),
        before_id: Optional[str] = Query(default=None),
        limit: int = Query(default=40, ge=1, le=200),
    ) -> MessagePageResponse:
        page = database.list_messages_page(
            topic_id=topic_id,
            before_id=before_id,
            limit=limit,
        )
        return MessagePageResponse(
            messages=[
                MessageItem.model_validate(_enrich_message(r, bus))
                for r in page["messages"]
            ],
            has_more=page["has_more"],
            next_cursor=page["next_cursor"],
        )

    @app.patch("/api/messages/{message_id}/topic", response_model=MessageTopicReassignResponse)
    async def reassign_message_topic(
        message_id: str,
        req: MessageTopicReassignRequest,
    ) -> MessageTopicReassignResponse:
        existing_msg = database.get_message(message_id)
        if existing_msg is None:
            raise HTTPException(status_code=404, detail="Message not found")

        if req.action == "split":
            title = (req.new_topic_title or "").strip()
            if not title:
                snippet = " ".join(existing_msg["content"].strip().split())
                title = snippet[:18] + ("…" if len(snippet) > 18 else "") or "拆分话题"
            target_topic = database.create_topic(title=title)
            bus.publish("topic_created", {"topic": target_topic})
        elif req.action == "move":
            if not req.target_topic_id:
                raise HTTPException(
                    status_code=400, detail="target_topic_id is required for action='move'"
                )
            target_topic = database.get_topic(req.target_topic_id)
            if target_topic is None:
                raise HTTPException(status_code=404, detail="Target topic not found")
        else:
            raise HTTPException(status_code=400, detail="Invalid action")

        source_topic_id = existing_msg["topic_id"]
        dream_consolidator.notify_activity(topic_created=(req.action == "split"))
        paired_ids, _ = database.expand_paired_topic_messages(source_topic_id, [message_id])
        ids_to_move = paired_ids or [message_id]
        updated_msgs = database.reassign_messages_topic(ids_to_move, target_topic["id"])
        if not updated_msgs:
            raise HTTPException(status_code=404, detail="Message not found")

        refreshed_topic = database.get_topic(target_topic["id"]) or target_topic
        enriched_msgs = [_enrich_message(m, bus) for m in updated_msgs]
        for em in enriched_msgs:
            bus.publish("message_updated", {"message": em})
        primary_msg = next((m for m in enriched_msgs if m["id"] == message_id), enriched_msgs[0])
        if source_topic_id != target_topic["id"]:
            source_topic = database.get_topic(source_topic_id)
            if source_topic:
                bus.publish("topic_updated", {"topic": source_topic})
        bus.publish("topic_updated", {"topic": refreshed_topic})
        return MessageTopicReassignResponse(
            message=MessageItem.model_validate(primary_msg),
            topic=TopicSummary.model_validate(refreshed_topic),
        )

    @app.post("/api/messages/batch-topic", response_model=BatchMessageTopicReassignResponse)
    async def batch_reassign_message_topics(
        req: BatchMessageTopicReassignRequest,
    ) -> BatchMessageTopicReassignResponse:
        existing_msgs = database.get_messages_by_ids(req.message_ids)
        if not existing_msgs:
            raise HTTPException(status_code=404, detail="Messages not found")

        if req.action == "split":
            title = (req.new_topic_title or "").strip()
            if not title:
                first_user = next((m for m in existing_msgs if m["role"] == "user"), existing_msgs[0])
                snippet = " ".join(first_user["content"].strip().split())
                title = snippet[:18] + ("…" if len(snippet) > 18 else "") or "拆分话题"
            target_topic = database.create_topic(title=title)
            bus.publish("topic_created", {"topic": target_topic})
        elif req.action == "move":
            if not req.target_topic_id:
                raise HTTPException(
                    status_code=400, detail="target_topic_id is required for action='move'"
                )
            target_topic = database.get_topic(req.target_topic_id)
            if target_topic is None:
                raise HTTPException(status_code=404, detail="Target topic not found")
        else:
            raise HTTPException(status_code=400, detail="Invalid action")

        source_topic_ids = {m["topic_id"] for m in existing_msgs if m["topic_id"] != target_topic["id"]}
        dream_consolidator.notify_activity(topic_created=(req.action == "split"))
        updated_msgs = database.reassign_messages_topic(
            [m["id"] for m in existing_msgs],
            target_topic["id"],
        )
        enriched_msgs = [_enrich_message(m, bus) for m in updated_msgs]
        for em in enriched_msgs:
            bus.publish("message_updated", {"message": em})

        for src_id in source_topic_ids:
            src_topic = database.get_topic(src_id)
            if src_topic:
                bus.publish("topic_updated", {"topic": src_topic})
        refreshed_topic = database.get_topic(target_topic["id"]) or target_topic
        bus.publish("topic_updated", {"topic": refreshed_topic})

        return BatchMessageTopicReassignResponse(
            messages=[MessageItem.model_validate(m) for m in enriched_msgs],
            topic=TopicSummary.model_validate(refreshed_topic),
        )

    # ------------------------------------------------------------------
    # Chat (Dual-Path Entry)
    # ------------------------------------------------------------------
    @app.post("/api/chat", response_model=ChatResponse)
    async def chat(req: ChatRequest) -> ChatResponse:
        dream_consolidator.notify_activity()

        quoted_msg: Optional[dict[str, Any]] = None
        quoted_topic_id: Optional[str] = None
        quoted_snapshot: Optional[dict[str, Any]] = None
        effective_content = req.content

        if req.quoted_message_id:
            quoted_msg = database.get_message(req.quoted_message_id)
            if quoted_msg is None:
                raise HTTPException(
                    status_code=404,
                    detail=f"Quoted message not found: {req.quoted_message_id}",
                )
            quoted_topic_id = quoted_msg["topic_id"]
            quoted_topic = database.get_topic(quoted_topic_id)
            raw_q_content = str(quoted_msg.get("content") or "").strip()
            preview_q_content = (
                raw_q_content[:240] + "…"
                if len(raw_q_content) > 240
                else raw_q_content
            )
            quoted_snapshot = {
                "id": quoted_msg["id"],
                "role": quoted_msg["role"],
                "content": preview_q_content,
                "topic_id": quoted_topic_id,
                "topic_title": quoted_topic["title"] if quoted_topic else None,
                "created_at": quoted_msg.get("created_at"),
            }
            effective_content = _build_quoted_prompt_content(req.content, quoted_msg)

        try:
            decision, topic, created, reassigned_messages = await decision_router.route(
                content=effective_content,
                focus_topic_id=req.focus_topic_id,
                quoted_topic_id=quoted_topic_id,
                force_path=req.force_path,
                force_agent_type=req.agent_type,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        dream_consolidator.notify_activity(topic_created=created)

        user_meta: dict[str, Any] = {
            "path": decision.path,
            "topic_action": decision.topic_action,
            "confidence": decision.confidence,
            "reasoning": decision.reasoning,
            "focus_mode": bool(req.focus_topic_id),
        }
        if quoted_snapshot is not None:
            user_meta["quoted_message_id"] = quoted_snapshot["id"]
            user_meta["quoted_message"] = quoted_snapshot
        user_msg = database.create_message(
            topic_id=topic["id"],
            role="user",
            content=req.content,
            metadata=user_meta,
        )
        bus.publish("message_created", {"message": user_msg})

        assistant_msg: Optional[dict[str, Any]] = None
        session_run: Optional[dict[str, Any]] = None

        if decision.path == "heavy":
            raw_req_agent = (req.agent_type or "").strip()
            requested_agent = (
                None if (not raw_req_agent or raw_req_agent.lower() == "auto") else raw_req_agent
            )
            inherited_topic_agent: Optional[str] = None
            if (
                not requested_agent
                and not registry.try_resolve_type(decision.agent_type)
                and decision.topic_action == "reuse"
            ):
                latest_topic_run = database.get_latest_topic_run(topic["id"])
                prior_agent = (
                    registry.try_resolve_type(latest_topic_run.get("agent_type"))
                    if latest_topic_run
                    else None
                )
                if prior_agent:
                    try:
                        if registry.get(prior_agent).probe().available:
                            inherited_topic_agent = prior_agent
                    except KeyError:
                        pass
            resolved_agent_type = (
                requested_agent
                or registry.try_resolve_type(decision.agent_type)
                or inherited_topic_agent
                or cfg.default_heavy_agent
                or "internal_agent"
            )
            if decision.agent_type != resolved_agent_type:
                canonical = registry.try_resolve_type(resolved_agent_type)
                if canonical:
                    decision = decision.model_copy(update={"agent_type": canonical})
            try:
                session_run = task_manager.enqueue_run(
                    topic_id=topic["id"],
                    trigger_message_id=user_msg["id"],
                    user_content=effective_content,
                    agent_type=resolved_agent_type,
                    model_override=req.model,
                    thinking_override=req.thinking_level,
                )
            except KeyError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            # Re-fetch user message so it includes linked session_run
            refreshed_user_msg = database.get_message(user_msg["id"])
            if refreshed_user_msg:
                user_msg = _enrich_message(refreshed_user_msg, bus)
        else:
            assistant_msg = fast_path.start_reply(
                topic=topic,
                user_content=effective_content,
                user_message_id=user_msg["id"],
            )

        refreshed_topic = database.get_topic(topic["id"]) or topic
        bus.publish("topic_updated", {"topic": refreshed_topic})

        return ChatResponse(
            user_message=MessageItem.model_validate(user_msg),
            route_decision=decision,
            topic=TopicSummary.model_validate(refreshed_topic),
            assistant_message=MessageItem.model_validate(assistant_msg) if assistant_msg else None,
            session_run=SessionRunDetail.model_validate(session_run) if session_run else None,
            reassigned_messages=[MessageItem.model_validate(m) for m in reassigned_messages],
        )

    # ------------------------------------------------------------------
    # SessionRuns (Heavy-Path + HITL)
    # ------------------------------------------------------------------
    @app.get("/api/runs", response_model=list[SessionRunDetail])
    async def list_runs(
        active_only: bool = Query(default=True),
        limit: int = Query(default=50, ge=1, le=200),
    ) -> list[SessionRunDetail]:
        rows = (
            database.list_active_session_runs()
            if active_only
            else database.list_session_runs(limit=limit)
        )
        return [SessionRunDetail.model_validate(bus.enrich_run(r)) for r in rows]

    @app.get("/api/runs/{run_id}", response_model=SessionRunDetail)
    async def get_run(run_id: str) -> SessionRunDetail:
        run = database.get_session_run(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="SessionRun not found")
        return SessionRunDetail.model_validate(bus.enrich_run(run))

    @app.post("/api/runs/{run_id}/resume", response_model=RunActionResponse)
    async def resume_run(run_id: str, req: RunResumeRequest) -> RunActionResponse:
        try:
            run = task_manager.resume_run(
                run_id=run_id,
                decision=req.decision,
                approved=req.approved,
                comment=req.comment,
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if run is None:
            raise HTTPException(status_code=404, detail="SessionRun not found")
        return RunActionResponse(run=SessionRunDetail.model_validate(run))

    @app.post("/api/runs/{run_id}/cancel", response_model=RunActionResponse)
    async def cancel_run(run_id: str) -> RunActionResponse:
        run = await task_manager.cancel_run(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="SessionRun not found")
        return RunActionResponse(run=SessionRunDetail.model_validate(run))

    @app.get("/api/runs/{run_id}/artifacts/{artifact_id}")
    async def get_run_artifact(
        run_id: str,
        artifact_id: str,
        download: bool = Query(default=False),
    ) -> FileResponse:
        artifact = database.get_artifact(run_id, artifact_id)
        if artifact is None:
            raise HTTPException(status_code=404, detail="Artifact not found")

        raw_path = Path(str(artifact["file_path"]))
        run = database.get_session_run(run_id)
        allowed_roots = [cfg.artifacts_dir.resolve(), cfg.workspace_root.resolve()]
        primary_root = cfg.workspace_root
        if run is not None:
            topic = database.get_topic(run["topic_id"])
            resolved_ws = workspace_manager.resolve_for_topic(
                run["topic_id"],
                topic_mounts=database.list_workspace_mounts(scope="topic", topic_id=run["topic_id"]),
                global_mounts=database.list_workspace_mounts(scope="global"),
                topic_workspace_dir=topic.get("workspace_dir") if topic else None,
                ensure_dirs=False,
            )
            primary_root = resolved_ws.primary_dir
            for r in resolved_ws.allowed_roots:
                if r not in allowed_roots:
                    allowed_roots.append(r)

        if raw_path.is_absolute():
            candidate = raw_path.resolve()
        else:
            candidate = (primary_root / raw_path).resolve()
            if not candidate.is_file():
                alt = (cfg.workspace_root / raw_path).resolve()
                if alt.is_file():
                    candidate = alt

        if not any(
            candidate == root or root in candidate.parents for root in allowed_roots
        ):
            raise HTTPException(status_code=403, detail="Artifact path outside workspace")
        if not candidate.is_file():
            raise HTTPException(status_code=404, detail="Artifact file missing on disk")

        return FileResponse(
            path=candidate,
            filename=candidate.name if download else None,
        )

    # ------------------------------------------------------------------
    # SSE Stream
    # ------------------------------------------------------------------
    @app.get("/api/events")
    async def events(request: Request) -> StreamingResponse:
        return StreamingResponse(
            bus.subscribe(heartbeat_interval=15.0, is_disconnected=request.is_disconnected),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    return app


_lazy_app: Optional[FastAPI] = None


def __getattr__(name: str) -> Any:
    global _lazy_app
    if name == "app":
        if _lazy_app is None:
            _lazy_app = create_app()
        return _lazy_app
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

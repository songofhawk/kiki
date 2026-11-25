"""FastAPI application and REST + SSE endpoints for Kiki V1."""

from __future__ import annotations

from contextlib import asynccontextmanager
import hmac
from typing import Any, Literal, Optional

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse

from .adapters import AdapterRegistry, create_default_registry
from .config import Settings, get_settings
from .db import Database
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
)
from .sse import EventBus
from .tools import WorkspaceToolExecutor
from .task_manager import TaskManager


def _enrich_message(msg: dict[str, Any], event_bus: EventBus) -> dict[str, Any]:
    if msg.get("session_run"):
        msg = dict(msg)
        msg["session_run"] = event_bus.enrich_run(msg["session_run"])
    return msg


def create_app(
    *,
    settings: Optional[Settings] = None,
    db: Optional[Database] = None,
    llm: Optional[KikiLLMClient] = None,
    event_bus: Optional[EventBus] = None,
    adapter_registry: Optional[AdapterRegistry] = None,
) -> FastAPI:
    cfg = settings or get_settings()
    database = db or Database(cfg.database_path)
    bus = event_bus or EventBus()
    llm_client = llm or KikiLLMClient(cfg)
    registry = adapter_registry or create_default_registry(
        gemini_model=cfg.gemini_model,
        gemini_api_key_configured=cfg.api_key_configured,
    )
    search_service = WebSearchService(cfg)
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

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        database.init_db()
        cfg.artifacts_dir.mkdir(parents=True, exist_ok=True)
        dream_consolidator.start()
        yield
        await dream_consolidator.shutdown()
        await fast_path.shutdown()
        await task_manager.shutdown()

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
    app.state.task_manager = task_manager
    app.state.decision_router = decision_router
    app.state.fast_path = fast_path
    app.state.dream_consolidator = dream_consolidator

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
            api_key_configured=cfg.api_key_configured,
        )

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
        updated_msg = database.reassign_message_topic(message_id, target_topic["id"])
        if updated_msg is None:
            raise HTTPException(status_code=404, detail="Message not found")

        refreshed_topic = database.get_topic(target_topic["id"]) or target_topic
        enriched_msg = _enrich_message(updated_msg, bus)
        bus.publish("message_updated", {"message": enriched_msg})
        if source_topic_id != target_topic["id"]:
            source_topic = database.get_topic(source_topic_id)
            if source_topic:
                bus.publish("topic_updated", {"topic": source_topic})
        bus.publish("topic_updated", {"topic": refreshed_topic})
        return MessageTopicReassignResponse(
            message=MessageItem.model_validate(enriched_msg),
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
        try:
            decision, topic, created, reassigned_messages = await decision_router.route(
                content=req.content,
                focus_topic_id=req.focus_topic_id,
                force_path=req.force_path,
                force_agent_type=req.agent_type,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        dream_consolidator.notify_activity(topic_created=created)

        user_meta = {
            "path": decision.path,
            "topic_action": decision.topic_action,
            "confidence": decision.confidence,
            "reasoning": decision.reasoning,
            "focus_mode": bool(req.focus_topic_id),
        }
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
            resolved_agent_type = (
                (req.agent_type.strip() if req.agent_type and req.agent_type.strip() else None)
                or registry.try_resolve_type(decision.agent_type)
                or "gemini_agent"
            )
            if decision.agent_type != resolved_agent_type:
                canonical = registry.try_resolve_type(resolved_agent_type)
                if canonical:
                    decision = decision.model_copy(update={"agent_type": canonical})
            try:
                session_run = task_manager.enqueue_run(
                    topic_id=topic["id"],
                    trigger_message_id=user_msg["id"],
                    user_content=req.content,
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
                user_content=req.content,
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

    # ------------------------------------------------------------------
    # SSE Stream
    # ------------------------------------------------------------------
    @app.get("/api/events")
    async def events() -> StreamingResponse:
        return StreamingResponse(
            bus.subscribe(heartbeat_interval=15.0),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    return app


app = create_app()

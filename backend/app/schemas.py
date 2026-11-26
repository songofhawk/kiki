"""Pydantic v2 schemas for Kiki V1 REST API, SSE events, and Decision Model."""

from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


class RouteDecision(BaseModel):
    path: Literal["fast", "heavy"] = Field(
        description="fast for lightweight chat/Q&A; heavy for multi-step coding, analysis, file/shell tasks"
    )
    topic_action: Literal["reuse", "create", "none"] = Field(
        description="reuse existing topic, create new topic, or none for casual lounge chat"
    )
    topic_id: Optional[str] = Field(
        default=None,
        description="Existing Topic ID if topic_action is reuse, else null",
    )
    new_topic_title: Optional[str] = Field(
        default=None,
        description="Short concise title if topic_action is create, else null",
    )
    agent_type: Optional[str] = Field(
        default=None,
        description="Explicitly requested Heavy-Path agent ID (e.g. codex, claude_code, internal_agent) if mentioned by user or continuing a previous agent task, else null",
    )
    confidence: float = Field(
        ge=0.0,
        le=1.0,
        description="Routing confidence between 0.0 and 1.0",
    )
    reasoning: str = Field(
        description="Brief Chinese explanation for the routing decision"
    )


class BacktrackDecision(BaseModel):
    matched_message_ids: list[str] = Field(
        default_factory=list,
        description="IDs of candidate messages from 日常闲聊 that belong to the target topic",
    )
    reasoning: str = Field(
        default="",
        description="Brief Chinese explanation for which earlier messages belong to the topic",
    )


class DreamMigrationGroup(BaseModel):
    action: Literal["move_to_existing", "create_topic"] = Field(
        description="Whether to merge messages into an existing topic or create a new topic",
    )
    target_topic_id: Optional[str] = Field(
        default=None,
        description="Existing active Topic ID when action is move_to_existing",
    )
    new_topic_title: Optional[str] = Field(
        default=None,
        description="Short concise title when action is create_topic",
    )
    new_topic_summary: Optional[str] = Field(
        default=None,
        description="One-sentence summary when action is create_topic",
    )
    message_ids: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)
    reasoning: str = Field(default="")


class DreamTopicSummaryUpdate(BaseModel):
    topic_id: str
    summary: str


class DreamConsolidationPlan(BaseModel):
    migrations: list[DreamMigrationGroup] = Field(default_factory=list)
    topic_summaries: list[DreamTopicSummaryUpdate] = Field(default_factory=list)
    reasoning: str = Field(default="")


class HealthResponse(BaseModel):
    status: str = "ok"
    model: str
    database_path: str
    api_key_configured: bool
    workspace_root: Optional[str] = None


class WorkspaceMountItem(BaseModel):
    id: str
    scope: Literal["global", "topic"]
    topic_id: Optional[str] = None
    path: str
    label: Optional[str] = None
    is_primary: bool = False
    exists: bool = True
    created_at: str


class WorkspaceMountCreateRequest(BaseModel):
    scope: Literal["global", "topic"]
    topic_id: Optional[str] = None
    path: str = Field(min_length=1)
    label: Optional[str] = None
    is_primary: bool = False


class WorkspaceMountUpdateRequest(BaseModel):
    label: Optional[str] = None
    is_primary: Optional[bool] = None


class DirectoryEntry(BaseModel):
    name: str
    path: str


class DirectoryBrowseResponse(BaseModel):
    current_path: str
    parent_path: Optional[str] = None
    entries: list[DirectoryEntry] = Field(default_factory=list)
    shortcuts: list[DirectoryEntry] = Field(default_factory=list)


class GlobalWorkspaceResponse(BaseModel):
    workspace_root: str
    rules_dir: str
    shared_dir: str
    topics_dir: str
    global_mounts: list[WorkspaceMountItem] = Field(default_factory=list)


class TopicWorkspaceResponse(BaseModel):
    workspace_root: str
    rules_dir: str
    shared_dir: str
    topic_id: str
    topic_dir: str
    topic_files_dir: str
    topic_artifacts_dir: str
    primary_dir: str
    extra_dirs: list[str] = Field(default_factory=list)
    global_mounts: list[WorkspaceMountItem] = Field(default_factory=list)
    topic_mounts: list[WorkspaceMountItem] = Field(default_factory=list)


class TopicCreateRequest(BaseModel):
    title: str = Field(min_length=1, max_length=120)
    summary: Optional[str] = None


class TopicUpdateRequest(BaseModel):
    title: Optional[str] = Field(default=None, min_length=1, max_length=120)
    summary: Optional[str] = None
    status: Optional[Literal["active", "archived", "deleted"]] = None
    unread: Optional[bool] = None


class TopicSummary(BaseModel):
    id: str
    title: str
    summary: Optional[str] = None
    status: Literal["active", "archived", "deleted"] = "active"
    unread: bool = False
    workspace_dir: Optional[str] = None
    created_at: str
    updated_at: str
    message_count: int = 0
    last_message_at: Optional[str] = None
    active_run_count: int = 0


class HITLOption(BaseModel):
    label: str
    value: str
    approved: bool = True
    style: Literal["primary", "danger", "secondary"] = "secondary"


class HITLRequest(BaseModel):
    run_id: str
    topic_id: str
    prompt: str
    kind: Literal["approval", "desktop_takeover"] = "approval"
    instructions: Optional[str] = None
    url: Optional[str] = None
    options: list[HITLOption] = Field(default_factory=list)


class StepLogItem(BaseModel):
    id: int
    run_id: str
    step_number: int
    action_type: str
    detail: str
    status: Literal["success", "failed", "in_progress"]
    created_at: str


class ArtifactItem(BaseModel):
    id: str
    run_id: str
    file_path: str
    file_type: str
    description: Optional[str] = None
    created_at: str


class RawLogItem(BaseModel):
    run_id: str
    line: str
    timestamp: str
    stream: Literal["stdout", "stderr", "ndjson"] = "stdout"


class SessionRunDetail(BaseModel):
    id: str
    topic_id: str
    trigger_message_id: str
    agent_type: str
    status: Literal[
        "queued",
        "running",
        "waiting_input",
        "completed",
        "failed",
        "cancelled",
    ]
    current_step: Optional[str] = None
    error_message: Optional[str] = None
    created_at: str
    updated_at: str
    step_logs: list[StepLogItem] = Field(default_factory=list)
    artifacts: list[ArtifactItem] = Field(default_factory=list)
    raw_logs: list[RawLogItem] = Field(default_factory=list)
    hitl_request: Optional[HITLRequest] = None


class MessageItem(BaseModel):
    id: str
    topic_id: str
    role: Literal["user", "assistant", "system"]
    content: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: str
    streaming: bool = False
    topic: Optional[TopicSummary] = None
    session_run: Optional[SessionRunDetail] = None


class MessagePageResponse(BaseModel):
    messages: list[MessageItem]
    has_more: bool = False
    next_cursor: Optional[str] = None


class AdapterInfoResponse(BaseModel):
    id: str
    name: str
    vendor: str
    description: str
    category: Literal["builtin", "cli", "hybrid"]
    available: bool
    binary_path: Optional[str] = None
    install_hint: str
    default_model: str
    models: dict[str, str] = Field(default_factory=dict)
    supports_resume: bool = True
    supports_streaming: bool = True
    supports_hitl: bool = True
    aliases: list[str] = Field(default_factory=list)


class ChatRequest(BaseModel):
    content: str = Field(min_length=1)
    focus_topic_id: Optional[str] = None
    quoted_message_id: Optional[str] = None
    force_path: Optional[Literal["fast", "heavy"]] = None
    agent_type: Optional[str] = None
    model: Optional[str] = None
    thinking_level: Optional[Literal["low", "medium", "high"]] = None


class ChatResponse(BaseModel):
    user_message: MessageItem
    route_decision: RouteDecision
    topic: TopicSummary
    assistant_message: Optional[MessageItem] = None
    session_run: Optional[SessionRunDetail] = None
    reassigned_messages: list[MessageItem] = Field(default_factory=list)


class MessageTopicReassignRequest(BaseModel):
    action: Literal["move", "split"]
    target_topic_id: Optional[str] = None
    new_topic_title: Optional[str] = None


class MessageTopicReassignResponse(BaseModel):
    message: MessageItem
    topic: TopicSummary


class BatchMessageTopicReassignRequest(BaseModel):
    message_ids: list[str] = Field(min_length=1)
    action: Literal["move", "split"]
    target_topic_id: Optional[str] = None
    new_topic_title: Optional[str] = None


class BatchMessageTopicReassignResponse(BaseModel):
    messages: list[MessageItem]
    topic: TopicSummary


class RunResumeRequest(BaseModel):
    decision: str
    approved: bool
    comment: Optional[str] = None


class RunActionResponse(BaseModel):
    run: SessionRunDetail

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
    confidence: float = Field(
        ge=0.0,
        le=1.0,
        description="Routing confidence between 0.0 and 1.0",
    )
    reasoning: str = Field(
        description="Brief Chinese explanation for the routing decision"
    )


class HealthResponse(BaseModel):
    status: str = "ok"
    model: str
    database_path: str
    api_key_configured: bool


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
    topic: Optional[TopicSummary] = None
    session_run: Optional[SessionRunDetail] = None


class ChatRequest(BaseModel):
    content: str = Field(min_length=1)
    focus_topic_id: Optional[str] = None
    force_path: Optional[Literal["fast", "heavy"]] = None


class ChatResponse(BaseModel):
    user_message: MessageItem
    route_decision: RouteDecision
    topic: TopicSummary
    assistant_message: Optional[MessageItem] = None
    session_run: Optional[SessionRunDetail] = None


class MessageTopicReassignRequest(BaseModel):
    action: Literal["move", "split"]
    target_topic_id: Optional[str] = None
    new_topic_title: Optional[str] = None


class MessageTopicReassignResponse(BaseModel):
    message: MessageItem
    topic: TopicSummary


class RunResumeRequest(BaseModel):
    decision: str
    approved: bool
    comment: Optional[str] = None


class RunActionResponse(BaseModel):
    run: SessionRunDetail

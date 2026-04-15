from __future__ import annotations

import uuid
from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field


class SkillStatus(str, Enum):
    CREATING = "creating"
    ACTIVE = "active"
    ITERATING = "iterating"
    COMPLETED = "completed"
    ERROR = "error"


class MessageRole(str, Enum):
    USER = "user"
    ASSISTANT = "assistant"


class Message(BaseModel):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    role: MessageRole
    content: str
    thinking: str = ""  # 思考内容（仅 assistant 消息）
    tool_details: list[dict] = Field(default_factory=list)  # 工具执行详情
    content_parts: list[dict] = Field(default_factory=list)  # 按轮次的文本片段 [{"round_index": 1, "content": "..."}]
    timestamp: datetime = Field(default_factory=datetime.now)


class Skill(BaseModel):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:8])
    name: str = ""
    description: str = ""
    status: SkillStatus = SkillStatus.CREATING
    workspace: str | None = None  # skill 工作目录路径
    opencode_session_id: str | None = None
    opencode_port: int | None = None
    auto_sent: bool = False  # 是否已触发过自动发送（防止重连重复触发）
    messages: list[Message] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=datetime.now)
    updated_at: datetime = Field(default_factory=datetime.now)


class ServerInstance(BaseModel):
    port: int
    in_use: bool = False
    skill_id: str | None = None
    opencode_pid: int | None = None


class ChatRequest(BaseModel):
    message: str
    session_id: str | None = None  # Streamable HTTP session id


class ChatChunk(BaseModel):
    """流式响应的单个 chunk"""
    type: str  # "text" | "status" | "done" | "error"
    content: str
    skill_id: str | None = None
    session_id: str | None = None


class StatusResponse(BaseModel):
    total_servers: int
    active_servers: int
    skills_count: int
    servers: list[ServerInstance] = Field(default_factory=list)

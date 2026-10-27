"""请求体 schema（输出统一走 serializers，便于携带派生字段）。"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class LoginIn(BaseModel):
    username: str
    password: str


class ChangePasswordIn(BaseModel):
    old_password: str
    new_password: str


class UserCreateIn(BaseModel):
    username: str = Field(min_length=3, max_length=64)
    display_name: str = ""
    password: str = Field(min_length=10, max_length=128)
    role: Literal["admin", "employee"] = "employee"
    grants: list["GrantIn"] = []


class GrantIn(BaseModel):
    kb_id: int
    level: Literal["read", "manage"] = "read"


class UserUpdateIn(BaseModel):
    display_name: str | None = None
    role: Literal["admin", "employee"] | None = None
    status: Literal["active", "disabled"] | None = None


class ResetPasswordIn(BaseModel):
    new_password: str = Field(min_length=10, max_length=128)


class GrantsIn(BaseModel):
    grants: list[GrantIn] = []


class KbCreateIn(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    description: str = ""
    mode: Literal["basic", "wiki"] = "basic"


class KbUpdateIn(BaseModel):
    name: str | None = None
    description: str | None = None
    mode: Literal["basic", "wiki"] | None = None


class ProviderIn(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    base_url: str = Field(min_length=4, max_length=255)
    api_key: str = ""
    enabled: bool = True
    # 三值协议族：openai（默认，/chat/completions + /embeddings）、
    # openai-responses（/responses）、anthropic（/messages）
    protocol: Literal["openai", "openai-responses", "anthropic"] = "openai"


class ProviderUpdateIn(BaseModel):
    name: str | None = None
    base_url: str | None = None
    api_key: str | None = None      # 空字符串=不修改；"__clear__"=清空
    enabled: bool | None = None
    protocol: Literal["openai", "openai-responses", "anthropic"] | None = None


class ModelImportItemIn(BaseModel):
    """探测结果里勾选的一项：模型标识 + 用途，可选显示名。"""
    model: str = Field(min_length=1, max_length=128)
    purpose: Literal["llm", "embedding", "vision"]
    label: str = ""


class ModelImportIn(BaseModel):
    items: list[ModelImportItemIn] = []


class ModelIn(BaseModel):
    provider_id: int
    purpose: Literal["llm", "embedding", "vision"]
    model: str = Field(min_length=1, max_length=128)
    label: str = ""
    params: dict = {}
    is_default: bool = False
    enabled: bool = True
    embed_dim: int = 0


class ModelUpdateIn(BaseModel):
    label: str | None = None
    model: str | None = None
    params: dict | None = None
    is_default: bool | None = None
    enabled: bool | None = None
    embed_dim: int | None = None


class EmbeddingSwitchIn(BaseModel):
    model_config_id: int
    confirm: bool = False


class ConversationIn(BaseModel):
    title: str = "新会话"
    kb_ids: list[int] = []


class ConversationUpdateIn(BaseModel):
    title: str | None = None
    kb_ids: list[int] | None = None


class AskIn(BaseModel):
    question: str = Field(min_length=1, max_length=4000)
    kb_ids: list[int] | None = None       # 覆盖会话选择
    top_k: int | None = None


class SearchPreviewIn(BaseModel):
    kb_ids: list[int]
    query: str = Field(min_length=1, max_length=4000)


class WikiPageUpdateIn(BaseModel):
    content_md: str
    summary: str | None = None
    note: str = ""


class RebuildIn(BaseModel):
    kind: Literal["chunk", "wiki"] = "chunk"

"""ORM 模型：单企业、知识库级授权、双形态索引（分块 / Wiki）。"""
from __future__ import annotations

import datetime as dt
import enum

from sqlalchemy import (
    Boolean, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint, Index,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


class Role(str, enum.Enum):
    admin = "admin"
    employee = "employee"


class UserStatus(str, enum.Enum):
    active = "active"
    disabled = "disabled"


class KbMode(str, enum.Enum):
    basic = "basic"      # 解析 + 分块 + 关键词/向量索引
    wiki = "wiki"        # 额外做主题归纳与 Wiki 页面


class KbStatus(str, enum.Enum):
    empty = "empty"
    indexing = "indexing"
    ready = "ready"
    partial = "partial"
    failed = "failed"


class GrantLevel(str, enum.Enum):
    read = "read"        # 可检索、可提问、可看来源
    manage = "manage"    # 额外可上传/删除文档、重建索引、校对 Wiki


class TaskKind(str, enum.Enum):
    ingest_document = "ingest_document"
    rebuild_wiki = "rebuild_wiki"
    reindex_kb = "reindex_kb"
    embedding_rebuild = "embedding_rebuild"


class TaskStatus(str, enum.Enum):
    queued = "queued"
    running = "running"
    success = "success"
    partial = "partial"
    failed = "failed"


class WikiPageStatus(str, enum.Enum):
    draft = "draft"
    published = "published"
    needs_review = "needs_review"


class ModelPurpose(str, enum.Enum):
    llm = "llm"
    embedding = "embedding"
    vision = "vision"


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(128), default="")
    password_hash: Mapped[str] = mapped_column(String(256))
    role: Mapped[str] = mapped_column(String(16), default=Role.employee.value)
    status: Mapped[str] = mapped_column(String(16), default=UserStatus.active.value)
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    last_login_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)

    grants: Mapped[list["KbGrant"]] = relationship(back_populates="user", cascade="all, delete-orphan")


class KnowledgeBase(Base):
    __tablename__ = "knowledge_bases"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True)
    description: Mapped[str] = mapped_column(Text, default="")
    mode: Mapped[str] = mapped_column(String(16), default=KbMode.basic.value)
    status: Mapped[str] = mapped_column(String(16), default=KbStatus.empty.value)
    status_detail: Mapped[str] = mapped_column(Text, default="")
    embed_model_id: Mapped[int | None] = mapped_column(ForeignKey("model_configs.id"), nullable=True)
    embed_signature: Mapped[str] = mapped_column(String(200), default="")
    embed_dim: Mapped[int] = mapped_column(Integer, default=0)
    wiki_ready_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)

    grants: Mapped[list["KbGrant"]] = relationship(back_populates="kb", cascade="all, delete-orphan")


class KbGrant(Base):
    __tablename__ = "kb_grants"
    __table_args__ = (UniqueConstraint("kb_id", "user_id", name="uq_grant_kb_user"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    kb_id: Mapped[int] = mapped_column(ForeignKey("knowledge_bases.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    level: Mapped[str] = mapped_column(String(16), default=GrantLevel.read.value)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)

    kb: Mapped[KnowledgeBase] = relationship(back_populates="grants")
    user: Mapped[User] = relationship(back_populates="grants")


class Document(Base):
    __tablename__ = "documents"
    id: Mapped[int] = mapped_column(primary_key=True)
    kb_id: Mapped[int] = mapped_column(ForeignKey("knowledge_bases.id", ondelete="CASCADE"), index=True)
    filename: Mapped[str] = mapped_column(String(255))
    suffix: Mapped[str] = mapped_column(String(16), default="")
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    mime: Mapped[str] = mapped_column(String(128), default="")
    status: Mapped[str] = mapped_column(String(16), default=KbStatus.empty.value)
    status_detail: Mapped[str] = mapped_column(Text, default="")
    current_version_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    deleted: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)

    versions: Mapped[list["DocumentVersion"]] = relationship(
        back_populates="document", cascade="all, delete-orphan",
        foreign_keys="DocumentVersion.doc_id",
    )


class DocumentVersion(Base):
    __tablename__ = "document_versions"
    __table_args__ = (UniqueConstraint("doc_id", "version_no", name="uq_docver_no"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    doc_id: Mapped[int] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"), index=True)
    version_no: Mapped[int] = mapped_column(Integer, default=1)
    storage_path: Mapped[str] = mapped_column(String(512))
    checksum: Mapped[str] = mapped_column(String(64), default="")
    status: Mapped[str] = mapped_column(String(16), default=TaskStatus.queued.value)
    status_detail: Mapped[str] = mapped_column(Text, default="")
    text_chars: Mapped[int] = mapped_column(Integer, default=0)
    chunk_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)

    document: Mapped[Document] = relationship(back_populates="versions", foreign_keys=[doc_id])


class Chunk(Base):
    """分块库检索单元。向量与关键词同表，便于单库检索时一次取全。"""
    __tablename__ = "chunks"
    __table_args__ = (
        Index("ix_chunk_kb_version", "kb_id", "doc_version_id"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    kb_id: Mapped[int] = mapped_column(ForeignKey("knowledge_bases.id", ondelete="CASCADE"), index=True)
    doc_id: Mapped[int] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"), index=True)
    doc_version_id: Mapped[int] = mapped_column(ForeignKey("document_versions.id", ondelete="CASCADE"), index=True)
    ordinal: Mapped[int] = mapped_column(Integer, default=0)
    text: Mapped[str] = mapped_column(Text)
    heading: Mapped[str] = mapped_column(String(512), default="")
    locator: Mapped[str] = mapped_column(String(255), default="")   # 第 N 页 / 表格 2 第 21-40 行
    keywords: Mapped[str] = mapped_column(Text, default="")          # 空格分隔的关键词，供关键词检索
    vector_blob: Mapped[str | None] = mapped_column(Text, nullable=True)
    dim: Mapped[int] = mapped_column(Integer, default=0)
    embed_signature: Mapped[str] = mapped_column(String(200), default="")
    chunk_hash: Mapped[str] = mapped_column(String(64), default="", index=True)
    kind: Mapped[str] = mapped_column(String(16), default="text")     # text | table
    meta: Mapped[str] = mapped_column(Text, default="{}")             # 表格行区间等结构化信息
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)


class WikiPage(Base):
    __tablename__ = "wiki_pages"
    __table_args__ = (UniqueConstraint("kb_id", "slug", name="uq_wiki_kb_slug"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    kb_id: Mapped[int] = mapped_column(ForeignKey("knowledge_bases.id", ondelete="CASCADE"), index=True)
    slug: Mapped[str] = mapped_column(String(160))
    title: Mapped[str] = mapped_column(String(255))
    summary: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(16), default=WikiPageStatus.draft.value)
    current_version_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    parent_slug: Mapped[str] = mapped_column(String(160), default="")
    order_index: Mapped[int] = mapped_column(Integer, default=0)
    human_locked: Mapped[bool] = mapped_column(Boolean, default=False)  # 人工校对过，重建时不静默覆盖
    generated_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class WikiPageVersion(Base):
    __tablename__ = "wiki_page_versions"
    __table_args__ = (UniqueConstraint("page_id", "version_no", name="uq_wikiver_no"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    page_id: Mapped[int] = mapped_column(ForeignKey("wiki_pages.id", ondelete="CASCADE"), index=True)
    version_no: Mapped[int] = mapped_column(Integer, default=1)
    content_md: Mapped[str] = mapped_column(Text, default="")
    summary: Mapped[str] = mapped_column(Text, default="")
    origin: Mapped[str] = mapped_column(String(16), default="generated")  # generated | human
    note: Mapped[str] = mapped_column(String(255), default="")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)


class WikiSection(Base):
    """Wiki 页面按章节切出的检索单元，保留目录层级与原文证据。"""
    __tablename__ = "wiki_sections"
    id: Mapped[int] = mapped_column(primary_key=True)
    kb_id: Mapped[int] = mapped_column(ForeignKey("knowledge_bases.id", ondelete="CASCADE"), index=True)
    page_id: Mapped[int] = mapped_column(ForeignKey("wiki_pages.id", ondelete="CASCADE"), index=True)
    page_version_id: Mapped[int] = mapped_column(ForeignKey("wiki_page_versions.id", ondelete="CASCADE"), index=True)
    ordinal: Mapped[int] = mapped_column(Integer, default=0)
    heading_path: Mapped[str] = mapped_column(String(512), default="")
    text: Mapped[str] = mapped_column(Text)
    keywords: Mapped[str] = mapped_column(Text, default="")
    vector_blob: Mapped[str | None] = mapped_column(Text, nullable=True)
    dim: Mapped[int] = mapped_column(Integer, default=0)
    embed_signature: Mapped[str] = mapped_column(String(200), default="")


class WikiEvidence(Base):
    """Wiki 内容 → 原文分块的证据关联。"""
    __tablename__ = "wiki_evidence"
    id: Mapped[int] = mapped_column(primary_key=True)
    kb_id: Mapped[int] = mapped_column(ForeignKey("knowledge_bases.id", ondelete="CASCADE"), index=True)
    page_version_id: Mapped[int] = mapped_column(ForeignKey("wiki_page_versions.id", ondelete="CASCADE"), index=True)
    chunk_id: Mapped[int | None] = mapped_column(ForeignKey("chunks.id", ondelete="SET NULL"), nullable=True)
    doc_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    doc_version_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    quote: Mapped[str] = mapped_column(Text, default="")
    locator: Mapped[str] = mapped_column(String(255), default="")


class Task(Base):
    __tablename__ = "tasks"
    __table_args__ = (Index("ix_task_status_kind", "status", "kind"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(32), index=True)
    status: Mapped[str] = mapped_column(String(16), default=TaskStatus.queued.value, index=True)
    stage: Mapped[str] = mapped_column(String(64), default="排队中")
    progress: Mapped[int] = mapped_column(Integer, default=0)
    kb_id: Mapped[int | None] = mapped_column(ForeignKey("knowledge_bases.id", ondelete="CASCADE"), nullable=True)
    doc_id: Mapped[int | None] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"), nullable=True)
    doc_version_id: Mapped[int | None] = mapped_column(ForeignKey("document_versions.id", ondelete="CASCADE"), nullable=True)
    dedupe_key: Mapped[str] = mapped_column(String(200), default="", index=True)
    payload: Mapped[str] = mapped_column(Text, default="{}")
    result: Mapped[str] = mapped_column(Text, default="{}")
    error: Mapped[str] = mapped_column(Text, default="")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    started_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)


class IndexVersion(Base):
    __tablename__ = "index_versions"
    id: Mapped[int] = mapped_column(primary_key=True)
    kb_id: Mapped[int] = mapped_column(ForeignKey("knowledge_bases.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(24))          # chunk | wiki
    embed_signature: Mapped[str] = mapped_column(String(200), default="")
    status: Mapped[str] = mapped_column(String(16), default=KbStatus.empty.value)
    unit_count: Mapped[int] = mapped_column(Integer, default=0)
    built_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)


class CostRecord(Base):
    """模型调用成本流水：每次产生模型开销的整理操作写一条，供 /kbs/{id}/cost 聚合。

    为什么需要这张表，而不是只读 Task.result：
    1. Task.result 是「任务」的记录，而人工校对后的章节重建**不是任务**，
       它是同步请求里发生的一次向量化。只读任务表，这部分成本永远查不到。
    2. 一个 ingest_document 任务同时包含「分块向量化」和「Wiki 生成」两段，
       而这两段在论文里必须分列（成本归因不同）。单一 usage 字段做不到拆分，
       所以按阶段拆成多条流水。
    3. 历史任务可以用 SQL 聚合，不必反序列化每个任务的 JSON 再在 Python 里累加。

    与 Task.result 的关系：Task.result["usage"] 仍是任务的完整用量快照（不删不减），
    本表是它的**可聚合投影**。两者不重复计入：/cost 只读本表，
    仅对「没有流水的历史任务」回退读 result，并以 task_id 去重。
    """

    __tablename__ = "cost_records"
    __table_args__ = (Index("ix_cost_kb_kind", "kb_id", "kind"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    kb_id: Mapped[int] = mapped_column(
        ForeignKey("knowledge_bases.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(32), index=True)   # 阶段/任务类型
    source: Mapped[str] = mapped_column(String(24), default="task")  # task | wiki_edit
    source_id: Mapped[int | None] = mapped_column(Integer, nullable=True)  # 任务 id / 版本 id
    doc_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # 计数
    calls: Mapped[int] = mapped_column(Integer, default=0)
    chat_calls: Mapped[int] = mapped_column(Integer, default=0)
    embed_calls: Mapped[int] = mapped_column(Integer, default=0)
    vision_calls: Mapped[int] = mapped_column(Integer, default=0)
    live_calls: Mapped[int] = mapped_column(Integer, default=0)
    offline_calls: Mapped[int] = mapped_column(Integer, default=0)
    # token
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    embed_tokens: Mapped[int] = mapped_column(Integer, default=0)
    embed_texts: Mapped[int] = mapped_column(Integer, default=0)
    # 耗时与模式
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    mode: Mapped[str] = mapped_column(String(16), default="live")
    embed_mode: Mapped[str] = mapped_column(String(16), default="live")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)


class ProviderProtocol(str, enum.Enum):
    """供应商 API 协议族。决定 llm.py 用哪套请求/响应结构说话。"""
    openai = "openai"                       # POST {base}/chat/completions + /embeddings
    openai_responses = "openai-responses"   # POST {base}/responses
    anthropic = "anthropic"                 # POST {base}/messages


PROTOCOL_VALUES = tuple(p.value for p in ProviderProtocol)
DEFAULT_PROTOCOL = ProviderProtocol.openai.value


class Provider(Base):
    __tablename__ = "providers"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True)
    base_url: Mapped[str] = mapped_column(String(255))
    api_key_enc: Mapped[str] = mapped_column(Text, default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # 必须带默认值：老库靠 db.ensure_sqlite_columns() 自动 ALTER TABLE ADD COLUMN，
    # 而 SQLite 不接受「非空且无默认值」的新列。这里只设 Python 侧 default，
    # 与表内其它列保持一致；补列 DDL 会据此生成 DEFAULT 'openai'。
    protocol: Mapped[str] = mapped_column(String(32), default=DEFAULT_PROTOCOL)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)

    models: Mapped[list["ModelConfig"]] = relationship(back_populates="provider", cascade="all, delete-orphan")


class ModelConfig(Base):
    __tablename__ = "model_configs"
    id: Mapped[int] = mapped_column(primary_key=True)
    provider_id: Mapped[int] = mapped_column(ForeignKey("providers.id", ondelete="CASCADE"), index=True)
    purpose: Mapped[str] = mapped_column(String(16), index=True)
    model: Mapped[str] = mapped_column(String(128))
    label: Mapped[str] = mapped_column(String(128), default="")
    params: Mapped[str] = mapped_column(Text, default="{}")
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    embed_dim: Mapped[int] = mapped_column(Integer, default=0)
    embed_signature: Mapped[str] = mapped_column(String(200), default="")
    last_test_ok: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    last_test_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    last_test_message: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)

    provider: Mapped[Provider] = relationship(back_populates="models")


class Conversation(Base):
    __tablename__ = "conversations"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(String(255), default="新会话")
    kb_ids: Mapped[str] = mapped_column(Text, default="[]")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class Message(Base):
    __tablename__ = "messages"
    id: Mapped[int] = mapped_column(primary_key=True)
    conversation_id: Mapped[int] = mapped_column(ForeignKey("conversations.id", ondelete="CASCADE"), index=True)
    role: Mapped[str] = mapped_column(String(16))
    content: Mapped[str] = mapped_column(Text, default="")
    kb_ids: Mapped[str] = mapped_column(Text, default="[]")
    evidence: Mapped[str] = mapped_column(Text, default="[]")   # 本轮实际使用的证据快照（含版本）
    route: Mapped[str] = mapped_column(Text, default="{}")       # 路由与检索统计
    model_config_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)


class AuditLog(Base):
    __tablename__ = "audit_logs"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    username: Mapped[str] = mapped_column(String(64), default="")
    action: Mapped[str] = mapped_column(String(64), index=True)
    target: Mapped[str] = mapped_column(String(255), default="")
    detail: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)


class AppSetting(Base):
    __tablename__ = "app_settings"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

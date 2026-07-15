from __future__ import annotations

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.orm import relationship

from .database import Base


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    username = Column(String, unique=True, nullable=False)
    password_hash = Column(String, nullable=False)
    display_name = Column(String, nullable=True)
    avatar_url = Column(String, nullable=True)
    google_token = Column(String, nullable=True)
    google_token_expires_at = Column(DateTime, nullable=True)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=func.now())
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())

    notebooks = relationship("Notebook", back_populates="user")
    sources = relationship("Source", back_populates="user")
    chat_sessions = relationship("ChatSession", back_populates="user")
    chat_messages = relationship("ChatMessage", back_populates="user")
    generated_contents = relationship("GeneratedContent", back_populates="user")
    request_logs = relationship("RequestLog", back_populates="user")
    external_kb_connections = relationship("ExternalKBConnection", back_populates="user")
    external_imports = relationship("ExternalImport", back_populates="user")
    sessions = relationship("UserSession", back_populates="user")


class UserSession(Base):
    __tablename__ = "user_sessions"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    token = Column(String, unique=True, nullable=False)
    expires_at = Column(DateTime, nullable=False)
    created_at = Column(DateTime, default=func.now())

    user = relationship("User", back_populates="sessions")


class Notebook(Base):
    __tablename__ = "notebooks"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    remote_id = Column(String, unique=True, nullable=False)
    title = Column(String, nullable=False)
    description = Column(Text, nullable=True)
    source_count = Column(Integer, default=0)
    chat_count = Column(Integer, default=0)
    last_synced_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=func.now())
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())

    user = relationship("User", back_populates="notebooks")
    sources = relationship("Source", back_populates="notebook")
    chat_sessions = relationship("ChatSession", back_populates="notebook")
    generated_contents = relationship("GeneratedContent", back_populates="notebook")


class Source(Base):
    __tablename__ = "sources"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    notebook_id = Column(Integer, ForeignKey("notebooks.id"), nullable=True)
    remote_id = Column(String, unique=True, nullable=False)
    filename = Column(String, nullable=False)
    original_filename = Column(String, nullable=True)
    file_type = Column(String, nullable=True)
    file_size = Column(Integer, nullable=True)
    page_count = Column(Integer, nullable=True)
    local_path = Column(String, nullable=True)
    source_url = Column(String, nullable=True)
    summary = Column(Text, nullable=True)
    status = Column(String, default="active")
    created_at = Column(DateTime, default=func.now())
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())

    user = relationship("User", back_populates="sources")
    notebook = relationship("Notebook", back_populates="sources")


class ChatSession(Base):
    __tablename__ = "chat_sessions"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    notebook_id = Column(Integer, ForeignKey("notebooks.id"), nullable=True)
    title = Column(String, nullable=True)
    message_count = Column(Integer, default=0)
    created_at = Column(DateTime, default=func.now())
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())

    user = relationship("User", back_populates="chat_sessions")
    notebook = relationship("Notebook", back_populates="chat_sessions")
    messages = relationship("ChatMessage", back_populates="session")


class ChatMessage(Base):
    __tablename__ = "chat_messages"

    id = Column(Integer, primary_key=True)
    session_id = Column(Integer, ForeignKey("chat_sessions.id"), nullable=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    role = Column(String, nullable=False)
    content = Column(Text, nullable=False)
    citations = Column(Text, nullable=True)
    request_body = Column(Text, nullable=True)
    response_body = Column(Text, nullable=True)
    latency_ms = Column(Integer, nullable=True)
    token_count = Column(Integer, nullable=True)
    status = Column(String, default="success")
    error_message = Column(Text, nullable=True)
    created_at = Column(DateTime, default=func.now())

    session = relationship("ChatSession", back_populates="messages")
    user = relationship("User", back_populates="chat_messages")


class GeneratedContent(Base):
    __tablename__ = "generated_contents"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    notebook_id = Column(Integer, ForeignKey("notebooks.id"), nullable=True)
    content_type = Column(String, nullable=False)
    title = Column(String, nullable=True)
    prompt = Column(Text, nullable=True)
    engine = Column(String, default="notebooklm")

    content = Column(Text, nullable=True)
    local_file_path = Column(String, nullable=True)
    file_size = Column(Integer, nullable=True)
    thumbnail_path = Column(String, nullable=True)
    status = Column(String, default="processing")
    error_message = Column(Text, nullable=True)

    ppt_page_count = Column(Integer, nullable=True)
    ppt_template = Column(String, nullable=True)
    ppt_json = Column(Text, nullable=True)
    ppt_preview_images = Column(Text, nullable=True)

    mindmap_data = Column(Text, nullable=True)
    mindmap_layout = Column(String, default="tree")

    infographic_template = Column(String, nullable=True)
    infographic_blocks = Column(Text, nullable=True)

    audio_file_path = Column(String, nullable=True)
    duration_seconds = Column(Integer, nullable=True)
    audio_speakers = Column(Text, nullable=True)
    audio_transcript = Column(Text, nullable=True)

    video_file_path = Column(String, nullable=True)
    video_duration_seconds = Column(Integer, nullable=True)
    video_resolution = Column(String, nullable=True)
    video_scenes = Column(Text, nullable=True)
    video_narration = Column(Text, nullable=True)
    video_bg_music = Column(String, nullable=True)

    doc_page_count = Column(Integer, nullable=True)
    doc_sections = Column(Text, nullable=True)
    doc_format = Column(String, default="markdown")

    request_body = Column(Text, nullable=True)
    response_body = Column(Text, nullable=True)
    latency_ms = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=func.now())

    user = relationship("User", back_populates="generated_contents")
    notebook = relationship("Notebook", back_populates="generated_contents")


class RequestLog(Base):
    __tablename__ = "request_logs"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    endpoint = Column(String, nullable=False)
    method = Column(String, nullable=False)
    request_headers = Column(Text, nullable=True)
    request_body = Column(Text, nullable=True)
    response_status = Column(Integer, nullable=True)
    response_headers = Column(Text, nullable=True)
    response_body = Column(Text, nullable=True)
    latency_ms = Column(Integer, nullable=True)
    client_ip = Column(String, nullable=True)
    user_agent = Column(String, nullable=True)
    created_at = Column(DateTime, default=func.now())

    user = relationship("User", back_populates="request_logs")


class ExternalKBConnection(Base):
    __tablename__ = "external_kb_connections"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    name = Column(String, nullable=False)
    provider_type = Column(String, nullable=False)
    api_base_url = Column(String, nullable=False)
    auth_type = Column(String, default="api_key")
    auth_credentials = Column(Text, nullable=True)
    extra_config = Column(Text, nullable=True)
    is_active = Column(Boolean, default=True)
    last_sync_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=func.now())
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())

    user = relationship("User", back_populates="external_kb_connections")
    collections = relationship("ExternalKBCollection", back_populates="connection")
    documents = relationship("ExternalKBDocument", back_populates="connection")


class ExternalKBCollection(Base):
    __tablename__ = "external_kb_collections"

    id = Column(Integer, primary_key=True)
    connection_id = Column(Integer, ForeignKey("external_kb_connections.id"), nullable=True)
    remote_id = Column(String, nullable=False)
    name = Column(String, nullable=False)
    description = Column(Text, nullable=True)
    document_count = Column(Integer, default=0)
    last_fetched_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=func.now())

    connection = relationship("ExternalKBConnection", back_populates="collections")
    documents = relationship("ExternalKBDocument", back_populates="collection")


class ExternalKBDocument(Base):
    __tablename__ = "external_kb_documents"

    id = Column(Integer, primary_key=True)
    collection_id = Column(Integer, ForeignKey("external_kb_collections.id"), nullable=True)
    connection_id = Column(Integer, ForeignKey("external_kb_connections.id"), nullable=True)
    remote_id = Column(String, nullable=False)
    title = Column(String, nullable=False)
    summary = Column(Text, nullable=True)
    file_type = Column(String, nullable=True)
    file_size = Column(Integer, nullable=True)
    url = Column(String, nullable=True)
    doc_metadata = Column("metadata", Text, nullable=True)
    last_fetched_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=func.now())

    collection = relationship("ExternalKBCollection", back_populates="documents")
    connection = relationship("ExternalKBConnection", back_populates="documents")


class ExternalImport(Base):
    __tablename__ = "external_imports"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    connection_id = Column(Integer, ForeignKey("external_kb_connections.id"), nullable=True)
    source_document_id = Column(Integer, ForeignKey("external_kb_documents.id"), nullable=True)
    target_notebook_id = Column(Integer, ForeignKey("notebooks.id"), nullable=True)
    target_source_id = Column(Integer, ForeignKey("sources.id"), nullable=True)
    status = Column(String, default="pending")
    error_message = Column(Text, nullable=True)
    imported_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=func.now())

    user = relationship("User", back_populates="external_imports")


ALL_MODELS = [
    User,
    UserSession,
    Notebook,
    Source,
    ChatSession,
    ChatMessage,
    GeneratedContent,
    RequestLog,
    ExternalKBConnection,
    ExternalKBCollection,
    ExternalKBDocument,
    ExternalImport,
]

Index("idx_notebooks_user", Notebook.user_id)
Index("idx_notebooks_remote", Notebook.remote_id)
Index("idx_sources_notebook", Source.notebook_id)
Index("idx_sources_user", Source.user_id)
Index("idx_chat_sessions_notebook", ChatSession.notebook_id)
Index("idx_chat_messages_session", ChatMessage.session_id)
Index("idx_chat_messages_user", ChatMessage.user_id)
Index("idx_generated_notebook", GeneratedContent.notebook_id)
Index("idx_generated_user", GeneratedContent.user_id)
Index("idx_request_logs_user", RequestLog.user_id)
Index("idx_request_logs_endpoint", RequestLog.endpoint)
Index("idx_request_logs_created", RequestLog.created_at)
Index("idx_ext_kb_conn_user", ExternalKBConnection.user_id)
Index("idx_ext_kb_coll_conn", ExternalKBCollection.connection_id)
Index("idx_ext_kb_docs_coll", ExternalKBDocument.collection_id)
Index("idx_ext_kb_docs_conn", ExternalKBDocument.connection_id)
Index("idx_ext_imports_user", ExternalImport.user_id)
Index("idx_ext_imports_notebook", ExternalImport.target_notebook_id)
Index("idx_ext_kb_coll_unique", ExternalKBCollection.connection_id, ExternalKBCollection.remote_id, unique=True)
Index("idx_ext_kb_docs_unique", ExternalKBDocument.collection_id, ExternalKBDocument.remote_id, unique=True)

# 三栏 Notebook 详情页实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 将 Notebook 详情页从标签页改为左中右三栏工作台，新增笔记、聊天会话、批量删除等后端 API。

**架构：** 后端在 `src/notebooklm/server/routes/` 新增 `notes.py` 和 `chat_sessions.py` 路由，扩展 `sources.py` 加批量删除。前端新建 `SourcesPanel.vue`、`ChatPanel.vue`、`GeneratePanel.vue` 三个面板组件，重写 `NotebookView.vue` 为三栏容器。

**技术栈：** FastAPI + SQLAlchemy（后端），Vue 3 + Element Plus + Pinia（前端）

---

## 文件结构

### 后端新建
- `src/notebooklm/server/routes/notes.py` — 笔记 CRUD 路由
- `src/notebooklm/server/routes/chat_sessions.py` — 聊天会话 + 消息路由

### 后端修改
- `src/notebooklm/server/models.py` — 新增 `Note` 模型
- `src/notebooklm/server/server.py` — 注册新路由
- `src/notebooklm/server/routes/sources.py` — 新增批量删除端点
- `src/notebooklm/server/routes/chat.py` — `ChatAsk` 增加 `source_ids` 字段

### 前端新建
- `frontend/src/components/SourcesPanel.vue` — 左栏：资料 + 笔记
- `frontend/src/components/ChatPanel.vue` — 中栏：AI 问答
- `frontend/src/components/GeneratePanel.vue` — 右栏：内容生成
- `frontend/src/components/SplitPane.vue` — 可拖拽分隔条
- `frontend/src/api/notes.ts` — 笔记 API 封装
- `frontend/src/api/chat-sessions.ts` — 聊天会话 API 封装

### 前端修改
- `frontend/src/views/NotebookView.vue` — 重写为三栏容器
- `frontend/src/router/index.ts` — 更新路由
- `frontend/src/api/chat.ts` — 修正会话 API URL
- `frontend/src/api/generation.ts` — 修正生成 API URL

---

## 任务 1：Note 数据模型

**文件：**
- 修改：`src/notebooklm/server/models.py`

- [ ] **步骤 1：在 models.py 中新增 Note 模型**

在 `ExternalImport` 类之后、`ALL_MODELS` 列表之前插入：

```python
class Note(Base):
    __tablename__ = "notes"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    notebook_id = Column(Integer, ForeignKey("notebooks.id"), nullable=True)
    title = Column(String, nullable=True)
    content = Column(Text, nullable=False, default="")
    created_at = Column(DateTime, default=func.now())
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())

    user = relationship("User")
    notebook = relationship("Notebook")
```

在 `ALL_MODELS` 列表中追加 `Note`：

```python
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
    Note,
]
```

在文件末尾的 Index 列表中追加：

```python
Index("idx_notes_notebook", Note.notebook_id)
Index("idx_notes_user", Note.user_id)
```

- [ ] **步骤 2：验证模型导入**

运行：`python -c "from notebooklm.server.models import Note; print(Note.__tablename__)"`
预期：输出 `notes`

- [ ] **步骤 3：Commit**

```bash
git add src/notebooklm/server/models.py
git commit -m "feat(server): add Note model for notebook notes"
```

---

## 任务 2：笔记 CRUD 路由

**文件：**
- 创建：`src/notebooklm/server/routes/notes.py`
- 修改：`src/notebooklm/server/server.py`

- [ ] **步骤 1：创建 notes.py 路由文件**

```python
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..auth_deps import get_current_user
from ..database import get_session
from ..models import Note as NoteModel
from ..models import Notebook, User

router = APIRouter(prefix="/notebooks/{notebook_id}/notes", tags=["notes"])


class NoteCreate(BaseModel):
    title: str | None = None
    content: str = ""


class NoteUpdate(BaseModel):
    title: str | None = None
    content: str | None = None


def _get_notebook_db_id(db: Session, notebook_id: str) -> int | None:
    row = db.query(Notebook).filter(Notebook.remote_id == notebook_id).first()
    return row.id if row else None


@router.get("")
def list_notes(
    notebook_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> dict[str, Any]:
    db_id = _get_notebook_db_id(db, notebook_id)
    if db_id is None:
        raise HTTPException(404, "Notebook not found")
    rows = (
        db.query(NoteModel)
        .filter(NoteModel.notebook_id == db_id, NoteModel.user_id == user.id)
        .order_by(NoteModel.created_at.desc())
        .all()
    )
    return {
        "items": [
            {
                "id": r.id,
                "title": r.title,
                "content": r.content,
                "created_at": r.created_at.isoformat() if r.created_at else None,
                "updated_at": r.updated_at.isoformat() if r.updated_at else None,
            }
            for r in rows
        ],
        "total": len(rows),
    }


@router.post("", status_code=201)
def create_note(
    notebook_id: str,
    body: NoteCreate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> dict[str, Any]:
    db_id = _get_notebook_db_id(db, notebook_id)
    if db_id is None:
        raise HTTPException(404, "Notebook not found")
    record = NoteModel(
        user_id=user.id,
        notebook_id=db_id,
        title=body.title,
        content=body.content,
    )
    db.add(record)
    db.commit()
    db.refresh(record)
    return {
        "id": record.id,
        "title": record.title,
        "content": record.content,
        "created_at": record.created_at.isoformat() if record.created_at else None,
        "updated_at": record.updated_at.isoformat() if record.updated_at else None,
    }


@router.put("/{note_id}")
def update_note(
    notebook_id: str,
    note_id: int,
    body: NoteUpdate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> dict[str, Any]:
    record = (
        db.query(NoteModel)
        .filter(NoteModel.id == note_id, NoteModel.user_id == user.id)
        .first()
    )
    if not record:
        raise HTTPException(404, "Note not found")
    if body.title is not None:
        record.title = body.title
    if body.content is not None:
        record.content = body.content
    db.commit()
    db.refresh(record)
    return {
        "id": record.id,
        "title": record.title,
        "content": record.content,
        "created_at": record.created_at.isoformat() if record.created_at else None,
        "updated_at": record.updated_at.isoformat() if record.updated_at else None,
    }


@router.delete("/{note_id}", status_code=204)
def delete_note(
    notebook_id: str,
    note_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> None:
    record = (
        db.query(NoteModel)
        .filter(NoteModel.id == note_id, NoteModel.user_id == user.id)
        .first()
    )
    if not record:
        raise HTTPException(404, "Note not found")
    db.delete(record)
    db.commit()
```

- [ ] **步骤 2：在 server.py 中注册路由**

在 `server.py` 的 import 部分追加：

```python
from .routes import auth, chat, chat_sessions, external_kb, generation, notebooks, notes, sources
```

在 `create_app()` 函数中，`app.include_router(chat.router, prefix="/api")` 之后追加：

```python
app.include_router(notes.router, prefix="/api")
```

- [ ] **步骤 3：验证 lint**

运行：`uv run ruff check src/notebooklm/server/routes/notes.py`
预期：无错误

- [ ] **步骤 4：Commit**

```bash
git add src/notebooklm/server/routes/notes.py src/notebooklm/server/server.py
git commit -m "feat(server): add notes CRUD API for notebook notes"
```

---

## 任务 3：聊天会话 CRUD 路由

**文件：**
- 创建：`src/notebooklm/server/routes/chat_sessions.py`
- 修改：`src/notebooklm/server/server.py`

- [ ] **步骤 1：创建 chat_sessions.py 路由文件**

```python
from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ...client import NotebookLMClient
from .._context import get_client
from ..auth_deps import get_current_user
from ..database import get_session
from ..models import ChatMessage, ChatSession, Notebook, User

router = APIRouter(prefix="/notebooks/{notebook_id}/chat", tags=["chat-sessions"])


class SessionCreate(BaseModel):
    title: str | None = None


class MessageCreate(BaseModel):
    content: str
    source_ids: list[str] | None = None


def _get_notebook_db_id(db: Session, notebook_id: str) -> int | None:
    row = db.query(Notebook).filter(Notebook.remote_id == notebook_id).first()
    return row.id if row else None


@router.get("/sessions")
def list_sessions(
    notebook_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> dict[str, Any]:
    db_id = _get_notebook_db_id(db, notebook_id)
    if db_id is None:
        return {"items": [], "total": 0}
    rows = (
        db.query(ChatSession)
        .filter(ChatSession.notebook_id == db_id, ChatSession.user_id == user.id)
        .order_by(ChatSession.created_at.desc())
        .all()
    )
    return {
        "items": [
            {
                "id": r.id,
                "title": r.title,
                "message_count": r.message_count,
                "created_at": r.created_at.isoformat() if r.created_at else None,
                "updated_at": r.updated_at.isoformat() if r.updated_at else None,
            }
            for r in rows
        ],
        "total": len(rows),
    }


@router.post("/sessions", status_code=201)
def create_session(
    notebook_id: str,
    body: SessionCreate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> dict[str, Any]:
    db_id = _get_notebook_db_id(db, notebook_id)
    if db_id is None:
        raise HTTPException(404, "Notebook not found")
    record = ChatSession(
        user_id=user.id,
        notebook_id=db_id,
        title=body.title,
    )
    db.add(record)
    db.commit()
    db.refresh(record)
    return {
        "id": record.id,
        "title": record.title,
        "message_count": 0,
        "created_at": record.created_at.isoformat() if record.created_at else None,
        "updated_at": record.updated_at.isoformat() if record.updated_at else None,
    }


@router.delete("/sessions/{session_id}", status_code=204)
def delete_session(
    notebook_id: str,
    session_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> None:
    record = (
        db.query(ChatSession)
        .filter(ChatSession.id == session_id, ChatSession.user_id == user.id)
        .first()
    )
    if not record:
        raise HTTPException(404, "Session not found")
    db.query(ChatMessage).filter(ChatMessage.session_id == session_id).delete()
    db.delete(record)
    db.commit()


@router.get("/sessions/{session_id}/messages")
def list_messages(
    notebook_id: str,
    session_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> dict[str, Any]:
    session = (
        db.query(ChatSession)
        .filter(ChatSession.id == session_id, ChatSession.user_id == user.id)
        .first()
    )
    if not session:
        raise HTTPException(404, "Session not found")
    rows = (
        db.query(ChatMessage)
        .filter(ChatMessage.session_id == session_id)
        .order_by(ChatMessage.created_at.asc())
        .all()
    )
    return {
        "items": [
            {
                "id": r.id,
                "role": r.role,
                "content": r.content,
                "citations": json.loads(r.citations) if r.citations else None,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ],
        "total": len(rows),
    }


@router.post("/sessions/{session_id}/messages", status_code=201)
async def create_message(
    notebook_id: str,
    session_id: int,
    body: MessageCreate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
    client: NotebookLMClient = Depends(get_client),
) -> dict[str, Any]:
    session = (
        db.query(ChatSession)
        .filter(ChatSession.id == session_id, ChatSession.user_id == user.id)
        .first()
    )
    if not session:
        raise HTTPException(404, "Session not found")

    user_msg = ChatMessage(
        session_id=session_id,
        user_id=user.id,
        role="user",
        content=body.content,
    )
    db.add(user_msg)
    session.message_count = (session.message_count or 0) + 1

    result = await client.chat.ask(
        notebook_id,
        body.content,
        source_ids=body.source_ids,
    )

    citations_json = json.dumps([{"source_id": r.source_id, "source_name": r.title} for r in (result.references or [])])

    assistant_msg = ChatMessage(
        session_id=session_id,
        user_id=user.id,
        role="assistant",
        content=result.answer,
        citations=citations_json,
    )
    db.add(assistant_msg)
    session.message_count = (session.message_count or 0) + 1
    db.commit()
    db.refresh(assistant_msg)

    return {
        "id": assistant_msg.id,
        "role": "assistant",
        "content": assistant_msg.content,
        "citations": json.loads(assistant_msg.citations) if assistant_msg.citations else None,
        "created_at": assistant_msg.created_at.isoformat() if assistant_msg.created_at else None,
    }
```

- [ ] **步骤 2：在 server.py 中注册路由**

在 `create_app()` 中 `app.include_router(chat.router, prefix="/api")` 之后追加：

```python
app.include_router(chat_sessions.router, prefix="/api")
```

确保 import 包含 `chat_sessions`：

```python
from .routes import auth, chat, chat_sessions, external_kb, generation, notebooks, notes, sources
```

- [ ] **步骤 3：验证 lint**

运行：`uv run ruff check src/notebooklm/server/routes/chat_sessions.py`
预期：无错误

- [ ] **步骤 4：Commit**

```bash
git add src/notebooklm/server/routes/chat_sessions.py src/notebooklm/server/server.py
git commit -m "feat(server): add chat session and message CRUD routes"
```

---

## 任务 4：Chat ask 路由增加 source_ids

**文件：**
- 修改：`src/notebooklm/server/routes/chat.py`

- [ ] **步骤 1：在 ChatAsk 模型中增加 source_ids 字段**

将 `ChatAsk` 类改为：

```python
class ChatAsk(BaseModel):
    """Request body for asking a notebook's sources a question."""

    question: str
    conversation_id: str | None = None
    source_ids: list[str] | None = None
```

将 `ask` 函数改为：

```python
@router.post("", dependencies=[Depends(limit_chat)])
async def ask(notebook_id: str, body: ChatAsk, client: ClientDep) -> dict[str, Any]:
    """Ask the notebook's sources a question and return the full answer."""
    result = await client.chat.ask(
        notebook_id,
        body.question,
        source_ids=body.source_ids,
        conversation_id=body.conversation_id,
    )
    return ask_result_view(result)
```

- [ ] **步骤 2：验证 lint**

运行：`uv run ruff check src/notebooklm/server/routes/chat.py`
预期：无错误

- [ ] **步骤 3：Commit**

```bash
git add src/notebooklm/server/routes/chat.py
git commit -m "feat(server): add source_ids to chat ask route"
```

---

## 任务 5：批量删除资料路由

**文件：**
- 修改：`src/notebooklm/server/routes/sources.py`

- [ ] **步骤 1：在 sources.py 末尾追加批量删除端点**

在文件末尾追加：

```python
class SourceBatchDelete(BaseModel):
    """Request body for batch-deleting sources."""

    source_ids: list[str]


@router.post("/batch-delete", status_code=204, dependencies=[Depends(limit_source_mutation)])
async def batch_delete_sources(
    notebook_id: str,
    body: SourceBatchDelete,
    client: ClientDep,
    pending: PendingDep,
) -> Response:
    """Delete multiple sources (sequentially, idempotent per item)."""
    for sid in body.source_ids:
        try:
            await client.sources.delete(notebook_id, sid)
            pending.drop(notebook_id, sid)
        except Exception:
            pass
    return Response(status_code=204)
```

- [ ] **步骤 2：验证 lint**

运行：`uv run ruff check src/notebooklm/server/routes/sources.py`
预期：无错误

- [ ] **步骤 3：Commit**

```bash
git add src/notebooklm/server/routes/sources.py
git commit -m "feat(server): add batch delete sources endpoint"
```

---

## 任务 6：前端笔记 API 封装

**文件：**
- 创建：`frontend/src/api/notes.ts`

- [ ] **步骤 1：创建 notes.ts**

```typescript
import request from "./request"

export interface Note {
  id: number
  title: string | null
  content: string
  created_at: string
  updated_at: string
}

export function fetchNotesApi(notebookId: string): Promise<{ items: Note[]; total: number }> {
  return request.get(`/api/notebooks/${notebookId}/notes`).then((r) => r.data)
}

export function createNoteApi(notebookId: string, data: { title?: string; content: string }): Promise<Note> {
  return request.post(`/api/notebooks/${notebookId}/notes`, data).then((r) => r.data)
}

export function updateNoteApi(notebookId: string, noteId: number, data: { title?: string; content?: string }): Promise<Note> {
  return request.put(`/api/notebooks/${notebookId}/notes/${noteId}`, data).then((r) => r.data)
}

export function deleteNoteApi(notebookId: string, noteId: number): Promise<void> {
  return request.delete(`/api/notebooks/${notebookId}/notes/${noteId}`).then((r) => r.data)
}
```

- [ ] **步骤 2：Commit**

```bash
git add frontend/src/api/notes.ts
git commit -m "feat(web): add notes API module"
```

---

## 任务 7：前端聊天会话 API 修正

**文件：**
- 修改：`frontend/src/api/chat.ts`

- [ ] **步骤 1：重写 chat.ts，修正 URL 和类型**

将 `chat.ts` 的接口和函数替换为（保留流式函数但改为调用新的消息端点）：

```typescript
import request from "./request"

export interface ChatSession { id: number; title: string | null; message_count: number; created_at: string; updated_at: string }
export interface CitationItem { source_id: string; source_name: string }
export interface ChatMessage { id: number; role: "user" | "assistant"; content: string; citations: CitationItem[] | null; created_at: string }

export function fetchSessionsApi(notebookId: string): Promise<ChatSession[]> {
  return request.get(`/api/notebooks/${notebookId}/chat/sessions`).then((r) => r.data.items || [])
}
export function createSessionApi(notebookId: string, title?: string): Promise<ChatSession> {
  return request.post(`/api/notebooks/${notebookId}/chat/sessions`, { title }).then((r) => r.data)
}
export function deleteSessionApi(notebookId: string, sessionId: number): Promise<void> {
  return request.delete(`/api/notebooks/${notebookId}/chat/sessions/${sessionId}`).then((r) => r.data)
}
export function fetchMessagesApi(notebookId: string, sessionId: number): Promise<ChatMessage[]> {
  return request.get(`/api/notebooks/${notebookId}/chat/sessions/${sessionId}/messages`).then((r) => r.data.items || [])
}
export function sendMessageApi(notebookId: string, sessionId: number, content: string, sourceIds?: string[]): Promise<ChatMessage> {
  return request.post(`/api/notebooks/${notebookId}/chat/sessions/${sessionId}/messages`, { content, source_ids: sourceIds }).then((r) => r.data)
}

export function sendMessageStreamApi(
  notebookId: string, sessionId: number, content: string,
  onMessage: (text: string) => void, onCitations?: (citations: CitationItem[]) => void,
  onDone?: () => void, onError?: (err: any) => void,
  sourceIds?: string[],
): { abort: () => void } {
  const controller = new AbortController()
  const token = localStorage.getItem("token")
  fetch(`/api/notebooks/${notebookId}/chat/sessions/${sessionId}/messages`, {
    method: "POST",
    headers: { "Content-Type": "application/json", Authorization: token ? `Bearer ${token}` : "" },
    body: JSON.stringify({ content, source_ids: sourceIds }), signal: controller.signal,
  }).then(async (res) => {
    if (!res.ok) { const err = await res.json().catch(() => ({ detail: "请求失败" })); onError?.(err); return }
    const data = await res.json()
    if (data.content) onMessage(data.content)
    if (data.citations) onCitations?.(data.citations)
    onDone?.()
  }).catch((err) => { if (err.name !== "AbortError") onError?.(err) })
  return { abort: () => controller.abort() }
}
```

- [ ] **步骤 2：更新 chat store**

修改 `frontend/src/stores/chat.ts`，将所有 `nbId: number` 改为 `nbId: string`，并给 `sendMessage` 加 `sourceIds` 参数：

```typescript
import { defineStore } from "pinia"; import { ref } from "vue"
import type { ChatSession, ChatMessage } from "@/api/chat"
import { fetchSessionsApi, createSessionApi, deleteSessionApi, fetchMessagesApi, sendMessageStreamApi } from "@/api/chat"

export const useChatStore = defineStore("chat", () => {
  const sessions = ref<ChatSession[]>([]); const currentSessionId = ref<number | null>(null)
  const messages = ref<ChatMessage[]>([]); const streaming = ref(false); const streamContent = ref("")

  async function fetchSessions(nbId: string) { sessions.value = await fetchSessionsApi(nbId) }
  async function createSession(nbId: string) { const s = await createSessionApi(nbId); sessions.value.unshift(s); currentSessionId.value = s.id; messages.value = []; return s }
  async function deleteSession(nbId: string, sid: number) { await deleteSessionApi(nbId, sid); sessions.value = sessions.value.filter((s) => s.id !== sid); if (currentSessionId.value === sid) { currentSessionId.value = null; messages.value = [] } }
  async function loadMessages(nbId: string, sid: number) { currentSessionId.value = sid; messages.value = await fetchMessagesApi(nbId, sid) }

  function sendMessage(nbId: string, sid: number, content: string, callbacks?: { onMessage?: (t: string) => void; onDone?: () => void; onError?: (err: any) => void }, sourceIds?: string[]) {
    streaming.value = true; streamContent.value = ""
    messages.value.push({ id: -Date.now(), role: "user", content, citations: null, created_at: new Date().toISOString() })
    return sendMessageStreamApi(nbId, sid, content,
      (text) => { streamContent.value = text; callbacks?.onMessage?.(text) },
      (citations) => { const last = messages.value[messages.value.length-1]; if (last?.role === "assistant") last.citations = citations },
      () => { streaming.value = false; messages.value.push({ id: -Date.now(), role: "assistant", content: streamContent.value, citations: null, created_at: new Date().toISOString() }); streamContent.value = ""; callbacks?.onDone?.() },
      (err) => { streaming.value = false; callbacks?.onError?.(err) },
      sourceIds,
    )
  }

  return { sessions, currentSessionId, messages, streaming, streamContent, fetchSessions, createSession, deleteSession, loadMessages, sendMessage }
})
```

- [ ] **步骤 3：验证类型检查**

运行：`cd frontend && npx vue-tsc --noEmit`
预期：无错误

- [ ] **步骤 4：Commit**

```bash
git add frontend/src/api/chat.ts frontend/src/stores/chat.ts
git commit -m "fix(web): correct chat session API URLs and add source_ids support"
```

---

## 任务 8：修正前端生成 API

**文件：**
- 修改：`frontend/src/api/generation.ts`

- [ ] **步骤 1：修正 generation.ts 的 URL**

```typescript
import request from "./request"

export interface GeneratedContent {
  id: number; content_type: string; title: string | null; prompt: string | null
  status: string; local_file_path: string | null; file_size: number | null; error_message: string | null
  content: string | null; thumbnail_path: string | null
  ppt_page_count: number | null; ppt_template: string | null; ppt_json: string | null; ppt_preview_images: string | null
  mindmap_data: string | null; mindmap_layout: string | null
  infographic_template: string | null; infographic_blocks: string | null
  audio_file_path: string | null; duration_seconds: number | null; audio_speakers: string | null; audio_transcript: string | null
  video_file_path: string | null; video_duration_seconds: number | null; video_resolution: string | null; video_scenes: string | null; video_narration: string | null
  doc_page_count: number | null; doc_sections: string | null; doc_format: string | null
  created_at: string
}

export interface GenerateRequest { content_type: string; prompt: string; template?: string; options?: Record<string, any> }
export interface TemplateInfo { id: string; name: string; description: string; thumbnail_url?: string }

export function fetchGeneratedContentsApi(notebookId: string): Promise<GeneratedContent[]> {
  return request.get("/api/generation/list", { params: { notebook_id: notebookId } }).then((r) => r.data.items || [])
}
export function generateContentApi(notebookId: string, data: GenerateRequest): Promise<GeneratedContent> {
  return request.post("/api/generation/generate", { ...data, notebook_id: notebookId }).then((r) => r.data)
}
export function fetchTemplatesApi(contentType: string): Promise<TemplateInfo[]> {
  return request.get("/api/generation/templates", { params: { content_type: contentType } }).then((r) => r.data)
}
export function fetchGeneratedDetailApi(notebookId: string, generatedId: number): Promise<GeneratedContent> {
  return request.get(`/api/generation/${generatedId}`).then((r) => r.data)
}
export function deleteGeneratedApi(notebookId: string, generatedId: number): Promise<void> {
  return request.delete(`/api/generation/${generatedId}`).then((r) => r.data)
}
```

- [ ] **步骤 2：验证类型检查**

运行：`cd frontend && npx vue-tsc --noEmit`
预期：可能有组件中的类型错误（后续任务修复）

- [ ] **步骤 3：Commit**

```bash
git add frontend/src/api/generation.ts
git commit -m "fix(web): correct generation API URLs to match backend"
```

---

## 任务 9：SplitPane 组件

**文件：**
- 创建：`frontend/src/components/SplitPane.vue`

- [ ] **步骤 1：创建可拖拽分隔条组件**

```vue
<template>
  <div class="split-pane" :style="{ flexDirection: direction }">
    <div class="pane pane-left" :style="leftStyle">
      <slot name="left" />
    </div>
    <div class="splitter" @mousedown="startDrag" @touchstart="startDrag">
      <div class="splitter-line" />
    </div>
    <div class="pane pane-right" :style="rightStyle">
      <slot name="right" />
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted } from "vue"

const props = withDefaults(defineProps<{
  minLeft?: number
  minRight?: number
  initialLeft?: number
  storageKey?: string
  direction?: "row" | "column"
}>(), {
  minLeft: 200, minRight: 200, initialLeft: 300, direction: "row",
})

const leftWidth = ref(props.initialLeft)
const dragging = ref(false)

onMounted(() => {
  if (props.storageKey) {
    const saved = localStorage.getItem(props.storageKey)
    if (saved) leftWidth.value = Number(saved)
  }
})

const leftStyle = computed(() => ({
  width: props.direction === "row" ? `${leftWidth.value}px` : "100%",
  height: props.direction === "column" ? `${leftWidth.value}px` : "100%",
  flexShrink: 0,
}))
const rightStyle = computed(() => ({ flex: 1, minWidth: 0 }))

function startDrag(e: MouseEvent | TouchEvent) {
  e.preventDefault()
  dragging.value = true
  const startX = "touches" in e ? e.touches[0].clientX : e.clientX
  const startY = "touches" in e ? e.touches[0].clientY : e.clientY
  const startWidth = leftWidth.value
  const parent = (e.target as HTMLElement).closest(".split-pane") as HTMLElement
  const parentSize = props.direction === "row" ? parent.offsetWidth : parent.offsetHeight

  function onMove(ev: MouseEvent | TouchEvent) {
    if (!dragging.value) return
    const currentX = "touches" in ev ? ev.touches[0].clientX : ev.clientX
    const currentY = "touches" in ev ? ev.touches[0].clientY : ev.clientY
    const delta = props.direction === "row" ? currentX - startX : currentY - startY
    let newWidth = startWidth + delta
    const max = parentSize - props.minRight
    newWidth = Math.max(props.minLeft, Math.min(max, newWidth))
    leftWidth.value = newWidth
  }
  function onUp() {
    dragging.value = false
    if (props.storageKey) localStorage.setItem(props.storageKey, String(leftWidth.value))
    document.removeEventListener("mousemove", onMove)
    document.removeEventListener("mouseup", onUp)
    document.removeEventListener("touchmove", onMove)
    document.removeEventListener("touchend", onUp)
  }
  document.addEventListener("mousemove", onMove)
  document.addEventListener("mouseup", onUp)
  document.addEventListener("touchmove", onMove)
  document.addEventListener("touchend", onUp)
}
</script>

<style scoped>
.split-pane { display: flex; width: 100%; height: 100%; overflow: hidden; }
.pane { overflow: auto; height: 100%; }
.splitter { width: 6px; height: 100%; cursor: col-resize; background: transparent; flex-shrink: 0; position: relative; z-index: 10; }
.splitter:hover .splitter-line, .splitter:active .splitter-line { background: var(--baoku-primary, #ff3650); }
.splitter-line { position: absolute; left: 2px; top: 0; bottom: 0; width: 2px; background: var(--baoku-border, #e8e8e8); transition: background 0.2s; }
</style>
```

- [ ] **步骤 2：Commit**

```bash
git add frontend/src/components/SplitPane.vue
git commit -m "feat(web): add draggable SplitPane component"
```

---

## 任务 10：SourcesPanel 组件

**文件：**
- 创建：`frontend/src/components/SourcesPanel.vue`

- [ ] **步骤 1：创建左栏面板**

```vue
<template>
  <div class="sources-panel">
    <div class="panel-header">
      <span class="panel-title">资料 ({{ sources.length }})</span>
      <el-button type="primary" size="small" @click="showUpload = true">+ 添加</el-button>
    </div>
    <div v-if="selectedIds.size > 0" class="batch-bar">
      <span>已选 {{ selectedIds.size }} 项</span>
      <el-button text type="danger" size="small" @click="handleBatchDelete">删除选中</el-button>
    </div>
    <div class="source-list">
      <div v-for="src in sources" :key="src.id" class="source-item" @mouseenter="hovered = src.id" @mouseleave="hovered = null">
        <el-checkbox :model-value="selectedIds.has(src.remote_id)" @change="toggleSelect(src.remote_id)" />
        <el-icon class="source-icon"><Document /></el-icon>
        <span class="source-name">{{ src.original_filename || src.filename }}</span>
        <el-button v-if="hovered === src.id" text size="small" type="danger" @click.stop="$emit('delete', src)"><el-icon><Delete /></el-icon></el-button>
      </div>
      <div v-if="sources.length === 0" class="empty-text">暂无资料</div>
    </div>
    <div class="notes-section">
      <div class="notes-header" @click="notesExpanded = !notesExpanded">
        <span>笔记 ({{ notes.length }})</span>
        <el-icon><ArrowDown :class="{ expanded: notesExpanded }" /></el-icon>
      </div>
      <div v-if="notesExpanded" class="notes-body">
        <div v-for="note in notes" :key="note.id" class="note-item">
          <el-input v-model="note.content" type="textarea" :rows="2" @blur="$emit('update-note', note)" />
          <el-button text type="danger" size="small" @click="$emit('delete-note', note.id)">删除</el-button>
        </div>
        <el-button text type="primary" size="small" @click="$emit('add-note')">+ 新建笔记</el-button>
      </div>
    </div>
    <UploadDialog :visible="showUpload" :notebook-id="notebookId" @update:visible="showUpload = $event" @uploaded="$emit('refresh')" />
  </div>
</template>

<script setup lang="ts">
import { ref, watch } from "vue"
import { Document, Delete, ArrowDown } from "@element-plus/icons-vue"
import { ElMessageBox, ElMessage } from "element-plus"
import UploadDialog from "./UploadDialog.vue"
import type { Source } from "@/api/sources"
import type { Note } from "@/api/notes"

const props = defineProps<{
  notebookId: string
  sources: Source[]
  notes: Note[]
}>()
const emit = defineEmits<{
  "update:selectedIds": [ids: Set<string>]
  refresh: []
  delete: [source: Source]
  "add-note": []
  "update-note": [note: Note]
  "delete-note": [noteId: number]
}>()

const showUpload = ref(false)
const hovered = ref<number | null>(null)
const notesExpanded = ref(true)
const selectedIds = ref<Set<string>>(new Set())

watch(() => props.sources, (srcs) => {
  selectedIds.value = new Set(srcs.map((s) => s.remote_id))
  emit("update:selectedIds", selectedIds.value)
}, { immediate: true })

function toggleSelect(remoteId: string) {
  const next = new Set(selectedIds.value)
  if (next.has(remoteId)) next.delete(remoteId)
  else next.add(remoteId)
  selectedIds.value = next
  emit("update:selectedIds", next)
}

async function handleBatchDelete() {
  try {
    await ElMessageBox.confirm(`确定删除选中的 ${selectedIds.value.size} 个资料？`, "确认", { type: "warning" })
    emit("refresh")
  } catch {}
}
</script>

<style scoped>
.sources-panel { display: flex; flex-direction: column; height: 100%; }
.panel-header { display: flex; align-items: center; justify-content: space-between; padding: 12px 16px; border-bottom: 1px solid var(--baoku-border, #e8e8e8); }
.panel-title { font-size: 14px; font-weight: 600; }
.batch-bar { display: flex; align-items: center; justify-content: space-between; padding: 6px 16px; background: var(--baoku-bg, #f5f5f5); font-size: 12px; }
.source-list { flex: 1; overflow-y: auto; padding: 8px; }
.source-item { display: flex; align-items: center; gap: 8px; padding: 8px; border-radius: 6px; cursor: pointer; &:hover { background: var(--baoku-bg, #f5f5f5); } }
.source-icon { color: var(--baoku-text-3, #999); flex-shrink: 0; }
.source-name { flex: 1; font-size: 13px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.empty-text { padding: 24px; text-align: center; color: var(--baoku-text-3, #999); font-size: 13px; }
.notes-section { border-top: 1px solid var(--baoku-border, #e8e8e8); }
.notes-header { display: flex; align-items: center; justify-content: space-between; padding: 10px 16px; cursor: pointer; font-size: 13px; font-weight: 500; }
.notes-body { padding: 8px 16px 16px; }
.note-item { margin-bottom: 8px; }
.expanded { transform: rotate(180deg); }
</style>
```

- [ ] **步骤 2：Commit**

```bash
git add frontend/src/components/SourcesPanel.vue
git commit -m "feat(web): add SourcesPanel component for three-column layout"
```

---

## 任务 11：ChatPanel 组件

**文件：**
- 创建：`frontend/src/components/ChatPanel.vue`

- [ ] **步骤 1：创建中栏面板**

```vue
<template>
  <div class="chat-panel">
    <div class="panel-header"><span class="panel-title">AI 问答</span></div>
    <div class="chat-body">
      <div v-if="!currentSessionId && !aiSummary" class="chat-welcome">
        <el-icon :size="48" color="#d0d0d0"><ChatLineSquare /></el-icon>
        <p>正在生成 AI 总结...</p>
      </div>
      <div v-if="aiSummary" class="ai-summary card">
        <div class="summary-label">AI 总结</div>
        <div v-if="summaryLoading" class="summary-skeleton"><el-skeleton :rows="3" animated /></div>
        <p v-else-if="summaryError" class="summary-error">{{ summaryError }}</p>
        <p v-else class="summary-text">{{ aiSummary }}</p>
      </div>
      <div v-if="recommendedQuestions.length > 0 && !currentSessionId" class="suggested-questions">
        <div class="suggested-label">推荐问题</div>
        <div v-for="(q, i) in recommendedQuestions" :key="i" class="suggested-item" @click="handleSuggestedQuestion(q)">
          {{ q }}
        </div>
      </div>
      <template v-if="currentSessionId">
        <div ref="messagesRef" class="messages-area">
          <ChatMessage v-for="msg in messages" :key="msg.id" :message="msg" />
          <div v-if="chatStore.streaming" class="streaming-bubble">{{ chatStore.streamContent }}<span class="cursor-blink">|</span></div>
        </div>
      </template>
    </div>
    <div class="chat-input-area">
      <el-input v-model="inputText" type="textarea" :rows="2" placeholder="输入问题..." @keydown.enter.exact.prevent="handleSend" :disabled="chatStore.streaming" />
      <el-button type="primary" :loading="chatStore.streaming" @click="handleSend" :disabled="!inputText.trim()">发送</el-button>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, watch, nextTick } from "vue"
import { ChatLineSquare } from "@element-plus/icons-vue"
import { ElMessage } from "element-plus"
import { useChatStore } from "@/stores/chat"
import { fetchSessionsApi, createSessionApi, sendMessageApi } from "@/api/chat"
import ChatMessage from "@/components/ChatMessage.vue"
import request from "@/api/request"

const props = defineProps<{
  notebookId: string
  selectedSourceIds: Set<string>
}>()

const chatStore = useChatStore()
const inputText = ref("")
const messagesRef = ref<HTMLElement>()
const aiSummary = ref("")
const summaryLoading = ref(false)
const summaryError = ref("")
const recommendedQuestions = ref<string[]>([])
const currentSessionId = computed(() => chatStore.currentSessionId)
const messages = computed(() => chatStore.messages)

const FALLBACK_QUESTIONS = ["这些资料的主要结论是什么？", "有哪些关键数据或事实？", "请列出主要要点"]

onMounted(async () => {
  await chatStore.fetchSessions(props.notebookId)
  await generateSummary()
  await loadRecommendedQuestions()
})

async function generateSummary() {
  summaryLoading.value = true
  summaryError.value = ""
  try {
    const session = await createSessionApi(props.notebookId, "AI 总结")
    chatStore.currentSessionId = session.id
    const sourceIds = Array.from(props.selectedSourceIds)
    const result = await sendMessageApi(props.notebookId, session.id, "请用中文总结这些资料的核心要点", sourceIds)
    aiSummary.value = result.content
    chatStore.messages = []
    chatStore.currentSessionId = null
  } catch (e: any) {
    summaryError.value = e?.detail || "总结生成失败"
  } finally {
    summaryLoading.value = false
  }
}

async function loadRecommendedQuestions() {
  try {
    const res = await request.get(`/api/notebooks/${props.notebookId}/suggested-prompts`, { params: { surface: "ask" } })
    recommendedQuestions.value = (res.data.suggestions || []).slice(0, 3).map((s: any) => s.prompt)
  } catch {
    recommendedQuestions.value = FALLBACK_QUESTIONS
  }
}

async function handleSend() {
  const text = inputText.value.trim()
  if (!text || chatStore.streaming) return
  inputText.value = ""
  if (!currentSessionId.value) {
    try {
      const session = await createSessionApi(props.notebookId, text.slice(0, 50))
      chatStore.sessions.unshift(session)
      chatStore.currentSessionId = session.id
    } catch { ElMessage.error("创建对话失败"); return }
  }
  const sourceIds = Array.from(props.selectedSourceIds)
  chatStore.sendMessage(props.notebookId, currentSessionId.value!, text, {
    onError: (err: any) => ElMessage.error(err?.detail || "发送失败"),
  }, sourceIds)
  nextTick(() => { if (messagesRef.value) messagesRef.value.scrollTop = messagesRef.value.scrollHeight })
}

function handleSuggestedQuestion(q: string) {
  inputText.value = q
  handleSend()
}

watch(() => messages.value.length, () => {
  nextTick(() => { if (messagesRef.value) messagesRef.value.scrollTop = messagesRef.value.scrollHeight })
})
</script>

<style scoped>
.chat-panel { display: flex; flex-direction: column; height: 100%; }
.panel-header { padding: 12px 16px; border-bottom: 1px solid var(--baoku-border, #e8e8e8); font-size: 14px; font-weight: 600; }
.panel-title { }
.chat-body { flex: 1; overflow-y: auto; padding: 16px; }
.chat-welcome { display: flex; flex-direction: column; align-items: center; justify-content: center; height: 100%; gap: 12px; color: var(--baoku-text-3, #999); }
.ai-summary { padding: 16px; border-radius: 8px; background: var(--baoku-surface, #fff); border: 1px solid var(--baoku-border, #e8e8e8); margin-bottom: 16px; }
.summary-label { font-size: 13px; font-weight: 600; margin-bottom: 8px; color: var(--baoku-primary, #ff3650); }
.summary-text { font-size: 14px; line-height: 1.7; }
.summary-error { font-size: 13px; color: var(--el-color-danger, #f56c6c); }
.suggested-questions { margin-bottom: 16px; }
.suggested-label { font-size: 13px; font-weight: 600; margin-bottom: 8px; }
.suggested-item { padding: 10px 12px; border-radius: 8px; background: var(--baoku-surface, #fff); border: 1px solid var(--baoku-border, #e8e8e8); margin-bottom: 6px; font-size: 13px; cursor: pointer; transition: all 0.2s; &:hover { border-color: var(--baoku-primary, #ff3650); color: var(--baoku-primary, #ff3650); } }
.messages-area { display: flex; flex-direction: column; gap: 12px; }
.streaming-bubble { padding: 12px 16px; background: var(--baoku-surface, #fff); border-radius: 8px; font-size: 14px; line-height: 1.7; }
.cursor-blink { animation: blink 1s step-end infinite; color: var(--baoku-primary, #ff3650); }
@keyframes blink { 50% { opacity: 0; } }
.chat-input-area { display: flex; gap: 8px; padding: 12px 16px; border-top: 1px solid var(--baoku-border, #e8e8e8); }
</style>
```

- [ ] **步骤 2：Commit**

```bash
git add frontend/src/components/ChatPanel.vue
git commit -m "feat(web): add ChatPanel component with AI summary and suggested questions"
```

---

## 任务 12：GeneratePanel 组件

**文件：**
- 创建：`frontend/src/components/GeneratePanel.vue`

- [ ] **步骤 1：创建右栏面板**

```vue
<template>
  <div class="generate-panel">
    <div class="panel-header"><span class="panel-title">内容生成</span></div>
    <div class="generate-grid">
      <div v-for="ct in contentTypes" :key="ct.type" class="generate-card" @click="openGenerator(ct.type)">
        <div class="ctype-icon" :style="{ background: ct.color + '18' }"><el-icon :size="20" :color="ct.color"><component :is="ct.icon" /></el-icon></div>
        <span class="ctype-name">{{ ct.label }}</span>
      </div>
    </div>
    <div class="history-section">
      <div class="history-label">生成记录</div>
      <div class="history-list">
        <div v-for="item in generatedList" :key="item.id" class="history-item" @click="goToDetail(item.id)">
          <el-tag size="small" :type="statusType(item.status)">{{ statusLabel(item.status) }}</el-tag>
          <span class="history-title">{{ item.title || item.content_type }}</span>
          <span class="history-time">{{ formatTime(item.created_at) }}</span>
        </div>
        <div v-if="generatedList.length === 0" class="empty-text">暂无生成记录</div>
      </div>
    </div>
    <el-dialog v-model="dialogVisible" :title="currentLabel" width="700px" @close="onDialogClose">
      <component v-if="currentType" :is="generatorComponent" :notebook-id="notebookId" @back="dialogVisible = false" />
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, shallowRef } from "vue"
import { useRouter } from "vue-router"
import { Document, DataAnalysis, PictureFilled, Microphone, VideoCamera, Edit } from "@element-plus/icons-vue"
import { fetchGeneratedContentsApi } from "@/api/generation"
import PptGenerator from "@/views/notebook/generate/PptGenerator.vue"
import MindmapGenerator from "@/views/notebook/generate/MindmapGenerator.vue"
import InfographicGenerator from "@/views/notebook/generate/InfographicGenerator.vue"
import PodcastGenerator from "@/views/notebook/generate/PodcastGenerator.vue"
import VideoGenerator from "@/views/notebook/generate/VideoGenerator.vue"
import DocumentGenerator from "@/views/notebook/generate/DocumentGenerator.vue"

const props = defineProps<{ notebookId: string }>()
const router = useRouter()
const dialogVisible = ref(false)
const currentType = ref("")
const generatedList = ref<any[]>([])

const contentTypes = [
  { type: "ppt", label: "PPT", icon: Document, color: "#ff3650" },
  { type: "mindmap", label: "脑图", icon: DataAnalysis, color: "#1a75ff" },
  { type: "podcast", label: "播客", icon: Microphone, color: "#e6a23c" },
  { type: "infographic", label: "信息图", icon: PictureFilled, color: "#67c23a" },
  { type: "video", label: "视频", icon: VideoCamera, color: "#909399" },
  { type: "document", label: "文档", icon: Edit, color: "#409eff" },
]

const currentLabel = computed(() => contentTypes.find((c) => c.type === currentType.value)?.label || "")
const generatorComponent = computed(() => {
  const map: Record<string, any> = { ppt: PptGenerator, mindmap: MindmapGenerator, infographic: InfographicGenerator, podcast: PodcastGenerator, video: VideoGenerator, document: DocumentGenerator }
  return currentType.value ? shallowRef(map[currentType.value]) : null
})

function openGenerator(type: string) { currentType.value = type; dialogVisible.value = true }
function onDialogClose() { currentType.value = ""; refreshList() }
function goToDetail(id: number) { router.push(`/notebook/${props.notebookId}/generate/${id}`) }

function statusType(s: string) { return s === "completed" ? "success" : s === "processing" ? "warning" : s === "failed" ? "danger" : "info" }
function statusLabel(s: string) { return s === "completed" ? "已完成" : s === "processing" ? "生成中" : s === "failed" ? "失败" : "排队中" }
function formatTime(t: string) { const d = new Date(t); const diff = Date.now() - d.getTime(); if (diff < 3600000) return `${Math.floor(diff/60000)}分钟前`; if (diff < 86400000) return `${Math.floor(diff/3600000)}小时前`; return d.toLocaleDateString("zh-CN") }

async function refreshList() { try { generatedList.value = await fetchGeneratedContentsApi(props.notebookId) } catch {} }
onMounted(() => { refreshList() })
defineExpose({ refreshList })
</script>

<style scoped>
.generate-panel { display: flex; flex-direction: column; height: 100%; }
.panel-header { padding: 12px 16px; border-bottom: 1px solid var(--baoku-border, #e8e8e8); font-size: 14px; font-weight: 600; }
.generate-grid { display: grid; grid-template-columns: repeat(2, 1fr); gap: 8px; padding: 12px; }
.generate-card { display: flex; flex-direction: column; align-items: center; gap: 6px; padding: 12px 8px; border-radius: 8px; border: 1px solid var(--baoku-border, #e8e8e8); cursor: pointer; transition: all 0.2s; &:hover { border-color: var(--baoku-primary, #ff3650); transform: translateY(-1px); } }
.ctype-icon { width: 36px; height: 36px; display: flex; align-items: center; justify-content: center; border-radius: 8px; }
.ctype-name { font-size: 12px; }
.history-section { border-top: 1px solid var(--baoku-border, #e8e8e8); padding: 12px 16px; flex: 1; overflow-y: auto; }
.history-label { font-size: 13px; font-weight: 600; margin-bottom: 8px; }
.history-item { display: flex; align-items: center; gap: 8px; padding: 8px 0; border-bottom: 1px solid var(--baoku-border, #e8e8e8); cursor: pointer; &:last-child { border-bottom: none; } &:hover { background: var(--baoku-bg, #f5f5f5); } }
.history-title { flex: 1; font-size: 13px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.history-time { font-size: 11px; color: var(--baoku-text-3, #999); flex-shrink: 0; }
.empty-text { padding: 24px; text-align: center; color: var(--baoku-text-3, #999); font-size: 13px; }
</style>
```

- [ ] **步骤 2：Commit**

```bash
git add frontend/src/components/GeneratePanel.vue
git commit -m "feat(web): add GeneratePanel component for three-column layout"
```

---

## 任务 13：重写 NotebookView 三栏布局

**文件：**
- 修改：`frontend/src/views/NotebookView.vue`

- [ ] **步骤 1：重写 NotebookView.vue**

保留现有的标题栏和重命名/删除对话框，将 tab-bar 和 router-view 替换为三栏布局：

```vue
<template>
  <div class="notebook-page">
    <header class="notebook-header">
      <div class="header-left">
        <el-button text class="back-btn" @click="router.push('/')"><el-icon><ArrowLeft /></el-icon><span>返回</span></el-button>
        <h2 class="notebook-title">{{ notebook?.title || "加载中..." }}</h2>
      </div>
      <div class="header-right">
        <el-radio-group v-model="viewMode" size="small" @change="onViewModeChange">
          <el-radio-button value="three-column">三栏</el-radio-button>
          <el-radio-button value="tabs">标签</el-radio-button>
        </el-radio-group>
        <el-button text :loading="syncing" @click="handleSync"><el-icon><Refresh /></el-icon></el-button>
        <el-dropdown trigger="click">
          <el-button text><el-icon><MoreFilled /></el-icon></el-button>
          <template #dropdown>
            <el-dropdown-menu>
              <el-dropdown-item @click="handleRename"><el-icon><Edit /></el-icon> 重命名</el-dropdown-item>
              <el-dropdown-item @click="handleShare"><el-icon><Share /></el-icon> 分享</el-dropdown-item>
              <el-dropdown-item divided @click="handleDelete"><el-icon><Delete /></el-icon> 删除知识库</el-dropdown-item>
            </el-dropdown-menu>
          </template>
        </el-dropdown>
      </div>
    </header>

    <div v-if="viewMode === 'three-column'" class="three-column-container">
      <SplitPane :initial-left="300" :min-left="220" :min-right="400" storage-key="nb-split-left">
        <template #left>
          <SourcesPanel
            :notebook-id="notebookId"
            :sources="sources"
            :notes="notes"
            @update:selected-ids="onSelectedIdsChange"
            @refresh="loadSources"
            @delete="handleDeleteSource"
            @add-note="handleAddNote"
            @update-note="handleUpdateNote"
            @delete-note="handleDeleteNote"
          />
        </template>
        <template #right>
          <SplitPane :initial-left="500" :min-left="300" :min-right="240" storage-key="nb-split-right" :direction="'row'">
            <template #left>
              <ChatPanel :notebook-id="notebookId" :selected-source-ids="selectedSourceIds" />
            </template>
            <template #right>
              <GeneratePanel ref="generatePanelRef" :notebook-id="notebookId" />
            </template>
          </SplitPane>
        </template>
      </SplitPane>
    </div>

    <div v-else class="tab-bar">
      <el-tabs v-model="activeTab" @tab-change="handleTabChange">
        <el-tab-pane label="概览" name="overview" />
        <el-tab-pane label="资料" name="sources" />
        <el-tab-pane label="问答" name="chat" />
        <el-tab-pane label="生成" name="generate" />
      </el-tabs>
      <div class="tab-content"><router-view /></div>
    </div>

    <el-dialog v-model="showRenameDialog" title="重命名知识库" width="400px">
      <el-form ref="renameFormRef" :model="renameForm" :rules="renameRules" label-position="top">
        <el-form-item label="名称" prop="title"><el-input v-model="renameForm.title" maxlength="100" /></el-form-item>
        <el-form-item label="描述" prop="description"><el-input v-model="renameForm.description" type="textarea" :rows="3" maxlength="500" /></el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="showRenameDialog = false">取消</el-button>
        <el-button type="primary" :loading="renaming" @click="handleRenameConfirm">确定</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, watch } from "vue"
import { useRouter, useRoute } from "vue-router"
import { ArrowLeft, Refresh, MoreFilled, Edit, Share, Delete } from "@element-plus/icons-vue"
import { ElMessage, ElMessageBox } from "element-plus"
import type { FormInstance, FormRules } from "element-plus"
import { useNotebooksStore } from "@/stores/notebooks"
import { fetchSourcesApi, deleteSourceApi } from "@/api/sources"
import { fetchNotesApi, createNoteApi, updateNoteApi, deleteNoteApi } from "@/api/notes"
import type { Source } from "@/api/sources"
import type { Note } from "@/api/notes"
import SplitPane from "@/components/SplitPane.vue"
import SourcesPanel from "@/components/SourcesPanel.vue"
import ChatPanel from "@/components/ChatPanel.vue"
import GeneratePanel from "@/components/GeneratePanel.vue"

const router = useRouter()
const route = useRoute()
const notebooksStore = useNotebooksStore()
const notebookId = computed(() => route.params.id as string)
const notebook = computed(() => notebooksStore.currentNotebook)

const viewMode = ref<"three-column" | "tabs">("three-column")
const activeTab = ref("overview")
const syncing = ref(false)
const showRenameDialog = ref(false)
const renaming = ref(false)
const renameFormRef = ref<FormInstance>()
const renameForm = ref({ title: "", description: "" })
const renameRules: FormRules = { title: [{ required: true, message: "请输入名称", trigger: "blur" }] }

const sources = ref<Source[]>([])
const notes = ref<Note[]>([])
const selectedSourceIds = ref<Set<string>>(new Set())
const generatePanelRef = ref<InstanceType<typeof GeneratePanel>>()

function onViewModeChange(val: string) {
  if (val === "tabs") {
    router.push(`/notebook/${notebookId.value}/overview`)
  }
}

function handleTabChange(name: string) { router.push(`/notebook/${notebookId.value}/${name}`) }

async function loadSources() {
  try {
    const res = await fetchSourcesApi(notebookId.value)
    sources.value = res.items
  } catch {}
}

async function loadNotes() {
  try {
    const res = await fetchNotesApi(notebookId.value)
    notes.value = res.items
  } catch {}
}

function onSelectedIdsChange(ids: Set<string>) { selectedSourceIds.value = ids }

async function handleDeleteSource(src: Source) {
  try {
    await ElMessageBox.confirm(`确定删除「${src.original_filename || src.filename}」？`, "确认", { type: "warning" })
    await deleteSourceApi(notebookId.value, src.id)
    ElMessage.success("已删除")
    loadSources()
  } catch {}
}

async function handleAddNote() {
  try {
    const note = await createNoteApi(notebookId.value, { content: "" })
    notes.value.unshift(note)
  } catch { ElMessage.error("创建笔记失败") }
}

async function handleUpdateNote(note: Note) {
  try { await updateNoteApi(notebookId.value, note.id, { content: note.content }) } catch {}
}

async function handleDeleteNote(noteId: number) {
  try { await deleteNoteApi(notebookId.value, noteId); notes.value = notes.value.filter((n) => n.id !== noteId) } catch {}
}

async function handleSync() {
  syncing.value = true
  try { await notebooksStore.syncNotebook(notebookId.value); ElMessage.success("同步完成") }
  catch (e: any) { ElMessage.error(e.response?.data?.detail || "同步失败") }
  finally { syncing.value = false }
}

function handleRename() {
  if (notebook.value) { renameForm.value.title = notebook.value.title; renameForm.value.description = notebook.value.description || "" }
  showRenameDialog.value = true
}

async function handleRenameConfirm() {
  const valid = await renameFormRef.value?.validate().catch(() => false)
  if (!valid) return
  renaming.value = true
  try { await notebooksStore.updateNotebook(notebookId.value, { title: renameForm.value.title, description: renameForm.value.description || undefined }); ElMessage.success("已更新"); showRenameDialog.value = false }
  catch (e: any) { ElMessage.error(e.response?.data?.detail || "更新失败") }
  finally { renaming.value = false }
}

function handleShare() { ElMessage.info("分享功能开发中") }

async function handleDelete() {
  try {
    await ElMessageBox.confirm("确定要删除这个知识库吗？此操作不可恢复。", "确认删除", { type: "warning", confirmButtonText: "删除", cancelButtonText: "取消" })
    await notebooksStore.deleteNotebook(notebookId.value); ElMessage.success("已删除"); router.push("/")
  } catch {}
}

onMounted(() => {
  notebooksStore.fetchNotebook(notebookId.value)
  loadSources()
  loadNotes()
  const childPath = route.path.split("/").pop()
  if (["overview", "sources", "chat", "generate"].includes(childPath || "")) {
    activeTab.value = childPath!
    viewMode.value = "tabs"
  }
})

watch(() => route.params.id, () => {
  if (route.params.id) {
    notebooksStore.fetchNotebook(notebookId.value)
    loadSources()
    loadNotes()
  }
})
</script>

<style scoped>
.notebook-page { height: calc(100vh - var(--baoku-header-height, 48px)); display: flex; flex-direction: column; background: var(--baoku-bg, #f5f5f5); }
.notebook-header { display: flex; align-items: center; justify-content: space-between; padding: 0 24px; height: 56px; background: var(--baoku-surface, #fff); border-bottom: 1px solid var(--baoku-border, #e8e8e8); flex-shrink: 0; }
.header-left { display: flex; align-items: center; gap: 12px; }
.back-btn { font-size: 14px; color: var(--baoku-text-2, #666); }
.notebook-title { font-size: 16px; font-weight: 600; }
.header-right { display: flex; align-items: center; gap: 8px; }
.three-column-container { flex: 1; overflow: hidden; }
.tab-bar { flex: 1; overflow-y: auto; }
.tab-content { padding: 24px; }
</style>
```

- [ ] **步骤 2：验证类型检查**

运行：`cd frontend && npx vue-tsc --noEmit`
预期：无错误

- [ ] **步骤 3：Commit**

```bash
git add frontend/src/views/NotebookView.vue
git commit -m "feat(web): rewrite NotebookView as three-column layout with draggable panes"
```

---

## 任务 14：更新路由

**文件：**
- 修改：`frontend/src/router/index.ts`

- [ ] **步骤 1：更新 notebook 路由**

将 notebook 路由的 `redirect` 改为不强制重定向到 overview（三栏模式不需要子路由），但保留子路由给标签模式：

```typescript
  {
    path: "/notebook/:id",
    name: "Notebook",
    component: () => import("@/views/NotebookView.vue"),
    meta: { requiresAuth: true },
    children: [
      {
        path: "overview",
        name: "NotebookOverview",
        component: () => import("@/views/notebook/OverviewTab.vue"),
      },
      {
        path: "sources",
        name: "NotebookSources",
        component: () => import("@/views/notebook/SourcesTab.vue"),
      },
      {
        path: "chat",
        name: "NotebookChat",
        component: () => import("@/views/notebook/ChatTab.vue"),
      },
      {
        path: "chat/:sid",
        name: "NotebookChatSession",
        component: () => import("@/views/notebook/ChatTab.vue"),
      },
      {
        path: "generate",
        name: "NotebookGenerate",
        component: () => import("@/views/notebook/GenerateTab.vue"),
      },
      {
        path: "generate/:gid",
        name: "NotebookGenerateDetail",
        component: () => import("@/views/notebook/GenerateTab.vue"),
      },
    ],
  },
```

移除 `redirect` 行，让 `/notebook/:id` 直接渲染 NotebookView（三栏模式），子路由仅用于标签模式切换。

- [ ] **步骤 2：验证类型检查和构建**

运行：`cd frontend && npx vue-tsc --noEmit && npm run build`
预期：构建成功

- [ ] **步骤 3：Commit**

```bash
git add frontend/src/router/index.ts
git commit -m "refactor(web): update router for three-column default view"
```

---

## 任务 15：最终验证

- [ ] **步骤 1：后端 lint + typecheck**

运行：
```bash
uv run ruff check src/notebooklm/server/
uv run mypy src/notebooklm/server/
```
预期：无错误

- [ ] **步骤 2：前端 lint + typecheck + build**

运行：
```bash
cd frontend && npx vue-tsc --noEmit && npm run build
```
预期：构建成功

- [ ] **步骤 3：手动验证**

1. 启动后端和前端开发服务器
2. 登录后进入任一知识库
3. 验证三栏布局渲染正常
4. 验证左栏资料列表、复选框、添加资料
5. 验证中栏 AI 总结自动生成、推荐问题、发送消息
6. 验证右栏生成入口卡片、生成记录
7. 验证拖拽分隔条调整栏宽
8. 验证切换到标签视图
9. 验证笔记新增/编辑/删除

- [ ] **步骤 4：最终 commit**

```bash
git add -A
git commit -m "feat: three-column notebook detail view with notes, chat sessions, and draggable panes"
```

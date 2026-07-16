# 有道宝库仿制 — 设计规格

## 概述

仿制 [有道宝库](https://baoku.youdao.com/home) 的 Web 前端，后端基于 notebooklm-py 现有的 REST API Server，AI 能力对接 Google NotebookLM。

所有请求信息及 Google NotebookLM 返回的内容均需在本地持久化存储，实现数据的本地缓存、离线可查、请求审计和快速加载。

## 架构

```
前端 (Vue 3 + Vite + Element Plus)
  │ axios
  ▼
notebooklm-py REST Server (FastAPI)
  ├── 用户系统模块 (SQLAlchemy + JWT)
  ├── 业务 API (复用已有 NotebookLM Client)
  ├── 本地数据持久层 (请求/响应全量存储)
  ├── 外部知识库连接器 (外部 KB 插件体系)
  ├── 内容生成引擎 (6 种类型)
  └── Middleware (auth 鉴权 + 请求日志)
        │
        ├── Google NotebookLM API (播客/文档生成)
        ├── 本地生成引擎 (PPT/脑图/信息图/视频)
        ├── 外部知识库 A (API 连接)
        ├── 外部知识库 B (API 连接)
        │   └── ...
        │
        ▼
  本地数据库 (SQLite/PostgreSQL)
  └── 文件存储 (上传文档 / 生成内容)
```

**数据流策略：**
1. 读请求：优先从本地数据库返回，若不存在或过期则回源到 Google NotebookLM
2. 写请求：先写入本地数据库，再同步到 Google，失败时回滚
3. 所有 API 请求/响应对（包含超时、错误）全部记录到本地
4. 外部知识库内容：通过连接器拉取 → 本地缓存元数据和可导入的内容 → 用户选择后导入到当前知识库

## 技术选型

| 层 | 技术 | 说明 |
|---|---|---|
| 前端框架 | Vue 3 + Vite + TypeScript | 与有道宝库一致 |
| UI 组件库 | Element Plus | 与有道宝库一致 |
| 路由 | Vue Router (Hash) | 便于静态部署 |
| 状态管理 | Pinia | |
| HTTP 客户端 | Axios | |
| 后端框架 | FastAPI (已有) | notebooklm-py REST Server |
| 本地数据库 | SQLite (开发) / PostgreSQL (生产) | |
| ORM | SQLAlchemy | |
| 文件存储 | 本地文件系统 (MEDIA_ROOT) | 文档原文、生成内容 |
| 认证 | JWT (系统登录) + Google OAuth (NotebookLM 绑定) | |
| 外部 KB 接入 | 插件化连接器体系 | 抽象基类 + 各 provider 实现 |
| PPT 生成 | python-pptx | 本地生成引擎 |
| 脑图生成 | graphviz / pydot | 本地生成引擎 |
| 信息图生成 | Pillow / weasyprint | 本地渲染引擎 |
| 视频生成 | FFmpeg + gTTS / pyttsx3 | 本地合成引擎 |

## 本地数据存储设计

### 设计原则

- 所有从 Google NotebookLM 获取的数据，在返回给前端之前先写入本地数据库
- 所有前端发送的请求（聊天、生成等）也写入本地数据库
- 上传的原始文档在本地文件系统保留副本
- 生成的内容（PPT、脑图、信息图、播客、视频、文档等）本地保留副本和元数据
- 本地数据作为缓存层，即使 Google API 暂时不可用，用户仍可查看历史数据

### 数据模型

```sql
-- ==================== 用户系统 ====================

CREATE TABLE users (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  username TEXT UNIQUE NOT NULL,
  password_hash TEXT NOT NULL,
  display_name TEXT,
  avatar_url TEXT,
  google_token TEXT,             -- NotebookLM 授权 token
  google_token_expires_at TIMESTAMP,
  is_active BOOLEAN DEFAULT TRUE,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE user_sessions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER REFERENCES users(id),
  token TEXT UNIQUE NOT NULL,
  expires_at TIMESTAMP NOT NULL,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- ==================== 知识库 ====================

-- 本地缓存的 NotebookLM notebook
CREATE TABLE notebooks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER REFERENCES users(id),
  remote_id TEXT UNIQUE NOT NULL,       -- Google NotebookLM 端 ID
  title TEXT NOT NULL,
  description TEXT,
  source_count INTEGER DEFAULT 0,
  chat_count INTEGER DEFAULT 0,
  last_synced_at TIMESTAMP,            -- 上次与 Google 同步时间
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- ==================== 资料/文档 ====================

-- 本地缓存的文档源
CREATE TABLE sources (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER REFERENCES users(id),
  notebook_id INTEGER REFERENCES notebooks(id),
  remote_id TEXT UNIQUE NOT NULL,       -- Google 端 ID
  filename TEXT NOT NULL,
  original_filename TEXT,               -- 用户上传时的原始文件名
  file_type TEXT,                       -- pdf / docx / txt / url
  file_size INTEGER,                    -- 字节
  page_count INTEGER,
  local_path TEXT,                      -- 本地文件存储路径 (MEDIA_ROOT/sources/)
  source_url TEXT,                      -- 如果是网页链接
  summary TEXT,                         -- AI 生成的摘要
  status TEXT DEFAULT 'active',         -- active / deleted / processing
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- ==================== 问答会话 ====================

-- 对话会话
CREATE TABLE chat_sessions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER REFERENCES users(id),
  notebook_id INTEGER REFERENCES notebooks(id),
  title TEXT,                           -- 自动生成或用户自定义
  message_count INTEGER DEFAULT 0,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 单条消息（请求 + 响应全量存储）
CREATE TABLE chat_messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id INTEGER REFERENCES chat_sessions(id),
  user_id INTEGER REFERENCES users(id),
  role TEXT NOT NULL,                   -- 'user' / 'assistant'
  content TEXT NOT NULL,                -- 消息正文
  citations TEXT,                       -- JSON: [{source_id, source_name, text, page, rect}]
  request_body TEXT,                    -- 发送给 Google 的完整请求体 (JSON)
  response_body TEXT,                   -- Google 返回的完整响应体 (JSON)
  latency_ms INTEGER,                   -- 请求耗时
  token_count INTEGER,                  -- 消耗的 token 数（如有）
  status TEXT DEFAULT 'success',        -- success / error / timeout
  error_message TEXT,                   -- 错误信息（如果有）
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- ==================== 生成内容 ====================

-- 生成的内容（6 种类型）
CREATE TABLE generated_contents (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER REFERENCES users(id),
  notebook_id INTEGER REFERENCES notebooks(id),
  content_type TEXT NOT NULL,           -- 'ppt' / 'mindmap' / 'infographic' / 'podcast' / 'video' / 'document'
  title TEXT,
  prompt TEXT,                          -- 用户输入的生成指令
  engine TEXT DEFAULT 'notebooklm',     -- 生成引擎: 'notebooklm' / 'local' / 'external_api'

  -- 通用字段
  content TEXT,                         -- 文本内容（文档/笔记的正文，信息图的 JSON 描述）
  local_file_path TEXT,                 -- 本地文件路径（PPTX / 图片 / MP3 / MP4 / PDF 等）
  file_size INTEGER,                    -- 文件大小（字节）
  thumbnail_path TEXT,                  -- 缩略图路径（预览用）
  status TEXT DEFAULT 'processing',     -- 'queued' / 'processing' / 'completed' / 'failed'
  error_message TEXT,

  -- PPT 专用
  ppt_page_count INTEGER,               -- PPT 总页数
  ppt_template TEXT,                    -- 模板名称
  ppt_json TEXT,                        -- PPT 的结构化 JSON（每页标题/要点/布局）
  ppt_preview_images TEXT,              -- JSON: 每页预览图路径列表

  -- 脑图专用
  mindmap_data TEXT,                    -- JSON: 思维导图节点数据结构
  mindmap_layout TEXT DEFAULT 'tree',   -- 布局: 'tree' / 'radial' / 'org'

  -- 信息图专用
  infographic_template TEXT,            -- 信息图模板
  infographic_blocks TEXT,              -- JSON: 信息图文块布局描述

  -- 播客专用
  audio_file_path TEXT,                 -- 音频文件路径
  duration_seconds INTEGER,             -- 音频时长（秒）
  audio_speakers TEXT,                  -- JSON: 播客角色配置 [{name, voice, lines}]
  audio_transcript TEXT,                -- 播客完整文字稿

  -- 视频专用
  video_file_path TEXT,                 -- 视频文件路径
  video_duration_seconds INTEGER,       -- 视频时长
  video_resolution TEXT,                -- '720p' / '1080p'
  video_scenes TEXT,                    -- JSON: 视频场景列表 [{scene_id, description, duration}]
  video_narration TEXT,                 -- 视频旁白文本
  video_bg_music TEXT,                  -- 背景音乐标识

  -- 文档专用
  doc_page_count INTEGER,              -- 文档页数
  doc_sections TEXT,                   -- JSON: 文档章节结构
  doc_format TEXT DEFAULT 'markdown',  -- 'markdown' / 'pdf' / 'docx'

  -- 审计
  request_body TEXT,                    -- 发送给引擎的请求体
  response_body TEXT,                   -- 引擎返回的响应体
  latency_ms INTEGER,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- ==================== 请求审计日志 ====================

-- 所有 API 调用的全量审计日志
CREATE TABLE request_logs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER REFERENCES users(id),
  endpoint TEXT NOT NULL,               -- /api/sources/upload /api/chat/send ...
  method TEXT NOT NULL,                 -- GET / POST / PUT / DELETE
  request_headers TEXT,                 -- JSON
  request_body TEXT,                    -- JSON
  response_status INTEGER,              -- HTTP 状态码
  response_headers TEXT,                -- JSON
  response_body TEXT,                   -- JSON（截断保护，最长 100KB）
  latency_ms INTEGER,
  client_ip TEXT,
  user_agent TEXT,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_notebooks_user ON notebooks(user_id);
CREATE INDEX idx_notebooks_remote ON notebooks(remote_id);
CREATE INDEX idx_sources_notebook ON sources(notebook_id);
CREATE INDEX idx_sources_user ON sources(user_id);
CREATE INDEX idx_chat_sessions_notebook ON chat_sessions(notebook_id);
CREATE INDEX idx_chat_messages_session ON chat_messages(session_id);
CREATE INDEX idx_chat_messages_user ON chat_messages(user_id);
CREATE INDEX idx_generated_notebook ON generated_contents(notebook_id);
CREATE INDEX idx_generated_user ON generated_contents(user_id);
CREATE INDEX idx_request_logs_user ON request_logs(user_id);
CREATE INDEX idx_request_logs_endpoint ON request_logs(endpoint);
CREATE INDEX idx_request_logs_created ON request_logs(created_at);
```

### 本地文件存储目录结构

```
MEDIA_ROOT/
├── users/
│   └── {user_id}/
│       └── avatar.jpg
├── sources/
│   └── {user_id}/
│       └── {notebook_id}/
│           ├── {source_id}_original.pdf
│           └── {source_id}_processed.pdf
├── generated/
│   └── {user_id}/
│       └── {notebook_id}/
│           ├── ppt_{id}.pptx                    # PPT 文件
│           ├── ppt_{id}_preview/                # PPT 每页预览图
│           │   ├── slide_01.png
│           │   └── slide_02.png
│           ├── mindmap_{id}.json                # 脑图数据
│           ├── mindmap_{id}.png                 # 脑图导出图片
│           ├── infographic_{id}.json            # 信息图数据
│           ├── infographic_{id}.png             # 信息图导出图片
│           ├── podcast_{id}.mp3                 # 播客音频
│           ├── podcast_{id}.json                # 播客脚本+时间轴
│           ├── video_{id}.mp4                   # 视频文件
│           ├── video_{id}.json                  # 视频场景描述
│           └── doc_{id}.md                      # 文档/报告
└── exports/
    └── {user_id}/
        └── {notebook_id}_export_{ts}.zip
```

MEDIA_ROOT 路径通过配置 `settings.MEDIA_ROOT` 指定，默认 `~/.notebooklm/data/`。

## 外部知识库接入

### 设计目标

- 支持通过 API 连接多种外部知识库系统（第三方向量数据库、Dify、LangChain、自定义 API 等）
- 可在系统内浏览外部知识库的目录结构和文档列表
- 可选择外部知识库中的内容，导入到当前知识库作为资料
- 连接器采用插件化架构，新增 provider 只需实现抽象基类

### 连接器架构

```
external_kb/
├── base.py                   # 抽象基类 ExternalKBConnector
├── registry.py               # 连接器注册中心
├── providers/
│   ├── __init__.py
│   ├── openapi.py            # 通用 OpenAPI/REST 连接器
│   ├── dify.py               # Dify 知识库连接器
│   ├── qanything.py          # QAnything 连接器（有道宝库同款引擎）
│   ├── vectordb.py           # 通用向量数据库连接器
│   └── custom.py             # 自定义 API 连接器（用户配 URL + 鉴权）
└── sync.py                   # 同步/导入引擎
```

### 连接器抽象接口

```python
class ExternalKBConnector(ABC):
    """外部知识库连接器抽象基类"""

    @abstractmethod
    async def test_connection(self) -> bool:
        """测试连接是否可用"""

    @abstractmethod
    async def list_collections(self) -> list[KBCollection]:
        """列出外部知识库中的所有集合/知识库列表"""

    @abstractmethod
    async def list_documents(self, collection_id: str, page: int, page_size: int) -> PageResult[KBDocument]:
        """列出指定集合中的文档列表"""

    @abstractmethod
    async def get_document_detail(self, document_id: str) -> KBDocument:
        """获取文档详情"""

    @abstractmethod
    async def search_documents(self, collection_id: str, query: str, top_k: int) -> list[KBSearchResult]:
        """搜索外部知识库中的文档"""

    @abstractmethod
    async def import_document(self, document_id: str, target_notebook_id: str) -> ImportResult:
        """将外部文档导入到本地知识库"""
```

### 数据模型

```sql
-- ==================== 外部知识库连接配置 ====================

CREATE TABLE external_kb_connections (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER REFERENCES users(id),
  name TEXT NOT NULL,                    -- 用户自定义名称，如"公司内部知识库"
  provider_type TEXT NOT NULL,           -- 'openapi' / 'dify' / 'qanything' / 'vectordb' / 'custom'
  api_base_url TEXT NOT NULL,            -- 外部服务地址
  auth_type TEXT DEFAULT 'api_key',      -- 'api_key' / 'bearer' / 'basic' / 'oauth2'
  auth_credentials TEXT,                 -- JSON: 加密存储的认证凭据
  extra_config TEXT,                     -- JSON: 其他配置参数
  is_active BOOLEAN DEFAULT TRUE,
  last_sync_at TIMESTAMP,               -- 上次同步时间
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 缓存的外部知识库集合/索引列表
CREATE TABLE external_kb_collections (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  connection_id INTEGER REFERENCES external_kb_connections(id),
  remote_id TEXT NOT NULL,               -- 外部系统的集合 ID
  name TEXT NOT NULL,
  description TEXT,
  document_count INTEGER DEFAULT 0,
  last_fetched_at TIMESTAMP,            -- 上次拉取时间
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(connection_id, remote_id)
);

-- 缓存的外部知识库文档列表
CREATE TABLE external_kb_documents (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  collection_id INTEGER REFERENCES external_kb_collections(id),
  connection_id INTEGER REFERENCES external_kb_connections(id),
  remote_id TEXT NOT NULL,               -- 外部系统的文档 ID
  title TEXT NOT NULL,
  summary TEXT,                          -- 文档摘要
  file_type TEXT,                        -- pdf / docx / txt / markdown
  file_size INTEGER,
  url TEXT,                              -- 外部访问链接
  metadata TEXT,                         -- JSON: 外部系统返回的元数据
  last_fetched_at TIMESTAMP,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(collection_id, remote_id)
);

-- 外部文档导入记录
CREATE TABLE external_imports (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER REFERENCES users(id),
  connection_id INTEGER REFERENCES external_kb_connections(id),
  source_document_id INTEGER REFERENCES external_kb_documents(id),
  target_notebook_id INTEGER REFERENCES notebooks(id),
  target_source_id INTEGER REFERENCES sources(id),   -- 导入后生成的本地 source
  status TEXT DEFAULT 'pending',         -- pending / importing / completed / failed
  error_message TEXT,
  imported_at TIMESTAMP,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_ext_kb_conn_user ON external_kb_connections(user_id);
CREATE INDEX idx_ext_kb_coll_conn ON external_kb_collections(connection_id);
CREATE INDEX idx_ext_kb_docs_coll ON external_kb_documents(collection_id);
CREATE INDEX idx_ext_kb_docs_conn ON external_kb_documents(connection_id);
CREATE INDEX idx_ext_imports_user ON external_imports(user_id);
CREATE INDEX idx_ext_imports_notebook ON external_imports(target_notebook_id);
```

### API 端点

```
# 连接管理
POST   /api/external-kb/connections                    # 创建连接
GET    /api/external-kb/connections                     # 列出所有连接
PUT    /api/external-kb/connections/:id                 # 更新连接
DELETE /api/external-kb/connections/:id                 # 删除连接
POST   /api/external-kb/connections/:id/test            # 测试连接

# 浏览外部知识库
GET    /api/external-kb/connections/:id/collections     # 列出集合
GET    /api/external-kb/connections/:id/collections/:cid/documents  # 列出文档
GET    /api/external-kb/connections/:id/collections/:cid/search?q=  # 搜索文档

# 导入
POST   /api/external-kb/import                         # 导入文档到指定知识库
GET    /api/external-kb/imports                         # 导入记录
```

### UI 页面

在知识库详情的 **资料 Tab** 中，新增"外部知识库"子面板：

```
┌─────────────────────────────────────────┐
│ 📖 资料                        ✚ 上传    │
│ ┌─────────────┬────────────────────────┐ │
│ │  本地资料    │  外部知识库  (新)        │ │
│ └─────────────┴────────────────────────┘ │
│ ┌────────────────────────────────────────┐│
│ │ 已接入的外部知识库:                      ││
│ │ ┌────────────────────────────────────┐ ││
│ │ │ 🔗 公司内部知识库 ← 选中           │ ││
│ │ │    Dify · 最后同步: 10分钟前       │ ││
│ │ │    ────────────────────────        │ ││
│ │ │    📁 技术文档 (24篇)              │ ││
│ │ │    📁 产品手册 (12篇)  ← 展开     │ ││
│ │ │       ├ 📄 API 设计规范 v2.3      │ ││
│ │ │       ├ 📄 数据库设计文档          │ ││
│ │ │       └ 📄 系统架构概览            │ ││
│ │ │    📁 项目管理 (8篇)               │ ││
│ │ └────────────────────────────────────┘ │
│ │ ┌────────────────────────────────────┐ │
│ │ │ 🔗 研究论文库                      │ │
│ │ │    QAnything · 最后同步: 1小时前   │ │
│ │ └────────────────────────────────────┘ │
│ │                                         │
│ │ [+ 添加外部知识库]                      │
│ └────────────────────────────────────────┘│
└─────────────────────────────────────────┘
```

功能说明：
- 资料 Tab 新增"外部知识库"切换标签，与"本地资料"同级
- 显示已接入的外部知识库连接列表
- 每个连接可展开查看集合/文档树
- 支持搜索外部知识库内容
- 文档旁有"导入"按钮，点击后将该文档导入到当前知识库
- 导入后的文档出现在"本地资料"列表中，并关联导入记录

### 新增路由

```
/external-kb              # 外部知识库管理页（单独页面）
  /external-kb/connections/:id  # 连接详情/浏览
```

同时在知识库详情页的"资料"Tab 中嵌入外部 KB 浏览面板。

### 路由表

```
/login                      # 登录/注册页
/auth/google                # Google OAuth 绑定页（首次使用需授权 NotebookLM）
/                           # 首页 - 知识库列表
/notebook/:id               # 知识库详情 (Tab 容器)
  /notebook/:id/overview    # 概览
  /notebook/:id/sources     # 资料 (含本地 + 外部知识库子 Tab)
  /notebook/:id/chat        # 问答
  /notebook/:id/chat/:sid   # 具体对话会话
  /notebook/:id/generate    # 生成
  /notebook/:id/generate/:gid  # 生成内容详情
/external-kb                # 外部知识库管理（独立页面）
  /external-kb/connections/:id  # 连接详情/浏览
/settings                   # 用户设置
/history                    # 请求历史 / 审计日志
```

### 页面说明

#### 首页 — 知识库列表
- 卡片网格展示所有知识库（数据源：本地 notebooks 表）
- 支持手动"同步"按钮从 Google 拉取最新数据
- 搜索、排序（按更新时间/创建时间）
- "新建知识库"按钮
- 每个卡片显示：名称、资料数、问答数、更新时间、最后同步时间

#### 知识库详情 — 资料 Tab
- 已上传文档列表（数据源：本地 sources 表）
- 拖拽/点击上传新文档（PDF / Word / TXT / 网页链接）
- 上传流程：本地保存文件 → 写入 sources 表 → 同步到 Google NotebookLM
- 离线支持：已上传的文档始终可从本地加载

#### 知识库详情 — 问答 Tab
- AI 对话界面（类 ChatGPT 布局）
- 左侧会话列表（数据源：本地 chat_sessions 表）
- 用户消息右对齐（border-radius: 12px 12px 2px）
- AI 消息左对齐（border-radius: 2px 12px 12px）
- 每条回答附带原文引用，可点击跳转到源段落
- 所有消息存储在本地 chat_messages 表，包含完整 request/response 记录
- 支持历史会话查看（即使 API 不可用）

#### 知识库详情 — 生成 Tab
- 支持 **6 种内容类型**：PPT / 脑图 / 信息图 / 播客 / 视频 / 文档
- 每种内容类型有独立的生成配置界面和模板选择
- 生成引擎：
  - **播客** → Google NotebookLM Audio API
  - **文档**（笔记/摘要/FAQs/学习指南） → Google NotebookLM Artifact API
  - **PPT** → 本地生成引擎（python-pptx），基于 NotebookLM 返回的内容或文档摘要
  - **脑图** → 本地生成引擎，基于文档结构化提取
  - **信息图** → 本地生成引擎，基于文档内容 + 模板渲染
  - **视频** → 本地生成引擎（需 FFmpeg），图文结合生成短视频
- 生成结果保存到本地 generated_contents 表（含完整的数据描述）
- 所有生成类型支持预览、二次编辑、导出下载

## 生成引擎架构

### 生成方式分类

| 内容类型 | 引擎 | 说明 |
|---|---|---|
| 播客 | NotebookLM Audio API | 直接调用 NotebookLM 生成双人对话音频 |
| 文档 | NotebookLM Artifact API | 笔记、摘要、FAQs、学习指南 |
| PPT | 本地引擎 (python-pptx) | 基于文档提取结构化内容，填充模板生成 PPTX |
| 脑图 | 本地引擎 (graphviz/py-mindmap) | 从文档中提取层级结构，生成思维导图 |
| 信息图 | 本地引擎 (Pillow/HTML渲染) | 基于模板 + 文档内容渲染信息图图片 |
| 视频 | 本地引擎 (FFmpeg + PIL) | 图文合成短视频，支持旁白 TTS |

### 本地生成引擎流程

```
用户选择生成类型 + 模板
         │
         ▼
  ┌─────────────────────────┐
  │  内容提取层              │
  │  从 NotebookLM 文档/笔记  │
  │  或本地文档中提取结构化内容 │
  └─────────┬───────────────┘
            │ 结构化数据 (JSON)
            ▼
  ┌─────────────────────────┐
  │  模板引擎                │
  │  根据内容类型选择模板     │
  │  PPT: 每页标题+要点布局   │
  │  脑图: 层级节点结构       │
  │  信息图: 图文块布局       │
  │  视频: 场景+旁白脚本      │
  └─────────┬───────────────┘
            │ 模板填充后的数据
            ▼
  ┌─────────────────────────┐
  │  渲染引擎                │
  │  PPT → python-pptx      │
  │  脑图 → graphviz / d3.js│
  │  信息图 → Pillow         │
  │  视频 → FFmpeg + TTS    │
  └─────────┬───────────────┘
            │ 输出文件
            ▼
  ┌─────────────────────────┐
  │  本地持久化              │
  │  generated_contents 表   │
  │  + MEDIA_ROOT 文件存储   │
  └─────────────────────────┘
```

### 引擎模块目录

```
src/notebooklm/generation/
├── __init__.py
├── base.py                    # 抽象基类 ContentGenerator
├── registry.py                # 生成器注册
├── engines/
│   ├── __init__.py
│   ├── ppt_engine.py          # PPT 生成 (python-pptx)
│   ├── mindmap_engine.py      # 脑图生成 (graphviz / d3.js JSON)
│   ├── infographic_engine.py  # 信息图生成 (Pillow)
│   ├── podcast_engine.py      # 播客生成 (调用 NotebookLM)
│   ├── video_engine.py        # 视频生成 (FFmpeg)
│   └── document_engine.py     # 文档生成 (调用 NotebookLM)
├── templates/                 # 模板文件
│   ├── ppt/
│   ├── mindmap/
│   ├── infographic/
│   └── video/
└── extractors/                # 内容提取器
    ├── __init__.py
    ├── source_extractor.py    # 从文档源提取
    └── chat_extractor.py      # 从对话提取
```

### 生成器抽象接口

```python
class ContentGenerator(ABC):
    """内容生成器抽象基类"""

    @property
    @abstractmethod
    def content_type(self) -> str: ...

    @abstractmethod
    async def generate(self, notebook_id: str, prompt: str,
                       template: str | None = None,
                       options: dict | None = None) -> GeneratedContent:
        """执行生成，返回本地持久化后的内容记录"""

    @abstractmethod
    async def preview(self, notebook_id: str, prompt: str,
                      template: str | None = None) -> PreviewResult:
        """生成前的预览（如 PPT 大纲预览）"""

    @abstractmethod
    async def get_supported_templates(self) -> list[TemplateInfo]:
        """获取支持的模板列表"""
```
#### 知识库详情 — 概览 Tab
- 知识库统计（文档数、问答数、生成次数）
- 最近活动时间线（数据源：request_logs 本地审计）
- AI 自动生成的摘要（本地缓存）

## 设计 Token (有道宝库风格)

### 品牌色
- `--color_main_1`: #ff3650 (品牌红)
- `--color_main_2`: #17181a (深黑)
- `--color_text_focus`: #1a75ff (链接蓝)
- `--el-color-primary`: #409eff (Element 蓝)

### 文字色
- `--color_text_1`: #2a2b2e (主要)
- `--color_text_2`: #626469 (次要)
- `--color_text_3`: #939599 (三级)
- `--color_text_4`: #a8aaad (禁用)

### 背景色
- `--color_bg_1`: #fff (主背景)
- `--color_bg_tab`: #f5f6f7 (标签背景)
- `--color_divider_1`: #eeeff0 (分割线)

### 圆角
- 卡片: 12px | 标签: 6px | 输入框: 8px | 按钮: 6px
- 用户消息: 12px 12px 2px | AI 消息: 2px 12px 12px

### 字体
- `-apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", sans-serif`

### 阴影
- 卡片: `0px 4px 40px 0px rgba(174,180,193,.17)`

## 用户系统

### API 端点

```
POST   /api/auth/register          # 注册
POST   /api/auth/login             # 登录，返回 JWT
POST   /api/auth/google/bind       # 绑定 Google NotebookLM 账号
GET    /api/auth/me                # 获取当前用户信息
DELETE /api/auth/logout            # 登出
```

### 认证流程
1. 用户首次访问 → 注册/登录页面
2. 登录后获得 JWT token
3. 首次使用需跳转 Google OAuth 授权 NotebookLM
4. 后续请求通过 JWT 鉴权，通过 Google token 调用 NotebookLM API

## 后端集成

复用 notebooklm-py 的 REST Server，在 `src/notebooklm/server/routes/` 下新增：

- `auth.py` — 用户注册/登录/JWT
- `storage.py` — 本地数据持久化层（数据库读写 + 文件存储）
- `external_kb/` — 外部知识库连接器体系
- `routes/external_kb.py` — 外部 KB 路由
- `generation/` — 内容生成引擎（6 种类型）
- `routes/generation.py` — 生成 API 路由

已有 API 的路由新增 middleware 层：
1. JWT 鉴权拦截
2. 请求/响应日志自动写入 request_logs 表
3. 数据优先从本地返回，按需回源 Google

## 项目目录

```
notebooklm-py/
├── frontend/                     # 新增：Vue 3 前端
│   ├── src/
│   │   ├── views/
│   │   │   ├── LoginView.vue
│   │   │   ├── HomeView.vue
│   │   │   ├── NotebookView.vue
│   │   │   │   ├── OverviewTab.vue
│   │   │   │   ├── SourcesTab.vue
│   │   │   │   │   └── ExternalSourcesPanel.vue  # 外部知识库子面板
│   │   │   │   ├── ChatTab.vue
│   │   │   │   └── GenerateTab.vue
│   │   │   │       ├── PptGenerator.vue
│   │   │   │       ├── MindmapGenerator.vue
│   │   │   │       ├── InfographicGenerator.vue
│   │   │   │       ├── PodcastGenerator.vue
│   │   │   │       ├── VideoGenerator.vue
│   │   │   │       └── DocumentGenerator.vue
│   │   │   ├── ExternalKbView.vue          # 外部知识库管理页
│   │   │   ├── ExternalKbDetailView.vue    # 连接详情/浏览
│   │   │   └── SettingsView.vue
│   │   ├── components/
│   │   │   ├── ExternalKbPanel.vue       # 外部知识库浏览面板
│   │   │   ├── ExternalKbConnForm.vue    # 连接配置表单
│   │   │   ├── SourceList.vue
│   │   │   ├── GenerationTemplatePicker.vue  # 模板选择器
│   │   ├── stores/
│   │   ├── api/
│   │   ├── router/
│   │   └── styles/
│   ├── index.html
│   ├── vite.config.ts
│   ├── tsconfig.json
│   └── package.json
├── src/notebooklm/
│   ├── server/
│   │   ├── __init__.py
│   │   ├── server.py
│   │   ├── database.py              # 新增：数据库初始化/迁移
│   │   ├── models.py                # 新增：SQLAlchemy 模型（含外部 KB 表）
│   │   ├── storage.py               # 新增：本地文件存储
│   │   ├── external_kb/             # 新增：外部知识库连接器体系
│   │   │   ├── __init__.py
│   │   │   ├── base.py              # 抽象基类
│   │   │   ├── registry.py          # 连接器注册中心
│   │   │   ├── providers/
│   │   │   │   ├── __init__.py
│   │   │   │   ├── openapi.py
│   │   │   │   ├── dify.py
│   │   │   │   ├── qanything.py
│   │   │   │   └── custom.py
│   │   │   └── sync.py              # 同步/导入引擎
│   │   ├── generation/              # 新增：内容生成引擎
│   │   │   ├── __init__.py
│   │   │   ├── base.py              # 抽象基类 ContentGenerator
│   │   │   ├── registry.py          # 生成器注册中心
│   │   │   ├── engines/
│   │   │   │   ├── __init__.py
│   │   │   │   ├── ppt_engine.py
│   │   │   │   ├── mindmap_engine.py
│   │   │   │   ├── infographic_engine.py
│   │   │   │   ├── podcast_engine.py
│   │   │   │   ├── video_engine.py
│   │   │   │   └── document_engine.py
│   │   │   ├── templates/
│   │   │   │   ├── ppt/
│   │   │   │   ├── mindmap/
│   │   │   │   ├── infographic/
│   │   │   │   └── video/
│   │   │   └── extractors/
│   │   │       ├── __init__.py
│   │   │       ├── source_extractor.py
│   │   │       └── chat_extractor.py
│   │   └── routes/
│   │       ├── __init__.py
│   │       ├── auth.py              # 新增
│   │       ├── external_kb.py       # 新增：外部 KB 路由
│   │       ├── middleware.py        # 新增：请求日志 + 缓存拦截
│   │       └── ... (已有路由)
│   └── ...
└── README.md
```

## 实现顺序

1. 后端：数据库模型 + 初始化 (SQLAlchemy models, database.py) — 包含所有表
2. 后端：本地文件存储模块 (storage.py)
3. 后端：用户系统 (auth 模块、JWT)
4. 后端：请求日志 middleware
5. 后端：业务 API 本地缓存层
6. 后端：外部知识库连接器体系 — 抽象基类 + 注册中心
7. 后端：外部知识库连接器 — OpenAPI provider 实现
8. 后端：外部知识库 API 路由
9. 后端：内容生成引擎 — 抽象基类 + 注册中心
10. 后端：文档生成引擎 (调用 NotebookLM Artifact API)
11. 后端：播客生成引擎 (调用 NotebookLM Audio API)
12. 后端：PPT 生成引擎 (python-pptx)
13. 后端：脑图生成引擎 (graphviz / JSON)
14. 后端：信息图生成引擎 (Pillow)
15. 后端：视频生成引擎 (FFmpeg)
16. 后端：内容生成 API 路由
17. 前端脚手架 (Vue + Vite + Element Plus + 路由 + 主题)
18. 首页 (知识库列表)
19. 知识库详情 + 资料 Tab (上传/列表/文件存储)
20. 资料 Tab — 外部知识库浏览面板 (UI + 集成)
21. 问答 Tab (对话界面 + 溯源引用 + 本地历史存储)
22. 生成 Tab (6 种内容类型 UI + 模板选择 + 预览 + 编辑)
23. 概览 Tab
24. 外部知识库管理页 (连接管理页面)
25. 登录/注册页面 + Google OAuth 绑定
26. 用户设置页面
27. 响应式 + 暗色主题适配
28. 集成测试 + 数据完整性验证

## 非功能需求

- 前端适配移动端（有道宝库有响应式设计）
- 支持亮色/暗色主题（baoku 已有 `.light` / `.dark` 双主题系统）
- API 响应时间对接 NotebookLM 本身的延迟，前端需展示加载状态
- 文件上传需显示进度条
- 生成长耗时任务（PPT/视频）使用异步队列，前端轮询进度
- 本地数据加密存储（Google token 等敏感信息）
- 请求日志自动清理策略（默认保留 90 天）
- 所有本地数据支持导出（JSON + 文件打包）

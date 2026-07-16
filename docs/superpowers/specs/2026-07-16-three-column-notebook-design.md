# 三栏 Notebook 详情页设计

**日期**: 2026-07-16
**主题**: 将 Notebook 详情页从标签页改为左中右三栏布局
**状态**: 待实现

## 目标

把现有的 `NotebookView.vue`（顶部标签页：概览/资料/问答/生成）改造为类似 Youdao Baoku 的三栏工作台：

- 左栏：资料管理
- 中栏：AI 问答
- 右栏：内容生成

## 背景

当前实现中，用户需要在不同标签页之间切换才能查看资料、提问和生成内容。目标截图显示的是一个单屏同时展示三栏的界面，信息密度更高，操作路径更短。

## 设计

### 页面布局

```
┌─────────────────┬─────────────────────────────┬─────────────────┐
│  资料 (N)       │  AI 问答                     │  内容生成        │
│  + 添加资料      │  ┌───────────────────────┐  │  PPT   脑图      │
│                 │  │ AI总结（自动生成）     │  │  播客  信息图    │
│  ☑ source 1     │  └───────────────────────┘  │  视频  文档      │
│  ☑ source 2     │  推荐问题                    │                 │
│  ...            │  • 问题1                     │  生成记录        │
│                 │  • 问题2                     │  • record 1      │
│  ───────────────│  • 问题3                     │  • record 2      │
│  笔记           │  ─────────────────────────  │                 │
│  自由记录...     │  [输入问题...]  [发送]       │                 │
└─────────────────┴─────────────────────────────┴─────────────────┘
```

- 顶部保留标题栏：返回、知识库标题、同步、更多操作，以及「三栏视图 / 标签视图」切换按钮。
- 下方三栏占满剩余视口高度。
- 左栏、右栏默认宽度 `300px`，中栏自适应；三栏之间可拖拽调整宽度，宽度持久化到 `localStorage`。
- 三栏各自独立滚动。

### 组件拆分

| 组件 | 职责 | 位置 |
|------|------|------|
| `NotebookView.vue` | 容器：拉取 notebook 详情、编排三栏 | 顶层 |
| `SourcesPanel.vue` | 资料列表、复选框、添加资料、笔记区 | 左栏 |
| `ChatPanel.vue` | AI 总结、推荐问题、对话区、输入框 | 中栏 |
| `GeneratePanel.vue` | 生成入口、生成记录 | 右栏 |

### 各栏细节

#### SourcesPanel.vue

- 标题行显示 `资料 (count)` 和 `+ 添加资料` 按钮。
- 资料列表项：复选框 + 文件类型图标 + 文件名 + 悬停删除/重命名。
- 默认全部选中；选中状态通过 `selectedSourceIds` 在内部管理，并通过事件暴露给父组件。
- 顶部工具栏：当存在选中项时显示「删除选中」按钮，调用批量删除 API。
- 底部可折叠的 `笔记` 区域：笔记列表，支持新增/编辑/删除单条笔记，数据保存到后端。
- 复用 API：`fetchSourcesApi`、`uploadSourceApi`、`addSourceUrlApi`、`deleteSourceApi`、`renameSourceApi`、批量删除 API、笔记 CRUD API。
- 复用组件：`UploadDialog`、`SourceList`。

#### ChatPanel.vue

- 标题 `AI 问答`。
- **AI 总结**：进入页面后自动调用 chat API，发送问题“请用中文总结这些资料的核心要点”，流式或一次性返回结果。显示骨架屏/加载态。
- **推荐问题**：后端根据资料内容动态生成 3 条问题，点击后自动填入输入框并发送。
- **对话区**：
  - 有当前会话时显示消息列表。
  - 无会话时显示欢迎态（如截图中的“开始新对话”）。
- **输入框**：底部固定，发送消息时创建新会话或继续当前会话；发送时携带当前选中的 `source_ids`，后端仅基于选中资料回答。
- 复用：`chat` store、流式输出、现有 `ChatMessage`、`ChatInput`、`CitationPopup`。

#### GeneratePanel.vue

- 标题 `内容生成`。
- 6 个生成入口卡片：`PPT`、`脑图`、`播客`、`信息图`、`视频`、`文档`。
- 点击卡片后弹出 `ElDialog`，内部复用现有的 `PptGenerator.vue`、`MindmapGenerator.vue` 等 6 个 generator 组件；生成请求携带当前选中的 `source_ids`。
- 下方 `生成记录` 列表，显示最近的生成结果（标题、类型、时间、状态）。
- 点击生成记录打开详情页 `/notebook/:id/generated/:generatedId`。
- 复用 API：`fetchGeneratedContentsApi`、`generateContentApi`。

### 数据流

1. `NotebookView.vue` 获取 `notebookId`，默认进入三栏视图。
2. 顶部提供切换按钮：三栏视图 / 标签视图（保留旧标签页作为回退）。
3. `onMounted` 时并行拉取：
   - notebook 详情
   - sources 列表
   - generated 列表
   - 笔记列表
   - 推荐问题（可选，失败时降级为固定问题）
4. 通过 props 把 `notebookId`、`sources`、`generated`、`notes`、`recommendedQuestions` 传给各 panel。
5. `selectedSourceIds` 在 `NotebookView` 统一管理，默认全选；变更后同步到 `ChatPanel` 和 `GeneratePanel`。
6. 生成完成后，通过事件通知 `NotebookView` 刷新生成记录。

### 复用与删除

- 保留现有 generator 组件：`PptGenerator.vue`、`MindmapGenerator.vue`、`InfographicGenerator.vue`、`PodcastGenerator.vue`、`VideoGenerator.vue`、`DocumentGenerator.vue`。
- 保留 `ChatMessage.vue`、`ChatInput.vue`、`CitationPopup.vue`、`UploadDialog.vue`、`SourceList.vue`。
- 保留旧 Tab 组件作为回退：`OverviewTab.vue`、`SourcesTab.vue`、`ChatTab.vue`、`GenerateTab.vue`。
- `NotebookView.vue` 内部维护 `viewMode`（`'three-column'` 或 `'tabs'`），默认三栏视图，顶部提供切换按钮。
- 路由：`/notebook/:id` 直接渲染 `NotebookView.vue`，子路由 `/notebook/:id/overview` 等保留给标签视图使用。

### 错误处理

- 各 panel 独立捕获错误，使用 `ElMessage.error` 提示。
- AI 总结失败时显示友好错误信息，不阻塞其他两栏。
- 网络超时遵循现有 `request.ts` 拦截器。

### 测试

- 类型检查：`npx vue-tsc --noEmit`
- 构建：`npm run build`
- 手动验证：创建/进入 notebook，检查三栏渲染、添加资料、AI 总结、新建对话、生成入口。

## 决策记录

- **方案选择**：方案 C（新建三个专用面板组件），因为最贴合目标截图，组件边界清晰。
- **栏宽**：三栏默认左/右 300px，中间自适应，支持拖拽调整并持久化到 `localStorage`。
- **AI 总结**：进入页面自动发送问题“请用中文总结这些资料的核心要点”，复用现有 chat API。
- **推荐问题**：后端根据资料动态生成 3 条，失败时降级为固定问题。
- **资料复选框**：既影响 AI 问答/生成内容的上下文，也支持批量删除。
- **生成记录**：点击后打开详情页 `/notebook/:id/generated/:generatedId`。
- **视图切换**：三栏视图为默认，保留旧标签视图作为回退。
- **笔记区**：每个知识库支持多条笔记，保存到后端。
- **移动端**：本次先保证桌面三栏，暂不响应式适配。

## 后端接口需求

1. **笔记 CRUD**
   - `GET /api/notebooks/{notebook_id}/notes`
   - `POST /api/notebooks/{notebook_id}/notes`
   - `PUT /api/notebooks/{notebook_id}/notes/{note_id}`
   - `DELETE /api/notebooks/{notebook_id}/notes/{note_id}`
   - 字段：`id`、`notebook_id`、`user_id`、`title`、`content`、`created_at`、`updated_at`

2. **推荐问题**
   - `GET /api/notebooks/{notebook_id}/recommended-questions`
   - 根据 notebook 资料内容，调用 chat API 或内部 LLM 生成 3 个问题；失败返回固定问题。

3. **批量删除资料**
   - `DELETE /api/notebooks/{notebook_id}/sources/batch`
   - Body: `{ source_ids: number[] }`

4. **问答/生成上下文过滤**
   - 现有 chat API 需要支持可选 `source_ids` 参数，未传时默认使用全部资料。
   - 现有 generate API 需要支持可选 `source_ids` 参数，未传时默认使用全部资料。

## 待办

### 后端
- [ ] 新增 `Note` 模型与数据库迁移
- [ ] 实现笔记 CRUD API
- [ ] 实现推荐问题 API
- [ ] 实现批量删除资料 API
- [ ] chat/generate API 支持 `source_ids` 过滤

### 前端
- [ ] 新增笔记、推荐问题、批量删除 API 封装
- [ ] 创建 `SourcesPanel.vue`
- [ ] 创建 `ChatPanel.vue`
- [ ] 创建 `GeneratePanel.vue`
- [ ] 重写 `NotebookView.vue` 为三栏容器，支持视图切换与拖拽调整栏宽
- [ ] 保留旧标签视图路由与组件
- [ ] 运行类型检查和构建
- [ ] 手动验证核心流程

<template>
  <div class="source-list">
    <div v-if="sources.length === 0" class="empty">
      <el-icon :size="48" color="#d0d0d0"><Upload /></el-icon>
      <p class="empty-text">{{ emptyText || "暂无资料，点击上方按钮上传" }}</p>
    </div>
    <div v-for="source in sources" :key="source.id" class="source-item" @click="$emit('select', source)">
      <div class="source-icon">
        <el-icon :size="20" :color="fileIconColor(source.file_type)"><component :is="fileIcon(source.file_type)" /></el-icon>
      </div>
      <div class="source-info">
        <span class="source-name">{{ source.original_filename || source.filename }}</span>
        <span class="source-meta">{{ formatFileSize(source.file_size) }}<template v-if="source.page_count"> · {{ source.page_count }} 页</template> · {{ formatTime(source.created_at) }}</span>
      </div>
      <div class="source-status">
        <el-tag v-if="source.status === 'processing'" size="small" type="warning">处理中</el-tag>
        <el-tag v-else-if="source.status === 'active'" size="small" type="success">就绪</el-tag>
        <el-tag v-else-if="source.status === 'deleted'" size="small" type="info">已删除</el-tag>
      </div>
      <div class="source-actions">
        <el-button v-if="showDelete" text type="danger" size="small" @click.stop="$emit('delete', source)"><el-icon><Delete /></el-icon></el-button>
        <el-button v-if="showRename" text size="small" @click.stop="$emit('rename', source)"><el-icon><Edit /></el-icon></el-button>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import type { Source } from "@/api/sources"
import { Document, Delete, Edit, Upload, Link } from "@element-plus/icons-vue"
defineProps<{ sources: Source[]; emptyText?: string; showDelete?: boolean; showRename?: boolean }>()
defineEmits<{ select: [source: Source]; delete: [source: Source]; rename: [source: Source] }>()

function fileIcon(type: string) { const m: Record<string, any> = { pdf: Document, docx: Document, doc: Document, txt: Document, url: Link }; return m[type] || Document }
function fileIconColor(type: string) { const m: Record<string, string> = { pdf: "#ff3650", docx: "#1a75ff", doc: "#1a75ff", txt: "#626469", url: "#409eff" }; return m[type] || "#626469" }
function formatFileSize(bytes: number) { if (!bytes) return "0 B"; if (bytes < 1024) return `${bytes} B`; if (bytes < 1048576) return `${(bytes/1024).toFixed(1)} KB`; return `${(bytes/1048576).toFixed(1)} MB` }
function formatTime(t: string) { const d = new Date(t); const diff = Date.now() - d.getTime(); if (diff < 3600000) return `${Math.floor(diff/60000)} 分钟前`; if (diff < 86400000) return `${Math.floor(diff/3600000)} 小时前`; return d.toLocaleDateString("zh-CN") }
</script>

<style scoped lang="scss">
.source-list {
  .empty { display: flex; flex-direction: column; align-items: center; padding: 48px 20px; gap: 12px; .empty-text { font-size: 14px; color: var(--color-text-3); } }
}
.source-item { display: flex; align-items: center; gap: 12px; padding: 8px 12px; border-radius: 10px; cursor: pointer; transition: background 0.15s; height: 40px; &:hover { background: var(--color-bg-tab); } }
.source-icon { flex-shrink: 0; display: flex; align-items: center; justify-content: center; width: 28px; height: 28px; }
.source-info { flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 2px; }
.source-name { font-size: 13px; font-weight: 500; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.source-meta { font-size: 11px; color: var(--color-text-3); }
.source-status { flex-shrink: 0; }
.source-actions { flex-shrink: 0; display: flex; gap: 4px; opacity: 0; transition: opacity 0.15s; }
.source-item:hover .source-actions { opacity: 1; }
</style>

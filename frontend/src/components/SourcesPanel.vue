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
        <el-icon class="toggle-icon" :class="{ expanded: notesExpanded }"><ArrowDown /></el-icon>
      </div>
      <div v-if="notesExpanded" class="notes-body">
        <div v-for="note in notes" :key="note.id" class="note-item">
          <el-input v-model="note.content" type="textarea" :rows="2" resize="none" @blur="$emit('update-note', note)" />
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
import { ElMessageBox } from "element-plus"
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
    const ids = Array.from(selectedIds.value)
    await import("@/api/sources").then((m) => {
      return Promise.all(ids.map((sid) => m.deleteSourceApi(props.notebookId, parseInt(sid) || 0).catch(() => {})))
    })
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
.source-item { display: flex; align-items: center; gap: 8px; padding: 8px; border-radius: 6px; cursor: pointer; }
.source-item:hover { background: var(--baoku-bg, #f5f5f5); }
.source-icon { color: var(--baoku-text-3, #999); flex-shrink: 0; }
.source-name { flex: 1; font-size: 13px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.empty-text { padding: 24px; text-align: center; color: var(--baoku-text-3, #999); font-size: 13px; }
.notes-section { border-top: 1px solid var(--baoku-border, #e8e8e8); }
.notes-header { display: flex; align-items: center; justify-content: space-between; padding: 10px 16px; cursor: pointer; font-size: 13px; font-weight: 500; }
.toggle-icon { font-size: 14px; color: var(--baoku-text-3); transition: transform 0.2s; }
.toggle-icon.expanded { transform: rotate(180deg); }
.notes-body { padding: 8px 16px 16px; }
.note-item { margin-bottom: 8px; }
</style>

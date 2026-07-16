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
          <SplitPane :initial-left="500" :min-left="300" :min-right="240" storage-key="nb-split-right">
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

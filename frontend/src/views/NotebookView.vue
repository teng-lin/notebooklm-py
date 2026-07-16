<template>
  <div class="notebook-page">
    <header class="notebook-header">
      <div class="header-left">
        <el-button text class="back-btn" @click="router.push('/')"><el-icon><ArrowLeft /></el-icon><span>返回</span></el-button>
        <h2 class="notebook-title">{{ notebook?.title || "加载中..." }}</h2>
      </div>
      <div class="header-right">
        <el-button text class="sync-btn" :loading="syncing" @click="handleSync"><el-icon><Refresh /></el-icon>同步</el-button>
        <el-dropdown trigger="click">
          <el-button text class="more-btn"><el-icon><MoreFilled /></el-icon></el-button>
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
    <div class="tab-bar">
      <el-tabs v-model="activeTab" class="notebook-tabs" @tab-change="handleTabChange">
        <el-tab-pane label="概览" name="overview" />
        <el-tab-pane label="资料" name="sources" />
        <el-tab-pane label="问答" name="chat" />
        <el-tab-pane label="生成" name="generate" />
      </el-tabs>
    </div>
    <div class="tab-content"><router-view /></div>

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
import { ref, computed, onMounted } from "vue"
import { useRouter, useRoute } from "vue-router"
import { ArrowLeft, Refresh, MoreFilled, Edit, Share, Delete } from "@element-plus/icons-vue"
import { ElMessage, ElMessageBox } from "element-plus"
import type { FormInstance, FormRules } from "element-plus"
import { useNotebooksStore } from "@/stores/notebooks"

const router = useRouter()
const route = useRoute()
const notebooksStore = useNotebooksStore()
const notebookId = computed(() => Number(route.params.id))
const notebook = computed(() => notebooksStore.currentNotebook)

const activeTab = ref("overview")
const syncing = ref(false)
const showRenameDialog = ref(false)
const renaming = ref(false)
const renameFormRef = ref<FormInstance>()
const renameForm = ref({ title: "", description: "" })
const renameRules: FormRules = { title: [{ required: true, message: "请输入名称", trigger: "blur" }] }

function handleTabChange(name: string) { router.push(`/notebook/${notebookId.value}/${name}`) }

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
  const childPath = route.path.split("/").pop()
  if (["overview", "sources", "chat", "generate"].includes(childPath || "")) activeTab.value = childPath!
})
</script>

<style scoped lang="scss">
.notebook-page { min-height: 100vh; background: var(--color-bg-tab); }
.notebook-header { display: flex; align-items: center; justify-content: space-between; padding: 0 24px; height: 48px; background: var(--color-bg-1); border-bottom: 1px solid var(--color-divider-1); position: sticky; top: 0; z-index: 100; }
.header-left { display: flex; align-items: center; gap: 12px; }
.back-btn { font-size: 14px; color: var(--color-text-2); .el-icon { margin-right: 4px; } }
.notebook-title { font-size: 16px; font-weight: 600; }
.header-right { display: flex; align-items: center; gap: 4px; }
.sync-btn, .more-btn { color: var(--color-text-2); }
.tab-bar { background: var(--color-bg-1); padding: 0 24px; border-bottom: 1px solid var(--color-divider-1); }
.notebook-tabs {
  :deep(.el-tabs__header) { margin: 0; }
  :deep(.el-tabs__item) { height: 40px; font-size: 14px; color: var(--color-text-2); &.is-active { color: var(--color-main-1); font-weight: 500; } }
  :deep(.el-tabs__active-bar) { background-color: var(--color-main-1); }
  :deep(.el-tabs__nav-wrap::after) { display: none; }
}
.tab-content { padding: 24px; }
</style>

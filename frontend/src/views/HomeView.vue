<template>
  <div class="home-page">
    <header class="home-header">
      <div class="header-left"><h1 class="page-title">我的宝库</h1></div>
      <div class="header-right">
        <el-input v-model="searchQuery" placeholder="搜索知识库" :prefix-icon="Search" clearable class="search-input" @input="debouncedSearch" />
        <el-button type="primary" class="create-btn" @click="showCreateDialog = true">+ 新建知识库</el-button>
      </div>
    </header>
    <div class="home-content page-container">
      <!-- Loading skeleton -->
      <div v-if="loading" class="card-grid">
        <div v-for="i in 6" :key="i" class="skeleton-card card">
          <div class="skeleton-line skeleton-title" /><div class="skeleton-line skeleton-desc" /><div class="skeleton-line skeleton-meta" />
        </div>
      </div>
      <!-- Empty state -->
      <div v-else-if="isEmpty" class="empty-state">
        <el-icon :size="64" color="#d0d0d0"><FolderOpened /></el-icon>
        <p class="empty-title">创建你的第一个知识库</p>
        <p class="empty-desc">上传文档，让 AI 帮你整理和生成内容</p>
        <el-button type="primary" @click="showCreateDialog = true">+ 新建知识库</el-button>
      </div>
      <!-- Notebook grid -->
      <div v-else class="card-grid">
        <div v-for="nb in notebooks" :key="nb.id" class="notebook-card card" @click="router.push(`/notebook/${nb.id}`)">
          <div class="card-header">
            <h3 class="card-title">{{ nb.title }}</h3>
            <el-tag v-if="nb.last_synced_at" size="small" type="info" class="sync-tag">已同步</el-tag>
          </div>
          <p v-if="nb.description" class="card-desc">{{ nb.description }}</p>
          <div class="card-meta">
            <span class="meta-item"><el-icon><Document /></el-icon>{{ nb.source_count }} 份资料</span>
            <span class="meta-item"><el-icon><ChatDotSquare /></el-icon>{{ nb.chat_count }} 次问答</span>
            <span class="meta-item time">{{ formatTime(nb.updated_at) }}</span>
          </div>
        </div>
      </div>
    </div>

    <!-- Create dialog -->
    <el-dialog v-model="showCreateDialog" title="新建知识库" width="420px" :close-on-click-modal="false">
      <el-form ref="createFormRef" :model="createForm" :rules="createRules" label-position="top">
        <el-form-item label="知识库名称" prop="title">
          <el-input v-model="createForm.title" placeholder="请输入知识库名称" maxlength="100" show-word-limit />
        </el-form-item>
        <el-form-item label="描述（选填）" prop="description">
          <el-input v-model="createForm.description" type="textarea" :rows="3" placeholder="简短描述这个知识库的用途" maxlength="500" show-word-limit />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="showCreateDialog = false">取消</el-button>
        <el-button type="primary" :loading="creating" @click="handleCreate">创建</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { ref, reactive, onMounted, computed } from "vue"
import { useRouter } from "vue-router"
import { Search, FolderOpened, Document, ChatDotSquare } from "@element-plus/icons-vue"
import { ElMessage } from "element-plus"
import type { FormInstance, FormRules } from "element-plus"
import { useNotebooksStore } from "@/stores/notebooks"
import { useAuthStore } from "@/stores/auth"

const router = useRouter()
const notebooksStore = useNotebooksStore()
const authStore = useAuthStore()

const notebooks = computed(() => notebooksStore.notebooks)
const loading = computed(() => notebooksStore.loading)
const isEmpty = computed(() => notebooksStore.isEmpty)
const searchQuery = ref("")
const showCreateDialog = ref(false)
const creating = ref(false)
const createFormRef = ref<FormInstance>()
const createForm = reactive({ title: "", description: "" })
const createRules: FormRules = {
  title: [{ required: true, message: "请输入知识库名称", trigger: "blur" }, { max: 100, message: "名称不能超过 100 字符", trigger: "blur" }],
}

let debounceTimer: ReturnType<typeof setTimeout> | null = null
function debouncedSearch() {
  if (debounceTimer) clearTimeout(debounceTimer)
  debounceTimer = setTimeout(() => { notebooksStore.fetchNotebooks({ search: searchQuery.value || undefined }) }, 300)
}

async function handleCreate() {
  const valid = await createFormRef.value?.validate().catch(() => false)
  if (!valid) return
  creating.value = true
  try {
    const nb = await notebooksStore.createNotebook({ title: createForm.title, description: createForm.description || undefined })
    ElMessage.success("创建成功"); showCreateDialog.value = false
    createForm.title = ""; createForm.description = ""
    router.push(`/notebook/${nb.id}`)
  } catch (e: any) { ElMessage.error(e.response?.data?.detail || "创建失败") }
  finally { creating.value = false }
}

function formatTime(t: string) {
  const d = new Date(t); const diff = Date.now() - d.getTime()
  if (diff < 3600000) return `${Math.floor(diff / 60000)} 分钟前`
  if (diff < 86400000) return `${Math.floor(diff / 3600000)} 小时前`
  return d.toLocaleDateString("zh-CN")
}

onMounted(() => {
  if (!authStore.isAuthenticated) { router.push("/login"); return }
  notebooksStore.fetchNotebooks()
})
</script>

<style scoped lang="scss">
.home-page { min-height: 100vh; background: var(--color-bg-tab); }
.home-header { display: flex; align-items: center; justify-content: space-between; padding: 16px 24px; background: var(--color-bg-1); border-bottom: 1px solid var(--color-divider-1); position: sticky; top: 0; z-index: 100; height: 64px; }
.header-left { display: flex; align-items: center; gap: 12px; }
.page-title { font-size: 20px; font-weight: 600; }
.header-right { display: flex; align-items: center; gap: 12px; }
.search-input { width: 240px; :deep(.el-input__wrapper) { border-radius: var(--radius-input); } }
.create-btn { height: 36px; border-radius: var(--radius-button); background: var(--color-main-1); border-color: var(--color-main-1); font-weight: 500; }
.home-content { padding-top: 24px; }
.card-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 20px; }
.notebook-card { cursor: pointer; transition: transform 0.2s, box-shadow 0.2s; &:hover { transform: translateY(-2px); box-shadow: 0px 6px 48px 0px rgba(174,180,193,.25); } }
.card-header { display: flex; align-items: center; justify-content: space-between; margin-bottom: 8px; }
.card-title { font-size: 16px; font-weight: 600; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.sync-tag { flex-shrink: 0; }
.card-desc { font-size: 13px; color: var(--color-text-2); margin-bottom: 12px; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }
.card-meta { display: flex; align-items: center; gap: 16px; font-size: 12px; color: var(--color-text-3); }
.meta-item { display: flex; align-items: center; gap: 4px; .el-icon { font-size: 14px; } &.time { margin-left: auto; } }
.empty-state { display: flex; flex-direction: column; align-items: center; justify-content: center; padding: 80px 20px; gap: 12px; }
.empty-title { font-size: 18px; font-weight: 600; margin-top: 12px; }
.empty-desc { font-size: 14px; color: var(--color-text-3); margin-bottom: 8px; }
.skeleton-card {
  .skeleton-line { height: 14px; background: linear-gradient(90deg, var(--color-bg-tab) 25%, #e8e8ea 50%, var(--color-bg-tab) 75%); background-size: 200% 100%; animation: shimmer 1.5s infinite; border-radius: 4px; margin-bottom: 12px; }
  .skeleton-title { width: 60%; height: 18px; }
  .skeleton-desc { width: 80%; }
  .skeleton-meta { width: 40%; }
}
@keyframes shimmer { 0% { background-position: 200% 0; } 100% { background-position: -200% 0; } }
</style>

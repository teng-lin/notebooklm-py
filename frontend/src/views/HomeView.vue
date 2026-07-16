<template>
  <div class="home-page">
    <div class="home-content mx-auto">
      <!-- Welcome banner -->
      <div class="welcome-banner card">
        <div class="banner-left">
          <h1 class="banner-title">
            用私有资料，一键生成
            <span class="text-gradient">知识体系脑图</span>
          </h1>
          <p class="banner-desc">
            探索知识的深度理解及管理，
            <a href="#" class="banner-link" @click.prevent="ElMessage.info('开发中')">详细了解宝库 &gt;</a>
          </p>
        </div>
        <div class="banner-actions">
          <button class="btn-import" @click="ElMessage.info('开发中')">
            <el-icon><Download /></el-icon>导入有道历史创建
          </button>
          <button class="btn-create" @click="openCreate">
            <el-icon><Plus /></el-icon>直接创建
          </button>
        </div>
      </div>

      <!-- Tab filter -->
      <div class="tab-filter">
        <button
          v-for="tab in tabs"
          :key="tab.key"
          class="tab-item"
          :class="{ active: activeTab === tab.key }"
          @click="activeTab = tab.key"
        >
          {{ tab.label }}
        </button>
      </div>

      <!-- Featured section -->
      <div class="featured-section">
        <div class="section-header">
          <div class="section-title-wrap">
            <span class="section-dot" />
            <h3 class="section-title">精选知识宝库</h3>
          </div>
          <a href="#" class="view-all" @click.prevent="ElMessage.info('开发中')">查看全部</a>
        </div>
        <div class="featured-grid">
          <div
            v-for="item in featuredItems"
            :key="item.id"
            class="featured-card"
            :style="{ backgroundImage: `url(${item.cover})` }"
            @click="ElMessage.info('开发中')"
          >
            <div class="featured-card-mask">
              <h4 class="featured-card-title">{{ item.title }}</h4>
              <p class="featured-card-meta">{{ item.count }}个资料</p>
            </div>
          </div>
        </div>
      </div>

      <!-- My notebooks -->
      <div class="my-section">
        <div class="section-header">
          <div class="section-title-wrap">
            <span class="section-dot" />
            <h3 class="section-title">我的知识宝库</h3>
          </div>
        </div>

        <div v-if="loading" class="card-grid">
          <div v-for="i in 6" :key="i" class="skeleton-card card">
            <div class="skeleton-line skeleton-title" />
            <div class="skeleton-line skeleton-desc" />
            <div class="skeleton-line skeleton-meta" />
          </div>
        </div>

        <div v-else-if="!isAuthenticated || notebooks.length === 0" class="empty-state card">
          <div class="empty-card-inner">
            <el-icon :size="48" color="#d0d0d0"><FolderOpened /></el-icon>
            <p class="empty-title">登录后开始使用知识宝库</p>
            <button class="btn-login" @click="openLogin">立即登录</button>
          </div>
        </div>

        <div v-else class="card-grid">
          <div v-for="nb in notebooks" :key="nb.id" class="notebook-card card" @click="router.push(`/notebook/${nb.id}`)">
            <div class="card-cover">
              <img src="https://baoku.youdao.com/home/assets/webp/ic_nav_back-BH4W20kS.webp" alt="" />
            </div>
            <div class="card-body">
              <div class="card-header">
                <h3 class="card-title">{{ nb.title }}</h3>
                <el-tag v-if="nb.last_synced_at" size="small" type="info" class="sync-tag">已同步</el-tag>
              </div>
              <div class="card-meta">
                <span class="meta-item"><el-icon><Document /></el-icon>{{ nb.source_count || 0 }} 份资料</span>
                <span class="meta-item time">{{ formatTime(nb.updated_at) }}</span>
              </div>
            </div>
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
import { ref, reactive, onMounted, computed, inject } from "vue"
import { useRouter } from "vue-router"
import { FolderOpened, Document, Plus, Download } from "@element-plus/icons-vue"
import { ElMessage, type FormInstance, type FormRules } from "element-plus"
import { useNotebooksStore } from "@/stores/notebooks"
import { useAuthStore } from "@/stores/auth"

const router = useRouter()
const notebooksStore = useNotebooksStore()
const authStore = useAuthStore()

const notebooks = computed(() => notebooksStore.notebooks)
const loading = computed(() => notebooksStore.loading)
const isAuthenticated = computed(() => authStore.isAuthenticated)

const tabs = [
  { key: "all", label: "全部" },
  { key: "featured", label: "精选" },
  { key: "mine", label: "我的" },
]
const activeTab = ref("all")
const showCreateDialog = ref(false)
const creating = ref(false)
const createFormRef = ref<FormInstance>()
const createForm = reactive({ title: "", description: "" })
const createRules: FormRules = {
  title: [{ required: true, message: "请输入知识库名称", trigger: "blur" }, { max: 100, message: "名称不能超过 100 字符", trigger: "blur" }],
}

const openLogin = inject<() => void>("openLogin")

const featuredItems = [
  { id: 1, title: "WAIC 2026参会须知", count: 19, cover: "https://luna-notebook.ydstatic.com/4ff9089c1b05019c5ca2cce15423ab34.jpg" },
  { id: 2, title: "英语四级阅读解题路径", count: 12, cover: "https://luna-notebook.ydstatic.com/fcf3b337f12760ace397f3e4e9d50bbb.png?imageView&type=jpg" },
  { id: 3, title: "全球AI算力与芯片市场趋势", count: 8, cover: "https://luna-notebook.ydstatic.com/1fc21ad0dea5ea023715d2a4594612ed.jpg" },
  { id: 4, title: "华为论文解读", count: 15, cover: "https://luna-notebook.ydstatic.com/247c48d53b029dd9fcf7417b85de5020.png?imageView&type=jpg" },
  { id: 5, title: "清华职业发展中心知识库", count: 6, cover: "https://luna-notebook.ydstatic.com/e9fc10d2bb5847b34897a0ce63fea292.png?imageView&type=jpg" },
  { id: 6, title: "TED英文精读", count: 22, cover: "https://luna-notebook.ydstatic.com/7dab6c9122fe762ea9295217badd17aa.jpg" },
]

function openCreate() {
  if (!isAuthenticated.value) {
    openLogin?.()
    return
  }
  showCreateDialog.value = true
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
  if (!t) return ""
  const d = new Date(t); const diff = Date.now() - d.getTime()
  if (diff < 3600000) return `${Math.floor(diff / 60000)} 分钟前`
  if (diff < 86400000) return `${Math.floor(diff / 3600000)} 小时前`
  return d.toLocaleDateString("zh-CN")
}

onMounted(() => {
  notebooksStore.fetchNotebooks()
})
</script>

<style scoped lang="scss">
.home-page {
  min-height: calc(100vh - var(--baoku-header-height));
  background: var(--baoku-bg);
  padding: 24px 0 48px;
}
.home-content {
  max-width: 1312px;
  margin: 0 auto;
  padding: 0 24px;
  display: flex;
  flex-direction: column;
  gap: 24px;
}
.welcome-banner {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 28px 36px;
  background: var(--baoku-surface);
  border-radius: var(--baoku-radius);
  box-shadow: var(--baoku-shadow-card);
}
.banner-title {
  font-size: 26px;
  font-weight: 600;
  color: var(--baoku-text);
  margin-bottom: 8px;
  .text-gradient {
    background: linear-gradient(90deg, #2f7bff, #00c6ff);
    -webkit-background-clip: text;
    -webkit-text-fill-color: transparent;
  }
}
.banner-desc {
  font-size: 14px;
  color: var(--baoku-text-2);
}
.banner-link { color: var(--baoku-primary); margin-left: 4px; }
.banner-actions { display: flex; gap: 12px; }
.btn-import {
  display: flex;
  align-items: center;
  gap: 6px;
  height: 44px;
  padding: 0 20px;
  border: 1px solid var(--baoku-border-2);
  border-radius: var(--baoku-radius-sm);
  background: var(--baoku-surface);
  color: var(--baoku-text);
  font-size: 14px;
  cursor: pointer;
  &:hover { background: var(--baoku-surface-hover); }
}
.btn-create {
  display: flex;
  align-items: center;
  gap: 6px;
  height: 44px;
  padding: 0 24px;
  border: none;
  border-radius: var(--baoku-radius-sm);
  background: var(--baoku-text);
  color: #fff;
  font-size: 14px;
  font-weight: 500;
  cursor: pointer;
  &:hover { background: #2a2b2e; }
}
.tab-filter {
  display: flex;
  gap: 8px;
}
.tab-item {
  height: 32px;
  padding: 0 16px;
  border: none;
  border-radius: 16px;
  background: transparent;
  color: var(--baoku-text-2);
  font-size: 14px;
  cursor: pointer;
  &.active { background: var(--baoku-surface); color: var(--baoku-text); font-weight: 500; }
}
.section-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 16px;
}
.section-title-wrap { display: flex; align-items: center; gap: 8px; }
.section-dot { width: 7px; height: 7px; border-radius: 50%; background: var(--baoku-primary); }
.section-title { font-size: 16px; font-weight: 600; color: var(--baoku-text); }
.view-all { font-size: 13px; color: var(--baoku-text-3); cursor: pointer; &:hover { color: var(--baoku-primary); } }
.featured-grid {
  display: flex;
  gap: 16px;
  overflow-x: auto;
  padding-bottom: 8px;
}
.featured-card {
  flex-shrink: 0;
  width: 270px;
  height: 150px;
  border-radius: var(--baoku-radius);
  background-size: cover;
  background-position: center;
  position: relative;
  cursor: pointer;
  overflow: hidden;
  &:hover { transform: translateY(-2px); transition: transform 0.2s; }
}
.featured-card-mask {
  position: absolute;
  bottom: 0;
  left: 0;
  right: 0;
  padding: 16px;
  background: linear-gradient(transparent, rgba(0,0,0,0.7));
  color: #fff;
}
.featured-card-title { font-size: 15px; font-weight: 600; margin-bottom: 4px; }
.featured-card-meta { font-size: 12px; opacity: 0.9; }
.my-section { margin-top: 8px; }
.card-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 16px; }
.notebook-card {
  cursor: pointer;
  overflow: hidden;
  transition: transform 0.2s, box-shadow 0.2s;
  &:hover { transform: translateY(-2px); box-shadow: 0px 6px 48px rgba(174,180,193,0.2); }
}
.card-cover {
  height: 120px;
  background: var(--baoku-bg);
  display: flex;
  align-items: center;
  justify-content: center;
  img { width: 48px; height: 48px; opacity: 0.2; }
}
.card-body { padding: 16px; }
.card-header { display: flex; align-items: center; justify-content: space-between; margin-bottom: 8px; }
.card-title { font-size: 15px; font-weight: 600; color: var(--baoku-text); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.card-meta { display: flex; align-items: center; justify-content: space-between; font-size: 12px; color: var(--baoku-text-3); }
.meta-item { display: flex; align-items: center; gap: 4px; }
.empty-state {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  padding: 80px 20px;
  min-height: 280px;
}
.empty-card-inner { display: flex; flex-direction: column; align-items: center; gap: 12px; }
.empty-title { font-size: 14px; color: var(--baoku-text-3); }
.btn-login {
  height: 36px;
  padding: 0 24px;
  border: none;
  border-radius: var(--baoku-radius-sm);
  background: var(--baoku-text);
  color: #fff;
  font-size: 14px;
  cursor: pointer;
  &:hover { background: #2a2b2e; }
}
.skeleton-card {
  padding: 20px;
  .skeleton-line { height: 14px; background: linear-gradient(90deg, var(--baoku-bg) 25%, #e8e8ea 50%, var(--baoku-bg) 75%); background-size: 200% 100%; animation: shimmer 1.5s infinite; border-radius: 4px; margin-bottom: 12px; }
  .skeleton-title { width: 60%; height: 18px; }
  .skeleton-desc { width: 80%; height: 14px; }
  .skeleton-meta { width: 40%; height: 12px; }
}
@keyframes shimmer { 0% { background-position: 200% 0; } 100% { background-position: -200% 0; } }
</style>

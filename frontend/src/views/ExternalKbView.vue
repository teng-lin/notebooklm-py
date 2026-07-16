<template>
  <div class="external-kb-page">
    <header class="page-header">
      <h1 class="page-title">外部知识库</h1>
      <el-button type="primary" @click="showForm = true">+ 添加连接</el-button>
    </header>

    <div class="page-container">
      <div v-if="loading" class="loading"><el-skeleton :rows="4" animated /></div>
      <div v-else-if="connections.length === 0" class="empty">
        <el-icon :size="64" color="#d0d0d0"><Connection /></el-icon>
        <p class="empty-title">尚未接入外部知识库</p>
        <p class="empty-desc">连接外部知识库，将外部文档导入到你的知识库中</p>
      </div>
      <div v-else class="conn-list">
        <div v-for="conn in connections" :key="conn.id" class="conn-item card" @click="router.push(`/external-kb/connections/${conn.id}`)">
          <div class="conn-header">
            <el-icon :size="20" color="var(--color-main-1)"><Connection /></el-icon>
            <span class="conn-name">{{ conn.name }}</span>
          </div>
          <div class="conn-meta">
            <span class="conn-provider">{{ providerLabel(conn.provider_type) }}</span>
            <span class="conn-time" v-if="conn.last_sync_at">最后同步: {{ formatTime(conn.last_sync_at) }}</span>
            <el-tag size="small" :type="conn.is_active ? 'success' : 'info'">{{ conn.is_active ? "活跃" : "已停用" }}</el-tag>
          </div>
          <div class="conn-actions">
            <el-button text type="danger" size="small" @click.stop="handleDelete(conn.id)">删除</el-button>
          </div>
        </div>
      </div>
    </div>

    <ExternalKbConnForm :visible="showForm" @update:visible="showForm = $event" @created="handleCreated" />
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted } from "vue"; import { useRouter } from "vue-router"
import { Connection } from "@element-plus/icons-vue"; import { ElMessage, ElMessageBox } from "element-plus"
import type { ExternalKbConnection } from "@/api/external-kb"
import { fetchConnectionsApi, deleteConnectionApi } from "@/api/external-kb"
import ExternalKbConnForm from "@/components/ExternalKbConnForm.vue"

const router = useRouter(); const connections = ref<ExternalKbConnection[]>([]); const loading = ref(false); const showForm = ref(false)
function providerLabel(type: string) { const m: Record<string, string> = { openapi:"OpenAPI", dify:"Dify", qanything:"QAnything", vectordb:"向量数据库", custom:"自定义" }; return m[type] || type }
function formatTime(t: string) { const d = new Date(t); const diff = Date.now() - d.getTime(); if (diff < 3600000) return `${Math.floor(diff/60000)} 分钟前`; if (diff < 86400000) return `${Math.floor(diff/3600000)} 小时前`; return d.toLocaleDateString("zh-CN") }
async function handleDelete(id: number) { try { await ElMessageBox.confirm("确定删除此连接？"); await deleteConnectionApi(id); connections.value = connections.value.filter((c) => c.id !== id); ElMessage.success("已删除") } catch {} }
function handleCreated() { showForm.value = false; fetchConnections() }
async function fetchConnections() { loading.value = true; try { connections.value = await fetchConnectionsApi() } catch { ElMessage.error("加载失败") } finally { loading.value = false } }
onMounted(() => { fetchConnections() })
</script>

<style scoped lang="scss">
.external-kb-page { min-height: 100vh; background: var(--color-bg-tab); }
.page-header { display: flex; align-items: center; justify-content: space-between; padding: 16px 24px; background: var(--color-bg-1); border-bottom: 1px solid var(--color-divider-1); position: sticky; top: 0; z-index: 100; }
.page-title { font-size: 20px; font-weight: 600; }
.page-container { padding: 24px; max-width: 900px; margin: 0 auto; }
.loading { padding: 40px 0; }
.empty { display: flex; flex-direction: column; align-items: center; padding: 80px 20px; gap: 12px; p { color: var(--color-text-3); } }
.conn-list { display: flex; flex-direction: column; gap: 12px; }
.conn-item { cursor: pointer; &:hover { box-shadow: 0px 6px 48px 0px rgba(174,180,193,.25); } }
.conn-header { display: flex; align-items: center; gap: 8px; margin-bottom: 8px; }
.conn-name { font-size: 16px; font-weight: 600; }
.conn-meta { display: flex; align-items: center; gap: 12px; font-size: 13px; color: var(--color-text-3); }
.conn-actions { margin-top: 8px; }
</style>

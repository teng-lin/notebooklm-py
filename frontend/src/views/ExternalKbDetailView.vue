<template>
  <div class="external-kb-detail">
    <header class="page-header">
      <el-button text @click="router.push('/external-kb')"><el-icon><ArrowLeft /></el-icon>返回</el-button>
      <h1 class="page-title">{{ connection?.name || "连接详情" }}</h1>
    </header>
    <div class="page-container">
      <div v-if="!connection" class="loading">加载中...</div>
      <template v-else>
        <div class="conn-info card">
          <div class="info-row"><span class="label">类型</span><span>{{ providerLabel(connection.provider_type) }}</span></div>
          <div class="info-row"><span class="label">API 地址</span><span>{{ connection.api_base_url }}</span></div>
          <div class="info-row"><span class="label">状态</span><el-tag :type="connection.is_active ? 'success' : 'info'">{{ connection.is_active ? "活跃" : "已停用" }}</el-tag></div>
        </div>
      </template>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted } from "vue"; import { useRouter, useRoute } from "vue-router"; import { ArrowLeft } from "@element-plus/icons-vue"
import type { ExternalKbConnection } from "@/api/external-kb"; import { fetchConnectionsApi } from "@/api/external-kb"
const router = useRouter(); const route = useRoute(); const connection = ref<ExternalKbConnection | null>(null)
function providerLabel(type: string) { const m: Record<string, string> = { openapi:"OpenAPI", dify:"Dify", qanything:"QAnything", vectordb:"向量数据库", custom:"自定义" }; return m[type] || type }
onMounted(async () => {
  try { const list = await fetchConnectionsApi(); connection.value = list.find((c) => c.id === Number(route.params.id)) || null }
  catch { connection.value = null }
})
</script>

<style scoped lang="scss">
.external-kb-detail { min-height: 100vh; background: var(--color-bg-tab); }
.page-header { display: flex; align-items: center; gap: 12px; padding: 16px 24px; background: var(--color-bg-1); border-bottom: 1px solid var(--color-divider-1); }
.page-title { font-size: 20px; font-weight: 600; }
.page-container { padding: 24px; max-width: 900px; margin: 0 auto; }
.conn-info { display: flex; flex-direction: column; gap: 12px; }
.info-row { display: flex; gap: 12px; font-size: 14px; .label { color: var(--color-text-3); min-width: 100px; } }
</style>

<template>
  <div class="history-page">
    <header class="page-header">
      <h1 class="page-title">请求历史</h1>
    </header>
    <div class="page-container">
      <div class="card">
        <el-table :data="logs" style="width: 100%" v-loading="loading" empty-text="暂无记录">
          <el-table-column prop="endpoint" label="端点" min-width="200" />
          <el-table-column prop="method" label="方法" width="80">
            <template #default="{ row }"><el-tag :type="row.method === 'GET' ? 'primary' : row.method === 'POST' ? 'success' : 'warning'" size="small">{{ row.method }}</el-tag></template>
          </el-table-column>
          <el-table-column prop="response_status" label="状态" width="80">
            <template #default="{ row }"><el-tag :type="row.response_status < 300 ? 'success' : 'danger'" size="small">{{ row.response_status }}</el-tag></template>
          </el-table-column>
          <el-table-column prop="latency_ms" label="耗时" width="100">
            <template #default="{ row }">{{ row.latency_ms }}ms</template>
          </el-table-column>
          <el-table-column prop="created_at" label="时间" width="180">
            <template #default="{ row }">{{ formatTime(row.created_at) }}</template>
          </el-table-column>
        </el-table>
        <el-pagination v-if="total > pageSize" v-model:current-page="page" :page-size="pageSize" :total="total" layout="prev, pager, next" @current-change="fetchLogs" style="margin-top: 16px; justify-content: center;" />
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted } from "vue"; import { ElMessage } from "element-plus"; import request from "@/api/request"
const loading = ref(false); const logs = ref<any[]>([]); const total = ref(0); const page = ref(1); const pageSize = 20
function formatTime(t: string) { const d = new Date(t); return d.toLocaleString("zh-CN") }
async function fetchLogs() {
  loading.value = true
  try { const res = await request.get("/api/request-logs", { params: { page: page.value, page_size: pageSize } }); logs.value = res.data.items || []; total.value = res.data.total || 0 }
  catch { ElMessage.error("加载失败") }
  finally { loading.value = false }
}
onMounted(() => { fetchLogs() })
</script>

<style scoped lang="scss">
.history-page { min-height: 100vh; background: var(--color-bg-tab); }
.page-header { padding: 16px 24px; background: var(--color-bg-1); border-bottom: 1px solid var(--color-divider-1); }
.page-title { font-size: 20px; font-weight: 600; }
.page-container { padding: 24px; max-width: 1000px; margin: 0 auto; }
</style>

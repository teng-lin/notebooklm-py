<template>
  <div class="overview-tab">
    <div class="stats-grid">
      <div class="stat-card card">
        <div class="stat-value">{{ stats.source_count }}</div>
        <div class="stat-label">文档数</div>
      </div>
      <div class="stat-card card">
        <div class="stat-value">{{ stats.chat_count }}</div>
        <div class="stat-label">问答数</div>
      </div>
      <div class="stat-card card">
        <div class="stat-value">{{ stats.generate_count }}</div>
        <div class="stat-label">生成次数</div>
      </div>
    </div>

    <div class="section">
      <h3 class="section-title">AI 摘要</h3>
      <div class="card">
        <p v-if="summary" class="summary-text">{{ summary }}</p>
        <p v-else class="summary-empty">暂无摘要，开始上传资料后会自动生成</p>
      </div>
    </div>

    <div class="section">
      <h3 class="section-title">最近活动</h3>
      <div class="card">
        <div v-if="activities.length === 0" class="activity-empty">暂无活动记录</div>
        <div v-for="act in activities" :key="act.id" class="activity-item">
          <el-tag size="small" :type="act.type === 'chat' ? 'primary' : act.type === 'source' ? 'success' : 'warning'" class="activity-badge">{{ act.label }}</el-tag>
          <span class="activity-desc">{{ act.description }}</span>
          <span class="activity-time">{{ formatTime(act.created_at) }}</span>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted } from "vue"; import { useRoute } from "vue-router"
import { fetchSourcesApi } from "@/api/sources"; import { fetchGeneratedContentsApi } from "@/api/generation"
import { fetchSessionsApi } from "@/api/chat"
const route = useRoute(); const notebookId = computed(() => route.params.id as string)
const stats = ref({ source_count: 0, chat_count: 0, generate_count: 0 })
const summary = ref(""); const activities = ref<any[]>([])

function formatTime(t: string) { const d = new Date(t); const diff = Date.now() - d.getTime(); if (diff < 3600000) return `${Math.floor(diff/60000)} 分钟前`; if (diff < 86400000) return `${Math.floor(diff/3600000)} 小时前`; return d.toLocaleDateString("zh-CN") }

onMounted(async () => {
  try {
    const [sources, sessions, generated] = await Promise.all([
      fetchSourcesApi(notebookId.value), fetchSessionsApi(notebookId.value), fetchGeneratedContentsApi(notebookId.value),
    ])
    stats.value = { source_count: sources.total || sources.items.length, chat_count: sessions.length, generate_count: generated.length }
    generated.forEach((g: any) => activities.value.push({ id: `gen-${g.id}`, type: "generate", label: "生成", description: `生成了 ${g.title || g.content_type}`, created_at: g.created_at }))
    sessions.forEach((s: any) => activities.value.push({ id: `chat-${s.id}`, type: "chat", label: "问答", description: s.title || "新对话", created_at: s.updated_at }))
    sources.items.forEach((s: any) => activities.value.push({ id: `src-${s.id}`, type: "source", label: "资料", description: `上传了 ${s.original_filename}`, created_at: s.created_at }))
    activities.value.sort((a, b) => new Date(b.created_at).getTime() - new Date(a.created_at).getTime())
    activities.value = activities.value.slice(0, 20)
  } catch {}
})
</script>

<style scoped lang="scss">
.overview-tab { max-width: 900px; margin: 0 auto; }
.stats-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 20px; margin-bottom: 24px; }
.stat-card { text-align: center; padding: 24px; }
.stat-value { font-size: 32px; font-weight: 700; color: var(--color-main-1); }
.stat-label { font-size: 14px; color: var(--color-text-2); margin-top: 4px; }
.section { margin-bottom: 24px; }
.summary-text { font-size: 14px; line-height: 1.7; color: var(--color-text-2); }
.summary-empty { font-size: 14px; color: var(--color-text-3); }
.activity-empty { font-size: 14px; color: var(--color-text-3); text-align: center; padding: 20px; }
.activity-item { display: flex; align-items: center; gap: 12px; padding: 10px 0; border-bottom: 1px solid var(--color-divider-1); &:last-child { border-bottom: none; } }
.activity-badge { flex-shrink: 0; }
.activity-desc { flex: 1; font-size: 14px; color: var(--color-text-1); }
.activity-time { font-size: 12px; color: var(--color-text-3); flex-shrink: 0; }
</style>

<template>
  <div class="generate-tab">
    <div class="content-type-grid">
      <div v-for="ct in contentTypes" :key="ct.type" class="content-type-card card" @click="currentType = ct.type">
        <div class="ctype-icon" :style="{ background: ct.color + '18' }"><el-icon :size="28" :color="ct.color"><component :is="ct.icon" /></el-icon></div>
        <span class="ctype-name">{{ ct.label }}</span>
      </div>
    </div>

    <div v-if="currentType" class="generator-panel card">
      <div class="generator-header">
        <h3 class="generator-title">{{ contentTypes.find((c) => c.type === currentType)?.label }}</h3>
        <el-button text @click="currentType = ''"><el-icon><Close /></el-icon></el-button>
      </div>
      <component :is="generatorComponent" :notebook-id="notebookId" @back="currentType = ''" />
    </div>

    <div class="history-section">
      <h3 class="section-title">生成历史</h3>
      <div v-if="generatedList.length === 0" class="history-empty">暂无生成记录</div>
      <div v-else class="history-grid">
        <div v-for="item in generatedList" :key="item.id" class="history-card card" @click="router.push(`/notebook/${notebookId}/generate/${item.id}`)">
          <div class="history-header">
            <el-tag size="small" :type="statusType(item.status)">{{ statusLabel(item.status) }}</el-tag>
            <el-tag size="small" type="info">{{ typeLabel(item.content_type) }}</el-tag>
          </div>
          <p class="history-title">{{ item.title || item.prompt || "无标题" }}</p>
          <span class="history-time">{{ formatTime(item.created_at) }}</span>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, shallowRef } from "vue"; import { useRouter, useRoute } from "vue-router"
import { Close, Document, DataAnalysis, PictureFilled, Microphone, VideoCamera, Edit } from "@element-plus/icons-vue"
import { fetchGeneratedContentsApi } from "@/api/generation"
import PptGenerator from "./generate/PptGenerator.vue"; import MindmapGenerator from "./generate/MindmapGenerator.vue"
import InfographicGenerator from "./generate/InfographicGenerator.vue"; import PodcastGenerator from "./generate/PodcastGenerator.vue"
import VideoGenerator from "./generate/VideoGenerator.vue"; import DocumentGenerator from "./generate/DocumentGenerator.vue"

const router = useRouter(); const route = useRoute(); const notebookId = computed(() => Number(route.params.id))
const currentType = ref(""); const generatedList = ref<any[]>([])
const contentTypes = [
  { type: "ppt", label: "PPT", icon: Document, color: "#ff3650" },
  { type: "mindmap", label: "脑图", icon: DataAnalysis, color: "#1a75ff" },
  { type: "infographic", label: "信息图", icon: PictureFilled, color: "#67c23a" },
  { type: "podcast", label: "播客", icon: Microphone, color: "#e6a23c" },
  { type: "video", label: "视频", icon: VideoCamera, color: "#909399" },
  { type: "document", label: "文档", icon: Edit, color: "#409eff" },
]
const generatorComponent = computed(() => {
  const map: Record<string, any> = { ppt: PptGenerator, mindmap: MindmapGenerator, infographic: InfographicGenerator, podcast: PodcastGenerator, video: VideoGenerator, document: DocumentGenerator }
  return currentType.value ? shallowRef(map[currentType.value]) : null
})
function statusType(s: string) { return s === "completed" ? "success" : s === "processing" ? "warning" : s === "failed" ? "danger" : "info" }
function statusLabel(s: string) { return s === "completed" ? "已完成" : s === "processing" ? "生成中" : s === "failed" ? "失败" : "排队中" }
function typeLabel(t: string) { const m: Record<string, string> = { ppt:"PPT", mindmap:"脑图", infographic:"信息图", podcast:"播客", video:"视频", document:"文档" }; return m[t] || t }
function formatTime(t: string) { const d = new Date(t); const diff = Date.now() - d.getTime(); if (diff < 3600000) return `${Math.floor(diff/60000)} 分钟前`; if (diff < 86400000) return `${Math.floor(diff/3600000)} 小时前`; return d.toLocaleDateString("zh-CN") }
onMounted(async () => { try { generatedList.value = await fetchGeneratedContentsApi(notebookId.value) } catch {} })
</script>

<style scoped lang="scss">
.generate-tab { max-width: 1000px; margin: 0 auto; }
.content-type-grid { display: grid; grid-template-columns: repeat(6, 1fr); gap: 16px; margin-bottom: 24px; }
.content-type-card { display: flex; flex-direction: column; align-items: center; gap: 8px; padding: 20px 12px; cursor: pointer; transition: transform .2s; &:hover { transform: translateY(-2px); } }
.ctype-icon { width: 48px; height: 48px; display: flex; align-items: center; justify-content: center; border-radius: 12px; }
.ctype-name { font-size: 13px; font-weight: 500; }
.generator-panel { margin-bottom: 24px; }
.generator-header { display: flex; align-items: center; justify-content: space-between; margin-bottom: 20px; }
.generator-title { font-size: 16px; font-weight: 600; }
.history-section { .section-title { font-size: 16px; font-weight: 600; margin-bottom: 16px; } }
.history-empty { text-align: center; padding: 40px; color: var(--color-text-3); font-size: 14px; }
.history-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 16px; }
.history-card { cursor: pointer; transition: transform .2s; &:hover { transform: translateY(-2px); } }
.history-header { display: flex; gap: 8px; margin-bottom: 8px; }
.history-title { font-size: 14px; font-weight: 500; margin-bottom: 8px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.history-time { font-size: 12px; color: var(--color-text-3); }
</style>

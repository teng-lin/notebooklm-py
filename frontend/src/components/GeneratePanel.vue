<template>
  <div class="generate-panel">
    <div class="panel-header"><span class="panel-title">内容生成</span></div>
    <div class="generate-grid">
      <div v-for="ct in contentTypes" :key="ct.type" class="generate-card" @click="openGenerator(ct.type)">
        <div class="ctype-icon" :style="{ background: ct.color + '18' }"><el-icon :size="20" :color="ct.color"><component :is="ct.icon" /></el-icon></div>
        <span class="ctype-name">{{ ct.label }}</span>
      </div>
    </div>
    <div class="history-section">
      <div class="history-label">生成记录</div>
      <div class="history-list">
        <div v-for="item in generatedList" :key="item.id" class="history-item" @click="goToDetail(item.id)">
          <el-tag size="small" :type="statusType(item.status)">{{ statusLabel(item.status) }}</el-tag>
          <span class="history-title">{{ item.title || item.content_type }}</span>
          <span class="history-time">{{ formatTime(item.created_at) }}</span>
        </div>
        <div v-if="generatedList.length === 0" class="empty-text">暂无生成记录</div>
      </div>
    </div>
    <el-dialog v-model="dialogVisible" :title="currentLabel" width="700px" @close="onDialogClose">
      <component v-if="currentType" :is="generatorComponent" :notebook-id="notebookId" @back="dialogVisible = false" />
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, shallowRef } from "vue"
import { useRouter } from "vue-router"
import { Document, DataAnalysis, PictureFilled, Microphone, VideoCamera, Edit } from "@element-plus/icons-vue"
import { fetchGeneratedContentsApi } from "@/api/generation"
import PptGenerator from "@/views/notebook/generate/PptGenerator.vue"
import MindmapGenerator from "@/views/notebook/generate/MindmapGenerator.vue"
import InfographicGenerator from "@/views/notebook/generate/InfographicGenerator.vue"
import PodcastGenerator from "@/views/notebook/generate/PodcastGenerator.vue"
import VideoGenerator from "@/views/notebook/generate/VideoGenerator.vue"
import DocumentGenerator from "@/views/notebook/generate/DocumentGenerator.vue"

const props = defineProps<{ notebookId: string }>()
const router = useRouter()
const dialogVisible = ref(false)
const currentType = ref("")
const generatedList = ref<any[]>([])

const contentTypes = [
  { type: "ppt", label: "PPT", icon: Document, color: "#ff3650" },
  { type: "mindmap", label: "脑图", icon: DataAnalysis, color: "#1a75ff" },
  { type: "podcast", label: "播客", icon: Microphone, color: "#e6a23c" },
  { type: "infographic", label: "信息图", icon: PictureFilled, color: "#67c23a" },
  { type: "video", label: "视频", icon: VideoCamera, color: "#909399" },
  { type: "document", label: "文档", icon: Edit, color: "#409eff" },
]

const currentLabel = computed(() => contentTypes.find((c) => c.type === currentType.value)?.label || "")
const generatorComponent = computed(() => {
  const map: Record<string, any> = { ppt: PptGenerator, mindmap: MindmapGenerator, infographic: InfographicGenerator, podcast: PodcastGenerator, video: VideoGenerator, document: DocumentGenerator }
  return currentType.value ? shallowRef(map[currentType.value]) : null
})

function openGenerator(type: string) { currentType.value = type; dialogVisible.value = true }
function onDialogClose() { currentType.value = ""; refreshList() }
function goToDetail(id: number) { router.push(`/notebook/${props.notebookId}/generate/${id}`) }

function statusType(s: string) { return s === "completed" ? "success" : s === "processing" ? "warning" : s === "failed" ? "danger" : "info" }
function statusLabel(s: string) { return s === "completed" ? "已完成" : s === "processing" ? "生成中" : s === "failed" ? "失败" : "排队中" }
function formatTime(t: string) { const d = new Date(t); const diff = Date.now() - d.getTime(); if (diff < 3600000) return `${Math.floor(diff/60000)}分钟前`; if (diff < 86400000) return `${Math.floor(diff/3600000)}小时前`; return d.toLocaleDateString("zh-CN") }

async function refreshList() { try { generatedList.value = await fetchGeneratedContentsApi(props.notebookId) } catch {} }
onMounted(() => { refreshList() })
defineExpose({ refreshList })
</script>

<style scoped>
.generate-panel { display: flex; flex-direction: column; height: 100%; }
.panel-header { padding: 12px 16px; border-bottom: 1px solid var(--baoku-border, #e8e8e8); font-size: 14px; font-weight: 600; }
.generate-grid { display: grid; grid-template-columns: repeat(2, 1fr); gap: 8px; padding: 12px; }
.generate-card { display: flex; flex-direction: column; align-items: center; gap: 6px; padding: 12px 8px; border-radius: 8px; border: 1px solid var(--baoku-border, #e8e8e8); cursor: pointer; transition: all 0.2s; }
.generate-card:hover { border-color: var(--baoku-primary, #ff3650); transform: translateY(-1px); }
.ctype-icon { width: 36px; height: 36px; display: flex; align-items: center; justify-content: center; border-radius: 8px; }
.ctype-name { font-size: 12px; }
.history-section { border-top: 1px solid var(--baoku-border, #e8e8e8); padding: 12px 16px; flex: 1; overflow-y: auto; }
.history-label { font-size: 13px; font-weight: 600; margin-bottom: 8px; }
.history-item { display: flex; align-items: center; gap: 8px; padding: 8px 0; border-bottom: 1px solid var(--baoku-border, #e8e8e8); cursor: pointer; }
.history-item:last-child { border-bottom: none; }
.history-item:hover { background: var(--baoku-bg, #f5f5f5); }
.history-title { flex: 1; font-size: 13px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.history-time { font-size: 11px; color: var(--baoku-text-3, #999); flex-shrink: 0; }
.empty-text { padding: 24px; text-align: center; color: var(--baoku-text-3, #999); font-size: 13px; }
</style>
